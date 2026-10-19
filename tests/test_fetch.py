"""Tests for the fetch orchestration: request de-duplication, health thresholds, and the
budget stop-condition. No network — a fake funnel client stands in for Timely."""
import random
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import mkconfig
from src import fetch_availability as fa
from src import timely
from src.catalog import STAFF
from src.fetch_availability import (MIN_LOOKUP_SUCCESS_RATIO, _service_allowed,
                                    _stylist_allowed, fetch)
from src.timely import BudgetExhausted, RawSlot

# Two staff we have in the catalog, so nothing is skipped as unknown.
NAO, SACHI = "175308", "24102"
MASA = "287218"                  # in the catalog too, but in no FakeClient service
NY = ZoneInfo("America/New_York")


def _open_days(n=6):
    """A realistic run of open days. A single-day window would hide the de-duplication
    entirely: the first day of every (service, staff) pair is always a real request,
    because that response is what tells us the pair's duration.

    Counted from New York's date, as fetch() counts its window, and read through fetch()'s
    own clock — so a test that freezes that clock (see ny_evening) moves these days too."""
    start = fa.datetime.now(NY).date()
    return [(start + timedelta(days=i + 1)).isoformat() for i in range(n)]


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
    # Every client (== Timely session) in creation order, as (cache_ns, funnel steps it got
    # through), and the session number of every gettimeslots request. Sessions are numbered
    # by creation, so failures can target ones opened after the sweep without touching it.
    sessions: list = []
    slot_sessions: list = []
    fail_sessions: set = set()           # session numbers whose bootstrap raises
    budget_error_sessions: set = set()   # session numbers whose bootstrap is throttled

    def __init__(self, cache=None, cache_ns=""):
        self.ns = cache_ns
        self.n = len(FakeClient.sessions)
        self.steps = []
        FakeClient.sessions.append((cache_ns, self.steps))

    def bootstrap(self):
        if self.n in FakeClient.budget_error_sessions:
            raise BudgetExhausted("throttled opening a session")
        if self.n in FakeClient.fail_sessions:
            raise RuntimeError("session would not open")
        if self.ns in self.budget_error_setup_for:
            raise BudgetExhausted("throttled during funnel setup")
        if self.ns in self.fail_setup_for:
            raise RuntimeError("funnel setup exploded")
        self.steps.append("bootstrap")
        return list(self.catalog), {"ServiceStaffIds[x:SV]": ""}

    def select_service(self, bookable_item_id, service_staff_ids):
        self.steps.append("service")

    def select_staff(self, staff_id):
        self.steps.append("staff")

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
        FakeClient.slot_sessions.append(self.n)
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
    FakeClient.sessions = []
    FakeClient.slot_sessions = []
    FakeClient.fail_sessions = set()
    FakeClient.budget_error_sessions = set()
    yield


def _fetch(rng=None, **kw):
    kw.setdefault("verify_dedup_samples", 0)
    cfg = mkconfig(lookahead_days=10, **kw)
    # Seeded: which memo hits get spot-checked is random by design, and a test must not be.
    return fetch(cfg, client_factory=FakeClient, rng=rng or random.Random(0))


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
    """The spot-checks are extra live calls on top of the de-duplicated set — and never
    two in one pair. The old sampler spent all four on svcB/Nao's days 2-5."""
    _fetch(verify_dedup_samples=4)
    per_pair = Counter((ns, staff) for _, ns, staff, _ in _timeslot_calls())
    # svcB is the only memo-served service: one check on each of its pairs, on top of the
    # one duration probe. svcC (85 min) is never memo-served, so it cannot use a check.
    assert per_pair[("svcB", NAO)] == 2
    assert per_pair[("svcB", SACHI)] == 2


