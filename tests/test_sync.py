"""Tests for the Google Calendar write path (src/sync_calendar.py).

This module exists because that file ran unattended in production for months with no test
coverage at all, and then spent three days failing every hour on an unhandled 409 that a
single test would have caught. The fake (tests/fakes.py) reproduces Google's real
tombstone semantics; see its docstring before changing anything here.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from conftest import mkconfig
from fakes import FakeCalendarService, frozen_datetime, http_error
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


def _window(n, days=18, day_from=1):
    """n openings that all fall inside even the 21-day window, each its own entry."""
    return [_event(stylist_id=f"w{i}", stylist=f"W{i}", days_ahead=day_from + (i % days))
            for i in range(n)]


def _result(events, ok=True, notices="", staff_ok=None):
    return FetchResult(slots=[], events=list(events), notices=notices, ok=ok,
                       lookups_ok=60 if ok else 0, lookups_failed=0 if ok else 60,
                       lookups_expected=60, staff_ok=staff_ok)


def _entry(ev, config):
    return build_entries([ev], config.event_style)[0]


def _existing_entry(entry, config, notices=""):
    """Calendar item for an ALREADY-BUILT entry (Event or Block).

    _existing_from() takes a raw opening and builds the entry itself; passing it a Block
    would re-wrap it, yielding a one-opening entry whose `openings` stamp is wrong.
    """
    return dict(sync_calendar.entry_body(entry, config, notices), status="confirmed")


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
    events = _window(200)          # 200 openings, comfortably over DELETE_BLAST_MIN
    svc = FakeCalendarService(events=[_existing_from(e, cfg) for e in events])

    # A "successful" fetch that somehow lost almost everything.
    stats = sync_calendar.sync(cfg, _result(events[:5]), service=svc)

    assert stats["delete"] == 0
    assert stats["blocked_delete"] == 195
    assert svc.methods("delete") == []


@pytest.mark.parametrize("style", ["slots", "blocks"])
def test_allow_mass_delete_overrides_the_guard(style):
    cfg = mkconfig(event_style=style)
    events = _window(200)
    svc = FakeCalendarService(events=[_existing_from(e, cfg) for e in events])

    stats = sync_calendar.sync(cfg, _result(events[:5]), service=svc,
                               allow_mass_delete=True)
    assert stats["delete"] == 195


def test_blast_radius_denominator_is_in_window_not_whole_calendar():
    """With a whole-calendar denominator, a 21-day run seeing ~350 of ~3300 events could
    delete every single one of them and still look like a ~10% change."""
    cfg = mkconfig(lookahead_days=21)
    in_window = [_event(stylist_id=f"w{i}", days_ahead=1 + (i % 18)) for i in range(200)]
    far = [_event(stylist_id=f"f{i}", days_ahead=40 + (i % 45)) for i in range(300)]
    svc = FakeCalendarService(
        events=[_existing_from(e, cfg) for e in in_window + far])

    stats = sync_calendar.sync(cfg, _result([]), service=svc)

    assert stats["delete"] == 0            # blocked: 200 of 200 in-window is a wipe
    assert stats["blocked_delete"] == 200


@pytest.mark.parametrize("style", ["slots", "blocks"])
def test_guard_allows_normal_churn(style):
    cfg = mkconfig(event_style=style)
    events = _window(200)
    svc = FakeCalendarService(events=[_existing_from(e, cfg) for e in events])

    stats = sync_calendar.sync(cfg, _result(events[10:]), service=svc)
    assert stats["delete"] == 10           # 5% churn is routine
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


def test_an_incomplete_fetch_does_not_rewrite_descriptions():
    """Timely persistently 429s two /Booking/Service POSTs. Those services then vanish from
    every affected description, and the next successful run puts them back — ~16 entries
    flip-flopping between two states every hour. A failed lookup means we do not KNOW
    whether that service is bookable, which is not the same as knowing it is not."""
    cfg = mkconfig()
    ev = _event()
    rich = dict(sync_calendar.entry_body(_entry(ev, cfg), cfg, ""), status="confirmed")
    rich["description"] += "\nHair Cut & Beard Trim (85m) — $95: 10:00 AM"
    svc = FakeCalendarService(events=[rich])

    partial = FetchResult(slots=[], events=[ev], notices="", ok=True,
                          lookups_ok=58, lookups_failed=2, lookups_expected=60)
    stats = sync_calendar.sync(cfg, partial, service=svc)

    assert stats["patch"] == 0
    assert stats["unchanged"] == 1
    assert "Beard Trim" in svc.live()[_entry(ev, cfg).google_event_id()]["description"]


def test_a_complete_fetch_does_rewrite_descriptions():
    cfg = mkconfig()
    ev = _event()
    stale = dict(sync_calendar.entry_body(_entry(ev, cfg), cfg, ""), status="confirmed")
    stale["description"] += "\nA service that is genuinely gone"
    svc = FakeCalendarService(events=[stale])

    stats = sync_calendar.sync(cfg, _result([ev]), service=svc)   # 60/60 lookups
    assert stats["patch"] == 1
    assert "genuinely gone" not in svc.live()[_entry(ev, cfg).google_event_id()]["description"]


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
    so a withhold rule computed over current-style entries cannot fire for it. A half-seen
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


def test_changeover_purge_does_not_count_replacements_it_withheld():
    """The two halves of the guard must cover the same population.

    A blind stylist's replacements are popped out of `desired` by the symmetric-withholding
    rule, so they are never inserted — and they never register as a failed write either,
    because no write was attempted. Sizing `live_now` from the full build credited the
    replacement set with openings that are not on the calendar, inflating the one guard
    standing between a partial fetch and a changeover wipe by whatever share of the salon
    the blind stylist owns.
    """
    cfg = mkconfig(event_style="blocks")
    # 10 covered stylists whose availability collapsed to 1 opening each this run...
    covered = [_event(stylist_id=f"s{i}", stylist=f"S{i}", days_ahead=3, hour=9)
               for i in range(10)]
    # ...against 60 legacy openings about to be retired.
    legacy = _legacy_calendar([_event(stylist_id=f"s{i}", stylist=f"S{i}",
                                      days_ahead=3, hour=9 + h)
                               for i in range(10) for h in range(6)])
    # A stylist we could not see completely, holding a stale CURRENT-style entry on each
    # day they have openings this run (so the withhold rule holds those days) and 30
    # openings this run (so the pop is worth 30 openings).
    sick = [_event(stylist_id="sick", stylist="Sick", days_ahead=4 + d, hour=9 + h)
            for d in range(5) for h in range(6)]
    sick_stale = [_existing_from(_event(stylist_id="sick", stylist="Sick",
                                        days_ahead=4 + d, hour=17), cfg) for d in range(5)]
    svc = FakeCalendarService(events=legacy + sick_stale)

    stats = sync_calendar.sync(
        cfg, _result(covered + sick, staff_ok={f"s{i}" for i in range(10)}), service=svc)

    # Sick is blind: their stale entry is withheld and their replacements are not written.
    assert stats["skipped_delete"] >= 1
    assert stats["insert"] == 10
    # 10 openings actually landed against 60 retiring — far under the blast-radius floor.
    # Counting sick's 30 withheld openings would have carried it over and wiped the lot.
    assert stats["migrated"] == 0
    assert stats["blocked_delete"] == 60
    # Nothing was removed: every legacy entry still stands, and so does sick's.
    assert svc.methods("delete") == []
    assert len([e for e in svc.live() if not e.startswith("kidav")]) == 60


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


# ------------------------------------------------- re-keying (blocks-specific)
def _block_day(stylist, day, hours):
    """One stylist-day's openings, which `blocks` merges into a single entry."""
    return [_event(stylist_id=stylist, stylist=stylist, days_ahead=day, hour=h)
            for h in hours]


def test_a_rekeyed_block_is_deleted_only_after_its_replacement_is_live():
    """Block ids key on the first opening, so booking that opening re-keys the whole block
    into a delete+insert. Deleting first removes still-bookable availability."""
    cfg = mkconfig(event_style="blocks")
    before = _block_day("nao", 3, [9, 10, 11, 12])
    after = _block_day("nao", 3, [10, 11, 12])        # the 9:00 got booked
    svc = FakeCalendarService(events=[_existing_entry(e, cfg)
                                      for e in build_entries(before, "blocks")])

    sync_calendar.sync(cfg, _result(after, staff_ok={"nao"}), service=svc)

    writes = [m for m, _ in svc.calls if m in ("delete", "insert")]
    assert writes.index("insert") < writes.index("delete"), \
        "the replacement block must be live before the superseded one is removed"
    live = svc.live()
    assert len(live) == 1
    assert "3 open" in next(iter(live.values()))["summary"]


