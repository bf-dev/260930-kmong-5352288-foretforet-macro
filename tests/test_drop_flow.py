# -*- coding: utf-8 -*-
"""1.0.4 drop-time tests (Kmong customer 5352288), fully offline.

Every request from the test browser goes through one Playwright route: product
pages are served from tests/fixtures (c_* = sold-out/stopped caution, p_* = on
sale), basket.action is answered by a local stub with per-product delays so the
replies cross, and everything else is aborted. The raw-GET poll is monkeypatched
to fixture bytes. Nothing reaches foretforet.com, nothing logs in, nothing pays.

Covers:
- a caution (or hidden) product is dropped once the grace period is over while
  the others are carted, with the Korean "품절 상태로 건너뜀: <name>" log line
- concurrent basket.action replies are attributed to their own tab
- each product tab closes as soon as that product is resolved
"""
import asyncio
import json
import time
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from foretforet import config, parser
from foretforet.engine import Engine, group_rows

FX = Path(__file__).parent / "fixtures"
OPEN = (FX / "p_10254531.html").read_bytes()        # on sale, options RLL,9_12M ...
CAUTION = (FX / "c_10279533.html").read_bytes()     # product_caution, CBW,9_12M ...
HIDDEN = "<script>alert('존재하지 않는 상품입니다.');parent.location.href='/';</script>".encode()

# Stand-ins for the shop's external basket_send.js / multi_option.js (aborted
# offline): change_option adds the MS_amount_basic_N input, send_multi posts
# basket.action for this page's branduid with the chosen options and amounts.
STUB = """<script>
window.change_option = function (sel, kind) {
  var box = document.getElementById('__picked') || (function () {
    var d = document.createElement('div'); d.id = '__picked'; document.body.appendChild(d); return d; })();
  var i = box.children.length;
  var e = document.createElement('input'); e.id = 'MS_amount_basic_' + i; e.value = '1';
  e.setAttribute('data-opt', sel.value); box.appendChild(e);
};
window.set_amount = function () {};
window.send_multi = function () {
  var bu = new URLSearchParams(location.search).get('branduid');
  var parts = Array.prototype.map.call(document.querySelectorAll('#__picked input'),
    function (e) { return e.getAttribute('data-opt') + 'x' + e.value; });
  var x = new XMLHttpRequest();
  x.open('POST', '/shop/basket.action', true);
  x.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
  x.send('branduid=' + bu + '&picks=' + encodeURIComponent(parts.join(';')));
};
</script>"""


class _Diag:
    def log(self, *_):
        pass

    def add_page(self, *_):
        pass

    def add_response(self, *_):
        pass


def _row(bu, opt, qty=1):
    return {"url": f"https://foretforet.com/shop/shopdetail.html?branduid={bu}",
            "option": opt, "qty": qty, "enabled": True}


def _run(rows, pages, basket, hidden=()):
    """pages: branduid -> fixture bytes. basket: branduid -> (delay_s, status, message).
    Returns (engine, products_by_branduid, logs, basket_posts, tab_closed_at,
    pages_left_in_context, main_page_open, t0)."""
    pw_api = pytest.importorskip("playwright.async_api")
    logs: list[str] = []
    posts: list[dict] = []
    closed_at: dict[str, float] = {}

    async def go():
        async with pw_api.async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except Exception as exc:  # no browser on this machine
                pytest.skip(f"no chromium: {exc}")
            eng = Engine(config.default_settings(), "", _Diag(), log=logs.append, headless=True)
            eng.ctx = await browser.new_context()

            async def handler(route):
                req = route.request
                url = req.url
                if "basket.action" in url:
                    q = parse_qs(req.post_data or "")
                    bu = q.get("branduid", [""])[0]
                    posts.append({"branduid": bu, "picks": q.get("picks", [""])[0], "at": time.time()})
                    delay, ok, msg = basket[bu]
                    await asyncio.sleep(delay)
                    body = {"status": ok, "message": msg,
                            "etc_data": {"basket_uid_array": [f"uid-{bu}"] if ok else []}}
                    await route.fulfill(status=200, content_type="application/json",
                                        body=json.dumps(body, ensure_ascii=False))
                    return
                if "shopdetail.html" in url:
                    bu = parser.branduid_of(url)
                    html = pages[bu].replace(b"</body>", STUB.encode() + b"</body>")
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
                    return
                await route.abort()

            await eng.ctx.route("**/*", handler)

            async def fake_fetch(prod):
                return HIDDEN if prod.branduid in hidden else pages[prod.branduid]
            eng._fetch_product = fake_fetch

            main = await eng._new_page()      # stands in for the order tab
            products = group_rows(rows)
            for p in products:
                p.title = f"상품 {p.branduid}"
                p.page = await eng._new_page()
                p.page.on("close", lambda _pg, bu=p.branduid: closed_at.setdefault(bu, time.time()))
            t0 = time.time()
            await eng.fire_all(products)
            main_open = not main.is_closed() and main in eng.ctx.pages
            n_pages = len(eng.ctx.pages)
            await browser.close()
            return eng, products, n_pages, main_open, t0

    eng, products, n_pages, main_open, t0 = asyncio.run(go())
    return eng, {p.branduid: p for p in products}, logs, posts, closed_at, n_pages, main_open, t0


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(config, "OPEN_POLL_MS", 100)
    monkeypatch.setattr(config, "HIDDEN_POLL_MS", 100)