# The salon has runs of same-length variants (Hair Cut / Blow Out, three 55-minute barber
# combos, four 55-minute junior services). This fixture is that shape: five 55-minute
# services over the same two stylists. s1 walks every day; s2..s5 are served from its memo
# after one duration probe each, so they are the only pairs a spot-check can land on.
class FiveCuts(FakeClient):
    durations = {f"s{i}": 55 for i in range(1, 6)}
    catalog = [{"service_id": f"s{i}", "name": f"Cut {i}", "staff_ids": [NAO, SACHI],
                "bookable_item_id": f"s{i}:SV"} for i in range(1, 6)]
    diverge: set = set()          # services whose live answer differs at the same duration

    def time_slots(self, staff_id, date_iso):
        FakeClient.calls.append(("time_slots", self.ns, staff_id, date_iso))
        FakeClient.slot_sessions.append(self.n)
        starts = (900,) if self.ns in FiveCuts.diverge else (600, 660)
        return [RawSlot(date=date_iso, service_id=self.ns, staff_id=staff_id,
                        start_min=m, end_min=m + 55, token="t") for m in starts]


def _five_cuts(seed, budget, diverge=()):
    FakeClient.calls = []
    FiveCuts.diverge = set(diverge)
    result = fetch(mkconfig(lookahead_days=10, verify_dedup_samples=budget),
                   client_factory=FiveCuts, rng=random.Random(seed))
    return result, [c for c in FakeClient.calls if c[0] == "time_slots"]


def _spot_checks(calls):
    """(service, staff, day) of every live re-fetch beyond the de-duplicated minimum. Only
    meaningful while the memo held: in s2..s5 a pair's first call is its duration probe,
    so every further call is a check."""
    by_pair = {}
    for _, ns, staff, day in calls:
        by_pair.setdefault((ns, staff), []).append(day)
    return [(ns, staff, day) for (ns, staff), days in by_pair.items() if ns != "s1"
            for day in days[1:]]


@pytest.mark.parametrize("seed", range(10))
def test_a_later_service_breaking_the_invariant_is_caught(seed):
    """The old sampler spent every check on the first memo hits in plan order — the same
    pair's days 2..5 on every run — so a later service with genuinely different openings
    at the same duration was never compared and was published wrong under ok=True. With
    as many checks as memo-served services, each of them now gets one, whatever the draw."""
    result, _ = _five_cuts(seed, budget=4, diverge={"s5"})
    assert result.ok is False
    assert any("dedup invariant broken for Cut 5" in e for e in result.errors)


def test_spot_checks_spread_across_services_and_rotate_between_runs():
    days = len(_open_days())
    services, pairs, check_days = set(), set(), set()
    for seed in range(30):
        result, calls = _five_cuts(seed, budget=2)
        checks = _spot_checks(calls)
        # The whole budget, as there are more memo-served pairs than checks...
        assert len(checks) == 2
        # ...on two DIFFERENT services, not two days of one pair.
        assert len({ns for ns, _, _ in checks}) == 2
        assert result.ok
        # A memo hit that was re-fetched live saved nothing; only the rest count.
        assert result.requests_saved == 10 * days - len(calls)
        services |= {ns for ns, _, _ in checks}
        pairs |= {(ns, staff) for ns, staff, _ in checks}
        check_days |= {day for _, _, day in checks}
    # Across runs the draw covers every memo-served service, both stylists, and more than
    # the pair's second day — the only day the old sampler could ever look at first.
    assert services == {"s2", "s3", "s4", "s5"}
    assert {staff for _, staff in pairs} == {NAO, SACHI}
    assert len(check_days) >= 3


def test_one_check_a_run_still_reaches_the_last_service_on_some_runs():
    """With one check per run, no single run can prove every service — but the rotation
    means the last service is reached on some runs instead of on none, ever."""
    caught = [seed for seed in range(40)
              if not _five_cuts(seed, budget=1, diverge={"s5"})[0].ok]
    assert caught, "the last service was never checked on any of 40 runs"
    assert len(caught) < 40      # a sample, not a re-fetch of everything


