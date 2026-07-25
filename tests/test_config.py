"""Tests for config loading and validation.

Config used to blanket-setattr every YAML key onto the object, so a typo like
`calender_id` silently attached a useless attribute — and ensure_calendar then fell
through to its find-or-create branch. Several fields also lacked annotations, making them
class attributes rather than dataclass fields: excluded from __repr__/asdict and shared
across instances, i.e. invisible in exactly the debug output where a misconfiguration
would have shown up.
"""
import dataclasses

import pytest
import yaml

from src.fetch_availability import Config

MINIMAL = {"timezone": "America/New_York", "lookahead_days": 30}


def _write(tmp_path, data):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(p)


def test_every_setting_is_a_real_dataclass_field():
    fields = {f.name for f in dataclasses.fields(Config)}
    for name in ("stylists", "services", "min_slot_hour", "calendar_id", "event_style",
                 "near_lookahead_days", "deep_sweep_every_hours"):
        assert name in fields, f"{name} is a class attribute, not a dataclass field"


def test_instances_do_not_share_mutable_state():
    a, b = Config(), Config()
    a.stylists = ["Nao"]
    assert b.stylists == "all"


def test_describe_mentions_the_settings_that_change_behaviour(capsys):
    text = Config().describe()
    for token in ("style=", "deep=", "near=", "tz=", "stylists=", "cap="):
        assert token in text


def test_unknown_key_warns_and_is_ignored(tmp_path, capsys):
    path = _write(tmp_path, dict(MINIMAL, calender_id="typo-not-a-real-key"))
    cfg = Config.load(path)
    assert not hasattr(cfg, "calender_id")
    assert cfg.calendar_id is None
    assert "unknown config key 'calender_id'" in capsys.readouterr().err


def test_known_keys_still_load(tmp_path):
    path = _write(tmp_path, dict(MINIMAL, event_style="slots", stylists=["Nao"],
                                 min_slot_hour=18, calendar_id="abc"))
    cfg = Config.load(path)
    assert (cfg.event_style, cfg.stylists, cfg.min_slot_hour, cfg.calendar_id) == \
        ("slots", ["Nao"], 18, "abc")


@pytest.mark.parametrize("bad", [
    {"event_style": "chunks"},
    {"lookahead_days": 0},
    {"lookahead_days": -5},
    {"lookahead_days": "ninety"},
    {"near_lookahead_days": 0},
    {"deep_sweep_every_hours": 0},
    {"max_requests_per_run": 0},
    {"min_slot_hour": 99},
    {"request_delay_seconds": -1},
    {"timezone": "Mars/Olympus_Mons"},
])
def test_invalid_values_fail_loudly_at_startup(tmp_path, bad):
    path = _write(tmp_path, dict(MINIMAL, **bad))
    with pytest.raises(SystemExit):
        Config.load(path)


def test_booleans_are_not_accepted_as_integers(tmp_path):
    """bool is a subclass of int; `lookahead_days: true` must not become 1 day."""
    path = _write(tmp_path, dict(MINIMAL, lookahead_days=True))
    with pytest.raises(SystemExit):
        Config.load(path)


def test_env_overrides_win_over_the_file(tmp_path, monkeypatch):
    path = _write(tmp_path, dict(MINIMAL, lookahead_days=90, cache_ttl_seconds=900))
    monkeypatch.setenv("KIDA_LOOKAHEAD_DAYS", "21")
    monkeypatch.setenv("KIDA_CACHE_TTL_SECONDS", "0")
    cfg = Config.load(path)
    assert cfg.lookahead_days == 21
    assert cfg.cache_ttl_seconds == 0


def test_deployed_config_is_valid():
    """The real config.yaml must load — this is the one place the suite is allowed to
    depend on it, precisely because shipping an invalid one breaks production."""
    cfg = Config.load("config.yaml")
    assert cfg.event_style in ("blocks", "slots")
    assert cfg.near_lookahead_days < cfg.lookahead_days