def test_a_failed_replacement_keeps_the_old_block_rather_than_going_dark():
    """The harm this ordering prevents: delete lands, insert fails, and the stylist's whole
    day is advertised nowhere while the run still exits 0."""
    cfg = mkconfig(event_style="blocks")
    before = _block_day("nao", 3, [9, 10, 11, 12])
    after = _block_day("nao", 3, [10, 11, 12])
    new_id = build_entries(after, "blocks")[0].google_event_id()
    svc = FakeCalendarService(
        events=[_existing_entry(e, cfg) for e in build_entries(before, "blocks")],
        fail_plan={("insert", new_id): [http_error(500, "Server Error", "backendError")]})

    stats = sync_calendar.sync(cfg, _result(after, staff_ok={"nao"}), service=svc)

    assert stats["failed_insert"] == 1
    assert stats["skipped_delete"] == 1
    assert len(svc.live()) == 1, "the day must still be represented by SOMETHING"


def test_a_rekey_left_standing_is_retired_once_its_replacement_is_unchanged():
    """A re-key outlives its run when its own delete fails, or when the blast guard refuses
    the delete pass. Next run its replacement is already on the calendar and needs no
    write, so it can only count as landed through pass 2's unchanged branch — without
    that, the old block kept advertising the booked 9:00 on every later run."""
    cfg = mkconfig(event_style="blocks")
    (old,) = build_entries(_block_day("nao", 3, [9, 10, 11, 12]), "blocks")
    after = _block_day("nao", 3, [10, 11, 12])
    (new,) = build_entries(after, "blocks")
    svc = FakeCalendarService(
        events=[_existing_entry(old, cfg)],
        fail_plan={("delete", old.google_event_id()): [
            http_error(500, "Server Error", "backendError")]})

    first = sync_calendar.sync(cfg, _result(after, staff_ok={"nao"}), service=svc)
    assert (first["insert"], first["failed_delete"]) == (1, 1)
    assert set(svc.live()) == {old.google_event_id(), new.google_event_id()}

    second = sync_calendar.sync(cfg, _result(after, staff_ok={"nao"}), service=svc)

    assert (second["unchanged"], second["delete"], second["skipped_delete"]) == (1, 1, 0)
    assert set(svc.live()) == {new.google_event_id()}


def test_a_rekey_whose_replacement_is_revived_from_a_tombstone_is_retired():
    """The booked-then-cancelled-then-booked-again lifecycle: the 10-12 block was live
    once, was deleted when the 9:00 freed up, and is wanted again now the 9:00 is taken.
    Its id is a tombstone, so it lands through the 409 revive — and that must count as
    landed, or the old 9-12 block is kept whole on every run."""
    cfg = mkconfig(event_style="blocks")
    (old,) = build_entries(_block_day("nao", 3, [9, 10, 11, 12]), "blocks")
    after = _block_day("nao", 3, [10, 11, 12])
    (new,) = build_entries(after, "blocks")
    svc = FakeCalendarService(events=[_existing_entry(old, cfg),
                                      dict(_existing_entry(new, cfg), status="cancelled")])

    stats = sync_calendar.sync(cfg, _result(after, staff_ok={"nao"}), service=svc)

    assert (stats["revive"], stats["delete"], stats["skipped_delete"]) == (1, 1, 0)
    assert set(svc.live()) == {new.google_event_id()}


def test_a_genuine_booking_is_still_deleted_first():
    """The re-key deferral must not blunt the urgent case: a stylist-day that has left the
    feed entirely is an advertised appointment that is already taken."""
    cfg = mkconfig(event_style="blocks")
    gone = _block_day("sachi", 4, [9, 10])
    other = _block_day("nao", 5, [9, 10])
    svc = FakeCalendarService(events=[_existing_entry(e, cfg)
                                      for e in build_entries(gone + other, "blocks")])

    sync_calendar.sync(cfg, _result(other, staff_ok={"nao", "sachi"}), service=svc)

    writes = [m for m, _ in svc.calls if m in ("delete", "insert")]
    assert writes and writes[0] == "delete"


def test_withholding_is_symmetric_so_a_day_is_never_double_published():
    """Suppressing a stylist's deletes while still inserting their new entries publishes
    two overlapping blocks for the same day, the older still advertising a booked slot."""
    cfg = mkconfig(event_style="blocks")
    before = _block_day("sick", 3, [9, 10, 11, 12])
    after = _block_day("sick", 3, [10, 11, 12])
    svc = FakeCalendarService(events=[_existing_entry(e, cfg)
                                      for e in build_entries(before, "blocks")])

    # 'sick' is not in staff_ok: we cannot see them completely this run.
    stats = sync_calendar.sync(cfg, _result(after, staff_ok={"nao"}), service=svc)

    assert len(svc.live()) == 1, "must not publish two overlapping blocks for one day"
    assert stats["insert"] == 0 and stats["patch"] == 0
    assert stats["skipped_delete"] >= 1


def test_blast_floor_is_measured_in_openings_not_entries():
    """A window of few block ENTRIES can still be a lot of availability. An entry-based
    floor let a small-entry-count calendar be wiped with the guard silent."""
    cfg = mkconfig(event_style="blocks")
    # 18 stylist-days x 10 openings = 180 openings in only 18 entries.
    evs = [e for i in range(18) for e in _block_day(f"s{i}", 1 + i, range(9, 19))]
    entries = build_entries(evs, "blocks")
    assert len(entries) < sync_calendar.DELETE_BLAST_MIN      # few entries...
    assert sum(e.opening_count for e in entries) >= sync_calendar.DELETE_BLAST_MIN
    svc = FakeCalendarService(events=[_existing_entry(e, cfg) for e in entries])

    stats = sync_calendar.sync(cfg, _result([]), service=svc)

    assert stats["delete"] == 0                              # ...but the guard still fires
    assert stats["blocked_delete"] == len(entries)


# --------------------------------------------------------------- depth + marker
def _marker(last_deep):
    return {"id": MARKER_ID, "summary": "checked", "status": "confirmed",
            "start": {"date": "2026-07-20"}, "end": {"date": "2026-07-21"},
            "extendedProperties": {"private": {"last_deep_sweep": last_deep.isoformat()}}}


def test_explicit_depth_ignores_the_marker():
    cfg = mkconfig()
    fresh = _marker(datetime.now(NY))         # would mean "stay near" under auto
    assert sync_calendar.resolve_depth(cfg, "near", None) == (cfg.near_lookahead_days, False)
    assert sync_calendar.resolve_depth(cfg, "full", fresh) == (cfg.lookahead_days, True)


def test_auto_depth_goes_deep_when_no_marker_exists():
    """First run against a fresh calendar: we know nothing, so do the full sweep."""
    cfg = mkconfig()
    days, deep = sync_calendar.resolve_depth(cfg, "auto", None)
    assert deep is True and days == cfg.lookahead_days


def test_auto_depth_stays_near_when_the_deep_sweep_is_recent():
    cfg = mkconfig()
    recent = datetime.now(NY) - timedelta(hours=1)
    days, deep = sync_calendar.resolve_depth(cfg, "auto", _marker(recent))
    assert deep is False and days == cfg.near_lookahead_days


def test_auto_depth_goes_deep_once_the_last_sweep_is_stale():
    """Elapsed time, not the wall clock. The old `date -u +%H % 6` scheme assumed the cron
    fires; GitHub delivered 56% of this repo's hourly ticks and 00 UTC never fired at all,
    so the far-future window went unrefreshed for a day at a time."""
    cfg = mkconfig()
    stale = datetime.now(NY) - timedelta(hours=cfg.deep_sweep_every_hours + 1)
    days, deep = sync_calendar.resolve_depth(cfg, "auto", _marker(stale))
    assert deep is True and days == cfg.lookahead_days


def test_auto_depth_survives_a_corrupt_marker():
    cfg = mkconfig()
    bad = {"id": MARKER_ID, "status": "confirmed", "start": {"date": "2026-07-20"},
           "end": {"date": "2026-07-21"},
           "extendedProperties": {"private": {"last_deep_sweep": "not-a-timestamp"}}}
    days, deep = sync_calendar.resolve_depth(cfg, "auto", bad)
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
    assert sync_calendar.resolve_depth(cfg, "auto", sync_calendar.read_marker(svc, "cal"))[1]


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


