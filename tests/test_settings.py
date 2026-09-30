# -*- coding: utf-8 -*-
"""Row model / settings persistence tests (Kmong customer 5352288)."""
import json

import pytest

from foretforet import config, engine


def test_default_rows_are_the_six_customer_picks():
    rows = config.default_settings()["rows"]
    assert [(r["option"], r["qty"]) for r in rows] == [
        ("CBK,9_12M", 1), ("CBK,1_2Y", 1), ("MOB,9_12M", 1),
        ("CBW,1_2Y", 2), ("CBW,2_3Y", 1), ("CBB,1_2Y", 1)]
    assert all(r["enabled"] for r in rows)


def test_normalize_rows_coerces_qty_and_enabled():
    rows = config.normalize_rows([
        {"url": " u1 ", "option": "A", "qty": "3"},
        {"url": "u2", "option": "B", "qty": "", "enabled": False},
        {"url": "u3", "option": "C", "qty": 500},
        "junk",
    ])
    assert rows[0] == {"url": "u1", "option": "A", "qty": 3, "enabled": True}
    assert rows[1]["qty"] == 0 and rows[1]["enabled"] is False
    assert rows[2]["qty"] == 99
    assert len(rows) == 3


def test_unchecked_or_zero_qty_is_skipped_but_kept():
    rows = [dict(r) for r in config.DEFAULT_ROWS]
    rows[1]["enabled"] = False
    rows[4]["qty"] = 0
    act = config.active_rows(rows)
    assert len(act) == 4
    assert rows[1]["url"] and rows[1]["option"] == "CBK,1_2Y"
    groups = engine.group_rows(rows)
    nums = sorted(w.row_no for p in groups for w in p.wants)
    assert nums == [1, 3, 4, 6]
    # the two rows of 10279528 group into one product, only row 1 active
    p = [g for g in groups if g.branduid == "10279528"][0]
    assert [w.wanted for w in p.wants] == ["CBK,9_12M"]


def test_parse_open_at_is_kst():
    t = engine.parse_open_at("2026-10-01 10:00:00")
    assert t == 1790816400.0  # 2026-10-01T01:00:00Z
    assert engine.parse_open_at("2026-10-01 10:00") == t
    with pytest.raises(ValueError):
        engine.parse_open_at("tomorrow")


def test_password_never_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "settings_path", lambda: tmp_path / "settings.json")
    s = config.default_settings()
    s["login_id"] = "someone"
    s["login_pw"] = "hunter2-secret"
    config.save_settings(s)
    raw = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert "hunter2-secret" not in raw and "login_pw" not in raw
    assert json.loads(raw)["login_id"] == "someone"
    s["remember_id"] = False
    config.save_settings(s)
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["login_id"] == ""
    # load drops any stale login_pw key
    (tmp_path / "settings.json").write_text(json.dumps({"login_pw": "x", "rows": []}), encoding="utf-8")
    loaded = config.load_settings()
    assert "login_pw" not in loaded and loaded["rows"] == []


def test_shipped_defaults_have_no_credentials():
    s = config.default_settings()
    assert s["login_id"] == "" and "login_pw" not in s