@pytest.mark.parametrize("seed", range(5))
def test_a_broken_invariant_drops_the_memo_for_the_rest_of_the_run(seed):
    """What happens after a mismatch is unchanged by the sampler: the memo is dropped and
    not reused, and the fetch is untrusted."""
    days = len(_open_days())
    result, calls = _five_cuts(seed, budget=4, diverge={"s3"})
    assert result.ok is False
    assert any("dedup invariant broken for Cut 3" in e for e in result.errors)
    per_pair = Counter((ns, staff) for _, ns, staff, _ in calls)
    for ns in ("s4", "s5"):
        for staff in (NAO, SACHI):
            assert per_pair[(ns, staff)] == days, f"{ns}/{staff} was served from the memo"


def test_the_spot_check_draw_comes_from_the_injected_rng():
    """The seam that keeps the tests above deterministic while production rotates."""
    assert _five_cuts(7, budget=2)[1] == _five_cuts(7, budget=2)[1]
    draws = {tuple(_spot_checks(_five_cuts(seed, budget=2)[1])) for seed in range(5)}
    assert len(draws) > 1


def test_production_draws_from_a_system_seeded_rng(monkeypatch):
    """Rotation between runs rests entirely on fetch() building an UNSEEDED Random when no
    rng is injected. Every other sampler test injects one, so `random.Random(0)` as the
    default — a static sample in production, the bug this sampler replaced — passed them
    all."""
    made = []

    class Spy(random.Random):
        def __init__(self, *args):
            made.append(args)
            super().__init__(*args)

    monkeypatch.setattr(fa.random, "Random", Spy)
    fetch(mkconfig(lookahead_days=10, verify_dedup_samples=1), client_factory=FiveCuts)
    assert made == [()]


def test_production_runs_do_not_all_check_the_same_day():
    """The same, end to end: one check a run, no rng injected. 40 equally likely draws, so
    twelve identical runs in a row would take odds of about 1 in 10^17."""
    draws = set()
    for _ in range(12):
        FakeClient.calls = []
        FiveCuts.diverge = set()
        fetch(mkconfig(lookahead_days=10, verify_dedup_samples=1), client_factory=FiveCuts)
        draws.add(tuple(_spot_checks(_timeslot_calls())))
    assert len(draws) > 1


# FiveCuts hides the hard case: every pair there whose stylist an earlier service walked
# IS memo-served, so a check can never be promised to a pair with no use for it. The real
# menu is full of pairs that look just as eligible up front and never are — the stylist was
# walked before, but never at this length — and a draft of this sampler lost every check it
# promised one of them.
class Menu(FakeClient):
    """`durations` maps service_id, or (service_id, staff_id) when one stylist's length
    differs, to minutes."""
    durations: dict = {}
    diverge: set = set()

    def time_slots(self, staff_id, date_iso):
        FakeClient.calls.append(("time_slots", self.ns, staff_id, date_iso))
        FakeClient.slot_sessions.append(self.n)
        dur = self.durations.get((self.ns, staff_id)) or self.durations[self.ns]
        starts = (900,) if self.ns in type(self).diverge else (600, 660)
        return [RawSlot(date=date_iso, service_id=self.ns, staff_id=staff_id,
                        start_min=m, end_min=m + dur, token="t") for m in starts]


def _menu_entries(names, staff_ids=(NAO,)):
    return [{"service_id": sid, "name": name, "staff_ids": list(staff_ids),
             "bookable_item_id": f"{sid}:SV"} for sid, name in names.items()]


class NaoSalon(Menu):
    """What `stylists: [Nao]` plans against the real menu (docs/timely-api.md): the nine
    salon services in catalog order, one pair each, at their real lengths. Every one after
    Hair Cut COULD be memo-served as far as anything knowable up front says, but only Blow
    Out (55m, as Hair Cut), Half Head Foil and Ombre (3h, as Full Head Foil) are."""
    durations = {"hc": 55, "blow": 55, "roots": 90, "single": 120, "full": 180,
                 "half": 180, "two": 240, "ombre": 180, "deep": 30}
    catalog = _menu_entries({
        "hc": "Hair Cut", "blow": "Blow Out", "roots": "Roots Color",
        "single": "Single Process", "full": "Full Head Foil", "half": "Half Head Foil",
        "two": "Two+ Process", "ombre": "Ombre", "deep": "Deep Conditioner"})
    MEMO_SERVED = {"blow", "half", "ombre"}


