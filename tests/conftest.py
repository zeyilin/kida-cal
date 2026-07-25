"""Shared test setup.

Tests construct their Config in-process rather than loading ../config.yaml. Coupling the
suite to the deployed config meant a routine config edit could turn tests red (or, worse,
green for the wrong reason) with no code change at all.
"""
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.fetch_availability import Config


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Hard-block outbound sockets for the whole suite.

    The salon's booking funnel rate-limits under repeated hits, and CI runs this on every
    push — a test that quietly reaches book.gettimely.com or kidanyc.com would be both
    flaky and rude. Anything needing a response must use a fake or a fixture.
    """
    def refuse(*a, **kw):
        raise AssertionError(
            "tests must not open network connections — use a fake client or a fixture")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(autouse=True)
def _stub_notices(monkeypatch):
    """The notices banner is scraped from the live site; stub it by default. Tests that
    care about None-vs-"" semantics set it explicitly."""
    monkeypatch.setattr("src.fetch_availability.fetch_notices", lambda *a, **kw: "")


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    """Never let a developer's real credentials or overrides leak into a test."""
    for var in ("KIDA_CALENDAR_ID", "KIDA_SERVICE_ACCOUNT_JSON", "KIDA_LOOKAHEAD_DAYS",
                "KIDA_CACHE_TTL_SECONDS", "KIDA_GOOGLE_CLIENT_SECRET",
                "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT"):
        monkeypatch.delenv(var, raising=False)


def mkconfig(**overrides) -> Config:
    """A Config for tests. event_style defaults to whatever PRODUCTION ships, so guards
    calibrated as ratios are exercised against the real denominator — a suite pinned to
    'slots' while production ran 'blocks' would have validated the wrong arithmetic."""
    cfg = Config()
    cfg.calendar_id = "cal-under-test"
    cfg.cache_ttl_seconds = 0
    cfg.request_delay_seconds = 0.0
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture
def config():
    return mkconfig()
