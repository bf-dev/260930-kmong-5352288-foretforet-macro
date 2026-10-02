# -*- coding: utf-8 -*-
"""Settings and constants for the foretforet purchase macro (Kmong customer 5352288, order 7643217)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_VERSION = "1.0.4"
APP_SLUG = "foretforet-macro"
APP_TITLE = "포레포레 오픈 구매 매크로"
CUSTOMER_ID = "5352288"
ORDER_ID = 7643217
WORKS_API = "https://works.insu.ng/works/api"
ARTIFACT_SOURCE = "foretforet-macro-run"
VERSION_URL = "https://static.neoworks.us/5352288/version-foretforet.json"
BASE_URL = "https://www.foretforet.com"

DEFAULT_OPEN_AT = "2026-10-01 10:00:00"

# Default rows requested by the customer (2026-09-30). Option strings are the
# exact site values; the matcher also accepts free text like "cbk 9-12m".
DEFAULT_ROWS = [
    {"url": "https://foretforet.com/m/product.html?branduid=10279528", "option": "CBK,9_12M", "qty": 1, "enabled": True},
    {"url": "https://foretforet.com/m/product.html?branduid=10279528", "option": "CBK,1_2Y", "qty": 1, "enabled": True},
    {"url": "https://foretforet.com/m/product.html?branduid=10279589", "option": "MOB,9_12M", "qty": 1, "enabled": True},
    {"url": "https://foretforet.com/m/product.html?branduid=10279533", "option": "CBW,1_2Y", "qty": 2, "enabled": True},
    {"url": "https://foretforet.com/m/product.html?branduid=10279540", "option": "CBW,2_3Y", "qty": 1, "enabled": True},
    {"url": "https://foretforet.com/m/product.html?branduid=10240351", "option": "CBB,1_2Y", "qty": 1, "enabled": True},
]

LOGIN_TYPES = ["네이버", "카카오", "자체"]
DEFAULT_LOGIN_TYPE = "네이버"

# Timing
PRELOAD_SECONDS = 180          # log in / load product pages this long before open
OPEN_POLL_MS = 500             # pause between reloads while the product is still closed (after open time)
OPEN_WAIT_MAX_SECONDS = 900    # stop polling for "open" after this long past the open time (15 min)
HIDDEN_POLL_MS = 400           # re-check interval while the product page is the "존재하지 않는 상품" alert stub
PRE_FIRE_RELOAD_MS = 150       # reload this many ms after open time on the server clock
# 1.0.4: after the open, a product still hidden / sold-out-caution this long is skipped
SOLDOUT_GRACE_SECONDS = 30


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def log_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    p = Path(base) / "ForetforetMacro"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


def profile_dir() -> Path:
    p = log_dir() / "browser-profile"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


def settings_path() -> Path:
    return log_dir() / "settings.json"


def default_settings() -> dict:
    return {
        "rows": [dict(r) for r in DEFAULT_ROWS],
        "open_at": DEFAULT_OPEN_AT,
        "login_type": DEFAULT_LOGIN_TYPE,
        "login_id": "",
        "pay_method": "KAKAOPAY", "auto_pay_click": True,
        "remember_id": True,
    }


def load_settings() -> dict:
    s = default_settings()
    try:
        p = settings_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k in s:
                    if k in data:
                        s[k] = data[k]
    except Exception:
        pass
    s.pop("login_pw", None)
    s["rows"] = normalize_rows(s.get("rows"))
    return s


def _to_qty(v) -> int:
    try:
        return max(0, min(99, int(str(v).strip() or 0)))
    except Exception:
        return 1


def normalize_rows(rows) -> list[dict]:
    """Coerce saved rows into {url, option, qty, enabled}. Old files without
    'enabled' load as enabled; qty is an int in 0..99."""
    out: list[dict] = []
    if not isinstance(rows, list):
        return [dict(r) for r in DEFAULT_ROWS]
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append({
            "url": str(r.get("url") or "").strip(),
            "option": str(r.get("option") or "").strip(),
            "qty": _to_qty(r.get("qty", 1)),
            "enabled": bool(r.get("enabled", True)),
        })
    return out


def active_rows(rows) -> list[dict]:
    """Rows the macro actually buys: checked, qty > 0 and a URL. Unchecked or
    qty 0 rows are skipped but stay in the settings with their URL/option."""
    return [r for r in normalize_rows(rows) if r["enabled"] and r["qty"] > 0 and r["url"]]


def save_settings(s: dict) -> None:
    """Persist settings. The password is never written to disk."""
    try:
        out = {k: v for k, v in s.items() if k != "login_pw"}
        if not out.get("remember_id", True):
            out["login_id"] = ""
        settings_path().write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