# ------------------------------------------- delete authority & ordering, on a frozen clock
# These pin "now" because the defects they cover are about WHEN: an entry under way at
# 11:30, a sync that crosses midnight after its fetch. Patching the module's `datetime`
# freezes every clock read in it, so each scenario is exactly the one the review traced.
T0 = datetime(2026, 10, 14, 11, 30, tzinfo=NY)      # a Wednesday, mid-morning


@pytest.fixture
def clock(monkeypatch):
    def freeze(at=T0):
        monkeypatch.setattr(sync_calendar, "datetime", frozen_datetime(at))
        return at
    return freeze


def _at(day, hour, stylist="nao", base=T0, minute=0):
    """One 55-minute opening `day` days after `base`, at `hour`:`minute` salon time."""
    start = (base.replace(hour=hour, minute=minute, second=0, microsecond=0)
             + timedelta(days=day))
    return Event(stylist_id=stylist, stylist=stylist.title(), stylist_role="Stylist",
                 start=start, book_url="https://book",
                 options=[ServiceOption("Hair Cut", "$75", 55, False)])


def _res(events, *, staff_ok=None, staff_planned=None, window_start=T0.date(),
         lookups_failed=0):
    return FetchResult(slots=[], events=list(events), notices="", ok=True,
                       lookups_ok=60 - lookups_failed, lookups_failed=lookups_failed,
                       lookups_expected=60, staff_ok=staff_ok,
                       staff_planned=staff_planned, window_start=window_start)


def _cal(cfg, *groups):
    """A calendar holding one entry per group of openings (a group must merge into one
    entry under cfg.event_style), plus the ids of those entries in the same order."""
    entries = [build_entries(g, cfg.event_style) for g in groups]
    assert all(len(e) == 1 for e in entries), "each group must be exactly one entry"
    ids = [e[0].google_event_id() for e in entries]
    return FakeCalendarService(events=[_existing_entry(e[0], cfg) for e in entries]), ids


def _block_id(openings):
    (entry,) = build_entries(openings, "blocks")
    return entry.google_event_id()


def test_an_under_way_block_of_a_half_seen_stylist_is_left_standing(clock, capsys):
    """Defect A. At 11:30 Nao's 10:00-14:55 block still offers 12:00 onward. One of her
    lookups failed and she has a held future entry, so her writes were withheld — but the
    started block skipped the authority check, was classed a re-key, and was deleted
    because its never-attempted replacement was not in failed_ids. Nao went dark for the
    day. A stylist we cannot see completely is left as they are, bar what has ended."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    under_way = [_at(0, h) for h in (10, 11, 12, 13, 14)]
    later = [_at(3, 9)]                       # gone from this partial feed: held
    ended = [_at(-1, 10)]                     # yesterday's: history
    svc, (block, later_id, ended_id) = _cal(cfg, under_way, later, ended)

    stats = sync_calendar.sync(
        cfg, _res([_at(0, h) for h in (12, 13, 14)] + [_at(2, 9, "sachi")],
                  staff_ok={"sachi"}, staff_planned={"nao", "sachi"}, lookups_failed=1),
        service=svc)

    assert block in svc.live(), "Nao's still-bookable afternoon must stay advertised"
    assert later_id in svc.live()
    assert stats["skipped_delete"] == 2
    # History is history whoever it belongs to: nothing a lookup sees can reopen it.
    assert svc.methods("delete") == [ended_id]
    # ...and nothing of Nao's was written, so her day is not double-published either.
    assert svc.methods("insert") == [_block_id([_at(2, 9, "sachi")])]
    assert "withholding 2 delete(s) and 1 write(s)" in capsys.readouterr().out


def test_an_under_way_block_is_retired_only_after_its_replacement_lands(clock):
    """Rule 2: an entry that has started but not ended is handled like a future one, so a
    covered stylist's under-way block is a re-key — its replacement goes in first."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    svc, (block,) = _cal(cfg, [_at(0, h) for h in (10, 11, 12, 13, 14)])
    after = [_at(0, h) for h in (12, 13, 14)]

    sync_calendar.sync(cfg, _res(after, staff_ok={"nao"}, staff_planned={"nao"}),
                       service=svc)

    assert [m for m, _ in svc.calls if m in ("delete", "insert")] == ["insert", "delete"]
    assert set(svc.live()) == {_block_id(after)}


def test_an_under_way_block_survives_a_failed_replacement(clock):
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    svc, (block,) = _cal(cfg, [_at(0, h) for h in (10, 11, 12, 13, 14)])
    after = [_at(0, h) for h in (12, 13, 14)]
    svc.fail_plan[("insert", _block_id(after))] = [
        http_error(500, "Server Error", "backendError")]

    stats = sync_calendar.sync(cfg, _res(after, staff_ok={"nao"}, staff_planned={"nao"}),
                               service=svc)

    assert block in svc.live()
    assert stats["skipped_delete"] == 1


def test_a_rekey_is_matched_to_the_block_it_overlaps_not_the_first_of_the_day(clock):
    """Defect B. With a morning and an evening block, a re-keyed evening block was judged
    by next(...) — the FIRST desired entry of that stylist-day, i.e. the untouched morning
    block. The evening re-key insert 500'd, the morning block was 'live', and the old
    evening block was deleted: 18:00 and 19:00 advertised nowhere."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    morning = [_at(3, h) for h in (9, 10)]
    evening = [_at(3, h) for h in (17, 18, 19)]
    evening_after = [_at(3, h) for h in (18, 19)]          # 17:00 got booked
    svc, (_, old_evening) = _cal(cfg, morning, evening)
    svc.fail_plan[("insert", _block_id(evening_after))] = [
        http_error(500, "Server Error", "backendError")]

    stats = sync_calendar.sync(cfg, _res(morning + evening_after, staff_ok={"nao"},
                                         staff_planned={"nao"}), service=svc)

    assert old_evening in svc.live()
    assert stats["skipped_delete"] == 1
    assert svc.methods("delete") == []


def test_a_rekey_waits_for_every_overlapping_replacement(clock):
    """Rule 3. A block that splits in two is replaced by BOTH halves; one landing is not
    enough, or the other half's openings are advertised nowhere."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    svc, (old,) = _cal(cfg, [_at(3, h) for h in (9, 10, 11, 12, 13)])
    first, second = [_at(3, 10)], [_at(3, h) for h in (12, 13)]   # 9:00 and 11:00 booked
    svc.fail_plan[("insert", _block_id(second))] = [
        http_error(500, "Server Error", "backendError")]

    stats = sync_calendar.sync(cfg, _res(first + second, staff_ok={"nao"},
                                         staff_planned={"nao"}), service=svc)

    assert _block_id(first) in svc.live()
    assert old in svc.live(), "12:00 and 13:00 must still be advertised somewhere"
    assert stats["skipped_delete"] == 1


def test_a_rekey_whose_replacements_all_landed_is_deleted(clock):
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    svc, (old,) = _cal(cfg, [_at(3, h) for h in (9, 10, 11, 12, 13)])
    first, second = [_at(3, 10)], [_at(3, h) for h in (12, 13)]

    stats = sync_calendar.sync(cfg, _res(first + second, staff_ok={"nao"},
                                         staff_planned={"nao"}), service=svc)

    assert set(svc.live()) == {_block_id(first), _block_id(second)}
    assert stats["skipped_delete"] == 0