class BeardShave(Menu):
    """The real Beard Shave shape, reduced: X has one memo-served pair (Nao, at A's length)
    and one that never is (Sachi, at a length nobody walked before) — as Fausto is
    memo-served from Head Shave and Sachi's is her first 30-minute walk."""
    durations = {"A": 55, "X": 55, ("X", SACHI): 85, "Y": 55}
    catalog = (_menu_entries({"A": "Svc A", "X": "Svc X"}, (NAO, SACHI))
               + _menu_entries({"Y": "Svc Y"}))
    MEMO_SERVED = {"X", "Y"}


def _menu(cls, seed, budget, diverge=()):
    """Run `cls`; return the result and the number of live requests per pair."""
    FakeClient.calls = []
    cls.diverge = set(diverge)
    result = fetch(mkconfig(lookahead_days=10, verify_dedup_samples=budget),
                   client_factory=cls, rng=random.Random(seed))
    return result, Counter((ns, staff) for _, ns, staff, _ in _timeslot_calls())


def _menu_checks(cls, seed, budget):
    """The spot-checks of a run where the memo held: requests beyond what the same plan
    costs with checking switched off."""
    _, floor = _menu(cls, seed, 0)
    result, made = _menu(cls, seed, budget)
    return result, made - floor


@pytest.mark.parametrize("cls", [NaoSalon, BeardShave])
def test_a_budget_covering_every_memo_served_service_checks_every_one(cls):
    """A draft of this sampler, given the default 4 checks for `stylists: [Nao]`'s 3
    memo-served services, checked only 2 of them on 34 runs in 50. It promised checks up
    front to pairs that then turned out never to be memo-served, and a promise to such a
    pair was simply lost. Here the budget is exactly the number of memo-served services:
    on that draft, 44 of 50 NaoSalon runs and 26 of 50 BeardShave runs fell short."""
    budget = len(cls.MEMO_SERVED)
    short = {}
    for seed in range(50):
        result, checks = _menu_checks(cls, seed, budget)
        assert result.ok
        assert sum(checks.values()) <= budget
        if {ns for ns, _ in checks} != cls.MEMO_SERVED:
            short[seed] = sorted({ns for ns, _ in checks})
    assert not short, f"{len(short)}/50 runs left a memo-served service unchecked: {short}"


@pytest.mark.parametrize("cls, culprit", [(NaoSalon, "blow"), (NaoSalon, "ombre"),
                                          (BeardShave, "X"), (BeardShave, "Y")])
def test_a_diverging_service_is_caught_on_every_run_when_the_budget_covers_it(cls, culprit):
    """The cost of the lost checks: on that draft, Blow Out alone diverging was published
    under ok=True on 31 of these 50 runs and BeardShave's X on 26 — where the old fixed
    sampler, for all its faults, caught both every time."""
    missed = [seed for seed in range(50)
              if _menu(cls, seed, len(cls.MEMO_SERVED), diverge={culprit})[0].ok]
    assert not missed, f"{culprit} published under ok=True on runs {missed}"


def test_with_fewer_checks_than_services_each_is_equally_likely_to_be_checked():
    """Uniform over the memo-served services, wherever they sit in the plan and however many
    never-memo-served pairs surround them — and the one check is always spent."""
    counts = Counter()
    for seed in range(300):
        _, checks = _menu_checks(NaoSalon, seed, 1)
        assert sum(checks.values()) == 1
        counts.update(ns for ns, _ in checks)
    assert set(counts) == NaoSalon.MEMO_SERVED
    assert all(70 <= n <= 130 for n in counts.values()), counts      # 100 each expected


