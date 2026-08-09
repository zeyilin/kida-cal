"""Tests for the fetch orchestration: request de-duplication, health thresholds, and the
budget stop-condition. No network — a fake funnel client stands in for Timely."""
from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import mkconfig
from src import timely
from src.fetch_availability import MIN_LOOKUP_SUCCESS_RATIO, fetch
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
    budget_error_setup_for: set = set()

    def __init__(self, cache=None, cache_ns=""):
        self.ns = cache_ns

    def bootstrap(self):
        if self.ns in self.budget_error_setup_for:
            raise BudgetExhausted("throttled during funnel setup")
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
    FakeClient.budget_error_setup_for = set()
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
def test_budget_exhaustion_aborts_the_sweep_without_becoming_a_lookup_failure():
    """BudgetExhausted means the REST of the run's data is missing. Laundering it into
    fail_count let a truncated run still report ok=True and then delete real events.

    It is no longer re-raised out of fetch(): a throttle window is an expected outcome, and
    a traceback out of main() cost the exit-2 'nothing was written' signal and the
    fetch-result artifact. It becomes an explicitly untrusted result instead."""
    FakeClient.budget_error_on = {("svcA", NAO)}
    result = _fetch()

    assert result.ok is False
    assert any("run aborted" in e for e in result.errors)
    # Not laundered: the abort is not counted as a failed lookup...
    assert result.lookups_failed == 0
    # ...and nothing it did manage to fetch grants authority to delete anyone's events.
    assert result.staff_ok == set()


def test_budget_exhaustion_during_funnel_setup_also_aborts_the_sweep():
    """The funnel-setup arm is the one the 2026-08-09 incident actually took — every
    /Booking/Service POST 429'd — and it had no coverage: deleting its BudgetExhausted
    handler left the whole suite green."""
    FakeClient.budget_error_setup_for = {"svcA"}
    result = _fetch()

    assert result.ok is False
    assert any("run aborted" in e for e in result.errors)
    assert result.lookups_failed == 0
    assert result.staff_ok == set()
    # The sweep stopped: services after the aborting one were never walked.
    assert not any(c[1] == "svcC" for c in FakeClient.calls)


def test_a_throttle_during_the_opening_bootstrap_is_still_a_clean_untrusted_result():
    """The catalog bootstrap runs before the sweep loop, so the loop's handler cannot see
    it — and a throttle window is most likely to be in force on the run's very first
    request. It must produce the same ok=False result as any other abort, not a traceback."""
    FakeClient.budget_error_setup_for = {""}      # "" is the catalog probe's namespace
    result = _fetch()

    assert result.ok is False
    assert result.lookups_expected == 0
    assert result.staff_ok == set()
    assert any("run aborted" in e for e in result.errors)


def test_an_aborted_sweep_is_untrusted_even_when_most_lookups_landed():
    """The ratio is not the question. svcC aborts last, so 4 of 5 lookups (80%) succeeded
    and `slots` is non-empty — every other health input says 'healthy'. But the rest of the
    window was never looked at, so the only honest verdict is untrusted."""
    FakeClient.budget_error_setup_for = {"svcC"}
    result = _fetch()

    assert result.lookups_ok >= MIN_LOOKUP_SUCCESS_RATIO * result.lookups_expected
    assert result.slots
    assert result.ok is False


def test_budget_counters_reset_between_runs(monkeypatch):
    """configure() is the only place the per-run spend and the per-endpoint throttle counts
    are cleared. A long-lived process (or any caller running fetch twice — the near-tier
    then deep-tier pattern) would otherwise inherit the previous run's state and abort
    having published nothing. FakeClient never spends budget, so seed the counters."""
    _fetch()
    monkeypatch.setattr(timely.BUDGET, "made", 5000)
    monkeypatch.setattr(timely.BUDGET, "throttles", {("POST", "/booking/service"): 2})
    monkeypatch.setattr(timely.BUDGET, "throttled_total", 4)
    _fetch()

    assert timely.BUDGET.made == 0, "configure() must reset the per-run spend"
    assert timely.BUDGET.throttles == {}, "configure() must reset the throttle streaks"
    assert timely.BUDGET.throttled_total == 0, "configure() must reset the throttle total"


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