def test_a_half_seen_stylist_is_not_patched_down_to_partial_data(clock):
    """Defect C. A block keeps its id while its first opening stands, so a stylist whose
    lookups failed but whose 9:00 survived produced the SAME id with fewer openings. With
    no stale entry nothing was 'held', the stylist was not 'blind', and the block was
    patched down to 2 open — then back up to 4 on the next clean run: the flip-flop that
    9999859 was meant to stop, reintroduced through the patch path."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    svc, (block,) = _cal(cfg, [_at(3, h, "sick") for h in (9, 10, 11, 12)])
    partial = [_at(3, h, "sick") for h in (9, 10)]
    assert _block_id(partial) == block

    stats = sync_calendar.sync(
        cfg, _res(partial + [_at(4, 9)], staff_ok={"nao"}, staff_planned={"nao", "sick"},
                  lookups_failed=1),
        service=svc)

    assert svc.methods("patch") == []
    assert stats["patch"] == 0
    assert "4 open" in svc.live()[block]["summary"]


def test_a_half_seen_stylists_day_with_anything_to_retire_or_rewrite_gets_no_write(
        clock, capsys):
    """Rule 1 covers inserts too, a day at a time: a day of theirs holding anything this
    run would delete, shrink or rewrite gets no write at all — even one that overlaps none
    of it, since the partial view of that day is exactly what we cannot trust."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    held = [_at(3, 9, "sick")]                               # gone from the feed: held
    growing = [_at(5, h, "sick") for h in (9, 10)]           # would grow: still a rewrite
    svc, (held_id, growing_id) = _cal(cfg, held, growing)
    fresh = [_at(4, 9, "sick")]                              # a day with nothing held
    feed = ([_at(3, 15, "sick")]                             # the held entry's day
            + [_at(5, h, "sick") for h in (9, 10, 11)] + [_at(5, 16, "sick")] + fresh)

    stats = sync_calendar.sync(cfg, _res(feed, staff_ok={"nao"},
                                         staff_planned={"nao", "sick"}, lookups_failed=1),
                               service=svc)

    assert svc.methods("insert") == [_block_id(fresh)]
    assert svc.methods("patch") == [] and svc.methods("delete") == []
    assert held_id in svc.live()
    assert svc.live()[growing_id]["summary"] == "Sick · 2 open"
    assert stats["skipped_delete"] == 1
    assert ("leaving 2 day(s) of theirs as they are: withholding 1 delete(s) and 3 "
            "write(s)") in capsys.readouterr().out


def test_a_half_seen_stylists_day_with_nothing_held_is_published(clock):
    """...and only a day at a time. Withholding the stylist's whole window kept every
    reopened slot, and on a deep sweep the whole day that had just entered the window, off
    the calendar for as long as one of their lookups kept failing (Timely 429s the same
    /Booking/Service POSTs run after run), though nothing of theirs was on that day for it
    to overlap. What the partial view found there is published: at worst it under-lists
    the failed service until a complete run grows it."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    svc = FakeCalendarService()

    stats = sync_calendar.sync(
        cfg, _res([_at(3, 9, "sick"), _at(4, 9)], staff_ok={"nao"},
                  staff_planned={"nao", "sick"}, lookups_failed=1), service=svc)

    assert sorted(svc.methods("insert")) == sorted([_block_id([_at(3, 9, "sick")]),
                                                    _block_id([_at(4, 9)])])
    assert stats["insert"] == 2 and stats["skipped_delete"] == 0


def test_a_stylist_who_was_never_looked_for_is_deleted_not_held(clock):
    """Defect D. staff_ok only ever names stylists the fetch planned lookups for, so one
    who left the salon, was dropped from Timely's catalog or is excluded by config.stylists
    was indistinguishable from one whose lookups failed. Their entries were held for the
    up-to-90 days it took them to age out, and with more than 10 of them every hourly run
    exited 1. Nothing was looked for, so nothing failed: they are ordinary stale entries."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    gone = [[_at(2 + i, 10, "left")] for i in range(3)]
    svc, ids = _cal(cfg, *gone, [_at(2, 14)])

    stats = sync_calendar.sync(cfg, _res([_at(2, 14)], staff_ok={"nao"},
                                         staff_planned={"nao"}), service=svc)

    assert stats["delete"] == 3
    assert stats["skipped_delete"] == 0
    assert set(svc.live()) == {ids[-1]}


def test_a_stylist_left_out_of_the_plan_is_named_as_their_entries_go(clock, capsys):
    """...and the run says whose. Deleting them is right for someone who left or is hidden
    from online booking, and looks exactly the same as a service catalog that parsed with a
    whole category missing (Hair Salon is the only home of three stylists): a third of the
    calendar gone at exit 0, with nothing in the log beyond a delete count."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    hiroki = "686999"
    svc, ids = _cal(cfg, *[[_at(2 + i, 10, hiroki)] for i in range(3)], [_at(-1, 10, hiroki)],
                    [_at(2, 14)])
    feed = _res([_at(2, 14)], staff_ok={"nao"}, staff_planned={"nao"})

    stats = sync_calendar.sync(cfg, feed, service=svc)

    assert stats["delete"] == 4 and set(svc.live()) == {ids[-1]}
    assert ("1 stylist(s) with current entries were not in this run's plan (Hiroki: 3 "
            "entr(ies), 3 opening(s))") in capsys.readouterr().out
    sync_calendar.sync(cfg, feed, service=svc)          # nothing of theirs left to name
    assert "not in this run's plan" not in capsys.readouterr().out


def test_without_a_recorded_plan_absence_from_staff_ok_still_holds(clock):
    """A capture from before staff_planned existed cannot tell 'not looked for' from
    'looked for and failed', so it keeps the old, stricter rule."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    svc, ids = _cal(cfg, *[[_at(2 + i, 10, "left")] for i in range(3)], [_at(2, 14)])

    stats = sync_calendar.sync(cfg, _res([_at(2, 14)], staff_ok={"nao"},
                                         staff_planned=None), service=svc)

    assert stats["delete"] == 0
    assert stats["skipped_delete"] == 3