def test_a_budget_covering_every_memo_served_pair_checks_every_pair():
    for seed in range(20):
        result, calls = _five_cuts(seed, budget=8)
        assert result.ok
        assert sorted((ns, staff) for ns, staff, _ in _spot_checks(calls)) == \
            sorted((f"s{i}", staff) for i in range(2, 6) for staff in (NAO, SACHI))


def test_a_check_made_after_the_sweep_goes_through_a_fresh_session():
    """svcB is the only memo-served service and has two pairs, so with two checks one is
    made as svcB's walk ends and the other has to wait for the sweep to finish. It must not
    reuse svcB's sweep session: by then that session has sat idle for the rest of the sweep,
    nothing in docs/timely-api.md says how long an obg session lives, and a stale one that
    answered with a well-formed empty day would read as a broken invariant and void the
    whole run. A new session makes the sweep's requests in the sweep's order: the funnel,
    then GetOpenDates for the stylist, then gettimeslots."""
    result = _fetch(verify_dedup_samples=2)

    assert result.ok
    per_pair = Counter((ns, staff) for _, ns, staff, _ in _timeslot_calls())
    assert per_pair[("svcB", NAO)] == 2 and per_pair[("svcB", SACHI)] == 2
    # Sessions 0-3 are the sweep's: the catalog probe, then svcA, svcB, svcC.
    assert [ns for ns, _ in FakeClient.sessions] == ["", "svcA", "svcB", "svcC", "svcB"]
    assert FakeClient.sessions[4][1] == ["bootstrap", "service", "staff"]
    assert FakeClient.slot_sessions[-1] == 4
    _, _, staff, day = FakeClient.calls[-1]
    checked = date.fromisoformat(day)
    assert FakeClient.calls[-2:] == [("open_dates", "svcB", staff, checked.year, checked.month),
                                     ("time_slots", "svcB", staff, day)]
    # Both checks were paid for, the one made after the sweep included: only the memo hits
    # that were never re-fetched count as saved.
    assert result.requests_saved == 5 * len(_open_days()) - len(_timeslot_calls())


def test_a_check_made_after_the_sweep_asks_for_the_open_dates_first():
    """Every sweep session asks GetOpenDates about a stylist before reading any of their
    days, and so do the browser and recon/capture.py; a make-up session used to go straight
    from the funnel to gettimeslots, an order nothing had ever been seen to answer. If that
    order earns a well-formed empty day, the check reads it as a broken invariant and voids
    a run whose every lookup landed — on about half of all real-menu runs, which is how
    often a make-up session is needed. Whether live Timely does this is unknown; nothing
    here should depend on it not doing so."""
    class DatesFirst(FakeClient):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.dated = set()

        def open_dates(self, staff_id, month, year):
            self.dated.add(staff_id)
            return super().open_dates(staff_id, month, year)

        def time_slots(self, staff_id, date_iso):
            if staff_id not in self.dated:
                FakeClient.calls.append(("time_slots", self.ns, staff_id, date_iso))
                FakeClient.slot_sessions.append(self.n)
                return []           # what a well-formed "no times available" parses to
            return super().time_slots(staff_id, date_iso)

    result = fetch(mkconfig(lookahead_days=10, verify_dedup_samples=2),
                   client_factory=DatesFirst, rng=random.Random(0))

    assert FakeClient.slot_sessions[-1] == 4        # one check was made after the sweep...
    assert not any("dedup invariant broken" in e for e in result.errors)
    assert result.ok                                # ...and it agreed with the memo