# ------------------------------------------------------- the funnel actually advanced
def _select_service_returning(monkeypatch, body):
    from src.timely import TimelyClient

    class Resp:
        status_code = 200
        headers = {}
        text = body

        def raise_for_status(self): pass

    c = TimelyClient()
    c.obg = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(c.session, "request", lambda *a, **kw: Resp())
    monkeypatch.setattr(timely.time, "sleep", lambda s: None)
    timely.BUDGET.configure(100, 0.0)
    return c


def test_a_service_selection_that_did_not_advance_is_refused(monkeypatch):
    """A 200 does not prove the funnel moved. If it did not, GetOpenDates answers a
    well-formed empty {"openDates": []} for every staff id, so every lookup 'succeeds'
    with zero openings while keeping delete authority — and the sync removes genuinely
    bookable times. The captured landing page IS the did-not-advance response."""
    import pathlib
    landing = pathlib.Path(__file__).parent / "fixtures" / "service_catalog.html"
    c = _select_service_returning(monkeypatch, landing.read_text(encoding="utf-8"))

    with pytest.raises(timely.TimelyError, match="did not advance"):
        c.select_service("svc:SV", {"ServiceStaffIds[0:SV]": "1,2"})


def test_a_service_selection_that_did_advance_is_accepted(monkeypatch):
    """The other half, against the REAL response captured from the live funnel on
    2026-08-09 (fixtures/post_service_advanced_2026-08-09.html).

    This is why the guard may not use `_OBG_RE`: the advanced page still links BACK to
    /Booking/Service?obg=..., so the obvious marker to reach for would have raised on every
    service of every run — a total outage. Only the service-selection form's own markup
    distinguishes the two pages."""
    import pathlib
    advanced = (pathlib.Path(__file__).parent / "fixtures"
                / "post_service_advanced_2026-08-09.html").read_text(encoding="utf-8")
    assert timely._OBG_RE.search(advanced), "fixture must still show the back-link trap"
    c = _select_service_returning(monkeypatch, advanced)

    c.select_service("svc:SV", {"ServiceStaffIds[0:SV]": "1,2"})   # must not raise


def test_a_dated_retry_after_is_honoured_and_can_stop_the_run(monkeypatch):
    """RFC 7231 allows `Retry-After: <HTTP-date>` as well as delta-seconds. Only the
    seconds form was parsed, so a dated header fell back to the 2s backoff — we retried
    five times inside 30 seconds while holding a note asking for 30 minutes, and the
    over-long stop condition could not fire no matter how distant the date."""
    from src.timely import TimelyClient

    from email.utils import format_datetime

    class Resp:
        status_code = 429
        # 30 minutes out, well past MAX_RETRY_AFTER_SECONDS.
        headers = {"Retry-After": format_datetime(
            datetime.now(timezone.utc) + timedelta(minutes=30))}
        text = ""

        def raise_for_status(self): pass

    c = TimelyClient()
    monkeypatch.setattr(c.session, "request", lambda *a, **kw: Resp())
    monkeypatch.setattr(timely.time, "sleep", lambda s: None)
    timely.BUDGET.configure(100, 0.0)
    with pytest.raises(BudgetExhausted, match=r"> 120s"):
        c._request("GET", "https://example.invalid/x")


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "later", "Wed, 99 Xxx 9999"])
def test_an_unreadable_retry_after_stops_the_run(raw, monkeypatch):
    """An unreadable back-off instruction is not permission to retry in two seconds.

    `float("nan")` in particular parsed *successfully* and then silently defeated every
    guard downstream: `nan > MAX_RETRY_AFTER_SECONDS` is False and `max(2.0, nan)` returns
    2.0, so the header both failed to stop the run and failed to slow it down."""
    from src.timely import TimelyClient

    class Resp:
        status_code = 429
        headers = {"Retry-After": raw}
        text = ""

        def raise_for_status(self): pass

    c = TimelyClient()
    monkeypatch.setattr(c.session, "request", lambda *a, **kw: Resp())
    monkeypatch.setattr(timely.time, "sleep", lambda s: None)
    timely.BUDGET.configure(100, 0.0)
    with pytest.raises(BudgetExhausted, match="unreadable"):
        c._request("GET", "https://example.invalid/x")


