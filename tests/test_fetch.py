"""Tests for the fetch orchestration: request de-duplication, health thresholds, and the
budget stop-condition. No network — a fake funnel client stands in for Timely."""
from datetime import date, datetime, timedelta

import pytest

from conftest import mkconfig
from src import timely
from src.fetch_availability import fetch
from src.timely import BudgetExhausted, RawSlot

# Two staff we have in the catalog, so nothing is skipped as unknown.
NAO, SACHI = "175308", "24102"


def _open_days(n=6):
    """A realistic run of open days. A single-day window would hide the de-duplication
    entirely: the first day of every (service, staff) pair is always a real request,
    because that response is what tells us the pair's duration."""
    return [(date.today() + timedelta(days=i + 1)).isoformat() for i in range(n)]


class FakeClient:
    """Stands in for TimelyClient. Records every lookup so tests can count requests.

    `durations` maps service_id -> minutes, which is the whole point of the de-duplication
    under test: the funnel's response depends on the selected service's DURATION, not on
    which service it was.
    """
    calls: list = []
    durations = {"svcA": 55, "svcB": 55, "svcC": 85}
    catalog = [
        {"service_id": "svcA", "name": "Hair Cut", "staff_ids": [NAO, SACHI],
         "bookable_item_id": "svcA:SV"},
        {"service_id": "svcB", "name": "Hair Cut Deluxe", "staff_ids": [NAO, SACHI],
         "bookable_item_id": "svcB:SV"},
        {"service_id": "svcC", "name": "Beard Sculpt", "staff_ids": [NAO],
         "bookable_item_id": "svcC:SV"},
    ]
    fail_setup_for: set = set()
    fail_staff_for: set = set()
    budget_error_on: set = set()

    def __init__(self, cache=None, cache_ns=""):
        self.ns = cache_ns

    def bootstrap(self):
        if self.ns in self.fail_setup_for:
            raise RuntimeError("funnel setup exploded")
        return list(self.catalog), {"ServiceStaffIds[x:SV]": ""}

    def select_service(self, bookable_item_id, service_staff_ids):
        pass

    def select_staff(self, staff_id):
        pass

    def open_dates(self, staff_id, month, year):
        FakeClient.calls.append(("open_dates", self.ns, staff_id, year, month))
        return [{"day": d} for d in _open_days()
                if datetime.fromisoformat(d).month == month
                and datetime.fromisoformat(d).year == year]

    def time_slots(self, staff_id, date_iso):
        if (self.ns, staff_id) in self.budget_error_on:
            raise BudgetExhausted("cap reached")
        if (self.ns, staff_id) in self.fail_staff_for:
            raise RuntimeError("lookup exploded")
        FakeClient.calls.append(("time_slots", self.ns, staff_id, date_iso))
        dur = self.durations[self.ns]
        return [RawSlot(date=date_iso, service_id=self.ns, staff_id=staff_id,
                        start_min=m, end_min=m + dur, token="t")
                for m in (600, 660)]


@pytest.fixture(autouse=True)
def _reset():
    FakeClient.calls = []
    FakeClient.fail_setup_for = set()
    FakeClient.fail_staff_for = set()
    FakeClient.budget_error_on = set()
    yield


def _fetch(**kw):
    kw.setdefault("verify_dedup_samples", 0)
    cfg = mkconfig(lookahead_days=10, **kw)
    return fetch(cfg, client_factory=FakeClient)


def _timeslot_calls():
    return [c for c in FakeClient.calls if c[0] == "time_slots"]


# --------------------------------------------------------------- de-duplication
def test_same_duration_services_share_one_lookup():
    """svcA and svcB are both 55 minutes, so the funnel returns identical openings for a
    given (staff, date). We must fetch that once, not once per service.

    Each (service, staff) pair still spends exactly ONE real request on its first open day
    — that response is what tells us the pair's duration, and hardcoding durations from a
    doc would silently attribute slots to the wrong service when the menu changes."""
    days = len(_open_days())
    result = _fetch()

    per_pair = {}
    for _, ns, staff, _day in _timeslot_calls():
        per_pair[(ns, staff)] = per_pair.get((ns, staff), 0) + 1

    # First service of each (staff, duration) group walks every day...
    assert per_pair[("svcA", NAO)] == days
    assert per_pair[("svcA", SACHI)] == days
    assert per_pair[("svcC", NAO)] == days          # different duration: its own group
    # ...every later service in that group pays only the one duration probe.
    assert per_pair[("svcB", NAO)] == 1
    assert per_pair[("svcB", SACHI)] == 1

    naive = 5 * days                                # what one-request-per-(service,staff) costs
    assert len(_timeslot_calls()) == 3 * days + 2
    assert result.requests_saved == naive - (3 * days + 2)


def test_deduped_slots_are_attributed_to_the_right_service():
    """A memo hit must adopt the CURRENT service's name and price, not the one whose
    response happened to be cached."""
    result = _fetch()
    by_service = {s.service: s.duration_min for s in result.slots}
    assert by_service["Hair Cut"] == 55
    assert by_service["Hair Cut Deluxe"] == 55          # served from the memo
    assert by_service["Beard Sculpt"] == 85
    # Both 55-minute services land on the same opening, so they collapse into one Event.
    nao_events = [e for e in result.events if e.stylist_id == NAO]
    for ev in nao_events:
        assert {"Hair Cut", "Hair Cut Deluxe"} <= set(ev.services)