def test_a_check_that_cannot_be_made_is_reported_not_charged_as_a_failed_lookup():
    """A spot-check re-reads one day of a pair whose lookup already completed. If that
    request fails, the pair is exactly as good as one the draw never picked. It used to run
    inside the pair's lookup, so a single 429 on it threw the pair's slots away and cost the
    stylist delete authority for the run."""
    class CheckErrors(FakeClient):
        def time_slots(self, staff_id, date_iso):
            if self.ns == "svcB" and any(c[1:3] == ("svcB", staff_id)
                                         for c in _timeslot_calls()):
                raise RuntimeError("check exploded")      # the probe went through
            return super().time_slots(staff_id, date_iso)

    result = fetch(mkconfig(lookahead_days=10, verify_dedup_samples=2),
                   client_factory=CheckErrors, rng=random.Random(0))

    assert result.ok
    assert result.lookups_failed == 0
    assert result.staff_ok == {NAO, SACHI}
    assert {s.service for s in result.slots} == {"Hair Cut", "Hair Cut Deluxe",
                                                 "Beard Sculpt"}
    # One made as svcB's walk ended, one after the sweep: both said so.
    assert len([e for e in result.errors if "spot-check not made" in e]) == 2


def test_a_session_that_will_not_open_after_the_sweep_leaves_the_run_trusted():
    FakeClient.fail_sessions = {4}          # the first session opened after the sweep
    result = _fetch(verify_dedup_samples=2)

    assert result.ok
    assert result.lookups_failed == 0 and result.staff_ok == {NAO, SACHI}
    assert any("spot-check not made" in e for e in result.errors)


def test_a_throttle_after_the_sweep_stops_the_checks_but_does_not_abort_the_run():
    """Every lookup has landed by then. BudgetExhausted says stop sending, and we do — no
    further session is opened — but it says nothing about data already complete."""
    FakeClient.budget_error_sessions = {6}  # FiveCuts' sweep is sessions 0-5
    result, _ = _five_cuts(0, budget=8)     # 4 checks in-session, 4 left for afterwards

    assert result.ok
    assert not any("run aborted" in e for e in result.errors)
    assert any("spot-checks stopped" in e for e in result.errors)
    assert len(FakeClient.sessions) == 7


def test_a_throttle_on_an_in_sweep_check_still_aborts_the_sweep():
    """Unlike a check after the sweep, this one has services still to walk."""
    class ThrottledCheck(FiveCuts):
        def time_slots(self, staff_id, date_iso):
            if self.ns == "s2" and date_iso != _open_days()[0]:
                # s2 is memo-served: past its probe day, every request it makes is a check.
                raise BudgetExhausted("cap reached")
            return super().time_slots(staff_id, date_iso)

    FiveCuts.diverge = set()
    result = fetch(mkconfig(lookahead_days=10, verify_dedup_samples=4),
                   client_factory=ThrottledCheck, rng=random.Random(0))

    assert result.ok is False
    assert any("run aborted" in e for e in result.errors)
    assert result.staff_ok == set()
    assert not any(c[1] in ("s3", "s4", "s5") for c in _timeslot_calls())
    # Nor is anything sent after it. Three checks are still unspent and s2's other pair is
    # still unchecked, but a stop condition means stop: no make-up session for it either.
    # (Only the request cap in src/timely.py is sticky. A throttle stop raises on the
    # request that completes the streak and lets the next one go out, so this holds only
    # because fetch() stops asking.)
    assert [ns for ns, _ in FakeClient.sessions] == ["", "s1", "s2"]


def test_no_check_is_made_after_a_sweep_that_aborted_in_a_funnel_setup():
    """svcB takes one of its two checks as its walk ends and leaves the other for after the
    sweep. svcC's funnel setup is then throttled, which ends the run: the check left over
    is dropped with everything else, not sent through a fresh session."""
    FakeClient.budget_error_setup_for = {"svcC"}
    result = _fetch(verify_dedup_samples=2)

    assert result.ok is False
    assert [ns for ns, _ in FakeClient.sessions] == ["", "svcA", "svcB", "svcC"]


def test_no_check_is_made_after_the_memo_was_caught_out():
    """s2's check finds the invariant broken, which already voids the run; s2's other pair
    and the checks still unspent could only add requests to a run that publishes nothing."""
    result, _ = _five_cuts(0, budget=8, diverge={"s2"})

    assert result.ok is False
    assert any("dedup invariant broken for Cut 2" in e for e in result.errors)
    assert len(FakeClient.sessions) == 6        # the probe and s1..s5; nothing after