def test_caution_product_dropped_after_grace_others_carted(fast, monkeypatch):
    monkeypatch.setattr(config, "SOLDOUT_GRACE_SECONDS", 1.5)
    rows = [_row("10279533", "CBW,9_12M"), _row("1001", "RLL,9_12M"), _row("1002", "RLL,12_18M", 2)]
    pages = {"10279533": CAUTION, "1001": OPEN, "1002": OPEN}
    basket = {"1001": (0.05, True, ""), "1002": (0.05, True, "")}
    eng, prods, logs, posts, closed, n_pages, main_open, t0 = _run(rows, pages, basket)

    soldout = prods["10279533"]
    assert soldout.dropped and not soldout.added
    assert soldout.message == "품절 상태로 건너뜀"
    assert any(m.startswith("품절 상태로 건너뜀: 상품 10279533") for m in logs), logs
    # never posted a cart request for the caution product
    assert all(p["branduid"] != "10279533" for p in posts)
    # dropped only after the grace period, not on the first caution look
    assert closed["10279533"] - t0 >= 1.5
    assert prods["1001"].added and prods["1002"].added
    assert any("담기 완료 2/3" in m for m in logs)


def test_hidden_product_dropped_after_grace(fast, monkeypatch):
    monkeypatch.setattr(config, "SOLDOUT_GRACE_SECONDS", 1.0)
    rows = [_row("10279528", "CBK,9_12M"), _row("1001", "RLL,9_12M")]
    pages = {"10279528": CAUTION, "1001": OPEN}
    eng, prods, logs, posts, closed, *_ = _run(rows, pages, {"1001": (0.05, True, "")},
                                               hidden={"10279528"})
    assert prods["10279528"].dropped and not prods["10279528"].added
    assert any(m.startswith("품절 상태로 건너뜀: ") for m in logs)
    assert prods["1001"].added


def test_concurrent_cart_replies_go_to_their_own_tab(fast):
    # 1001 posts first but answers last with 품절; 1002 and 1003 answer first with
    # success. Pre-1.0.4 code read basket_responses[-1], so 1001 took a success
    # that was not its own.
    rows = [_row("1001", "RLL,9_12M"), _row("1002", "RLL,12_18M", 2), _row("1003", "RLL,18_24M")]
    pages = {"1001": OPEN, "1002": OPEN, "1003": OPEN}
    basket = {"1001": (1.2, False, "선택된 상품/옵션은 품절입니다."),
              "1002": (0.1, True, ""),
              "1003": (0.4, True, "")}
    eng, prods, logs, posts, *_ = _run(rows, pages, basket)

    assert not prods["1001"].added
    assert "품절" in prods["1001"].message and prods["1001"].cart_ids == []
    assert prods["1002"].added and prods["1002"].cart_ids == ["uid-1002"]
    assert prods["1003"].added and prods["1003"].cart_ids == ["uid-1003"]
    # exactly one post per product: no retry triggered by someone else's reply,
    # no duplicate quantity, and 1002 asked for qty 2 once
    by = {}
    for p in posts:
        by.setdefault(p["branduid"], []).append(p["picks"])
    assert sorted(by) == ["1001", "1002", "1003"]
    assert all(len(v) == 1 for v in by.values()), by
    assert by["1002"][0].endswith("x2")
    # every reply was recorded against the tab that sent it
    assert len(eng.basket_responses) == 3
    for r in eng.basket_responses:
        assert r["page"] is not None


def test_tabs_close_as_each_product_resolves(fast, monkeypatch):
    monkeypatch.setattr(config, "SOLDOUT_GRACE_SECONDS", 6.0)
    rows = [_row("1001", "RLL,9_12M"), _row("10279533", "CBW,9_12M"), _row("1002", "RLL,12_18M")]
    pages = {"1001": OPEN, "10279533": CAUTION, "1002": OPEN}
    basket = {"1001": (0.05, True, ""), "1002": (0.6, False, "선택된 상품/옵션은 품절입니다.")}
    eng, prods, logs, posts, closed, n_pages, main_open, t0 = _run(rows, pages, basket)

    # carted tab closes well before the sold-out product's grace runs out
    assert closed["1001"] < closed["10279533"]
    assert closed["10279533"] - t0 >= 6.0
    assert closed["10279533"] - closed["1001"] > 1.0
    # 품절 reply resolves 1002 without retry and closes its tab too
    assert closed["1002"] < closed["10279533"]
    assert set(closed) == {"1001", "10279533", "1002"}
    assert all(p.page is None for p in prods.values())
    # only the main/order tab is left, the context is untouched
    assert main_open and n_pages == 1