def test_retry_after_parsing_edge_cases():
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    assert timely.parse_retry_after(None) is None
    assert timely.parse_retry_after("  ") is None
    assert timely.parse_retry_after("120") == 120.0
    # A past date, or a negative delta, means "retry now" — never a negative sleep.
    assert timely.parse_retry_after("-5") == 0.0
    assert timely.parse_retry_after("Sun, 09 Aug 2026 11:00:00 GMT", now=now) == 0.0
    assert timely.parse_retry_after("Sun, 09 Aug 2026 12:02:00 GMT", now=now) == 120.0
    # A "-0000" offset parses NAIVE; subtracting it from an aware now would raise
    # TypeError and kill the run with a traceback instead of stopping cleanly.
    assert timely.parse_retry_after("Sun, 09 Aug 2026 12:02:00 -0000", now=now) == 120.0


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
    # One throttled REQUEST, not five — and keyed by the endpoint it happened on.
    assert timely.BUDGET.throttles == {("GET", "/x"): 1}


# --------------------------------------------------- the throttle streak is per endpoint
BASE = "https://book.gettimely.com"
BOOTSTRAP = f"{BASE}/kidanyc/book/embed?client-login=true"
SERVICE = f"{BASE}/Booking/Service?obg=11111111-1111-1111-1111-111111111111"
STAFFSEL = f"{BASE}/Booking/StaffSelection?obg=11111111-1111-1111-1111-111111111111"
OPENDATES = f"{BASE}/Booking/GetOpenDates?obg=1&month=8&year=2026&staffId=175308"
TIMESLOTS = f"{BASE}/booking/gettimeslots/?obg=1&dateSelected=2026-08-10&staffId=175308"


def _client_throttling(monkeypatch, throttled_url_part):
    """A TimelyClient whose session 429s only the url containing `throttled_url_part`."""
    from src.timely import TimelyClient

    class Resp:
        def __init__(self, status):
            self.status_code = status
            self.headers = {}
            self.text = "ok"

        def raise_for_status(self): pass

    c = TimelyClient()
    monkeypatch.setattr(
        c.session, "request",
        lambda method, url, **kw: Resp(429 if throttled_url_part in url else 200))
    monkeypatch.setattr(timely.time, "sleep", lambda s: None)
    return c


def _walk(c, urls):
    """Drive the funnel's real call order, swallowing per-request throttles.

    Returns the BudgetExhausted that stopped the run, or None if it never stopped.
    """
    try:
        for method, url in urls:
            try:
                c._request(method, url)
            except BudgetExhausted:
                raise
            except timely.TimelyError:
                pass          # a per-lookup failure; fetch_availability logs and moves on
        return None
    except BudgetExhausted as e:
        return e


# The real per-service call order from fetch_availability.fetch(): a GET bootstraps, then
# the two funnel POSTs, then the per-staff GET reads. Every throttled step here has a
# HEALTHY SIBLING OF THE SAME HTTP METHOD immediately before it, which is what defeated
# both the shared counter and the per-method counter that replaced it.
_SERVICE_WALK = [("GET", BOOTSTRAP), ("POST", SERVICE), ("POST", STAFFSEL),
                 ("GET", OPENDATES), ("GET", TIMESLOTS)]


