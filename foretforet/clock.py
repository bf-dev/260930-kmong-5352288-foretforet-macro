# -*- coding: utf-8 -*-
"""Server clock sync from the HTTP Date header (foretforet.com, customer 5352288).

The Date header only has 1 second resolution. We poll HEAD until the second
ticks over; the moment it changes, the server clock is at .000 of the new
second, give or take half the round trip. That gets the offset to ~RTT/2
(typically 20-60 ms from Korea) instead of +-500 ms.
"""
from __future__ import annotations

import time
from email.utils import parsedate_to_datetime

from . import config

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"}


def _probe(session, url: str) -> tuple[float, float, float] | None:
    """(t_send, t_recv, server_epoch_seconds) or None."""
    t0 = time.time()
    try:
        r = session.head(url, timeout=5, allow_redirects=False, headers=_HEADERS)
    except Exception:
        return None
    t1 = time.time()
    d = r.headers.get("Date")
    if not d:
        return None
    try:
        return t0, t1, parsedate_to_datetime(d).timestamp()
    except Exception:
        return None


def measure_offset(url: str | None = None, budget_s: float = 4.0, log=lambda *_: None) -> dict:
    """Return {"offsetMs": server - local, "rttMs", "method"}. Never raises."""
    import requests
    url = url or (config.BASE_URL + "/")
    s = requests.Session()
    first = _probe(s, url)
    if first is None:
        log("서버 시각 측정 실패: 로컬 시계를 그대로 씁니다")
        return {"offsetMs": 0.0, "rttMs": None, "method": "local"}
    t0, t1, srv = first
    rough = (srv + 0.5) - (t0 + t1) / 2
    best = {"offsetMs": rough * 1000, "rttMs": (t1 - t0) * 1000, "method": "date-rough"}
    prev = srv
    deadline = time.time() + budget_s
    while time.time() < deadline:
        p = _probe(s, url)
        if p is None:
            continue
        a, b, sv = p
        if sv != prev:
            # the second ticked between our last sample and this one: sv.000 happened
            # somewhere inside (a, b); assume the middle.
            off = sv - (a + b) / 2
            best = {"offsetMs": off * 1000, "rttMs": (b - a) * 1000, "method": "date-edge"}
            break
        prev = sv
        # sleep so the next probe lands just after the predicted tick
        predicted_tick = (sv + 1) - rough
        wait = predicted_tick - time.time() - (b - a) / 2
        if 0 < wait < 1.0:
            time.sleep(max(0.0, wait - 0.03))
    log(f"서버 시각 동기화: 오차 {best['offsetMs']:+.0f}ms (왕복 {best['rttMs']:.0f}ms, {best['method']})")
    return best


class Clock:
    def __init__(self) -> None:
        self.offset_ms = 0.0
        self.info: dict = {"offsetMs": 0.0, "method": "local"}

    def sync(self, log=lambda *_: None) -> dict:
        self.info = measure_offset(log=log)
        self.offset_ms = float(self.info.get("offsetMs") or 0.0)
        return self.info

    def now(self) -> float:
        """Server epoch seconds."""
        return time.time() + self.offset_ms / 1000.0