def test_different_durations_are_never_shared():
    _fetch()
    durations_by_call = [(ns, staff) for _, ns, staff, _ in _timeslot_calls()]
    # svcC (85 min) must have its own lookup for Nao even though svcA already ran.
    assert ("svcC", NAO) in durations_by_call


def test_verification_sampling_refetches_a_few_memo_hits():
    _fetch(verify_dedup_samples=2)
    # The spot-checks are extra live calls on top of the de-duplicated set.
    assert len(_timeslot_calls()) > 3


# --------------------------------------------------------------- health threshold
def test_partial_outage_is_not_reported_as_ok():
    """The old rule (ok_count > 0 and fail_count <= ok_count) called a fetch healthy with
    49% of the salon's availability missing, and the delete pass then removed all of it."""
    FakeClient.fail_staff_for = {("svcA", NAO), ("svcA", SACHI), ("svcB", NAO)}
    result = _fetch()
    assert result.lookups_expected == 5          # A:2 + B:2 + C:1
    assert result.lookups_failed == 3
    assert result.ok is False


def test_one_flaky_lookup_still_counts_as_ok():
    """Not zero-tolerance: freezing every delete over one 429 leaves the calendar
    advertising already-booked appointments, which is the worse failure."""
    FakeClient.fail_staff_for = {("svcC", NAO)}
    result = _fetch()
    assert result.lookups_ok == 4 and result.lookups_expected == 5
    assert result.ok is True


def test_funnel_setup_failure_charges_every_staff_it_skipped():
    """Charging 1 for a failure that cost 2 lookups biased the health check toward 'ok'
    exactly when the outage was broadest."""
    FakeClient.fail_setup_for = {"svcA"}
    result = _fetch()
    assert result.lookups_failed == 2            # svcA had two eligible staff
    assert any("funnel setup failed" in e for e in result.errors)


def test_a_funnel_setup_failure_also_withdraws_delete_authority():
    """The counters must agree. A stylist charged a failed lookup CANNOT keep delete
    authority — and because every stylist appears in several services, both of svcA's
    stylists still complete lookups elsewhere and would otherwise land in staff_ok while
    simultaneously being counted as failed. The sync would then delete the events only
    the failed service could see."""
    FakeClient.fail_setup_for = {"svcA"}
    result = _fetch()

    assert result.lookups_failed == 2
    assert NAO not in result.staff_ok
    assert SACHI not in result.staff_ok
    # ...even though both stylists did succeed on other services this run.
    assert any(s.stylist_id == NAO for s in result.slots)


# --------------------------------------------------------------- budget stop condition
def test_budget_exhaustion_propagates_instead_of_becoming_a_lookup_failure():
    """BudgetExhausted means the REST of the run's data is missing. Laundering it into
    fail_count let a truncated run still report ok=True and then delete real events."""
    FakeClient.budget_error_on = {("svcA", NAO)}
    with pytest.raises(BudgetExhausted):
        _fetch()


def test_budget_counters_reset_between_runs():
    _fetch()
    first = timely.BUDGET.made
    _fetch()
    assert timely.BUDGET.made <= first + 1, "configure() must reset the per-run spend"


@pytest.mark.parametrize("status", [429, 503])
def test_an_over_long_retry_after_stops_the_run_on_any_status(status, monkeypatch):
    """Clamping Retry-After down to our own ceiling would retry SOONER than the server
    asked — the opposite of what docs/compliance.md promises. A 503 carrying a long
    Retry-After is what a Cloudflare overload page looks like, so this cannot be 429-only.
    """
    from src.timely import TimelyClient

    class Resp:
        status_code = status
        headers = {"Retry-After": "3600"}
        text = ""

        def raise_for_status(self): pass

    c = TimelyClient()
    monkeypatch.setattr(c.session, "request", lambda *a, **kw: Resp())
    monkeypatch.setattr(timely.time, "sleep", lambda s: None)
    timely.BUDGET.configure(100, 0.0)
    with pytest.raises(BudgetExhausted, match="3600"):
        c._request("GET", "https://example.invalid/x")


def test_one_stubborn_url_does_not_end_the_whole_sweep(monkeypatch):
    """throttled() used to count every ATTEMPT, and the retry loop runs 5 of them, so a
    single 429ing url tripped the run-wide stop condition by itself."""
    from src.timely import TimelyClient

    class Resp:
        status_code = 429
        headers = {}
        text = ""

        def raise_for_status(self): pass

    c = TimelyClient()
    monkeypatch.setattr(c.session, "request", lambda *a, **kw: Resp())
    monkeypatch.setattr(timely.time, "sleep", lambda s: None)
    timely.BUDGET.configure(100, 0.0)
    with pytest.raises(timely.TimelyError) as ei:      # per-lookup error, reachable again
        c._request("GET", "https://example.invalid/x")
    assert not isinstance(ei.value, BudgetExhausted)
    assert timely.BUDGET.throttles == 1                # one throttled REQUEST, not five