@pytest.mark.parametrize("throttled_part, expect_path", [
    ("/Booking/Service", "/booking/service"),
    ("/Booking/StaffSelection", "/booking/staffselection"),
    ("GetOpenDates", "/booking/getopendates"),
    ("gettimeslots", "/booking/gettimeslots"),
])
def test_a_single_throttled_endpoint_stops_the_run(monkeypatch, throttled_part, expect_path):
    """Timely throttles ONE funnel step at a time while its neighbours stay healthy.

    Counting throttles per HTTP method was not enough: a successful /Booking/Service POST
    cleared the counter that /Booking/StaffSelection was incrementing, and a successful
    GetOpenDates GET cleared the one gettimeslots was incrementing, so the counter
    oscillated 0->1->0->1 and the documented stop condition stayed unreachable for those
    two shapes — the exact bug the per-method fix was written to close, one level down.
    """
    timely.BUDGET.configure(1000, 0.0)
    c = _client_throttling(monkeypatch, throttled_part)
    stopped = _walk(c, _SERVICE_WALK * 16)      # 16 services, as in production

    assert stopped is not None, "a persistently throttled endpoint must stop the run"
    assert expect_path in str(stopped)


def test_isolated_throttles_do_not_stop_a_healthy_run(monkeypatch):
    """The other half of the contract: a stop condition that trips on ordinary flakiness
    would abort healthy sweeps and publish nothing. MIN_LOOKUP_SUCCESS_RATIO exists to
    absorb the occasional 429, so a throttle with successes on either side must not count
    toward a streak."""
    from src.timely import TimelyClient

    calls = {"n": 0}

    class Resp:
        def __init__(self, status):
            self.status_code = status
            self.headers = {}
            self.text = "ok"

        def raise_for_status(self): pass

    def flaky(method, url, **kw):
        # Throttle gettimeslots every 4th time it is asked, never twice in a row.
        if "gettimeslots" in url:
            calls["n"] += 1
            return Resp(429 if calls["n"] % 4 == 0 else 200)
        return Resp(200)

    timely.BUDGET.configure(10_000, 0.0)
    c = TimelyClient()
    monkeypatch.setattr(c.session, "request", flaky)
    monkeypatch.setattr(timely.time, "sleep", lambda s: None)

    assert _walk(c, _SERVICE_WALK * 12) is None


def test_throttles_scattered_across_endpoints_still_stop_the_run(monkeypatch):
    """The backstop. 'Consecutive' has been defeated twice by call orders nobody predicted,
    so a monotonic total that no success clears is what actually guarantees we stop
    hammering a host that is throttling us broadly."""
    from src.timely import TimelyClient

    class Resp:
        def __init__(self, status):
            self.status_code = status
            self.headers = {}
            self.text = "ok"

        def raise_for_status(self): pass

    # Decided per REQUEST, not per attempt: a throttled request must burn all 5 retries,
    # so alternating inside the retry loop would just let every request recover.
    plan = {"throttle": False}

    timely.BUDGET.configure(10_000, 0.0)
    c = TimelyClient()
    monkeypatch.setattr(c.session, "request",
                        lambda method, url, **kw: Resp(429 if plan["throttle"] else 200))
    monkeypatch.setattr(timely.time, "sleep", lambda s: None)

    endpoints = [("POST", SERVICE), ("POST", STAFFSEL),
                 ("GET", OPENDATES), ("GET", TIMESLOTS)]
    stopped = None
    try:
        for i in range(12):
            # Round 0 throttles all four endpoints, round 1 clears every streak, round 2
            # throttles again — so no endpoint ever reaches a streak of 3.
            plan["throttle"] = (i // len(endpoints)) % 2 == 0
            method, url = endpoints[i % len(endpoints)]
            try:
                c._request(method, url)
            except timely.TimelyError as e:
                if isinstance(e, BudgetExhausted):
                    raise
    except BudgetExhausted as e:
        stopped = e

    assert stopped is not None
    assert max(timely.BUDGET.throttles.values()) < timely.MAX_CONSECUTIVE_THROTTLES
    assert "fully-throttled requests this run" in str(stopped)


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
