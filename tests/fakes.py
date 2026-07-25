"""
An in-memory stand-in for the Google Calendar v3 `service` object.

The point of this fake is to reproduce the *semantics that broke production*, not just the
method names. In particular:

  * `delete` is a SOFT delete. The event becomes ``status="cancelled"`` and its id stays
    reserved — it does not vanish from the store. This is what Google does, and it is why
    re-inserting a previously-deleted id fails.
  * `insert` with a caller-supplied id that is already reserved (live OR cancelled) raises a
    real ``HttpError`` with status 409 / reason ``duplicate``.
  * `update` is a PUT: it replaces the resource and, because our bodies carry no ``status``,
    revives a cancelled event back to ``confirmed``. This is the documented escape hatch.
  * `list` honours ``showDeleted`` and paginates via ``nextPageToken``.

A fake that popped ids on delete would make the 409 regression test pass for the wrong
reason, so please keep the tombstone behaviour if you extend this.
"""
from __future__ import annotations

import json

import httplib2
from googleapiclient.errors import HttpError


def http_error(status: int, message: str, reason: str) -> HttpError:
    """Build an HttpError shaped like a real one (both resp.status and content populated)."""
    body = json.dumps({
        "error": {
            "code": status,
            "message": message,
            "errors": [{"domain": "global", "reason": reason, "message": message}],
        }
    }).encode()
    return HttpError(httplib2.Response({"status": status}), body,
                     uri="https://www.googleapis.com/calendar/v3/calendars/fake/events")


class _Request:
    """Mimics a googleapiclient request: work is deferred until .execute()."""

    def __init__(self, fn):
        self._fn = fn

    def execute(self, http=None, num_retries=0):
        return self._fn()


class FakeEvents:
    def __init__(self, owner: "FakeCalendarService"):
        self._o = owner

    # -- reads ---------------------------------------------------------------
    def list(self, **kw):
        def run():
            self._o.record("list", None)
            self._o.maybe_fail("list", None)
            show_deleted = kw.get("showDeleted", False)
            page_size = kw.get("maxResults", 2500)
            items = [e for e in self._o.store.values()
                     if show_deleted or e.get("status") != "cancelled"]
            items.sort(key=lambda e: e["id"])
            start = int(kw.get("pageToken") or 0)
            page = items[start:start + page_size]
            resp = {"items": [dict(e) for e in page]}
            if start + page_size < len(items):
                resp["nextPageToken"] = str(start + page_size)
            return resp
        return _Request(run)

    def get(self, **kw):
        def run():
            eid = kw["eventId"]
            self._o.record("get", eid)
            self._o.maybe_fail("get", eid)
            if eid not in self._o.store:
                raise http_error(404, "Not Found", "notFound")
            return dict(self._o.store[eid])
        return _Request(run)

    # -- writes --------------------------------------------------------------
    def insert(self, **kw):
        def run():
            body = kw["body"]
            eid = body.get("id")
            self._o.record("insert", eid)
            self._o.maybe_fail("insert", eid)
            if eid and eid in self._o.store:
                # Reserved — whether the existing event is live or a tombstone.
                raise http_error(409, "The requested identifier already exists.", "duplicate")
            ev = dict(body)
            ev.setdefault("status", "confirmed")
            self._o.store[eid] = ev
            return dict(ev)
        return _Request(run)

    def update(self, **kw):
        def run():
            eid = kw["eventId"]
            self._o.record("update", eid)
            self._o.maybe_fail("update", eid)
            if eid not in self._o.store:
                raise http_error(404, "Not Found", "notFound")
            ev = dict(kw["body"])
            ev["id"] = eid
            ev.setdefault("status", "confirmed")   # PUT with no status revives a tombstone
            self._o.store[eid] = ev
            return dict(ev)
        return _Request(run)

    def patch(self, **kw):
        def run():
            eid = kw["eventId"]
            self._o.record("patch", eid)
            self._o.maybe_fail("patch", eid)
            existing = self._o.store.get(eid)
            if existing is None or existing.get("status") == "cancelled":
                raise http_error(404, "Not Found", "notFound")
            existing.update(kw["body"])
            return dict(existing)
        return _Request(run)

    def delete(self, **kw):
        def run():
            eid = kw["eventId"]
            self._o.record("delete", eid)
            self._o.maybe_fail("delete", eid)
            ev = self._o.store.get(eid)
            if ev is None:
                raise http_error(404, "Not Found", "notFound")
            if ev.get("status") == "cancelled":
                raise http_error(410, "Resource has been deleted", "deleted")
            ev["status"] = "cancelled"       # tombstone: id stays reserved
            return ""
        return _Request(run)


class FakeCalendars:
    def __init__(self, owner):
        self._o = owner

    def get(self, **kw):
        def run():
            self._o.record("calendars.get", kw.get("calendarId"))
            self._o.maybe_fail("calendars.get", kw.get("calendarId"))
            return {"id": kw.get("calendarId"), "summary": "fake"}
        return _Request(run)

    def insert(self, **kw):
        def run():
            self._o.record("calendars.insert", None)
            self._o.maybe_fail("calendars.insert", None)
            return {"id": "created-calendar-id"}
        return _Request(run)


class FakeCalendarService:
    """Drop-in for the object returned by ``build("calendar", "v3", ...)``.

    fail_plan maps (method, event_id) -> list of exceptions to raise on successive calls.
    Use event_id=None to match any id for that method. An entry that runs out of exceptions
    falls through to normal behaviour, which is how you test "fails twice then succeeds".
    """

    def __init__(self, events=None, fail_plan=None):
        self.store: dict[str, dict] = {e["id"]: dict(e) for e in (events or [])}
        for e in self.store.values():
            e.setdefault("status", "confirmed")
        self.fail_plan = fail_plan or {}
        self.calls: list[tuple[str, str | None]] = []
        self._events = FakeEvents(self)
        self._calendars = FakeCalendars(self)

    def events(self):
        return self._events

    def calendars(self):
        return self._calendars

    # -- helpers -------------------------------------------------------------
    def record(self, method, eid):
        self.calls.append((method, eid))

    def maybe_fail(self, method, eid):
        for key in ((method, eid), (method, None)):
            queue = self.fail_plan.get(key)
            if queue:
                raise queue.pop(0)

    def live(self) -> dict[str, dict]:
        return {k: v for k, v in self.store.items() if v.get("status") != "cancelled"}

    def tombstones(self) -> set[str]:
        return {k for k, v in self.store.items() if v.get("status") == "cancelled"}

    def methods(self, name: str) -> list[str | None]:
        return [eid for m, eid in self.calls if m == name]