def test_a_departed_stylists_entries_still_answer_to_the_blast_radius_guard(clock):
    """Covered-by-absence grants delete authority, not an exemption from the global guard:
    a plan that somehow lost most of the salon must still be refused, not executed."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    gone = [[_at(1 + (i % 18), 10, f"left{i}")] for i in range(200)]
    kept = [[_at(1 + i, 14)] for i in range(10)]
    svc, _ = _cal(cfg, *gone, *kept)

    stats = sync_calendar.sync(cfg, _res([g[0] for g in kept], staff_ok={"nao"},
                                         staff_planned={"nao"}), service=svc)

    assert stats["delete"] == 0
    assert stats["blocked_delete"] == 200


def test_the_window_is_the_fetchs_not_the_clock_at_sync_time(clock):
    """Defect E. A near run whose fetch starts at 23:50 and whose sync runs at 00:10 used
    to recompute the horizon from the sync's clock — one day further than the fetch looked
    — and delete that whole unfetched day as 'no longer offered'."""
    after_midnight = clock(datetime(2026, 10, 15, 0, 10, tzinfo=NY))
    fetched_from = date(2026, 10, 14)                     # the fetch's "today"
    cfg = mkconfig(event_style="blocks", lookahead_days=21)
    base = after_midnight - timedelta(days=1)             # day 0 == fetched_from
    unfetched = [_at(22, 10, "sachi", base=base)]         # the day after the window
    last = [_at(21, 10, "sachi", base=base)]              # last fetched day; now booked
    keep = [_at(6, 10, base=base)]
    svc, (unfetched_id, last_id, _) = _cal(cfg, unfetched, last, keep)

    stats = sync_calendar.sync(cfg, _res(keep, window_start=fetched_from), service=svc)

    assert unfetched_id in svc.live(), "a day the fetch never looked at is not stale"
    assert last_id in svc.tombstones(), "the last day it DID look at is still pruned"
    assert stats["delete"] == 1


def test_an_entry_at_the_midnight_after_the_window_is_outside_it(clock):
    """The horizon is the first instant of the first UNFETCHED day; `<=` let an entry
    starting exactly then count as in-window and be deleted unseen."""
    clock(T0)
    cfg = mkconfig(event_style="slots", lookahead_days=21)
    midnight = datetime.combine(T0.date() + timedelta(days=22), datetime.min.time(),
                                tzinfo=NY)
    svc, (eid,) = _cal(cfg, [_at(0, 0, "sachi", base=midnight)])

    stats = sync_calendar.sync(cfg, _res([_at(2, 9)]), service=svc)

    assert eid in svc.live()
    assert stats["delete"] == 0


def test_a_result_without_window_start_falls_back_to_today_and_says_so(clock, capsys):
    clock(T0)
    cfg = mkconfig(event_style="blocks", lookahead_days=21)
    svc, (late,) = _cal(cfg, [_at(21, 22, "sachi")])

    stats = sync_calendar.sync(cfg, _res([_at(2, 9)], window_start=None), service=svc)

    assert stats["delete"] == 1                           # today + 21 is still in window
    out = capsys.readouterr().out
    assert "::warning::" in out and "window_start" in out


def test_slots_style_deletes_a_booked_slot_before_inserting(clock):
    """Rule 4. Classifying by stylist-DAY made every booked slot of a stylist with any
    other opening that day a 're-key', deferring its delete behind the whole insert pass.
    A re-key is an entry something in the feed actually overlaps; a 9:00 slot nothing
    overlaps is simply booked."""
    clock(T0)
    cfg = mkconfig(event_style="slots")
    booked, still, new = _at(3, 9), _at(3, 14), _at(3, 16)
    svc, (booked_id, _) = _cal(cfg, [booked], [still])

    sync_calendar.sync(cfg, _res([still, new], staff_ok={"nao"}, staff_planned={"nao"}),
                       service=svc)

    writes = [(m, e) for m, e in svc.calls if m in ("delete", "insert")]
    assert writes == [("delete", booked_id), ("insert", new.google_event_id())]


def test_overlap_treats_a_missing_end_as_the_start_instant():
    t = T0
    h = timedelta(hours=1)
    assert sync_calendar._overlaps(t, t + h, t + h / 2, t + 2 * h)
    assert not sync_calendar._overlaps(t, t + h, t + h, t + 2 * h)      # half-open
    assert sync_calendar._overlaps(t, t, t, t + h)                      # zero-length
    assert not sync_calendar._overlaps(t, t, t - h, t)


def test_legacy_item_without_an_end_is_history_once_it_starts(clock):
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    svc, (eid,) = _cal(cfg, [_at(0, 10, "sick")])          # started 10:00, now 11:30
    svc.store[eid].pop("end")

    stats = sync_calendar.sync(cfg, _res([_at(2, 9)], staff_ok={"nao"},
                                         staff_planned={"nao", "sick"}), service=svc)

    assert eid in svc.tombstones()                         # history, so even 'sick' loses it
    assert stats["skipped_delete"] == 0


def test_live_openings_counts_everything_left_on_the_calendar(clock):
    """Defect F. The marker reported only this run's window — 21 of 90 days on a near
    run — so '· N open' understated availability several-fold and jumped at every deep
    sweep. It must count what a subscriber can actually see once the run is done."""
    clock(T0)
    cfg = mkconfig(event_style="blocks", lookahead_days=21)
    far = [_at(40, h, "sachi") for h in range(9, 15)]           # 6, out of window
    same = [_at(2, h) for h in (9, 10)]                          # 2, unchanged
    sick = [_at(5, h, "sick") for h in (9, 10, 11)]              # 3, withheld
    booked = [_at(6, 9, "sachi")]                                # deleted
    ended = [_at(-1, 9, "sachi")]                                # history, deleted
    svc, _ = _cal(cfg, far, same, sick, booked, ended)
    fresh = [_at(3, h) for h in (9, 10, 11, 12)]                 # 4, inserted
    failing = [_at(4, h, "sachi") for h in range(9, 14)]         # 5, insert fails
    svc.fail_plan[("insert", _block_id(failing))] = [
        http_error(500, "Server Error", "backendError")]

    stats = sync_calendar.sync(
        cfg, _res(same + fresh + failing + [_at(5, 9, "sick")],
                  staff_ok={"nao", "sachi"}, staff_planned={"nao", "sachi", "sick"}),
        service=svc)

    assert stats["delete"] == 2 and stats["failed_insert"] == 1
    assert stats["live_openings"] == 6 + 2 + 3 + 4


def test_dry_run_without_a_calendar_reports_what_it_would_publish():
    stats = sync_calendar.sync(mkconfig(event_style="blocks"),
                               _res([_at(2, h) for h in (9, 10, 11)]), dry_run=True)
    assert stats["insert"] == 1 and stats["live_openings"] == 3


def test_live_openings_does_not_count_a_kept_rekey_and_its_replacement_twice(clock):
    """A kept re-key and whichever of its replacements DID land advertise the same
    openings. Counting both at full size put 6 on the marker for a calendar showing 5
    distinct times (9-13 on the old block; 10:00 again on its landed replacement)."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    svc, (old,) = _cal(cfg, [_at(3, h) for h in (9, 10, 11, 12, 13)])
    first, second = [_at(3, 10)], [_at(3, h) for h in (12, 13)]   # 9:00 and 11:00 booked
    svc.fail_plan[("insert", _block_id(second))] = [
        http_error(500, "Server Error", "backendError")]

    stats = sync_calendar.sync(cfg, _res(first + second, staff_ok={"nao"},
                                         staff_planned={"nao"}), service=svc)

    assert set(svc.live()) == {old, _block_id(first)}
    assert stats["live_openings"] == 5


def test_live_openings_does_not_double_count_a_blocked_run(clock):
    """The same overlap on every re-keyed stylist-day of a run whose deletes the
    blast-radius guard refused: each replacement landed beside the block it replaces."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    days = [[_at(1 + (i % 18), h, f"s{i}") for h in range(9, 14)] for i in range(40)]
    svc, _ = _cal(cfg, *days)
    rekeyed = [o for d in days for o in d[1:]]           # every first opening booked

    stats = sync_calendar.sync(cfg, _res(rekeyed), service=svc)

    assert stats["blocked_delete"] == 40
    assert len(svc.live()) == 80                         # both generations are up...
    assert stats["live_openings"] == 40 * 5              # ...advertising 200 openings


def test_a_refused_delete_pass_shrinks_no_block_either(clock):
    """The guard refused the deletes but not the patches, so a feed showing only part of
    the salon's days still patched every block that kept its id down to the part it saw
    — the loss the refusal was there to stop, landed through the patch path. Retiring part
    of a block is a delete of that part, refused with the rest."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    days = [[_at(1 + (i % 18), h, f"s{i}") for h in range(9, 14)] for i in range(40)]
    svc, ids = _cal(cfg, *days)
    lost_tail = days[0][:3]                              # 12:00 and 13:00 gone: pass 2
    head, tail = days[1][:2], days[1][3:]                # 11:00 gone: a narrowing patch
    # 21 of the 40 entries gone entirely: over half, so the delete pass is refused.
    feed = lost_tail + head + tail + [o for d in days[2:19] for o in d]

    stats = sync_calendar.sync(cfg, _res(feed), service=svc)

    assert stats["delete"] == 0 and svc.methods("patch") == []
    assert all(svc.live()[i]["summary"].endswith("· 5 open") for i in ids)
    assert stats["blocked_delete"] == 21 + 2


# ------------------------------------------- a split block: the head shrinks, the tail is new
# A block keeps its id while its first opening stands, so a booking in the MIDDLE of one
# does not re-key it: the head is patched smaller under the old id and the tail is a new
# entry. The old block is never stale, so the re-key rule cannot see this shape.
def _split(cfg):
    whole = [_at(3, h) for h in (9, 10, 11, 12, 13)]
    head, tail = [_at(3, h) for h in (9, 10)], [_at(3, h) for h in (12, 13)]  # 11:00 booked
    svc, (block,) = _cal(cfg, whole)
    assert _block_id(head) == block
    return svc, block, head, tail


def test_a_split_blocks_head_is_not_shrunk_before_its_tail_lands(clock):
    """Patching the head in pass 2 and inserting the tail in pass 3 meant one 500 on the
    tail left 12:00 and 13:00 advertised nowhere — and a single failure is under the exit
    floor, so the run went green."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    svc, block, head, tail = _split(cfg)
    svc.fail_plan[("insert", _block_id(tail))] = [
        http_error(500, "Server Error", "backendError")]

    stats = sync_calendar.sync(cfg, _res(head + tail, staff_ok={"nao"},
                                         staff_planned={"nao"}), service=svc)

    assert "5 open" in svc.live()[block]["summary"], "12:00 and 13:00 must stay advertised"
    assert svc.methods("patch") == []
    assert stats["patch"] == 0 and stats["skipped_delete"] == 1


def test_a_quota_abort_never_leaves_a_split_blocks_head_shrunk(clock):
    """A quota 403 aborts the run at the first insert. Every head shrunk before that was
    left without its tail until Google's quota reset — the opposite of 'a run that aborts
    part-way leaves the old entries standing'."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    svc, block, head, tail = _split(cfg)
    svc.fail_plan[("insert", None)] = [http_error(403, "Quota Exceeded", "quotaExceeded")]

    with pytest.raises(sync_calendar.WriteAborted):
        sync_calendar.sync(cfg, _res(head + tail, staff_ok={"nao"}, staff_planned={"nao"}),
                           service=svc)

    assert svc.methods("patch") == []
    assert "5 open" in svc.live()[block]["summary"]


def test_a_split_blocks_head_is_shrunk_once_its_tail_is_live(clock):
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    svc, block, head, tail = _split(cfg)

    stats = sync_calendar.sync(cfg, _res(head + tail, staff_ok={"nao"},
                                         staff_planned={"nao"}), service=svc)

    writes = [(m, e) for m, e in svc.calls if m in ("patch", "insert", "delete")]
    assert writes == [("insert", _block_id(tail)), ("patch", block)]
    assert "2 open" in svc.live()[block]["summary"]
    assert stats["skipped_delete"] == 0 and stats["live_openings"] == 4


