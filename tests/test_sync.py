"""Tests for the Google Calendar write path (src/sync_calendar.py).

This module exists because that file ran unattended in production for months with no test
coverage at all, and then spent three days failing every hour on an unhandled 409 that a
single test would have caught. The fake (tests/fakes.py) reproduces Google's real
tombstone semantics; see its docstring before changing anything here.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from conftest import mkconfig
from fakes import FakeCalendarService, http_error
from src import sync_calendar
from src.fetch_availability import FetchResult
from src.models import MARKER_ID, Event, ServiceOption, build_entries

NY = ZoneInfo("America/New_York")


def _event(stylist_id="175308", stylist="Nao", days_ahead=2, hour=10, duration=55):
    """One opening.

    Openings are spaced by whole DAYS by default, and distinct stylists get distinct
    entries, so a set of N events yields exactly N calendar entries under either
    event_style. That keeps these tests measuring the diff engine rather than the merging
    rules, which tests/test_normalize.py covers directly.
    """
    start = (datetime.now(NY).replace(hour=hour, minute=0, second=0, microsecond=0)
             + timedelta(days=days_ahead))
    return Event(stylist_id=stylist_id, stylist=stylist, stylist_role="Stylist",
                 start=start, book_url="https://book",
                 options=[ServiceOption("Hair Cut", "$75", duration, False)])


def _events(n, days_from=1):
    """n openings that stay separate entries in either style (unique stylist per event)."""
    return [_event(stylist_id=f"s{i}", stylist=f"Stylist {i}", days_ahead=days_from + i)
            for i in range(n)]


def _result(events, ok=True, notices="", staff_ok=None):
    return FetchResult(slots=[], events=list(events), notices=notices, ok=ok,
                       lookups_ok=60 if ok else 0, lookups_failed=0 if ok else 60,
                       lookups_expected=60, staff_ok=staff_ok)


def _entry(ev, config):
    return build_entries([ev], config.event_style)[0]


def _existing_from(ev, config, notices=""):
    """The calendar item that a previous successful sync would have left behind."""
    body = sync_calendar.entry_body(_entry(ev, config), config, notices)
    return dict(body, status="confirmed")


# --------------------------------------------------------------- suite self-check
def test_every_style_parametrized_test_actually_uses_its_parameter():
    """Twice now, a test has declared @parametrize("style", ...) and then built its config
    without threading the parameter through — running the same case twice and reporting two
    passes. That is precisely how a calendar-wiping bug stayed invisible to a 90-test suite,
    so the suite checks itself."""
    import re
    src = open(__file__, encoding="utf-8").read()
    blind = [m.group(1) for m in re.finditer(
        r'@pytest\.mark\.parametrize\("style".*?\ndef (\w+)\(style\):\n((?:    .*\n|\n)+?)(?=\n\n|\Z)',
        src) if "event_style=style" not in m.group(2)]
    assert blind == [], f"parametrized over style but never use it: {blind}"


# --------------------------------------------------------------- the 409 regression
def test_reopened_slot_revives_tombstone_instead_of_409():
    """THE production bug. Slot open -> booked (we delete) -> customer cancels -> reopens.

    Google's delete is soft: the id stays reserved on a cancelled event that showDeleted=False
    hides from us. A plain insert then 409s. We must fall back to update(), which revives it.
    """
    cfg = mkconfig()
    ev = _event()
    eid = _entry(ev, cfg).google_event_id()

    svc = FakeCalendarService(events=[_existing_from(ev, cfg)])
    # Round 1: the slot gets booked, so the feed no longer contains it -> we delete it.
    sync_calendar.sync(cfg, _result([]), service=svc)
    assert eid in svc.tombstones(), "delete must tombstone, not remove"
    assert eid not in svc.live()

    # Round 2: the customer cancels, the slot reopens, the feed has it again.
    stats = sync_calendar.sync(cfg, _result([ev]), service=svc)

    assert stats["revive"] == 1
    assert stats["failed_insert"] == 0
    assert eid in svc.live()
    assert svc.live()[eid]["status"] == "confirmed"
    # It really went insert-then-update, not some other path.
    assert ("insert", eid) in svc.calls and ("update", eid) in svc.calls


def test_revive_survives_a_lost_response_retry():
    """Second independent 409 source: our insert succeeded but the response was lost, and
    the retry sees its own event. Same fallback must cover it."""
    cfg = mkconfig()
    ev = _event()
    eid = _entry(ev, cfg).google_event_id()
    # The event is already live and NOT known to us (simulating the lost response) by
    # seeding the store while leaving the sync to believe it must insert.
    svc = FakeCalendarService(events=[dict(_existing_from(ev, cfg), summary="stale")])
    svc.store[eid]["id"] = eid

    # Pretend the listing missed it (that is exactly what a lost response looks like).
    original_list = svc.events().list

    def blind_list(**kw):
        req = original_list(**kw)
        inner = req.execute

        def execute(http=None, num_retries=0):
            resp = inner(num_retries=num_retries)
            resp["items"] = [i for i in resp["items"] if i["id"] != eid]
            return resp
        req.execute = execute
        return req

    svc.events().list = blind_list
    stats = sync_calendar.sync(cfg, _result([ev]), service=svc)
    assert stats["revive"] == 1
    assert svc.live()[eid]["summary"] == _entry(ev, cfg).summary()   # update overwrote the stale copy


# --------------------------------------------------------------- diff correctness
def test_insert_patch_and_unchanged_are_counted_on_success():
    cfg = mkconfig()
    keep, change, add = _events(3)
    stale_body = dict(_existing_from(change, cfg), summary="something else")
    svc = FakeCalendarService(events=[_existing_from(keep, cfg), stale_body])

    stats = sync_calendar.sync(cfg, _result([keep, change, add]), service=svc)

    assert (stats["unchanged"], stats["patch"], stats["insert"]) == (1, 1, 1)
    assert stats["delete"] == 0
    assert svc.live()[_entry(change, cfg).google_event_id()]["summary"] == _entry(change, cfg).summary()


def test_deletes_run_before_inserts():
    """A stale event is a publicly advertised, already-booked appointment. Removing it is
    more urgent than adding good news, so it must not be starved by a failing insert."""
    cfg = mkconfig()
    gone, fresh = _events(2)
    svc = FakeCalendarService(events=[_existing_from(gone, cfg)])

    sync_calendar.sync(cfg, _result([fresh]), service=svc)

    writes = [m for m, _ in svc.calls if m in ("delete", "insert")]
    assert writes.index("delete") < writes.index("insert")


def test_delete_of_already_gone_event_is_success_not_failure():
    """googleapiclient's own retry can turn a successful delete into a 404/410 on the
    retried call; 'already gone' is the state we wanted."""
    cfg = mkconfig()
    gone = _event()
    eid = _entry(gone, cfg).google_event_id()
    svc = FakeCalendarService(
        events=[_existing_from(gone, cfg)],
        fail_plan={("delete", eid): [http_error(410, "Resource has been deleted", "deleted")]})

    stats = sync_calendar.sync(cfg, _result([]), service=svc)
    assert stats["delete"] == 1
    assert stats["failed_delete"] == 0


# --------------------------------------------------------------- failure isolation
def test_one_bad_event_does_not_abort_the_run():
    cfg = mkconfig()
    events = _events(3)
    bad = _entry(events[0], cfg).google_event_id()
    svc = FakeCalendarService(
        fail_plan={("insert", bad): [http_error(400, "Bad Request", "invalid")]})

    stats = sync_calendar.sync(cfg, _result(events), service=svc)

    assert stats["insert"] == 2            # the other two still landed
    assert stats["failed_insert"] == 1
    assert len(svc.live()) == 2


def test_permission_failure_aborts_immediately():
    """403 forbidden is systemic. Retrying it once per event would mean thousands of
    pointless calls before the job finally died."""
    cfg = mkconfig()
    events = _events(10)
    svc = FakeCalendarService(
        fail_plan={("insert", None): [http_error(403, "Forbidden", "forbidden")]})

    with pytest.raises(sync_calendar.WriteAborted):
        sync_calendar.sync(cfg, _result(events), service=svc)
    assert len(svc.methods("insert")) == 1     # stopped at the first one


def test_rate_limit_403_is_not_treated_as_permission_failure():
    cfg = mkconfig()
    events = _events(2)
    svc = FakeCalendarService(fail_plan={
        ("insert", _entry(events[0], cfg).google_event_id()):
            [http_error(403, "Rate Limit Exceeded", "rateLimitExceeded")]})

    stats = sync_calendar.sync(cfg, _result(events), service=svc)
    assert stats["failed_insert"] == 1
    assert stats["insert"] == 1                # kept going


def test_consecutive_failures_trip_the_breaker():
    cfg = mkconfig()
    events = _events(40)
    svc = FakeCalendarService(
        fail_plan={("insert", None): [http_error(400, "Bad Request", "invalid")] * 40})

    with pytest.raises(sync_calendar.WriteAborted):
        sync_calendar.sync(cfg, _result(events), service=svc)
    assert len(svc.methods("insert")) == sync_calendar.MAX_CONSECUTIVE_FAILURES


# --------------------------------------------------------------- delete guards
@pytest.mark.parametrize("style", ["slots", "blocks"])
def test_skips_all_deletes_when_fetch_failed(style):
    """The core safety property: a failed fetch must never delete existing events."""
    cfg = mkconfig(event_style=style)
    gone = _event()
    svc = FakeCalendarService(events=[_existing_from(gone, cfg)])

    stats = sync_calendar.sync(cfg, _result([], ok=False), service=svc)

    assert stats["delete"] == 0
    assert stats["skipped_delete"] == 1
    assert svc.methods("delete") == []


@pytest.mark.parametrize("style", ["slots", "blocks"])
def test_window_scoped_delete_protects_far_out_events(style):
    """A short near-term run must prune stale in-window events but NOT the deep run's
    far-out ones, so the hourly and 6-hourly sweeps can share one calendar."""
    cfg = mkconfig(lookahead_days=21, event_style=style)
    near = _event(days_ahead=5)
    far = _event(stylist_id="24102", stylist="Sachi", days_ahead=40)
    svc = FakeCalendarService(events=[_existing_from(near, cfg), _existing_from(far, cfg)])

    stats = sync_calendar.sync(cfg, _result([]), service=svc)

    assert svc.methods("delete") == [_entry(near, cfg).google_event_id()]
    assert stats["delete"] == 1


@pytest.mark.parametrize("style", ["slots", "blocks"])
def test_blast_radius_guard_refuses_a_mass_delete(style):
    cfg = mkconfig(event_style=style)
    events = _events(80)
    svc = FakeCalendarService(events=[_existing_from(e, cfg) for e in events])

    # A "successful" fetch that somehow lost almost everything.
    stats = sync_calendar.sync(cfg, _result(events[:5]), service=svc)

    assert stats["delete"] == 0
    assert stats["blocked_delete"] == 75
    assert svc.methods("delete") == []


@pytest.mark.parametrize("style", ["slots", "blocks"])
def test_allow_mass_delete_overrides_the_guard(style):
    cfg = mkconfig(event_style=style)
    events = _events(80)
    svc = FakeCalendarService(events=[_existing_from(e, cfg) for e in events])

    stats = sync_calendar.sync(cfg, _result(events[:5]), service=svc,
                               allow_mass_delete=True)
    assert stats["delete"] == 75


def test_blast_radius_denominator_is_in_window_not_whole_calendar():
    """With a whole-calendar denominator, a 21-day run seeing ~350 of ~3300 events could
    delete every single one of them and still look like a ~10% change."""
    cfg = mkconfig(lookahead_days=21)
    in_window = [_event(stylist_id=f"w{i}", days_ahead=1 + (i % 18)) for i in range(60)]
    far = [_event(stylist_id=f"f{i}", days_ahead=40 + (i % 45)) for i in range(300)]
    svc = FakeCalendarService(
        events=[_existing_from(e, cfg) for e in in_window + far])

    stats = sync_calendar.sync(cfg, _result([]), service=svc)

    assert stats["delete"] == 0            # blocked: 60 of 60 in-window is a wipe
    assert stats["blocked_delete"] == 60


@pytest.mark.parametrize("style", ["slots", "blocks"])
def test_guard_allows_normal_churn(style):
    cfg = mkconfig(event_style=style)
    events = _events(80)
    svc = FakeCalendarService(events=[_existing_from(e, cfg) for e in events])

    stats = sync_calendar.sync(cfg, _result(events[10:]), service=svc)
    assert stats["delete"] == 10           # 12.5% churn is routine
    assert stats["blocked_delete"] == 0


# --------------------------------------------------------------- id schemes
def test_obsolete_id_scheme_is_migrated_away():
    """Switching event_style must clean up entries written in the other style, at any
    distance in the future, without being mistaken for a suspicious mass delete."""
    cfg = mkconfig(event_style="blocks")
    legacy = _event()
    legacy_item = dict(_existing_from(legacy, mkconfig(event_style="slots")),
                       status="confirmed")
    svc = FakeCalendarService(events=[legacy_item])

    stats = sync_calendar.sync(cfg, _result([legacy]), service=svc)

    assert stats["migrated"] == 1
    assert legacy.google_event_id() in svc.tombstones()
    assert stats["insert"] == 1            # re-published as a block
    new_id = next(iter(svc.live()))
    assert new_id.startswith("kidav")


def test_block_ids_never_collide_with_slot_ids():
    """sha1 hex is [0-9a-f], so 'v' can never appear where the block prefix puts it."""
    ev = _event()
    slot_id = ev.google_event_id()
    assert slot_id.startswith("kida") and not slot_id.startswith("kidav")


def test_status_marker_is_never_treated_as_a_slot():
    cfg = mkconfig()
    marker = {"id": MARKER_ID, "summary": "checked", "status": "confirmed",
              "start": {"date": datetime.now(NY).date().isoformat()},
              "end": {"date": (datetime.now(NY).date() + timedelta(days=1)).isoformat()}}
    svc = FakeCalendarService(events=[marker])

    stats = sync_calendar.sync(cfg, _result([]), service=svc)

    assert stats["delete"] == 0
    assert MARKER_ID not in svc.tombstones()   # it must survive its own sync


# --------------------------------------------------------------- parsing edge cases
def test_start_parsing_handles_naive_and_all_day_and_garbage():
    tz = NY
    assert sync_calendar._parse_start({"start": {"dateTime": "2026-07-20T09:00:00-04:00"}}, tz)
    naive = sync_calendar._parse_start({"start": {"dateTime": "2026-07-20T09:00:00"}}, tz)
    assert naive is not None and naive.tzinfo is not None   # must not stay naive
    allday = sync_calendar._parse_start({"start": {"date": "2026-07-20"}}, tz)
    assert allday is not None and allday.tzinfo is not None
    assert sync_calendar._parse_start({"start": {"dateTime": "not a date"}}, tz) is None
    assert sync_calendar._parse_start({}, tz) is None


def test_naive_datetime_does_not_kill_the_delete_phase():
    """Comparing a naive datetime to an aware horizon raises TypeError, which used to
    escape the comprehension and abort every delete."""
    cfg = mkconfig()
    gone = _event()
    item = _existing_from(gone, cfg)
    item["start"] = {"dateTime": gone.start.replace(tzinfo=None).isoformat()}
    svc = FakeCalendarService(events=[item])

    stats = sync_calendar.sync(cfg, _result([]), service=svc)
    assert stats["delete"] == 1


# --------------------------------------------------------------- patch churn
def test_description_is_not_compared_when_the_notices_scrape_failed():
    """notices=None means we do not know the banner. Blanking it would rewrite every
    event on the calendar, and restore them all on the next run."""
    cfg = mkconfig()
    ev = _event()
    with_notice = dict(sync_calendar.entry_body(_entry(ev, cfg), cfg, "Closed July 4"),
                       status="confirmed")
    svc = FakeCalendarService(events=[with_notice])

    stats = sync_calendar.sync(cfg, _result([ev], notices=None), service=svc)

    assert stats["patch"] == 0
    assert stats["unchanged"] == 1
    assert svc.methods("patch") == []


def test_a_real_notice_change_still_patches():
    cfg = mkconfig()
    ev = _event()
    svc = FakeCalendarService(
        events=[dict(sync_calendar.entry_body(_entry(ev, cfg), cfg, "old notice"),
                     status="confirmed")])

    stats = sync_calendar.sync(cfg, _result([ev], notices="new notice"), service=svc)
    assert stats["patch"] == 1


def test_event_body_omits_color_and_source():
    """colorId overrode a subscriber's own colour choice irreversibly; source was visible
    only to the service account that wrote it."""
    body = sync_calendar.entry_body(_event(), mkconfig(), "")
    assert "colorId" not in body
    assert "source" not in body
    assert body["transparency"] == "transparent"


def test_round_trip_of_our_own_body_needs_no_patch():
    """Guards against a full-calendar patch storm: whatever we write must compare equal to
    itself on the next run."""
    cfg = mkconfig()
    ev = _event()
    body = sync_calendar.entry_body(ev, cfg, "notice")
    assert not sync_calendar._needs_patch(dict(body, status="confirmed"), body)


# ------------------------------------------- the event_style changeover (migration)
def _legacy_calendar(events, n=None):
    """A calendar full of entries written under the OTHER event_style."""
    slots_cfg = mkconfig(event_style="slots")
    return [_existing_from(e, slots_cfg) for e in (events if n is None else events[:n])]


def test_changeover_is_refused_when_the_replacement_set_is_implausible():
    """The hole the reviewers found: on the run that flips event_style, EVERY existing
    entry is 'obsolete'. `existing` is empty, so the in-window blast-radius guard has a
    denominator of zero and cannot fire — on the single largest delete this system ever
    performs. A partial-but-'ok' fetch would then empty the public calendar and exit 0."""
    cfg = mkconfig(event_style="blocks")
    events = _events(80)
    svc = FakeCalendarService(events=_legacy_calendar(events))

    # Fetch says ok but only produced 5 of the 80 openings.
    stats = sync_calendar.sync(cfg, _result(events[:5]), service=svc)

    assert stats["migrated"] == 0
    assert stats["blocked_delete"] == 80
    assert svc.methods("delete") == []
    # Every legacy entry is still standing. The 5 blocks we could build were inserted, so
    # those 5 openings are briefly shown twice — a visible duplicate is a far better
    # failure than a deleted calendar, and blocked_delete makes the run exit non-zero.
    legacy_left = [e for e in svc.live() if not e.startswith("kidav")]
    assert len(legacy_left) == 80
    assert stats["insert"] == 5


def test_a_real_slots_to_blocks_changeover_is_not_refused():
    """The guard must size replacements in OPENINGS, not entries.

    Blocks encode the same availability in ~3.3x fewer entries by design (measured on real
    captured data: 418 openings -> 126 entries). An entry-count guard would refuse this
    healthy migration and leave BOTH styles live on the calendar at once — which is what
    the production numbers would have done: 3414 openings -> ~1030 blocks against 3413
    legacy entries needs 1707 to clear an entry-count threshold.
    """
    cfg = mkconfig(event_style="blocks")
    # 40 stylists x 6 contiguous hourly openings = 240 openings that merge into 40 blocks.
    events = [_event(stylist_id=f"s{i}", stylist=f"S{i}", days_ahead=3, hour=9 + h)
              for i in range(40) for h in range(6)]
    svc = FakeCalendarService(events=_legacy_calendar(events))
    assert len(svc.store) == 240

    stats = sync_calendar.sync(cfg, _result(events), service=svc)

    assert stats["insert"] == 40              # far fewer entries than we retired...
    assert stats["migrated"] == 240           # ...yet the migration still proceeds
    assert stats["blocked_delete"] == 0
    live = svc.live()
    assert len(live) == 40
    assert all(eid.startswith("kidav") for eid in live)


def test_a_changeover_losing_most_openings_is_still_refused():
    """The openings-based guard must not become a rubber stamp: a fetch that lost most of
    its data still has to be caught, even though blocks legitimately shrink entry counts."""
    cfg = mkconfig(event_style="blocks")
    events = [_event(stylist_id=f"s{i}", stylist=f"S{i}", days_ahead=3, hour=9 + h)
              for i in range(40) for h in range(6)]
    svc = FakeCalendarService(events=_legacy_calendar(events))

    # Only 5 stylists' worth of openings came back (30 of 240).
    stats = sync_calendar.sync(cfg, _result(events[:30]), service=svc)

    assert stats["migrated"] == 0
    assert stats["blocked_delete"] == 240
    assert len([e for e in svc.live() if not e.startswith("kidav")]) == 240


def test_opening_count_is_the_unit_that_survives_a_style_change():
    # Several OPTIONS on one opening is still one opening — guards against
    # opening_count accidentally being len(self.options).
    multi = Event(stylist_id="1", stylist="N", stylist_role="", start=_event().start,
                  options=[ServiceOption(n, "", 30, False) for n in ("a", "b", "c")])
    assert multi.opening_count == 1
    block = build_entries([_event(days_ahead=3, hour=9), _event(days_ahead=3, hour=10)],
                          "blocks")[0]
    assert block.opening_count == 2
    # The invariant that makes the changeover guard sound: openings are conserved across
    # styles even though entry counts are not.
    evs = [_event(days_ahead=3, hour=9 + h) for h in range(5)]
    assert (sum(e.opening_count for e in build_entries(evs, "slots"))
            == sum(e.opening_count for e in build_entries(evs, "blocks")) == 5)
    assert len(build_entries(evs, "slots")) != len(build_entries(evs, "blocks"))


def test_changeover_respects_per_stylist_authority():
    """The purge is the ONE delete path that runs when `existing` is empty by construction,
    so the withhold rule computed over `future_stale` cannot fire for it. A half-seen
    stylist would otherwise be erased with no replacement written."""
    cfg = mkconfig(event_style="blocks")
    seen = [_event(stylist_id=f"s{i}", days_ahead=3, hour=9 + h)
            for i in range(20) for h in range(6)]
    sick = [_event(stylist_id="sick", days_ahead=4, hour=9 + h) for h in range(6)]
    svc = FakeCalendarService(events=_legacy_calendar(seen + sick))

    # 'sick' produced no openings this run and is not in staff_ok.
    stats = sync_calendar.sync(cfg, _result(seen, staff_ok={f"s{i}" for i in range(20)}),
                               service=svc)

    assert stats["skipped_delete"] == 6          # sick's legacy entries withheld
    assert stats["migrated"] == 120              # everyone else's retired
    survivors = [e for e in svc.live() if not e.startswith("kidav")]
    assert len(survivors) == 6                   # sick still visible to the public


def test_changeover_purge_is_sized_on_writes_that_landed():
    """Counting entries we merely INTENDED to write let a run that failed most of its
    inserts still retire everything those inserts were meant to replace."""
    cfg = mkconfig(event_style="blocks")
    events = [_event(stylist_id=f"s{i}", days_ahead=3, hour=9 + h)
              for i in range(40) for h in range(6)]
    svc = FakeCalendarService(
        events=_legacy_calendar(events),
        # Every insert fails but not consecutively enough to trip the breaker.
        fail_plan={("insert", None): [http_error(400, "Bad Request", "invalid")] * 24})

    stats = sync_calendar.sync(cfg, _result(events), service=svc)

    assert stats["failed_insert"] == 24
    assert stats["migrated"] == 0                # nothing retired without a live replacement
    assert stats["blocked_delete"] == 240


def test_unstamped_legacy_entries_stay_deletable():
    """An entry with no stylist stamp has UNKNOWN provenance, not 'not covered'. Treating
    unknown as protected would mean nothing written before the stamp existed could ever be
    pruned."""
    cfg = mkconfig()
    gone = _event(stylist_id="24102")
    item = _existing_from(gone, cfg)
    item.pop("extendedProperties")
    svc = FakeCalendarService(events=[item])

    stats = sync_calendar.sync(cfg, _result([], staff_ok={"175308"}), service=svc)
    assert stats["delete"] == 1
    assert stats["skipped_delete"] == 0


def test_an_entry_that_just_started_is_not_deleted_while_still_in_the_feed():
    """A slot that merely STARTED during the fetch is still in `desired`; deleting it would
    churn an entry the same run then re-writes, and under blocks could retire a block whose
    later openings are still bookable."""
    cfg = mkconfig()
    started = _event(days_ahead=0, hour=(datetime.now(NY).hour - 1) % 24)
    if started.start > datetime.now(NY):          # guard the midnight wrap
        started = _event(days_ahead=-1, hour=12)
    svc = FakeCalendarService(events=[_existing_from(started, cfg)])

    stats = sync_calendar.sync(cfg, _result([started]), service=svc)
    assert stats["delete"] == 0
    assert svc.methods("delete") == []


def test_changeover_is_skipped_entirely_when_the_fetch_is_untrusted():
    cfg = mkconfig(event_style="blocks")
    events = _events(60)
    svc = FakeCalendarService(events=_legacy_calendar(events))

    stats = sync_calendar.sync(cfg, _result(events, ok=False), service=svc)

    assert stats["migrated"] == 0
    assert svc.methods("delete") == []
    # sync() blocks deletes on an untrusted fetch; main() is what refuses to write at all.
    # The property here is that every legacy entry survives.
    assert len([e for e in svc.live() if not e.startswith("kidav")]) == 60


def test_changeover_proceeds_when_replacements_actually_landed():
    cfg = mkconfig(event_style="blocks")
    events = _events(80)
    svc = FakeCalendarService(events=_legacy_calendar(events))

    stats = sync_calendar.sync(cfg, _result(events), service=svc)

    assert stats["insert"] == 80
    assert stats["migrated"] == 80
    live = svc.live()
    assert len(live) == 80
    assert all(eid.startswith("kidav") for eid in live)


def test_changeover_purge_runs_after_inserts_so_an_abort_leaves_the_old_entries():
    """Deleting first meant a mid-run abort (quota, auth, a failure streak) left the
    calendar emptied with nothing written back."""
    cfg = mkconfig(event_style="blocks")
    events = _events(40)
    svc = FakeCalendarService(
        events=_legacy_calendar(events),
        fail_plan={("insert", None): [http_error(500, "Server Error", "backendError")] * 60})

    with pytest.raises(sync_calendar.WriteAborted):
        sync_calendar.sync(cfg, _result(events), service=svc)

    assert svc.methods("delete") == []        # purge never got the chance
    assert len(svc.live()) == 40              # every legacy entry still standing


def test_changeover_purge_is_window_scoped():
    """A near-tier run meeting a style flip must not purge 90 days of entries when it can
    only replace 21 days of them."""
    cfg = mkconfig(event_style="blocks", lookahead_days=21)
    near = [_event(stylist_id=f"n{i}", days_ahead=1 + i) for i in range(15)]
    far = [_event(stylist_id=f"f{i}", days_ahead=40 + i) for i in range(30)]
    svc = FakeCalendarService(events=_legacy_calendar(near + far))

    stats = sync_calendar.sync(cfg, _result(near), service=svc)

    assert stats["migrated"] == 15
    # The far-out legacy entries survive for the next deep sweep rather than vanishing.
    assert len(svc.live()) == 15 + 30 - 15 + 15
    surviving_legacy = [e for e in svc.live() if not e.startswith("kidav")]
    assert len(surviving_legacy) == 30


# --------------------------------------------------------- per-stylist delete authority
def test_a_stylist_whose_lookups_failed_is_not_deleted_off_the_calendar():
    """Global health is too coarse: the busiest stylist owns ~15% of the lookups, inside
    the 20% slack that still reports ok=True. Without per-stylist authority their entire
    schedule would be deleted as 'no longer offered'."""
    cfg = mkconfig()
    seen = _event(stylist_id="175308", stylist="Nao", days_ahead=3)
    unseen = _event(stylist_id="24102", stylist="Sachi", days_ahead=4)
    svc = FakeCalendarService(events=[_existing_from(seen, cfg), _existing_from(unseen, cfg)])

    # Sachi's lookups all failed, so the feed has only Nao — but ok is still True.
    stats = sync_calendar.sync(cfg, _result([seen], staff_ok={"175308"}), service=svc)

    assert stats["delete"] == 0
    assert stats["skipped_delete"] == 1
    assert _entry(unseen, cfg).google_event_id() in svc.live()


def test_a_covered_stylist_losing_a_slot_is_still_deleted():
    cfg = mkconfig()
    a = _event(stylist_id="175308", stylist="Nao", days_ahead=3)
    b = _event(stylist_id="175308", stylist="Nao", days_ahead=4)
    svc = FakeCalendarService(events=[_existing_from(a, cfg), _existing_from(b, cfg)])

    stats = sync_calendar.sync(cfg, _result([a], staff_ok={"175308"}), service=svc)
    assert stats["delete"] == 1
    assert _entry(b, cfg).google_event_id() in svc.tombstones()


def test_entries_carry_their_stylist_for_delete_authority():
    body = sync_calendar.entry_body(_event(stylist_id="24102"), mkconfig(), "")
    assert body["extendedProperties"]["private"]["stylist"] == "24102"
    # ...and it must not drive patch churn.
    assert not sync_calendar._needs_patch(dict(body, status="confirmed"), body)


# --------------------------------------------------------------- window arithmetic
def test_a_stale_slot_on_the_last_day_of_the_window_is_still_deleted():
    """fetch() filters by DATE while the delete horizon was a datetime, so a booked slot
    later in the day on the final day was absent from `desired` yet past the cutoff —
    permanently undeletable and permanently advertised."""
    cfg = mkconfig(lookahead_days=21)
    late = _event(days_ahead=21, hour=23)
    svc = FakeCalendarService(events=[_existing_from(late, cfg)])

    stats = sync_calendar.sync(cfg, _result([]), service=svc)
    assert stats["delete"] == 1


def test_past_events_are_pruned_without_counting_toward_the_guard():
    """Elapsed events can never be in `desired`, so counting them as 'stale' let routine
    pruning trip the blast-radius guard and freeze real deletes."""
    cfg = mkconfig()
    upcoming = _events(60)
    past = [_event(stylist_id=f"p{i}", days_ahead=-2 - i) for i in range(200)]
    svc = FakeCalendarService(
        events=[_existing_from(e, cfg) for e in upcoming + past])

    stats = sync_calendar.sync(cfg, _result(upcoming), service=svc)

    assert stats["blocked_delete"] == 0
    assert stats["delete"] == 200          # all the past ones, none of the upcoming
    assert stats["unchanged"] == 60


# --------------------------------------------------------------- depth + marker
def _marker(last_deep):
    return {"id": MARKER_ID, "summary": "checked", "status": "confirmed",
            "start": {"date": "2026-07-20"}, "end": {"date": "2026-07-21"},
            "extendedProperties": {"private": {"last_deep_sweep": last_deep.isoformat()}}}


def test_explicit_depth_does_not_consult_the_calendar():
    cfg = mkconfig()
    svc = FakeCalendarService()
    assert sync_calendar.resolve_depth(svc, "cal", cfg, "near") == (cfg.near_lookahead_days, False)
    assert sync_calendar.resolve_depth(svc, "cal", cfg, "full") == (cfg.lookahead_days, True)
    assert svc.methods("get") == []


def test_auto_depth_goes_deep_when_no_marker_exists():
    """First run against a fresh calendar: we know nothing, so do the full sweep."""
    cfg = mkconfig()
    days, deep = sync_calendar.resolve_depth(FakeCalendarService(), "cal", cfg, "auto")
    assert deep is True and days == cfg.lookahead_days


def test_auto_depth_stays_near_when_the_deep_sweep_is_recent():
    cfg = mkconfig()
    recent = datetime.now(NY) - timedelta(hours=1)
    svc = FakeCalendarService(events=[_marker(recent)])
    days, deep = sync_calendar.resolve_depth(svc, "cal", cfg, "auto")
    assert deep is False and days == cfg.near_lookahead_days


def test_auto_depth_goes_deep_once_the_last_sweep_is_stale():
    """Elapsed time, not the wall clock. The old `date -u +%H % 6` scheme assumed the cron
    fires; GitHub delivered 56% of this repo's hourly ticks and 00 UTC never fired at all,
    so the far-future window went unrefreshed for a day at a time."""
    cfg = mkconfig()
    stale = datetime.now(NY) - timedelta(hours=cfg.deep_sweep_every_hours + 1)
    svc = FakeCalendarService(events=[_marker(stale)])
    days, deep = sync_calendar.resolve_depth(svc, "cal", cfg, "auto")
    assert deep is True and days == cfg.lookahead_days


def test_auto_depth_survives_a_corrupt_marker():
    cfg = mkconfig()
    bad = {"id": MARKER_ID, "status": "confirmed", "start": {"date": "2026-07-20"},
           "end": {"date": "2026-07-21"},
           "extendedProperties": {"private": {"last_deep_sweep": "not-a-timestamp"}}}
    days, deep = sync_calendar.resolve_depth(FakeCalendarService(events=[bad]), "cal",
                                             cfg, "auto")
    assert deep is True          # unreadable state means we cannot claim it is fresh


def test_marker_is_created_then_updated_in_place():
    svc = FakeCalendarService()
    now = datetime.now(NY)
    sync_calendar.write_marker(svc, "cal", checked_at=now, entry_count=312, last_deep=now)
    assert MARKER_ID in svc.live()
    assert "312 open" in svc.live()[MARKER_ID]["summary"]

    later = now + timedelta(hours=1)
    sync_calendar.write_marker(svc, "cal", checked_at=later, entry_count=44, last_deep=now)
    assert len([e for e in svc.store if e == MARKER_ID]) == 1     # upsert, not duplicate
    assert "44 open" in svc.live()[MARKER_ID]["summary"]
    assert ("update", MARKER_ID) in svc.calls                     # 409 -> update path
    stored = svc.live()[MARKER_ID]["extendedProperties"]["private"]
    assert stored["last_deep_sweep"] == now.isoformat()           # deep time is preserved


def test_a_near_run_never_fabricates_a_deep_sweep_timestamp():
    """`last_deep or checked_at` treated 'I don't know when the last deep sweep was' as
    'it just happened', so a `--depth near` dispatch against a marker-less calendar
    suppressed the real deep sweep for deep_sweep_every_hours."""
    cfg = mkconfig()
    svc = FakeCalendarService()
    now = datetime.now(NY)
    sync_calendar.write_marker(svc, "cal", checked_at=now, entry_count=12, last_deep=None)

    private = svc.live()[MARKER_ID]["extendedProperties"]["private"]
    assert "last_run" in private
    assert "last_deep_sweep" not in private
    # ...and the next auto run therefore still goes deep.
    assert sync_calendar.resolve_depth(svc, "cal", cfg, "auto")[1] is True


def test_marker_round_trips_the_deep_sweep_timestamp():
    svc = FakeCalendarService()
    now = datetime.now(NY).replace(microsecond=0)
    sync_calendar.write_marker(svc, "cal", checked_at=now, entry_count=1, last_deep=now)
    assert sync_calendar.marker_last_deep_sweep(sync_calendar.read_marker(svc, "cal")) == now


# --------------------------------------------------------------- dry run
def test_dry_run_writes_nothing():
    cfg = mkconfig()
    events = _events(2)
    svc = FakeCalendarService(events=[_existing_from(events[0], cfg)])

    stats = sync_calendar.sync(cfg, _result([events[1]]), service=svc, dry_run=True)

    assert stats["insert"] == 1 and stats["delete"] == 1
    assert svc.methods("insert") == [] and svc.methods("delete") == []
