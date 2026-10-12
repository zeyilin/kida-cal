"""Tests for the low-level funnel client in src/timely.py: how a request survives the
network, and how the funnel POSTs prove they actually moved the session forward.

No network. A scripted fake stands in for requests.Session and plays back one outcome
per ATTEMPT — a status code, or an exception to raise — so a test states exactly which
attempt failed and how.
"""
import json
import os
from datetime import datetime, timedelta
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
    def __init__(self, status, text="ok"):
        self.status_code = status
        self.text = text
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error")


class FakeSession:
    """Plays back `script`, one entry per attempt: an int is a response with that status
    (body `body`), an exception instance is raised. Running past the end of the script is
    a test failure — it means the client made an attempt nobody planned for."""

    def __init__(self, *script, body="ok"):
        self.script = list(script)
        self.body = body
        self.calls: list[tuple[str, str]] = []
        self.headers: dict[str, str] = {}

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        assert self.script, f"unplanned attempt #{len(self.calls)}: {method} {url}"
        outcome = self.script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return FakeResponse(outcome, self.body)


@pytest.fixture
def slept(monkeypatch):
    """Record every sleep instead of taking it, and start each test on a fresh budget
    with no pacing delay — so for GETs the list holds the backoff waits and nothing else."""
    waits: list[float] = []
    monkeypatch.setattr(timely.time, "sleep", waits.append)
    timely.BUDGET.configure(1000, 0.0)
    return waits


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
    per_endpoint = timely.MAX_CONSECUTIVE_TRANSPORT_FAILURES - 1
    endpoints = [URL, SLOTS_URL, timely.EMBED]
    assert per_endpoint * len(endpoints) >= n and 5 * n < timely.MAX_TRANSPORT_ERRORS_PER_RUN
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
def test_an_endpoint_that_never_answers_stops_the_run(slept):
    """Retrying transport errors made each request against a host that never answers cost
    ~3 min instead of ~30s, and nothing stopped the sweep: one hung endpoint took 5x the
    requests and pushed a deep sweep past the job's 120-min timeout. A few requests that
    each spent every attempt are proof enough that the endpoint is down — the run must
    stop as untrusted (BudgetExhausted), not grind on. Each request here carries a fresh
    obg and staff id, as real lookups do, so the streak has to be keyed on the path."""
    k = timely.MAX_CONSECUTIVE_TRANSPORT_FAILURES
    session = FakeSession(*[_timeout() for _ in range(5 * k)])
    c = _client(session)

    for i in range(k - 1):
        with pytest.raises(TimelyError) as ei:
            c._request("GET", _open_dates_url(obg=f"{i:08d}" + OBG[8:], staff=str(i)))
        assert not isinstance(ei.value, BudgetExhausted)    # one lookup lost; run goes on
    with pytest.raises(BudgetExhausted) as ei:
        c._request("GET", _open_dates_url(obg="9" * 8 + OBG[8:], staff="9"))
    assert f"{k} consecutive GET /booking/getopendates" in str(ei.value)
    assert "Read timed out" in str(ei.value)                 # says what the failure was
    assert len(session.calls) == 5 * k
    assert timely.BUDGET.throttled_total == 0                # still not a throttle


def test_a_siblings_success_does_not_reset_the_transport_streak(slept):
    """Every gettimeslots call is preceded by a GetOpenDates call. If that sibling's success
    cleared the streak, it would oscillate 0->1->0 and never trip — the exact way the
    throttle streak was defeated twice (see MAX_CONSECUTIVE_THROTTLES)."""
    k = timely.MAX_CONSECUTIVE_TRANSPORT_FAILURES
    script = []
    for _ in range(k):
        script += [200] + [_timeout() for _ in range(5)]
    c = _client(FakeSession(*script))

    for i in range(k):
        assert c._request("GET", URL) == "ok"               # GetOpenDates is healthy
        with pytest.raises(TimelyError) as ei:
            c._request("GET", SLOTS_URL)                     # gettimeslots never answers
        assert isinstance(ei.value, BudgetExhausted) == (i == k - 1)


def test_the_endpoints_own_success_resets_its_transport_streak(slept):
    """The other half: "consecutive" means consecutive. An endpoint that answers again has
    recovered, so two bad requests either side of a good one must not end the run."""
    k = timely.MAX_CONSECUTIVE_TRANSPORT_FAILURES
    script = ([_timeout() for _ in range(5 * (k - 1))] + [200]
              + [_timeout() for _ in range(5 * (k - 1))])
    c = _client(FakeSession(*script))

    for _ in range(k - 1):
        with pytest.raises(TimelyError) as ei:
            c._request("GET", URL)
        assert not isinstance(ei.value, BudgetExhausted)
    assert c._request("GET", URL) == "ok"
    for _ in range(k - 1):
        with pytest.raises(TimelyError) as ei:
            c._request("GET", URL)
        assert not isinstance(ei.value, BudgetExhausted)