# --------------------------------------------------------------- honest health
def test_a_run_that_finds_nothing_is_not_reported_healthy():
    """`ok` counted lookups that did not RAISE, never whether they produced data. A Timely
    shape change makes every lookup 'succeed' with zero slots — and that empty result is
    exactly the input that drives the calendar wipe and the empty .ics publish."""
    class Barren(FakeClient):
        def time_slots(self, staff_id, date_iso):
            FakeClient.calls.append(("time_slots", self.ns, staff_id, date_iso))
            return []

    result = fetch(mkconfig(lookahead_days=10, verify_dedup_samples=0),
                   client_factory=Barren)
    assert result.lookups_failed == 0        # nothing raised...
    assert result.slots == []
    assert result.ok is False                # ...but we do not call that healthy


def test_dedup_mismatch_makes_the_whole_fetch_untrusted():
    """The spot-check used to warn and then publish the wrong times anyway. If the memo
    invariant is false, every de-duplicated slot in the run is suspect."""
    class Divergent(FakeClient):
        def time_slots(self, staff_id, date_iso):
            FakeClient.calls.append(("time_slots", self.ns, staff_id, date_iso))
            dur = self.durations[self.ns]
            # svcB genuinely differs from svcA despite sharing a duration.
            starts = (900,) if self.ns == "svcB" else (600, 660)
            return [RawSlot(date=date_iso, service_id=self.ns, staff_id=staff_id,
                            start_min=m, end_min=m + dur, token="t") for m in starts]

    result = fetch(mkconfig(lookahead_days=10, verify_dedup_samples=4),
                   client_factory=Divergent)
    assert result.ok is False
    assert any("dedup invariant broken" in e for e in result.errors)


def test_a_lookup_that_fails_midway_contributes_no_partial_data():
    """Slots were appended as they were parsed, so a lookup dying on day 3 of 6 left the
    first two days in the result — downstream that reads as 'this stylist has fewer
    openings', which the delete pass acts on."""
    class HalfWay(FakeClient):
        seen = {}

        def time_slots(self, staff_id, date_iso):
            key = (self.ns, staff_id)
            HalfWay.seen[key] = HalfWay.seen.get(key, 0) + 1
            if self.ns == "svcC" and HalfWay.seen[key] == 3:
                raise RuntimeError("died on day 3")
            return super().time_slots(staff_id, date_iso)

    HalfWay.seen = {}
    result = fetch(mkconfig(lookahead_days=10, verify_dedup_samples=0),
                   client_factory=HalfWay)
    # svcC/Nao died partway. None of its first two days' slots may survive.
    assert [s for s in result.slots if s.service == "Beard Sculpt"] == []
    assert result.lookups_failed == 1
    # Nao had a failed lookup, so she loses delete authority for this run; Sachi keeps hers.
    assert NAO not in result.staff_ok
    assert SACHI in result.staff_ok


def test_staff_ok_excludes_a_stylist_with_any_failed_lookup():
    FakeClient.fail_staff_for = {("svcC", NAO)}
    result = _fetch()
    assert SACHI in result.staff_ok
    assert NAO not in result.staff_ok       # one failure withdraws delete authority


def test_open_dates_shape_change_is_an_error_not_silence():
    """A missing 'openDates' key would otherwise read as 'no open days', skipping every
    gettimeslots call so the strict slot parser never even runs."""
    from src.timely import TimelyClient

    class Shifted(TimelyClient):
        def _request(self, method, url, **kw):
            return '{"dates": []}'

    c = Shifted.__new__(Shifted)
    c.cache = None
    c.cache_ns = ""
    c.obg = "x"
    with pytest.raises(timely.TimelyError, match="openDates"):
        c.open_dates(NAO, 7, 2026)


def test_open_dates_accepts_a_genuinely_empty_month():
    from src.timely import TimelyClient

    class Empty(TimelyClient):
        def _request(self, method, url, **kw):
            return '{"openDates": []}'

    c = Empty.__new__(Empty)
    c.cache = None
    c.cache_ns = ""
    c.obg = "x"
    assert c.open_dates(NAO, 7, 2026) == []


# --------------------------------------------------------------- filters
def test_unknown_staff_are_skipped_not_published_as_placeholders():
    FakeClient.catalog = FakeClient.catalog + [
        {"service_id": "svcA", "name": "Mystery", "staff_ids": ["991234"],
         "bookable_item_id": "svcA:SV"}]
    try:
        result = _fetch()
        assert all(s.stylist_id != "991234" for s in result.slots)
        assert not any("Staff 991234" in e.stylist for e in result.events)
    finally:
        FakeClient.catalog = FakeClient.catalog[:3]


def test_stylist_filter_narrows_the_plan():
    result = _fetch(stylists=["Nao"])
    assert {s.stylist for s in result.slots} == {"Nao"}


def test_min_slot_hour_filter():
    result = _fetch(min_slot_hour=11)
    assert all(s.start.hour >= 11 for s in result.slots)
