"""Tests for the low-level funnel client in src/timely.py: how a request survives the
network, and how the funnel POSTs prove they actually moved the session forward.

No network. A scripted fake stands in for requests.Session and plays back one outcome
per ATTEMPT — a status code, or an exception to raise — so a test states exactly which
attempt failed and how.
"""
import base64
import json
import os
import random
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
import requests

from src import fetch_availability as fa
from src import timely
from src.timely import BudgetExhausted, TimelyClient, TimelyError

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
OBG = "11111111-1111-1111-1111-111111111111"


def _open_dates_url(obg=OBG, staff="175308"):
    return (f"{timely.BASE}/Booking/GetOpenDates?obg={obg}&month=9&year=2026"
            f"&staffId={staff}&tzName=&tzId=80")


URL = _open_dates_url()
SLOTS_URL = (f"{timely.BASE}/booking/gettimeslots/?obg={OBG}&dateSelected=2026-09-12"
             f"&staffId=175308&tzName=&tzId=80")


def _read(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


class FakeResponse:
    def __init__(self, status, text="ok", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error")


class _Clock:
    """Simulated time: sleeps advance it instead of blocking, and the fakes below advance
    it by each response's latency, so a whole sweep's wall clock is measurable."""

    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += max(0.0, seconds)

    def monotonic(self):
        return self.now


class FakeSession:
    """Plays back `script`, one entry per attempt: an int is a response with that status
    (body `body`), a FakeResponse is returned as is (for headers such as Retry-After), an
    exception instance is raised. Running past the end of the script is a test failure —
    it means the client made an attempt nobody planned for.

    Given a `clock`, every attempt first advances it by `latency` seconds — 30 for a read
    timeout sitting out its full timeout — so a test can say how long a failure took."""

    def __init__(self, *script, body="ok", clock=None, latency=0.0):
        self.script = list(script)
        self.body = body
        self.clock, self.latency = clock, latency
        self.calls: list[tuple[str, str]] = []
        self.headers: dict[str, str] = {}

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        assert self.script, f"unplanned attempt #{len(self.calls)}: {method} {url}"
        if self.clock is not None:
            self.clock.now += self.latency
        outcome = self.script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, FakeResponse):
            return outcome
        return FakeResponse(outcome, self.body)


@pytest.fixture
def slept(monkeypatch):
    """Record every sleep instead of taking it, and start each test on a fresh budget
    with no pacing delay — so for GETs the list holds the backoff waits and nothing else."""
    waits: list[float] = []
    monkeypatch.setattr(timely.time, "sleep", waits.append)
    timely.BUDGET.configure(1000, 0.0)
    return waits


@pytest.fixture
def clock(monkeypatch):
    """A simulated clock for timely: sleeps advance it, and a FakeSession given it advances
    it by each attempt's latency. Starts each test on a fresh budget with no pacing delay."""
    c = _Clock()
    monkeypatch.setattr(timely.time, "sleep", c.sleep)
    monkeypatch.setattr(timely.time, "monotonic", c.monotonic)
    monkeypatch.setattr(timely.BUDGET, "_last", 0.0)
    timely.BUDGET.configure(1000, 0.0)
    return c


def _client(session):
    c = TimelyClient()
    c.session = session
    c.obg = OBG
    return c


def _timeout():
    # The exact text from the 2026-09-05 run.
    return requests.ReadTimeout("Read timed out. (read timeout=30)")


TRANSPORT_ERRORS = [
    pytest.param(_timeout, id="read-timeout"),
    pytest.param(lambda: requests.ConnectTimeout("Connection to book.gettimely.com timed out"),
                 id="connect-timeout"),
    pytest.param(lambda: requests.ConnectionError("('Connection aborted.', "
                                                  "RemoteDisconnected())"),
                 id="connection-error"),
    pytest.param(lambda: requests.Timeout("timed out"), id="bare-timeout"),
]

# Attempts that get no USABLE answer, which the unanswered streak counts alike: (a factory
# for one attempt's outcome, the seconds each attempt takes, what the stop message must
# name). Each is slow enough — a 30s read timeout, or a 5xx asking for a 60s wait — that a
# request spending all five attempts on it lasts 3-4 min, so three of them outlast
# MIN_UNANSWERED_STREAK_SECONDS and only the streak's other conditions are in play.
UNANSWERED = [
    pytest.param(_timeout, 30.0, "ReadTimeout: Read timed out", id="read-timeout"),
    pytest.param(lambda: FakeResponse(503, headers={"Retry-After": "60"}), 0.3, "HTTP 503",
                 id="503"),
    pytest.param(lambda: FakeResponse(502, headers={"Retry-After": "60"}), 0.3, "HTTP 502",
                 id="502"),
]
HIROKI = "686999"     # sole stylist of the catalog's last six services
FAUSTO = "24105"      # in nine services, interleaved with other stylists


# ------------------------------------------------------------ transport errors are retried
@pytest.mark.parametrize("make_exc", TRANSPORT_ERRORS)
def test_a_transport_error_is_retried_with_the_5xx_backoff(slept, make_exc):
    """Only 429/5xx were retried, so a single dropped connection or read timeout escaped
    on the FIRST attempt. The 2026-09-05 run lost 4 lookups that way — each one costing a
    stylist delete authority for the run — and exited 1, over failures a two-second wait
    would have cleared."""
    session = FakeSession(make_exc(), make_exc(), 200, body="recovered")
    c = _client(session)

    assert c._request("GET", URL) == "recovered"
    assert len(session.calls) == 3
    assert slept == [2.0, 4.0]                  # the same exponential backoff as a 5xx
    assert timely.BUDGET.made == 3              # every attempt is charged to the cap


def test_a_transport_error_that_never_clears_raises_timely_error_naming_url_and_cause(slept):
    """After the last attempt the failure must surface as a TimelyError — what fetch()
    treats as one failed lookup — and say which url failed and why, rather than a bare
    requests exception that names neither the funnel step nor the attempts spent."""
    session = FakeSession(*[_timeout() for _ in range(5)])
    c = _client(session)

    with pytest.raises(TimelyError) as ei:
        c._request("GET", URL)
    assert not isinstance(ei.value, BudgetExhausted)    # one lookup, not the whole run
    assert URL in str(ei.value)
    assert "Read timed out" in str(ei.value)
    assert isinstance(ei.value.__cause__, requests.ReadTimeout)
    assert len(session.calls) == 5               # the same attempt limit as a 5xx
    assert slept == [2.0, 4.0, 8.0, 16.0]


def test_transport_failures_never_trip_the_throttle_stop_conditions(slept):
    """A timeout is not Timely asking us to slow down. Counted as a throttle, a few
    unlucky requests to one endpoint would trip MAX_CONSECUTIVE_THROTTLES and abort an
    otherwise healthy sweep as untrusted — and the run-wide backstop would make even
    scattered network blips fatal.

    Transport failures have stop conditions of their own (tested below), so the failed
    requests are spread over enough endpoints that no transport streak reaches its
    threshold and the run-wide transport total stays under its cap: with those out of the
    way, none of these requests may end the run."""
    n = max(timely.MAX_CONSECUTIVE_THROTTLES, timely.MAX_THROTTLED_REQUESTS_PER_RUN) + 1
    per_endpoint = timely.MAX_CONSECUTIVE_UNANSWERED_REQUESTS - 1
    endpoints = [URL, SLOTS_URL, timely.EMBED]
    assert per_endpoint * len(endpoints) >= n and 5 * n < timely.MAX_TRANSPORT_FAILURES_PER_RUN
    session = FakeSession(*[_timeout() for _ in range(5 * n)])
    c = _client(session)

    for i in range(n):
        with pytest.raises(TimelyError) as ei:
            c._request("GET", endpoints[i // per_endpoint])
        assert not isinstance(ei.value, BudgetExhausted)
    assert timely.BUDGET.throttled_total == 0
    assert not any(timely.BUDGET.throttles.values())


def test_every_transport_retry_is_charged_to_the_request_cap(slept):
    """Retrying must not be a way around max_requests_per_run: the cap is the guarantee
    that a truncated run is marked untrusted, so it has to see every attempt."""
    timely.BUDGET.configure(3, 0.0)
    session = FakeSession(*[_timeout() for _ in range(5)])
    c = _client(session)

    with pytest.raises(BudgetExhausted, match="request cap"):
        c._request("GET", URL)
    assert len(session.calls) == 3
    assert timely.BUDGET.made == 3


def test_transport_errors_and_5xx_share_one_attempt_limit_and_backoff(slept):
    """One request gets five attempts in total, however its failures are mixed — not five
    per kind of failure — and the backoff keeps doubling across the mix."""
    session = FakeSession(requests.ConnectionError("reset"), 503, _timeout(), 502,
                          requests.ConnectionError("Connection reset by peer"))
    c = _client(session)

    with pytest.raises(TimelyError, match="Connection reset by peer"):
        c._request("GET", URL)
    assert len(session.calls) == 5
    assert slept == [2.0, 4.0, 8.0, 16.0]


# ------------------------------------- ...but a host that stops answering ends the run
def _exhaust(clock, make_outcome, latency, staff, *, call="open_dates"):
    """One request from a FRESH client — fetch() opens one per service — that spends all
    five attempts on `make_outcome()`, through the real client method so the stylist it
    names reaches the stop conditions the way production's does. Returns what it raised."""
    c = _client(FakeSession(*[make_outcome() for _ in range(5)], clock=clock,
                            latency=latency))
    c.obg = f"{c.session_no:08d}" + OBG[8:]          # a fresh obg, as each session has
    with pytest.raises(TimelyError) as ei:
        if call == "open_dates":
            c.open_dates(staff, 9, 2026)
        elif call == "time_slots":
            c.time_slots(staff, "2026-09-12")
        else:
            c.select_staff(staff)
    assert len(c.session.calls) == 5
    return ei.value


@pytest.mark.parametrize("make_outcome, latency, named", UNANSWERED)
def test_an_endpoint_that_never_answers_stops_the_run(clock, make_outcome, latency, named):
    """Retrying transport errors made each request against a host that never answers cost
    ~3 min instead of ~30s, and nothing stopped the sweep: one hung endpoint took 5x the
    requests and pushed a deep sweep past the job's 120-min timeout. A few requests that
    each spent every attempt — for different stylists, from different sessions, over more
    than a blip's worth of time — are proof enough that the endpoint is down: the run must
    stop as untrusted (BudgetExhausted), not grind on. Each request carries a fresh obg,
    as real lookups do, so the streak has to be keyed on the path.

    An endpoint answering nothing but 5xx is the same outage. It used to count toward no
    stop condition at all: each request spent its five attempts and raised a plain
    TimelyError, and the sweep ground through every lookup."""
    k = timely.MAX_CONSECUTIVE_UNANSWERED_REQUESTS
    for i in range(k - 1):
        e = _exhaust(clock, make_outcome, latency, staff=str(i))
        assert not isinstance(e, BudgetExhausted)              # one lookup lost; run goes on
    e = _exhaust(clock, make_outcome, latency, staff="9")
    assert isinstance(e, BudgetExhausted)
    assert f"{k} consecutive GET /booking/getopendates" in str(e)
    assert f"for {k} stylists, from {k} sessions" in str(e)
    assert named in str(e)                                      # says what the failure was
    assert clock.now >= timely.MIN_UNANSWERED_STREAK_SECONDS
    assert timely.BUDGET.throttled_total == 0                   # still not a throttle


# The unanswered streak's rule, fed straight to BUDGET.unanswered(): one exhausted request
# per (stylist, session), back to back, each lasting `minutes_each`.
def _streak(clock, requests, *, url=URL, minutes_each=3.0):
    """Returns the 1-based index of the request that stopped the run, or None."""
    for i, (staff, session) in enumerate(requests, 1):
        since = clock.now
        clock.now += minutes_each * 60
        try:
            timely.BUDGET.unanswered("GET", url, "HTTP 500", staff=staff, session=session,
                                     since=since)
        except BudgetExhausted:
            return i
    return None


def test_one_stylists_outage_never_stops_the_run(clock):
    """The catalog's last six services are Hiroki's alone, and nothing else calls the
    stylist endpoints between them, so a fault confined to him — Timely 500ing or timing
    out on anything about one corrupt roster — exhausted three of his requests in a row.
    "Three consecutive" read that as the endpoint being down and aborted the run (exit 2,
    nothing written for any of the nine stylists) on every run after, where before these
    stop conditions it cost his 6 of 60 lookups and stayed trusted. However long it goes
    on, and across however many sessions, one stylist's streak is that stylist's fault."""
    hiroki_only = [(HIROKI, session) for session in range(1, 7)]

    assert _streak(clock, hiroki_only) is None
    # ...which is not a blind spot: once a second stylist fails too, it is the endpoint.
    assert _streak(clock, [(FAUSTO, 7)]) == 1


def test_one_services_outage_never_stops_the_run(clock):
    """The same, one level up. A service whose own setup makes GetOpenDates throw fails
    every one of its stylists' requests back to back, all from that service's session —
    five stylists in a row for Haircut — while every other service is fine."""
    haircut = [(staff, 1) for staff in ["24102", "173532", "24107", FAUSTO, "24104"]]

    assert _streak(clock, haircut) is None
    assert _streak(clock, [("24102", 2)]) == 1


def test_a_streak_of_fast_failures_must_outlast_a_blip(clock):
    """A 5xx with no Retry-After fails in a fraction of a second, so three exhausted
    requests took ~105s and a two-minute 503 window on gettimeslots (an app pool
    recycling) aborted a sweep that otherwise lost 3 lookups and stayed trusted. A streak
    only trips once it has lasted MIN_UNANSWERED_STREAK_SECONDS."""
    diverse = [(str(i), i) for i in range(1, 40)]
    blip = diverse[:4]                                           # 4 x 30s = 2 min

    assert _streak(clock, blip, minutes_each=0.5) is None
    timely.BUDGET.configure(1000, 0.0)
    minimum = timely.MIN_UNANSWERED_STREAK_SECONDS
    assert _streak(clock, diverse, minutes_each=0.5) == minimum // 30


def test_requests_that_name_no_stylist_are_never_one_stylists_fault(clock):
    """The bootstrap GET and the /Booking/Service POST are about no stylist, so a streak of
    them is never excused as one stylist's outage — only the session and time rules apply."""
    assert _streak(clock, [(None, s) for s in (1, 2, 3)], url=timely.EMBED) == 3


@pytest.mark.parametrize("call", ["select_staff", "open_dates", "time_slots"])
def test_every_stylist_request_names_its_stylist(clock, call):
    """The rule above is only as good as its input: each request that is about a stylist
    must say which one, or every streak looks like many stylists' (or none's) and one
    stylist's outage aborts the run again. Six of Hiroki's requests in a row, one per
    session as fetch() makes them, must not stop the run."""
    for _ in range(6):
        e = _exhaust(clock, _timeout, 30.0, staff=HIROKI, call=call)
        assert not isinstance(e, BudgetExhausted), e
    assert clock.now >= timely.MIN_UNANSWERED_STREAK_SECONDS


@pytest.mark.parametrize("make_outcome, latency, named", UNANSWERED)
def test_a_siblings_success_does_not_reset_the_unanswered_streak(clock, make_outcome, latency,
                                                                 named):
    """Every gettimeslots call is preceded by a GetOpenDates call. If that sibling's success
    cleared the streak, it would oscillate 0->1->0 and never trip — the exact way the
    throttle streak was defeated twice (see MAX_CONSECUTIVE_THROTTLES)."""
    k = timely.MAX_CONSECUTIVE_UNANSWERED_REQUESTS
    for i in range(k):
        staff = str(i)
        assert _client(FakeSession(200, body='{"openDates": []}')).open_dates(
            staff, 9, 2026) == []                                # GetOpenDates is healthy
        e = _exhaust(clock, make_outcome, latency, staff, call="time_slots")
        assert isinstance(e, BudgetExhausted) == (i == k - 1)    # gettimeslots never answers


@pytest.mark.parametrize("make_outcome, latency, named", UNANSWERED)
def test_the_endpoints_own_success_resets_its_unanswered_streak(clock, make_outcome, latency,
                                                                named):
    """The other half: "consecutive" means consecutive. An endpoint that answers again has
    recovered, so bad requests either side of a good one must not end the run — even
    though, uncleared, these would be a streak long enough, diverse enough and slow
    enough to trip."""
    k = timely.MAX_CONSECUTIVE_UNANSWERED_REQUESTS
    for i in range(k - 1):
        assert not isinstance(_exhaust(clock, make_outcome, latency, str(i)), BudgetExhausted)
    assert _client(FakeSession(200, body='{"openDates": []}')).open_dates("x", 9, 2026) == []
    for i in range(k - 1):
        e = _exhaust(clock, make_outcome, latency, str(10 + i))
        assert not isinstance(e, BudgetExhausted)
    assert 2 * (k - 1) >= k and clock.now >= timely.MIN_UNANSWERED_STREAK_SECONDS


@pytest.mark.parametrize("make_exc", TRANSPORT_ERRORS)
def test_transport_failures_that_keep_recovering_still_stop_the_run_at_the_run_wide_cap(
        slept, make_exc):
    """A network losing a request in twenty at random never forms a streak — every
    request recovers on its retry — yet each failed attempt still costs a 30s timeout plus
    backoff, and a simulated deep sweep at that rate ran ~126 min, past the job timeout.
    So every failed transport ATTEMPT counts toward a run-wide total that no success
    clears, and the run stops at the failure that reaches it rather than spending another
    retry."""
    cap = timely.MAX_TRANSPORT_FAILURES_PER_RUN
    script = []
    for _ in range(cap):
        script += [make_exc(), 200]
    session = FakeSession(*script)
    c = _client(session)

    for _ in range(cap - 1):
        assert c._request("GET", URL) == "ok"                # each one recovers...
    with pytest.raises(BudgetExhausted, match=f"{cap} connection failures/timeouts") as ei:
        c._request("GET", URL)                                # ...until the total runs out
    assert "costs up to a 30s timeout" in str(ei.value)
    assert session.script == [200], "it stopped at the failure, not after one more retry"
    assert timely.BUDGET.throttled_total == 0


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_attempts_never_count_toward_the_transport_cap(slept, status):
    """That cap is sized for 30s timeouts, and a 5xx arrives in a fraction of a second, so
    50 of them are ~2 min of backoff. Counting them aborted runs that lost nothing: a
    sweep where ~3.5% of attempts got a fast 503, every one recovering on its retry, was
    stopped at the 50th; and one stylist's deterministic 500 interleaved with healthy
    lookups (Fausto, in nine services) used 45 of the 50 and left room for only five
    ordinary timeouts in the rest of the run. What a 5xx costs is time, which
    MAX_RETRY_SECONDS_PER_RUN charges directly."""
    cap = timely.MAX_TRANSPORT_FAILURES_PER_RUN
    script = []
    for _ in range(2 * cap):
        script += [status, 200]                       # recovered 5xx, far past the cap...
    for _ in range(9):
        script += [status] * 5 + [200]                # ...Fausto's nine exhausted requests,
                                                      # each before a stylist that answers...
    for _ in range(cap - 1):
        script += [_timeout(), 200]                   # ...and every timeout the cap allows
    session = FakeSession(*script)
    c = _client(session)

    for _ in range(2 * cap):
        assert c._request("GET", URL) == "ok"
    for _ in range(9):
        with pytest.raises(TimelyError) as ei:
            c._request("GET", _open_dates_url(staff=FAUSTO), staff=FAUSTO)
        assert not isinstance(ei.value, BudgetExhausted)
        assert c._request("GET", URL) == "ok"         # the next stylist answers
    assert timely.BUDGET.transport_failures == 0
    for _ in range(cap - 1):
        assert c._request("GET", URL) == "ok"
    assert session.script == []


def test_configure_resets_the_unanswered_and_retry_time_counters(monkeypatch):
    """Like the throttle counters: a second fetch() in the same process (near tier, then
    deep tier) must not inherit the first run's streaks, totals or spent retry time and
    abort at once."""
    streak = timely._UnansweredStreak(since=0.0, requests=2, staff={"1", "2"},
                                      sessions={1, 2})
    monkeypatch.setattr(timely.BUDGET, "unanswered_streaks",
                        {("GET", "/booking/getopendates"): streak})
    monkeypatch.setattr(timely.BUDGET, "transport_failures", 49)
    monkeypatch.setattr(timely.BUDGET, "retry_seconds", timely.MAX_RETRY_SECONDS_PER_RUN)
    timely.BUDGET.configure(1000, 0.0)

    assert timely.BUDGET.unanswered_streaks == {}
    assert timely.BUDGET.transport_failures == 0
    assert timely.BUDGET.retry_seconds == 0


# ------------------------------------------- ...and no run outlives its retry budget
@pytest.fixture
def frozen(monkeypatch, slept):
    """`slept`, plus a clock that never moves: each attempt then costs exactly the waits
    the test scripts, with no stray microseconds of real time charged to the budget."""
    monkeypatch.setattr(timely.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(timely.BUDGET, "_last", 0.0)
    return slept


@pytest.mark.parametrize("status", [429, 503])
def test_requests_that_recover_after_long_retry_after_waits_still_stop_the_run(
        frozen, status):
    """The other stop conditions count requests that fail outright, or failed transport
    attempts — none bounds how long Timely may ask us to wait. A 429 or a 503 with
    Retry-After: 120 and then a 200 counts toward nothing else at all, and on every
    lookup that is 8 min each. Time is what runs out, so time is charged — and the run
    stops at the wait that would overspend, refusing to sleep it, rather than after."""
    budget = timely.MAX_RETRY_SECONDS_PER_RUN
    fits = int(budget // 120)
    script = []
    for _ in range(fits + 1):
        script += [FakeResponse(status, headers={"Retry-After": "120"}), 200]
    session = FakeSession(*script)
    c = _client(session)

    for _ in range(fits):
        assert c._request("GET", URL) == "ok"           # every wait honoured in full...
    with pytest.raises(BudgetExhausted, match="retry budget") as ei:
        c._request("GET", URL)                           # ...until the next would overspend
    assert f"HTTP {status}" in str(ei.value)
    assert frozen == [120.0] * fits, "the overspending wait is refused, not slept"
    assert session.script == [200]
    assert timely.BUDGET.throttled_total == 0            # no request was ever exhausted


def test_the_retry_budget_charges_what_a_failed_attempt_itself_took(monkeypatch):
    """A failure can be slow to arrive — a 429 trickling in just under the read timeout —
    and that time is gone whether or not the request then recovers. Charging only the
    waits would let 25s-slow 429s each followed by a 200 run ~1,200 requests (~9 hours)
    before the 2s backoffs alone added up; charging the attempt too stops the run once
    the budget is truly spent."""
    clock = _Clock()
    monkeypatch.setattr(timely.time, "sleep", clock.sleep)
    monkeypatch.setattr(timely.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(timely.BUDGET, "_last", 0.0)
    timely.BUDGET.configure(100_000, 0.0)

    class SlowThrottle:
        headers: dict[str, str] = {}
        calls = 0

        def request(self, method, url, **kw):
            self.calls += 1
            if self.calls % 2:
                clock.now += 25.0
                return FakeResponse(429)
            return FakeResponse(200)

    session = SlowThrottle()
    c = _client(session)
    with pytest.raises(BudgetExhausted, match="retry budget"):
        for _ in range(2000):
            c._request("GET", URL)

    budget = timely.MAX_RETRY_SECONDS_PER_RUN
    assert session.calls < 2 * (budget // 27 + 2)      # 25s attempt + 2s backoff each
    assert clock.now <= budget + 25.0                   # over by one attempt at most


class _SweepTimely:
    """A healthy Timely, served from the captured fixtures, except that each request to
    the endpoint whose url contains `endpoint` is answered by `misbehave(self, method,
    url)` instead — which may raise, return a FakeResponse, or return None to answer
    healthily after all. Shared by every client of the sweep so it can count what that
    endpoint received."""

    def __init__(self, clock, open_day, endpoint, misbehave):
        self.clock, self.open_day = clock, open_day
        self.endpoint, self.misbehave = endpoint, misbehave
        self.headers: dict[str, str] = {}
        self.misbehaved_requests = 0
        self.catalog = _read("service_catalog.html")
        self.step2 = _read("post_service_advanced_2026-08-09.html")
        self.empty_day = _read("gettimeslots_empty_2026-07-22.html")

    def request(self, method, url, **kw):
        if self.endpoint in url:
            self.misbehaved_requests += 1
            resp = self.misbehave(self, method, url)
            if resp is not None:
                return resp
        self.clock.now += 0.3
        if "/book/embed" in url:
            return FakeResponse(200, self.catalog)
        if "/Booking/Service?" in url:
            return FakeResponse(200, self.step2)
        if "/Booking/StaffSelection?" in url:
            return FakeResponse(200, "<html><body>date selection</body></html>")
        if "/Booking/GetOpenDates?" in url:
            return FakeResponse(200, json.dumps({"openDates": [{"day": self.open_day}]}))
        if "/booking/gettimeslots/" in url:
            return FakeResponse(200, self.empty_day)
        raise AssertionError(f"unexpected request {method} {url}")


def _sweep(monkeypatch, endpoint, misbehave):
    """Run the real fa.fetch() against _SweepTimely on a simulated clock. Returns the
    result, the simulated minutes it took, and the fake (for its request count)."""
    clock = _Clock()
    monkeypatch.setattr(timely.time, "sleep", clock.sleep)
    monkeypatch.setattr(timely.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(timely.BUDGET, "_last", 0.0)     # restored, so pacing stays sane
    monkeypatch.setattr(fa, "fetch_notices", lambda *a, **kw: None)   # no network
    cfg = fa.Config()
    cfg.cache_ttl_seconds = 0
    cfg.validate()
    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    timely_ = _SweepTimely(clock, (today + timedelta(days=3)).isoformat(), endpoint,
                           misbehave)

    def client_factory(cache=None, cache_ns=""):
        c = TimelyClient(cache=cache, cache_ns=cache_ns)
        c.session = timely_
        return c

    res = fa.fetch(cfg, client_factory=client_factory)
    return res, clock.now / 60, timely_


SWEEP_ENDPOINTS = ["/Booking/StaffSelection?", "/Booking/GetOpenDates?",
                   "/booking/gettimeslots/"]


def _hang(timely_, method, url):
    timely_.clock.now += 30.0                    # sits out the full read timeout
    raise _timeout()


# How many requests a black-holed endpoint gets before the unanswered streak stops the
# run: the first request at which the streak names a second stylist AND comes from a
# second session. At ~3 min a request, MIN_UNANSWERED_STREAK_SECONDS is long past by then.
HUNG_REQUESTS = {
    # fetch() POSTs StaffSelection for each service's FIRST eligible stylist — Masa for
    # the catalog's first nine services — so until the tenth (Haircut: 24102) a hung
    # endpoint is indistinguishable from a fault in Masa's roster.
    "/Booking/StaffSelection?": 10,
    # The first service has three stylists, all in its one session; the second
    # service's first request is the one that rules out one service's fault.
    "/Booking/GetOpenDates?": 4,
    "/booking/gettimeslots/": 4,
}


@pytest.mark.parametrize("hung", SWEEP_ENDPOINTS)
def test_one_hung_endpoint_ends_the_sweep_untrusted_well_inside_the_job_timeout(
        monkeypatch, hung):
    """The whole sweep, end to end, with one endpoint black-holed and the rest healthy —
    the per-step degradation docs/compliance.md describes. Without a transport stop
    condition every lookup spent ~3 min on that endpoint: ~185 simulated min, 300 requests
    to the hung endpoint, and GitHub killing the job at 120 min with no fetch artifact.
    It must instead end as a clean untrusted fetch (exit 2 downstream), stopped by the
    streak as soon as the outage can no longer be one stylist's or one service's — ~3 min
    a request, long before the retry-time budget would have to."""
    res, minutes, timely_ = _sweep(monkeypatch, hung, _hang)

    n = HUNG_REQUESTS[hung]
    assert minutes < 3.5 * n, (f"sweep took {minutes:.0f} simulated min and sent the hung "
                               f"endpoint {timely_.misbehaved_requests} requests")
    assert minutes < timely.MAX_RETRY_SECONDS_PER_RUN / 60
    assert timely_.misbehaved_requests == 5 * n
    assert res.ok is False
    assert res.staff_ok == set(), "an aborted sweep grants delete authority over nobody"
    assert any("run aborted" in e and f"{n} consecutive" in e and "not answering" in e
               for e in res.errors), res.errors


@pytest.mark.parametrize("retry_after", [None, "30", "60", "120"])
@pytest.mark.parametrize("endpoint", SWEEP_ENDPOINTS)
def test_one_endpoint_answering_only_503_ends_the_sweep_well_inside_the_job_timeout(
        monkeypatch, endpoint, retry_after):
    """The same outage answered with 503s instead of silence. A request that spent all
    five attempts on 5xx counted toward no stop condition, so every lookup paid for them
    — four Retry-After waits each — and with GetOpenDates 503ing a simulated deep sweep
    ran ~126 min at Retry-After: 30, ~246 at 60 and ~486 at 120: past the 120-min job
    timeout, with no fetch artifact and the sync concurrency group held for two hours.
    It must end untrusted inside the job timeout whichever endpoint it is and whatever
    Retry-After it sends: by the streak once the outage has outlasted a blip across two
    stylists and two sessions, or by the retry-time budget when the waits get there
    first (Masa's nine StaffSelection POSTs at 4-8 min each). Fast 503s with no
    Retry-After are the streak's to stop, within minutes."""
    headers = {"Retry-After": retry_after} if retry_after else {}

    def answer_503(timely_, method, url):
        timely_.clock.now += 0.3
        return FakeResponse(503, "Service Unavailable", headers)

    res, minutes, timely_ = _sweep(monkeypatch, endpoint, answer_503)

    assert minutes < timely.MAX_RETRY_SECONDS_PER_RUN / 60 + 5, (
        f"sweep took {minutes:.0f} simulated min and sent the 503ing endpoint "
        f"{timely_.misbehaved_requests} requests")
    # Never the 300 a full sweep used to send it: at most the ten StaffSelection needs.
    assert timely_.misbehaved_requests <= 5 * HUNG_REQUESTS["/Booking/StaffSelection?"]
    assert res.ok is False
    assert res.staff_ok == set(), "an aborted sweep grants delete authority over nobody"
    assert any("run aborted" in e and ("not answering" in e and "HTTP 503" in e
                                       or "retry budget" in e)
               for e in res.errors), res.errors
    if retry_after is None:
        assert minutes < 10
        assert any("not answering" in e for e in res.errors), res.errors


@pytest.mark.parametrize("status", [429, 503])
def test_lookups_that_recover_after_long_waits_end_the_sweep_inside_the_job_timeout(
        monkeypatch, status):
    """No streak, no exhausted request, no throttle: every gettimeslots call is answered
    `status` with Retry-After: 120 four times and then 200 — 8 min a lookup, and nothing
    but the retry-time budget ever sees it. The sweep must end untrusted once that budget
    is spent, inside the job timeout, instead of running ~8 simulated hours."""
    def slow_recovery(timely_, method, url):
        if timely_.misbehaved_requests % 5:              # attempts 1-4 of each request
            timely_.clock.now += 0.3
            return FakeResponse(status, "", {"Retry-After": "120"})
        return None                                       # the 5th answers

    res, minutes, timely_ = _sweep(monkeypatch, "/booking/gettimeslots/", slow_recovery)

    budget_min = timely.MAX_RETRY_SECONDS_PER_RUN / 60
    assert minutes < budget_min + 15, f"sweep took {minutes:.0f} simulated min"
    assert minutes < 120
    assert res.ok is False
    assert res.staff_ok == set(), "an aborted sweep grants delete authority over nobody"
    assert any("run aborted" in e and "retry budget" in e for e in res.errors), res.errors


@pytest.mark.parametrize("exc", [
    pytest.param(requests.exceptions.InvalidURL("bad url"), id="invalid-url"),
    pytest.param(requests.TooManyRedirects("Exceeded 30 redirects."), id="redirect-loop"),
])
def test_other_request_exceptions_are_not_retried(slept, exc):
    """Only connection failures and timeouts are worth waiting out. An invalid url or a
    redirect loop fails identically on every attempt, so retrying it would just spend
    four more requests and 30 seconds of the run on a certainty."""
    session = FakeSession(exc, 200)
    c = _client(session)

    with pytest.raises(type(exc)):
        c._request("GET", URL)
    assert len(session.calls) == 1
    assert slept == []


def test_a_connection_broken_mid_body_is_not_retried(slept):
    """Pinned, not endorsed. A connection that breaks after the status line arrived —
    Content-Length promised, the socket closed part-way through the body — makes requests
    raise ChunkedEncodingError ("Connection broken: IncompleteRead"), which it does NOT
    file under ConnectionError. So it is outside the retried set and fails its lookup on
    the first attempt, exactly as before transport retries existed. The module docstring
    used to promise backoff on "dropped connections", which reads as covering this; it
    must say this case is excluded. If it is ever retried, change this test, the note in
    TimelyClient._request and the module docstring together."""
    exc = requests.exceptions.ChunkedEncodingError(
        "('Connection broken: IncompleteRead(10 bytes read, 990 more expected)', "
        "IncompleteRead(10 bytes read, 990 more expected))")
    session = FakeSession(exc, 200)
    c = _client(session)

    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        c._request("GET", URL)
    assert len(session.calls) == 1
    assert slept == []
    assert not isinstance(exc, (requests.ConnectionError, requests.Timeout))
    assert "ChunkedEncodingError" in (timely.__doc__ or "")


def test_a_retried_funnel_post_is_still_checked_for_advancing(slept):
    """The funnel POSTs ride the same retry loop. Whatever page the retry lands on must
    still go through the "did not advance" check — a timeout followed by a session reset
    is exactly the sequence that would otherwise read as a clean, empty schedule."""
    session = FakeSession(_timeout(), 200, body=_read("service_catalog.html"))
    c = _client(session)

    with pytest.raises(TimelyError, match="did not advance"):
        c.select_staff("175308")
    assert [m for m, _ in session.calls] == ["POST", "POST"]


# ------------------------------------------------- the staff POST actually advanced
def test_a_staff_selection_that_reset_to_step_one_is_refused(slept):
    """select_service refused a response still parked on the service-selection page;
    select_staff had no check at all. If the session resets between the two POSTs,
    GetOpenDates answers a well-formed empty {"openDates": []} for every stylist, every
    lookup 'succeeds' with zero openings while keeping delete authority, and the sync
    removes genuinely bookable times. The captured landing page IS that response."""
    c = _client(FakeSession(200, body=_read("service_catalog.html")))

    with pytest.raises(TimelyError, match="did not advance"):
        c.select_staff("175308")


def test_the_step_two_capture_backs_what_the_staff_guard_keys_on():
    """The guard's evidence, pinned against the real page captured 2026-08-09.

    Timely keeps a back-LINK to an earlier step but drops that step's form fields: the
    step-2 page still links to /Booking/Service?obg=... yet carries none of step 1's
    checkboxes or ServiceStaffIds inputs. That is why the step-1 form fields are safe to
    key on and a back-link is not. It also shows the staff page's own markup is real and
    distinctive — leaving it out of the guard (next test) is a decision, not an oversight."""
    landing = _read("service_catalog.html")
    advanced = _read("post_service_advanced_2026-08-09.html")

    assert timely._SERVICE_RE.search(landing) and timely._SERVICE_STAFF_RE.search(landing)
    assert timely._OBG_RE.search(advanced), "the step-2 page links back to step 1"
    assert not timely._SERVICE_RE.search(advanced)
    assert not timely._SERVICE_STAFF_RE.search(advanced)
    assert 'class="staff-selection-form"' in advanced
    assert 'name="SelectedStaffId"' in advanced


def test_a_staff_selection_guard_does_not_key_on_unproven_markers(slept):
    """The page this POST 302s to (/Booking/DateSelection) has never been captured, so
    nothing proves the staff page's markup is ABSENT there. The step-2 page links back to
    step 1, so a date page linking back to step 2, or offering a stylist switcher named
    SelectedStaffId, is entirely plausible — and a guard keyed on either would fail every
    lookup of every run and take the calendar down. If this test ever has to change,
    replace the stand-in below with a real DateSelection capture first."""
    stand_in = f"""<!DOCTYPE html><html><body>
      <a href="/Booking/Service?obg={OBG}">Change service</a>
      <a href="/Booking/StaffSelection?obg={OBG}">Change stylist</a>
      <form action="/Booking/DateSelection?obg={OBG}" method="post">
        <select name="SelectedStaffId"><option value="175308" selected>Nao</option></select>
        <div class="calendar"></div>
      </form>
    </body></html>"""
    c = _client(FakeSession(200, body=stand_in))

    c.select_staff("175308")        # must not raise