def test_transport_errors_that_keep_recovering_still_stop_the_run_at_the_run_wide_cap(slept):
    """A network losing a request in twenty at random never forms a streak — every
    request recovers on its retry — yet each failed attempt still costs a 30s timeout plus
    backoff, and a simulated deep sweep at that rate ran ~126 min, past the job timeout.
    So every failed ATTEMPT counts toward a run-wide total that no success clears, and the
    run stops at the failure that reaches it rather than spending another retry."""
    cap = timely.MAX_TRANSPORT_ERRORS_PER_RUN
    script = []
    for _ in range(cap):
        script += [_timeout(), 200]
    session = FakeSession(*script)
    c = _client(session)

    for _ in range(cap - 1):
        assert c._request("GET", URL) == "ok"                # each one recovers...
    with pytest.raises(BudgetExhausted, match=f"{cap} connection failures/timeouts"):
        c._request("GET", URL)                                # ...until the total runs out
    assert session.script == [200], "it stopped at the failure, not after one more retry"
    assert timely.BUDGET.throttled_total == 0


def test_configure_resets_the_transport_counters(monkeypatch):
    """Like the throttle counters: a second fetch() in the same process (near tier, then
    deep tier) must not inherit the first run's streaks or total and abort at once."""
    monkeypatch.setattr(timely.BUDGET, "transport_streaks",
                        {("GET", "/booking/getopendates"): 2})
    monkeypatch.setattr(timely.BUDGET, "transport_errors", 49)
    timely.BUDGET.configure(1000, 0.0)

    assert timely.BUDGET.transport_streaks == {}
    assert timely.BUDGET.transport_errors == 0


class _Clock:
    """Simulated time: sleeps advance it instead of blocking, and the fake Timely below
    advances it by each response's latency, so a whole sweep's wall clock is measurable."""

    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += max(0.0, seconds)

    def monotonic(self):
        return self.now


class _HungEndpointTimely:
    """A healthy Timely, served from the captured fixtures, except that every request to
    the endpoint whose url contains `hung` sits out the full 30s read timeout and raises.
    Shared by every client of the sweep so it can count what the hung endpoint received."""

    def __init__(self, hung, clock, open_day):
        self.hung, self.clock, self.open_day = hung, clock, open_day
        self.headers: dict[str, str] = {}
        self.hung_requests = 0
        self.catalog = _read("service_catalog.html")
        self.step2 = _read("post_service_advanced_2026-08-09.html")
        self.empty_day = _read("gettimeslots_empty_2026-07-22.html")

    def request(self, method, url, **kw):
        if self.hung in url:
            self.hung_requests += 1
            self.clock.now += 30.0
            raise _timeout()
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


@pytest.mark.parametrize("hung", ["/Booking/StaffSelection?", "/Booking/GetOpenDates?",
                                  "/booking/gettimeslots/"])
def test_one_hung_endpoint_ends_the_sweep_untrusted_well_inside_the_job_timeout(
        monkeypatch, hung):
    """The whole sweep, end to end, with one endpoint black-holed and the rest healthy —
    the per-step degradation docs/compliance.md describes. Without a transport stop
    condition every lookup spent ~3 min on that endpoint: ~185 simulated min, 300 requests
    to the hung endpoint, and GitHub killing the job at 120 min with no fetch artifact.
    It must instead end as a clean untrusted fetch (exit 2 downstream) within minutes,
    having sent the hung endpoint no more than the streak's worth of requests."""
    clock = _Clock()
    monkeypatch.setattr(timely.time, "sleep", clock.sleep)
    monkeypatch.setattr(timely.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(timely.BUDGET, "_last", 0.0)     # restored, so pacing stays sane
    monkeypatch.setattr(fa, "fetch_notices", lambda *a, **kw: None)   # no network
    cfg = fa.Config()
    cfg.cache_ttl_seconds = 0
    cfg.validate()
    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    timely_ = _HungEndpointTimely(hung, clock, (today + timedelta(days=3)).isoformat())

    def client_factory(cache=None, cache_ns=""):
        c = TimelyClient(cache=cache, cache_ns=cache_ns)
        c.session = timely_
        return c

    res = fa.fetch(cfg, client_factory=client_factory)

    minutes = clock.now / 60
    assert minutes < 15, (f"sweep took {minutes:.0f} simulated min and sent the hung "
                          f"endpoint {timely_.hung_requests} requests")
    assert timely_.hung_requests == 5 * timely.MAX_CONSECUTIVE_TRANSPORT_FAILURES
    assert res.ok is False
    assert res.staff_ok == set(), "an aborted sweep grants delete authority over nobody"
    assert any("run aborted" in e and "not answering" in e for e in res.errors), res.errors


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
