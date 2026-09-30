# -*- coding: utf-8 -*-
from pathlib import Path

import pytest

from foretforet import config, parser

FX = Path(__file__).parent / "fixtures"


def load(name):
    return (FX / name).read_text(encoding="utf-8", errors="replace")


def opts(bid):
    return parser.parse_options(load(f"c_{bid}.html"))


def test_branduid_both_url_forms():
    assert parser.branduid_of("https://foretforet.com/m/product.html?branduid=10279528") == "10279528"
    assert parser.branduid_of("https://www.foretforet.com/shop/shopdetail.html?branduid=10279528&xcode=001") == "10279528"
    assert parser.branduid_of(" 10279528 ") == "10279528"
    assert parser.branduid_of("https://foretforet.com/") is None


def test_options_and_stock_10279528():
    o = opts("10279528")
    assert [(x.value, x.text, x.stock, x.state) for x in o] == [
        ("0", "CBK,9_12M", 2, "SALE"), ("1", "CBK,1_2Y", 5, "SALE")]


def test_mobile_page_same_options():
    m = parser.parse_options(load("cm_10279528.html"))
    assert [x.text for x in m] == ["CBK,9_12M", "CBK,1_2Y"]


def test_preview_is_closed_and_sale_is_open():
    assert parser.is_open(load("c_10279528.html")) is False
    assert parser.is_open(load("cm_10279528.html")) is False
    assert parser.is_open(load("p_10254531.html")) is True


@pytest.mark.parametrize("bid,typed,expect", [
    ("10279528", "cbk 9-12m", "CBK,9_12M"),
    ("10279528", "cbk 1-2y", "CBK,1_2Y"),
    ("10279528", "CBK,9_12M", "CBK,9_12M"),
    ("10279589", "cbk 9-12m", "MOB,9_12M"),   # size-first: colour missing, single colour
    ("10279589", "MOB,9_12M", "MOB,9_12M"),
    ("10279533", "cbw_1-2y", "CBW,1_2Y"),
    ("10279540", "cbw_2-3y", "CBW,2_3Y"),
    ("10279540", "2-3y", "CBW,2_3Y"),
    ("10240351", "cbb_1-2y", "CBB,1_2Y"),
])
def test_match(bid, typed, expect):
    o, note = parser.match_option(typed, opts(bid))
    assert o is not None, note
    assert o.text == expect


def test_missing_size_reports():
    o, note = parser.match_option("cbk 3-4y", opts("10279528"))
    assert o is None and "없음" in note


def test_default_rows_resolve_with_stock():
    for row in config.DEFAULT_ROWS:
        bid = parser.branduid_of(row["url"])
        o, note = parser.match_option(row["option"], opts(bid))
        assert o is not None and o.buyable, (bid, row, note)


def test_settings_never_store_password(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    s = config.default_settings()
    s["login_id"] = "someone"
    s["login_pw"] = "secret-value"
    config.save_settings(s)
    raw = (tmp_path / "ForetforetMacro" / "settings.json").read_text(encoding="utf-8")
    assert "secret-value" not in raw
    assert "login_pw" not in config.load_settings()


def test_shipped_defaults_have_empty_credentials():
    s = config.default_settings()
    assert s["login_id"] == "" and "login_pw" not in s