def test_an_abort_after_a_split_blocks_tail_landed_does_not_publish_its_day_twice(clock):
    """The head's narrowing patch waited behind EVERY insert, so a quota 403 on a later,
    unrelated insert left the tail live beside a head still stored wide: one stylist-day
    published twice, the booked 11:00 still advertised, until the next run reached it. The
    head is narrowed the moment its tail lands."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    svc, block, head, tail = _split(cfg)
    later = [_at(4, 9, "sachi")]                         # inserted after the tail
    svc.fail_plan[("insert", _block_id(later))] = [
        http_error(403, "Quota Exceeded", "quotaExceeded")]

    with pytest.raises(sync_calendar.WriteAborted):
        sync_calendar.sync(cfg, _res(head + tail + later, staff_ok={"nao", "sachi"},
                                     staff_planned={"nao", "sachi"}), service=svc)

    assert set(svc.live()) == {block, _block_id(tail)}
    assert svc.live()[block]["summary"] == "Nao · 2 open"


def test_a_block_that_only_lost_its_last_openings_is_patched_before_the_inserts(clock):
    """Nothing replaces openings booked off the END of a block, so that shrink has nothing
    to wait for and keeps its place ahead of the inserts."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    svc, (block,) = _cal(cfg, [_at(3, h) for h in (9, 10, 11, 12)])
    new = [_at(4, 9, "sachi")]

    sync_calendar.sync(cfg, _res([_at(3, 9), _at(3, 10)] + new, staff_ok={"nao", "sachi"},
                                 staff_planned={"nao", "sachi"}), service=svc)

    writes = [(m, e) for m, e in svc.calls if m in ("patch", "insert")]
    assert writes == [("patch", block), ("insert", _block_id(new))]


# ------------------------------------------- the changeover purge, entry by entry
def test_changeover_keeps_an_obsolete_entry_whose_own_replacement_failed(clock):
    """The purge's aggregate guard is sized over the whole calendar, so one stylist's
    failed block was covered by everyone else's: Nao's 6 landed openings cleared the bar
    for all 8 retiring, and Sachi's 2 still-bookable slots were deleted with nothing in
    their place. An obsolete entry overlapped by the feed is a re-key across styles, and
    waits for every overlapping replacement exactly as one in pass 4 does."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    slots = mkconfig(event_style="slots")
    nao = [_at(3, h) for h in range(9, 15)]
    sachi = [_at(3, h, "sachi") for h in (9, 10)]
    booked = _at(3, 16)                                  # nothing overlaps it: retired
    svc = FakeCalendarService(events=[_existing_from(e, slots)
                                      for e in nao + sachi + [booked]])
    svc.fail_plan[("insert", _block_id(sachi))] = [
        http_error(500, "Server Error", "backendError")]

    stats = sync_calendar.sync(cfg, _res(nao + sachi, staff_ok={"nao", "sachi"},
                                         staff_planned={"nao", "sachi"}), service=svc)

    assert {e.google_event_id() for e in sachi} <= set(svc.live())
    assert stats["migrated"] == 6 + 1
    assert stats["skipped_delete"] == 2
    assert booked.google_event_id() in svc.tombstones()


def test_changeover_purges_an_uncovered_stylists_ended_entries_but_holds_the_rest(clock):
    """History is over whoever it belongs to, so the purge's per-stylist hold exempts it —
    otherwise a stylist with a run of failed lookups keeps yesterday's slots up for good."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    slots = mkconfig(event_style="slots")
    seen = [_at(3, h) for h in range(9, 15)]
    ended, upcoming = _at(-1, 10, "sick"), _at(4, 10, "sick")
    svc = FakeCalendarService(events=[_existing_from(e, slots)
                                      for e in seen + [ended, upcoming]])

    stats = sync_calendar.sync(cfg, _res(seen, staff_ok={"nao"},
                                         staff_planned={"nao", "sick"}), service=svc)

    assert ended.google_event_id() in svc.tombstones()
    assert upcoming.google_event_id() in svc.live()
    assert stats["skipped_delete"] == 1


# ------------------------------------------- slots: an id IS the opening, nothing re-keys it
def test_slots_a_booked_slot_overlapping_an_open_neighbour_is_deleted_first(clock):
    """Timely's grid is 30 minutes and a haircut 55, so a stylist's slots overlap their
    neighbours. The overlap rule then called a booked 10:00 a 're-key' of the open 10:30
    and deferred its delete behind every insert."""
    clock(T0)
    cfg = mkconfig(event_style="slots")
    booked, neighbour, new = _at(3, 10), _at(3, 10, minute=30), _at(3, 16)
    svc, (booked_id, _) = _cal(cfg, [booked], [neighbour])

    sync_calendar.sync(cfg, _res([neighbour, new], staff_ok={"nao"}, staff_planned={"nao"}),
                       service=svc)

    writes = [(m, e) for m, e in svc.calls if m in ("delete", "insert")]
    assert writes == [("delete", booked_id), ("insert", new.google_event_id())]


def test_slots_a_booked_slot_is_deleted_even_when_a_neighbours_patch_fails(clock):
    """...and when the neighbour's unrelated patch failed (a notices-banner change rewrites
    every description), the booked slot was 'kept because its replacement failed' — an
    appointment already taken, still advertised as open."""
    clock(T0)
    cfg = mkconfig(event_style="slots")
    before, booked, after = _at(3, 9), _at(3, 9, minute=30), _at(3, 10)
    svc, (before_id, booked_id, _) = _cal(cfg, [before], [booked], [after])
    svc.store[before_id]["summary"] = "an older title"                # forces a patch
    svc.fail_plan[("patch", before_id)] = [http_error(500, "Server Error", "backendError")]

    stats = sync_calendar.sync(cfg, _res([before, after], staff_ok={"nao"},
                                         staff_planned={"nao"}), service=svc)

    assert booked_id in svc.tombstones()
    assert stats["failed_patch"] == 1 and stats["skipped_delete"] == 0


# --------------------------------------------------------------- marker state
def test_write_marker_keeps_the_run_start_as_last_attempt():
    """write_marker REPLACES the marker. Without last_attempt in its body, the stamp main()
    put there when the run started is erased at the end, and the skip gate falls back to
    last_run — measuring end-to-start, so a long deep sweep skips the next on-time tick."""
    svc = FakeCalendarService()
    started = datetime(2026, 10, 14, 10, 23, tzinfo=NY)
    finished = started + timedelta(minutes=38)
    sync_calendar.write_marker(svc, "cal", checked_at=finished, entry_count=5,
                               last_deep=finished, last_attempt=started)

    private = svc.live()[MARKER_ID]["extendedProperties"]["private"]
    assert private["last_attempt"] == started.isoformat()
    assert private["last_run"] == finished.isoformat()
    assert sync_calendar.marker_last_attempt(sync_calendar.read_marker(svc, "cal")) == started


def test_last_attempt_falls_back_to_last_run_for_older_markers():
    ran = datetime(2026, 10, 14, 10, 23, tzinfo=NY)
    m = {"extendedProperties": {"private": {"last_run": ran.isoformat()}}}
    assert sync_calendar.marker_last_attempt(m) == ran
    assert sync_calendar.marker_last_attempt(None) is None


def test_stamp_last_attempt_preserves_every_other_marker_field():
    svc = FakeCalendarService()
    deep = datetime(2026, 10, 14, 4, 0, tzinfo=NY)
    sync_calendar.write_marker(svc, "cal", checked_at=deep, entry_count=5, last_deep=deep)
    marker = sync_calendar.read_marker(svc, "cal")
    at = deep + timedelta(hours=2)

    sync_calendar.stamp_last_attempt(svc, "cal", marker, at)

    stored = svc.live()[MARKER_ID]
    assert stored["extendedProperties"]["private"] == {
        "last_run": deep.isoformat(), "last_deep_sweep": deep.isoformat(),
        "last_attempt": at.isoformat()}
    assert stored["summary"] == marker["summary"]


@pytest.mark.parametrize("status, reason", [
    (503, "backendError"), (429, "rateLimitExceeded"), (403, "userRateLimitExceeded")])
def test_a_transient_stamp_failure_does_not_stop_the_run(capsys, status, reason):
    svc = FakeCalendarService(fail_plan={("patch", MARKER_ID): [
        http_error(status, "Try later", reason)]})
    sync_calendar.stamp_last_attempt(svc, "cal", {"id": MARKER_ID}, T0)   # must not raise
    assert "::warning::" in capsys.readouterr().out