def test_a_throttle_on_the_last_services_check_stops_the_checks_but_not_the_run():
    """The last service's check is made as its walk ends, inside the sweep, but there is
    nothing left to walk by then, exactly as after the sweep. It used to abort the run all
    the same: ok=False, exit 2, nothing written, over a request that only re-read a day
    every lookup had already landed — while the same throttle one session later only
    stopped the checks. The real menu's last service (docs/timely-api.md: the Junior
    Barber's Portion Hair Cut & Beard Trim, 1h25 like the Hair Cut & Beard Trim before it)
    is memo-served and takes its check this way on some runs."""
    class ThrottledLastCheck(FiveCuts):
        def time_slots(self, staff_id, date_iso):
            # s5 is the last plan entry; past its probe day, every request it makes is a check.
            if self.ns == "s5" and date_iso != _open_days()[0]:
                raise BudgetExhausted("3 consecutive throttled GET /booking/gettimeslots")
            return super().time_slots(staff_id, date_iso)

    FiveCuts.diverge = set()
    # 8 checks for 8 memo-served pairs: s2..s5 each take one as their walk ends, whatever
    # the draw, and four are left for after the sweep.
    result = fetch(mkconfig(lookahead_days=10, verify_dedup_samples=8),
                   client_factory=ThrottledLastCheck, rng=random.Random(0))

    assert result.lookups_ok == result.lookups_expected == 10
    assert result.ok
    assert result.staff_ok == {NAO, SACHI}
    assert not any("run aborted" in e for e in result.errors)
    assert any("spot-checks stopped" in e for e in result.errors)
    # ...and they did stop: the four left over are not made, and no session is opened
    # after the sweep (the probe and s1..s5 are sessions 0-5).
    assert len(FakeClient.sessions) == 6


def test_a_service_whose_make_up_session_would_not_open_is_not_tried_again():
    """Three stylists, so svcB has three memo-served pairs: one checked as its walk ends,
    two left for after the sweep. The first make-up session for svcB will not open. Its
    second pair must not open another: a service whose funnel just failed is passed over,
    and the check it would have cost goes to the next service on the list, if any."""
    class ThreeStylists(FakeClient):
        catalog = [{"service_id": sid, "name": name, "staff_ids": [NAO, SACHI, MASA],
                    "bookable_item_id": f"{sid}:SV"}
                   for sid, name in (("svcA", "Hair Cut"), ("svcB", "Hair Cut Deluxe"))]

    FakeClient.fail_sessions = {3}    # 0 probe, 1 svcA, 2 svcB; 3 = first after the sweep
    result = fetch(mkconfig(lookahead_days=10, verify_dedup_samples=3),
                   client_factory=ThreeStylists, rng=random.Random(0))

    assert result.ok
    assert result.lookups_failed == 0
    assert len([e for e in result.errors if "spot-check not made" in e]) == 1
    assert [ns for ns, _ in FakeClient.sessions] == ["", "svcA", "svcB", "svcB"]


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


# ------------------------------------------------- who was looked for, and over what window
# 02:00 UTC on Oct 13 is still 22:00 EDT on Oct 12: one of the four or five hours a day
# when New York's date and a UTC host's (a GitHub Actions runner's) are different days.
# Outside those hours "today" read from the host's clock is the right answer by accident,
# which is how a host-date window_start would sail through CI most of the time.
_FROZEN = datetime(2026, 10, 13, 2, 0, tzinfo=timezone.utc)
NY_TODAY = date(2026, 10, 12)


class _UtcHostDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        # A naive now() is the host's wall clock: a UTC runner's.
        return _FROZEN.astimezone(tz) if tz else _FROZEN.replace(tzinfo=None)


class _UtcHostDate(date):
    @classmethod
    def today(cls):
        return _FROZEN.date()


@pytest.fixture
def ny_evening(monkeypatch):
    """Freeze fetch()'s clock at _FROZEN, on a UTC host."""
    monkeypatch.setattr(fa, "datetime", _UtcHostDatetime)
    monkeypatch.setattr(fa, "date", _UtcHostDate)