@pytest.mark.parametrize("status, reason", [
    (403, "forbidden"), (403, "requiredAccessLevel"), (401, "authError"),
    (403, "quotaExceeded")])
def test_a_stamp_the_calendar_refuses_aborts_before_any_timely_request(status, reason):
    """The stamp is the run's first calendar WRITE, and preflight only proved a read. A
    calendar shared read-only (or out of quota) let the stamp merely warn: every tick then
    spent a full Timely sweep before sync() hit the same 403, and since nothing was ever
    stamped the gate never skipped one — a sweep per tick for as long as it lasted."""
    svc = FakeCalendarService(fail_plan={("patch", MARKER_ID): [
        http_error(status, "No", reason)]})
    with pytest.raises(sync_calendar.WriteAborted):
        sync_calendar.stamp_last_attempt(svc, "cal", {"id": MARKER_ID}, T0)


def test_write_marker_accepts_a_hand_edited_naive_last_deep_sweep():
    """_elapsed() reads a naive stamp as salon-local, so resolve_depth survives one — but
    write_marker subtracted it from an aware checked_at, raising TypeError after every
    calendar write, on every run, until a deep sweep replaced the value."""
    svc = FakeCalendarService()
    checked = datetime(2026, 10, 14, 11, 30, tzinfo=NY)
    naive = datetime(2026, 10, 14, 8, 30)

    sync_calendar.write_marker(svc, "cal", checked_at=checked, entry_count=1,
                               last_deep=naive)

    stored = svc.live()[MARKER_ID]
    assert "3h ago" in stored["description"]
    assert (sync_calendar.marker_last_deep_sweep(stored)
            == datetime(2026, 10, 14, 8, 30, tzinfo=NY))


def test_a_marker_written_after_a_failed_run_keeps_the_last_success():
    """last_run is the last SUCCESSFUL sync: the workflow opens an issue off its age, and a
    skipped tick pings the heartbeat only while it is fresh. A run that exits 1 must not
    move it, or a persistent failure reads as an hour old forever."""
    svc = FakeCalendarService()
    ok_at = datetime(2026, 10, 14, 1, 0, tzinfo=NY)
    failed_at = datetime(2026, 10, 14, 11, 30, tzinfo=NY)

    sync_calendar.write_marker(svc, "cal", checked_at=failed_at, entry_count=3,
                               last_deep=None, succeeded=False, previous_success=ok_at)
    assert svc.live()[MARKER_ID]["extendedProperties"]["private"]["last_run"] == \
        ok_at.isoformat()

    sync_calendar.write_marker(svc, "cal", checked_at=failed_at, entry_count=3,
                               last_deep=None, succeeded=False, previous_success=None)
    assert "last_run" not in svc.live()[MARKER_ID]["extendedProperties"]["private"]


@pytest.mark.parametrize("stored, shown", [
    ("2026-10-02T21:00:00-04:00", "2026-10-02 9:00 PM EDT"),
    ("2026-03-02T21:00:00-05:00", "2026-03-02 9:00 PM EST"),
    ("2026-10-02T21:00:00", "2026-10-02 9:00 PM EDT"),         # a hand-edited, naive stamp
])
def test_a_failed_runs_marker_shows_the_last_clean_sync_in_salon_time(stored, shown):
    """last_run is read back off the marker with a fixed offset, which %Z printed as
    'UTC-04:00' on the public status entry, right under a line saying 'EDT'."""
    svc = FakeCalendarService()
    sync_calendar.write_marker(svc, "cal", checked_at=datetime(2026, 10, 3, 2, 0, tzinfo=NY),
                               entry_count=3, last_deep=None, succeeded=False,
                               previous_success=datetime.fromisoformat(stored))

    marker = svc.live()[MARKER_ID]
    assert f"the last clean sync was at {shown}." in marker["description"]
    assert "UTC-" not in marker["description"]
    assert marker["extendedProperties"]["private"]["last_run"] == stored


def test_a_deleted_marker_reads_as_no_marker():
    """events.get returns a soft-deleted event rather than 404. A marker someone deleted
    to reset the state must not keep vouching for a recent deep sweep or run start."""
    svc = FakeCalendarService()
    now = datetime(2026, 10, 14, 10, 0, tzinfo=NY)
    sync_calendar.write_marker(svc, "cal", checked_at=now, entry_count=1, last_deep=now)
    svc.store[MARKER_ID]["status"] = "cancelled"
    assert sync_calendar.read_marker(svc, "cal") is None


# --------------------------------------------------------------- round 2: review findings
def _offering(ev, *names):
    """The same opening, bookable for exactly these services."""
    menu = {"Hair Cut": ServiceOption("Hair Cut", "$75", 55, False),
            "Beard Trim": ServiceOption("Beard Trim", "$40", 25, False)}
    return Event(stylist_id=ev.stylist_id, stylist=ev.stylist, stylist_role=ev.stylist_role,
                 start=ev.start, book_url=ev.book_url, options=[menu[n] for n in names])


@pytest.mark.parametrize("style", ["blocks", "slots"])
def test_one_stylists_failed_lookup_does_not_freeze_everyone_elses_descriptions(clock, style):
    """One failed lookup ANYWHERE stopped description updates for EVERY stylist. Beard Trim
    moved from Nao's 10:00 to her 11:00 — same ids, same spans, same titles, so only the
    descriptions changed — and because Sachi had a failed lookup, Nao's calendar kept
    offering a 10:00 Beard Trim that was no longer bookable and never offered the 11:00
    one that was, on every run where anything anywhere failed. No existing entry of a
    stylist with a failed lookup is ever rewritten (rule 1), so whatever is still compared
    belongs to someone every one of whose lookups landed."""
    clock(T0)
    cfg = mkconfig(event_style=style)
    before = [_offering(_at(3, 10), "Hair Cut", "Beard Trim"), _offering(_at(3, 11), "Hair Cut")]
    after = [_offering(_at(3, 10), "Hair Cut"), _offering(_at(3, 11), "Hair Cut", "Beard Trim")]
    old = build_entries(before, style)
    svc = FakeCalendarService(events=[_existing_entry(e, cfg) for e in old])
    want = {e.google_event_id(): sync_calendar.entry_body(e, cfg, "")
            for e in build_entries(after, style)}
    assert set(want) == {e.google_event_id() for e in old}      # only descriptions moved

    stats = sync_calendar.sync(
        cfg, _res(after + [_at(4, 9, "sachi")], staff_ok={"nao"},
                  staff_planned={"nao", "sachi"}, lookups_failed=1),
        service=svc)

    for eid, body in want.items():
        assert svc.live()[eid]["description"] == body["description"]
    assert stats["patch"] == len(want)
    # Sachi's one new entry is on a day holding nothing of hers, so it is published.
    assert svc.methods("insert") == [build_entries([_at(4, 9, "sachi")],
                                                   style)[0].google_event_id()]


def test_a_narrowing_that_waits_on_a_later_narrowing_lands_on_a_clean_run(clock, capsys):
    """A narrowing entry only ever waits on entries that start AFTER it (what overlaps its
    stored span, which begins at its own first opening). Walking them earliest-first
    checked X before Y — which X waits on, and which was itself waiting to narrow — so X
    was kept wide on a run where nothing failed: still advertising 11:00 and 14:00, both
    booked, and a warning that a write had failed when none had."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    # Left by an earlier run whose narrowing patch of X failed after 11:00 was booked: the
    # tail Y landed, and X is still stored wide over it.
    x_wide = [_at(3, h) for h in (9, 10, 11, 12, 13, 14, 15)]
    y_wide = [_at(3, h) for h in (12, 13, 14, 15)]
    svc, (x, y) = _cal(cfg, x_wide, y_wide)
    # Now 14:00 is booked as well: X narrows onto Y', Y narrows onto the new Z.
    head, mid, z = [_at(3, h) for h in (9, 10)], [_at(3, h) for h in (12, 13)], [_at(3, 15)]
    assert (_block_id(head), _block_id(mid)) == (x, y)

    stats = sync_calendar.sync(cfg, _res(head + mid + z, staff_ok={"nao"},
                                         staff_planned={"nao"}), service=svc)

    assert svc.live()[x]["summary"] == "Nao · 2 open"
    assert svc.live()[y]["summary"] == "Nao · 2 open"
    assert set(svc.live()) == {x, y, _block_id(z)}
    writes = [(m, e) for m, e in svc.calls if m in ("patch", "insert", "delete")]
    assert writes == [("insert", _block_id(z)), ("patch", y), ("patch", x)]
    assert stats["skipped_delete"] == 0
    assert "failed" not in capsys.readouterr().out


def test_a_narrowing_kept_behind_a_failed_one_says_what_actually_failed(clock, capsys):
    """When the chain IS broken by a failure, both blocks stay wide (X waits on Y, which
    waits on Z) — and the warning counts the one write that failed, not two."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    svc, (x, y) = _cal(cfg, [_at(3, h) for h in (9, 10, 11, 12, 13, 14, 15)],
                       [_at(3, h) for h in (12, 13, 14, 15)])
    head, mid, z = [_at(3, h) for h in (9, 10)], [_at(3, h) for h in (12, 13)], [_at(3, 15)]
    svc.fail_plan[("insert", _block_id(z))] = [http_error(500, "Server Error", "backendError")]

    stats = sync_calendar.sync(cfg, _res(head + mid + z, staff_ok={"nao"},
                                         staff_planned={"nao"}), service=svc)

    assert svc.methods("patch") == []
    assert stats["skipped_delete"] == 2 and stats["failed_insert"] == 1
    out = capsys.readouterr().out
    assert "kept 0 superseded entr(ies) whole and 2 split block(s)" in out
    assert "did not land this run (1 write(s) failed;" in out
    assert "failed to write" not in out