def _uncovered(result, stylist):
    """The sync's delete-authority rule, as the shared interface defines it: True means
    this stylist's entries are withheld (no deletes, no writes) this run."""
    if result.staff_ok is None:
        return False
    if result.staff_planned is not None:
        return stylist in result.staff_planned - result.staff_ok
    return stylist not in result.staff_ok          # legacy rule: planned set unknown


def test_staff_planned_is_everyone_the_plan_looked_for(ny_evening):
    """A stylist we looked for and could not see must keep their entries; one we never
    looked for (left the salon, dropped from the catalog, filtered out) has ordinary stale
    entries. staff_ok alone cannot tell those apart — both are simply absent from it."""
    FakeClient.fail_staff_for = {("svcC", NAO)}
    result = _fetch()

    assert result.staff_planned == {NAO, SACHI}
    assert _uncovered(result, NAO)                 # planned, failed: withheld
    assert not _uncovered(result, SACHI)           # planned, seen: deletable
    assert not _uncovered(result, MASA)            # never planned: covered by absence
    # The window the fetch actually walked, so the sync judges staleness against it — New
    # York's date, not the host's, which is already the 13th.
    assert result.window_start == NY_TODAY


def test_a_stylist_filtered_out_by_config_is_not_planned():
    result = _fetch(stylists=["Nao"])
    assert result.staff_planned == {NAO}
    assert not _uncovered(result, SACHI)


@pytest.mark.parametrize("abort_at", ["svcA", "svcB", "svcC"])
def test_an_aborted_sweep_still_reports_the_whole_plan(abort_at):
    """staff_planned - staff_ok is who the sync withholds. Shrinking staff_planned to the
    stylists the sweep reached would hand everyone it never reached their deletes back.

    Aborting at svcA is the case that can tell: nobody has been reached. svcA walks both
    stylists, so from svcB on "reached" and "planned" are the same set."""
    FakeClient.budget_error_setup_for = {abort_at}
    result = _fetch()

    assert result.ok is False
    assert result.staff_ok == set()
    assert result.staff_planned == {NAO, SACHI}
    assert _uncovered(result, NAO) and _uncovered(result, SACHI)
    assert result.window_start is not None


def test_a_throttled_bootstrap_reports_no_plan_rather_than_an_empty_one(ny_evening):
    """No plan existed, so the honest value is None ('unknown'), and it is also the only
    safe one: an EMPTY staff_planned would make staff_planned - staff_ok empty, i.e. every
    stylist on the calendar 'never looked for' and every entry deletable — on the one run
    that saw nothing at all. None falls back to the legacy rule, which withholds everyone."""
    FakeClient.budget_error_setup_for = {""}       # the catalog probe's namespace
    result = _fetch()

    assert result.staff_ok == set()
    assert result.staff_planned is None
    assert all(_uncovered(result, sid) for sid in STAFF)
    assert result.window_start == NY_TODAY


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


def test_a_single_stylist_name_is_one_name_not_its_letters():
    """`stylists: Nao` was iterated letter by letter, and Sachi has an 'a' — so was every
    one of the nine stylists. mkconfig validates, as Config.load does, so this is the
    config production would actually run with."""
    result = _fetch(stylists="Nao")
    assert {s.stylist for s in result.slots} == {"Nao"}
    assert result.staff_planned == {NAO}


def test_the_filters_never_iterate_a_bare_string_by_letter():
    """Belt and braces for a Config that never went through validate()."""
    assert _stylist_allowed(NAO, "Nao")
    assert not _stylist_allowed(SACHI, "Nao")
    assert not _service_allowed("Hair Cut", "Buzz")    # "Buzz" shares only a 'u' with it
    assert _service_allowed("Buzz", "Buzz")


def test_min_slot_hour_filter():
    result = _fetch(min_slot_hour=11)
    assert all(s.start.hour >= 11 for s in result.slots)