def test_a_half_seen_stylists_shrinking_block_counts_as_a_withheld_delete(clock, capsys):
    """Rule 1 withholds an uncovered stylist's patches as well as their deletes, but only
    the deletes reached skipped_delete. A block that would shrink retires openings it
    advertises — a delete of that part, which pass 4 already counts as one when it keeps a
    split block wide — so a stylist uncovered on every run kept 12 booked 12:00s advertised
    with skipped_delete at 0 and every run exiting 0: no issue, a green heartbeat."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    days = range(1, 13)
    svc, ids = _cal(cfg, *[[_at(d, h) for h in (10, 11, 12, 13, 14)] for d in days])
    feed = [_at(d, h) for d in days for h in (10, 11, 13, 14)]     # 12:00 booked, daily

    stats = sync_calendar.sync(
        cfg, _res(feed + [_at(2, 9, "sachi")], staff_ok={"sachi"},
                  staff_planned={"nao", "sachi"}, lookups_failed=1),
        service=svc)

    assert all(svc.live()[i]["summary"] == "Nao · 5 open" for i in ids)    # still held
    assert stats["skipped_delete"] == 12 > sync_calendar.SKIPPED_DELETE_EXIT_FLOOR
    assert "withholding 12 delete(s)" in capsys.readouterr().out


def test_a_half_seen_stylists_growing_block_is_withheld_but_is_no_delete(clock):
    """Only a write that would RETIRE advertised openings is a withheld delete. Holding back
    a new opening under-advertises, the lesser harm, and stays a warning."""
    clock(T0)
    cfg = mkconfig(event_style="blocks")
    svc, (block,) = _cal(cfg, [_at(3, h, "sick") for h in (9, 10)])

    stats = sync_calendar.sync(
        cfg, _res([_at(3, h, "sick") for h in (9, 10, 11)] + [_at(4, 9)], staff_ok={"nao"},
                  staff_planned={"nao", "sick"}, lookups_failed=1),
        service=svc)

    assert svc.methods("patch") == []
    assert stats["skipped_delete"] == 0


# --------------------------------------------------------------- auto depth after a failed deep
def _depth_marker(**stamps):
    return {"id": MARKER_ID, "status": "confirmed", "summary": "checked",
            "start": {"date": T0.date().isoformat()},
            "end": {"date": (T0.date() + timedelta(days=1)).isoformat()},
            "extendedProperties": {"private": {k: v.isoformat() for k, v in stamps.items()}}}


def test_auto_goes_near_straight_after_a_deep_sweep_that_did_not_complete():
    """A deep sweep makes ~1575 Timely requests to a near one's ~240, so a throttle that
    aborts every deep sweep can leave near sweeps untouched. Re-picking deep on every run
    then meant no near sweep ever ran: days 0-21 went unrefreshed and every run exited 2."""
    cfg = mkconfig()
    started = T0 - timedelta(hours=1)
    m = _depth_marker(last_attempt=started, last_deep_attempt=started,
                      last_deep_sweep=T0 - timedelta(hours=10))
    assert sync_calendar.resolve_depth(cfg, "auto", m, now=T0) == (cfg.near_lookahead_days,
                                                                     False)


def test_auto_retries_deep_once_a_near_run_has_had_its_turn():
    cfg = mkconfig()
    m = _depth_marker(last_attempt=T0 - timedelta(hours=1),
                      last_deep_attempt=T0 - timedelta(hours=2),
                      last_deep_sweep=T0 - timedelta(hours=10))
    assert sync_calendar.resolve_depth(cfg, "auto", m, now=T0) == (cfg.lookahead_days, True)


def test_a_deep_sweep_that_completed_is_not_read_as_a_failed_one():
    """Its last_deep_sweep is its FINISH, later than the start stamped as last_deep_attempt;
    once that is stale, the next deep sweep is simply due."""
    cfg = mkconfig()
    started = T0 - timedelta(hours=cfg.deep_sweep_every_hours + 1)
    m = _depth_marker(last_attempt=started, last_deep_attempt=started,
                      last_deep_sweep=started + timedelta(minutes=38))
    assert sync_calendar.resolve_depth(cfg, "auto", m, now=T0) == (cfg.lookahead_days, True)


@pytest.mark.parametrize("clean_ago, deep", [
    (timedelta(hours=sync_calendar.NEAR_FALLBACK_HOURS) - timedelta(minutes=1), False),
    (timedelta(hours=sync_calendar.NEAR_FALLBACK_HOURS), True),
    (None, True),                     # no clean deep sweep recorded at all
])
def test_a_failed_deep_sweep_hands_runs_to_near_only_up_to_the_fallback_limit(clean_ago, deep):
    """Alternating for good let a deep sweep fail on every attempt with no alert: the near
    runs in between exit 0, so last_success_age_hours never aged toward the workflow's
    12-hour issue rule while days 22-90 and the .ics feed went stale. Past the limit every
    auto run is deep again, and a deep sweep that keeps failing stops the runs succeeding."""
    cfg = mkconfig()
    started = T0 - timedelta(hours=1)
    stamps = {"last_attempt": started, "last_deep_attempt": started}
    if clean_ago is not None:
        stamps["last_deep_sweep"] = T0 - clean_ago
    days, got = sync_calendar.resolve_depth(cfg, "auto", _depth_marker(**stamps), now=T0)
    assert (days, got) == ((cfg.lookahead_days, True) if deep
                           else (cfg.near_lookahead_days, False))


def test_an_explicit_full_is_deep_even_right_after_a_failed_one():
    cfg = mkconfig()
    started = T0 - timedelta(hours=1)
    m = _depth_marker(last_attempt=started, last_deep_attempt=started)
    assert sync_calendar.resolve_depth(cfg, "full", m, now=T0) == (cfg.lookahead_days, True)


def test_stamping_a_deep_attempt_keeps_every_other_marker_field():
    svc = FakeCalendarService()
    deep = datetime(2026, 10, 14, 4, 0, tzinfo=NY)
    sync_calendar.write_marker(svc, "cal", checked_at=deep, entry_count=5, last_deep=deep)
    at = deep + timedelta(hours=7)

    sync_calendar.stamp_last_attempt(svc, "cal", sync_calendar.read_marker(svc, "cal"), at,
                                     deep=True)

    assert svc.live()[MARKER_ID]["extendedProperties"]["private"] == {
        "last_run": deep.isoformat(), "last_deep_sweep": deep.isoformat(),
        "last_attempt": at.isoformat(), "last_deep_attempt": at.isoformat()}


def test_write_marker_keeps_the_last_deep_attempt():
    """write_marker REPLACES the marker; a near run that dropped last_deep_attempt would
    make the failed deep sweep before it invisible to the next auto decision."""
    svc = FakeCalendarService()
    tried = datetime(2026, 10, 14, 9, 0, tzinfo=NY)
    sync_calendar.write_marker(svc, "cal", checked_at=tried + timedelta(hours=1),
                               entry_count=5, last_deep=None, last_deep_attempt=tried)
    private = svc.live()[MARKER_ID]["extendedProperties"]["private"]
    assert private["last_deep_attempt"] == tried.isoformat()
