# -*- coding: utf-8 -*-
"""1.0.5/1.0.6 drop-time tests (Kmong customer 5352288), fully offline.

Every request from the test browser goes through one Playwright route: product
pages are served from tests/fixtures (c_* = not-yet-open caution, p_*/f_* = on
sale, s_* = sold-out page, n_* = the shop's real "does not exist" answers),
basket.action is answered by a local stub with per-product delays so the replies
cross, and everything else is aborted. The raw-GET poll is monkeypatched to the
same bytes. Nothing reaches foretforet.com, nothing logs in, nothing pays.

Covers:
- open all, check once, cart once: a missing size or a sold-out option finishes the
  row at once, a sold-out page is final, nothing is ever posted twice
- checkbox "재고 있는 옵션 전부 담기": every buyable option, row qty cut to the site cap
- 1.0.6 stock shortfall: a qty > 1 row that the shop refuses is retried once with
  qty 1; qty 1 failing, or a sold-out answer, skips the row; no other retries
- page does not exist: final "상품 없음" with the checkbox, retried until it opens without
- NetFunnel queue: waited out, never reloaded
- checkout every 3 finished rows (on_batch), the last group may be smaller
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
from foretforet.engine import TRACKER_STUBS, Engine, blocked, cap_qty, group_rows

FX = Path(__file__).parent / "fixtures"
OPEN = (FX / "p_10254531.html").read_bytes()          # RLL 9_12M(4) 12_18M(7) 18_24M SOLDOUT 2_3Y(10) 3_4Y(2) 4_5Y(4), cap 3
OPEN_1LEFT = (FX / "f_10279535_open.html").read_bytes()  # 17:00 drop: only CBW,4_5Y SALE (stock 1), cap 2
CAUTION = (FX / "c_10279533.html").read_bytes()       # product_caution
SOLDOUT = (FX / "s_10279540_soldout.html").read_bytes()  # soldout_area, every option SOLDOUT
STUB_NOTEXIST = (FX / "n_notexist_stub.html").read_bytes()  # HTTP 200, alert + redirect home
PAGE_404 = (FX / "n_makeshop404.html").read_bytes()   # HTTP 404, Makeshop error page

# Stand-ins for the shop's external basket_send.js / multi_option.js (aborted
# offline). change_option behaves like the real one: a SOLDOUT option raises an
# alert and adds nothing, any other option adds an MS_amount_basic_N input.
# send_multi calls the (blocked) Kakao pixel unguarded, then posts basket.action for
# this page's branduid with options and amounts.
STUB = """<script>
window.change_option = function (sel, kind) {
  var o = sel.options[sel.selectedIndex];
  if (o && o.getAttribute('sto_state') === 'SOLDOUT') { alert('품절된 옵션입니다.'); return; }
  var box = document.getElementById('__picked') || (function () {
    var d = document.createElement('div'); d.id = '__picked'; document.body.appendChild(d); return d; })();
  var i = box.children.length;
  var e = document.createElement('input'); e.id = 'MS_amount_basic_' + i; e.value = '1';
  e.setAttribute('data-opt', o ? o.getAttribute('title') : sel.value); box.appendChild(e);
};
window.set_amount = function () {};
window.send_multi = function () {
  var bu = new URLSearchParams(location.search).get('branduid');
  // like the real multi_option.js: insert_kakao_pixel_basket() runs unguarded
  // before the post, and kp.js is blocked on product tabs
  kakaoPixel('3628262915593974979').addToCart({id: bu, tag: 'basket'});
  var parts = Array.prototype.map.call(document.querySelectorAll('#__picked input'),
    function (e) { return e.getAttribute('data-opt') + 'x' + e.value; });
  var x = new XMLHttpRequest();
  x.open('POST', '/shop/basket.action', true);
  x.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
  x.send('branduid=' + bu + '&picks=' + encodeURIComponent(parts.join(';')));
};
</script>"""

NETFUNNEL = b"""<div id="NetFunnel_Loading_Popup" style="display:block;position:fixed">
<span id="NetFunnel_Loading_Popup_Count">1532</span><span id="NetFunnel_Loading_Popup_NextCnt">88</span>
<span id="NetFunnel_Loading_Popup_TimeLeft">00:00:02</span></div>
<script>setTimeout(function () { document.getElementById('NetFunnel_Loading_Popup').style.display = 'none'; }, 1500);</script>"""


class _Diag:
    def log(self, *_):
        pass

    def add_page(self, *_):
        pass

    def add_response(self, *_):
        pass


def _row(bu, opt, qty=1, all_stock=False):
    return {"url": f"https://foretforet.com/shop/shopdetail.html?branduid={bu}",
            "option": opt, "qty": qty, "enabled": True, "all_stock": all_stock}


def _run(rows, pages, basket, hidden=None, batch=None, slow=None):
    """pages: branduid -> fixture bytes, or "404" for the Makeshop 404 page.
    basket: branduid -> (delay_s, status, message), or a callable
            (picks, n_posts_for_this_branduid) -> that tuple.
    hidden: branduid -> seconds after start until the "does not exist" stub turns
            into the real page (None = forever).
    Returns a dict with engine, products, logs, posts, page loads, closed times,
    batches handed to on_batch, main tab state."""
    pw_api = pytest.importorskip("playwright.async_api")
    hidden = hidden or {}
    slow = slow or {}
    out = {"logs": [], "posts": [], "loads": {}, "closed": {}, "batches": []}

    async def go():
        async with pw_api.async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except Exception as exc:  # no browser on this machine
                pytest.skip(f"no chromium: {exc}")
            eng = Engine(config.default_settings(), "", _Diag(), log=out["logs"].append, headless=True)
            eng.ctx = await browser.new_context()
            t_start = time.time()

            def answer(bu):
                """(status, body) the shop gives for this product right now."""
                if bu in hidden and (hidden[bu] is None or time.time() - t_start < hidden[bu]):
                    return 200, STUB_NOTEXIST
                if pages[bu] == "404":
                    return 404, PAGE_404
                return 200, pages[bu]

            async def handler(route):
                req = route.request
                url = req.url
                if "basket.action" in url:
                    q = parse_qs(req.post_data or "")
                    bu = q.get("branduid", [""])[0]
                    picks = q.get("picks", [""])[0]
                    out["posts"].append({"branduid": bu, "picks": picks, "at": time.time()})
                    v = basket[bu]
                    if callable(v):
                        v = v(picks, sum(1 for x in out["posts"] if x["branduid"] == bu))
                    delay, ok, msg = v
                    await asyncio.sleep(delay)
                    body = {"status": ok, "message": msg,
                            "etc_data": {"basket_uid_array": [f"uid-{bu}"] if ok else []}}
                    await route.fulfill(status=200, content_type="application/json",
                                        body=json.dumps(body, ensure_ascii=False))
                    return
                if "shopdetail.html" in url and req.resource_type == "document":
                    bu = parser.branduid_of(url)
                    out["loads"][bu] = out["loads"].get(bu, 0) + 1
                    if slow.get(bu):
                        await asyncio.sleep(slow[bu])
                    status, html = answer(bu)
                    if status == 200 and b"optionlist" in html:
                        html = html.replace(b"</body>", STUB.encode() + b"</body>")
                    await route.fulfill(status=status, content_type="text/html; charset=utf-8", body=html)
                    return
                await route.abort()

            await eng.ctx.route("**/*", handler)

            async def fake_fetch(prod):
                status, body = answer(prod.branduid)
                eng._last_status[prod.branduid] = status
                return body
            eng._fetch_product = fake_fetch

            main = await eng._new_page()      # stands in for the order tab
            products = group_rows(rows)
            for p in products:
                p.title = f"상품 {p.branduid}"
                p.page = await eng._new_page(product=True)   # same light route as the real run
                p.page.on("close", lambda _pg, bu=p.branduid: out["closed"].setdefault(bu, time.time()))

            async def on_batch(k, group):
                out["batches"].append({"k": k, "at": time.time(), "bus": [p.branduid for p in group],
                                       "carted": [p.branduid for p in group if p.added]})
                if not any(p.added for p in group):
                    await eng.checkout_group(k, group, main, click=False)   # skip path, no page use

            out["t0"] = time.time()
            await eng.fire_all(products, batch=batch, on_batch=on_batch)
            out["main_open"] = not main.is_closed() and main in eng.ctx.pages
            out["n_pages"] = len(eng.ctx.pages)
            await browser.close()
            out["eng"] = eng
            out["prods"] = {p.branduid: p for p in products}

    asyncio.run(go())
    return out


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(config, "OPEN_POLL_MS", 100)
    monkeypatch.setattr(config, "HIDDEN_POLL_MS", 100)


# ------------------------------------------------------------------ pure helpers
def test_blocked_resources_but_never_netfunnel():
    assert blocked("image", "https://www.foretforet.com/shopimages/x.jpg")
    assert blocked("font", "https://cdn.example/f.woff2")
    assert blocked("script", "https://www.googletagmanager.com/gtm.js")
    assert blocked("script", "https://wcs.naver.net/wcslog.js")
    assert not blocked("script", "https://www.foretforet.com/shop/js/basket_send.js")
    assert not blocked("document", "https://www.foretforet.com/shop/shopdetail.html?branduid=1")
    assert not blocked("script", "https://nf.foretforet.com/js/netfunnel.js")
    assert not blocked("image", "https://nf.example/netfunnel/loading.gif")


def test_tracker_stubs_cover_the_shop_calls():
    """kp.js / fbevents / Kakao SDK are blocked on product tabs; the shop's inline
    code and send_multi still call them. Without stand-ins send_multi throws
    "kakaoPixel is not defined" before posting (live 2026-10-02, 0 carted)."""
    pw_api = pytest.importorskip("playwright.async_api")

    async def go():
        async with pw_api.async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except Exception as exc:
                pytest.skip(f"no chromium: {exc}")
            page = await browser.new_page()
            await page.add_init_script(TRACKER_STUBS)
            await page.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html",
                                                          body="<p>x</p>"))
            await page.goto("https://www.foretforet.com/shop/shopdetail.html?branduid=1")
            res = await page.evaluate("""() => {
                kakaoPixel.setServiceOrigin('20003');
                kakaoPixel('3628262915593974979').pageView();
                kakaoPixel('3628262915593974979').addToCart({id: '1', tag: 'basket'});
                fbq('track', 'AddToCart'); Kakao.init('k'); ChannelIO('boot', {});
                return [typeof kakaoPixel, String(window.kakaoPixel === undefined)];
            }""")
            await browser.close()
            return res
    assert asyncio.run(go()) == ["function", "false"]


def test_cap_qty_site_cap_only_never_stock():
    o = parser.Option("0", "RLL,9_12M", 4, "SALE")
    assert cap_qty(5, 3, o) == 3          # site per-option cap
    assert cap_qty(2, 3, o) == 2
    assert cap_qty(9, None, o) == 9       # 1.0.6: stock is never weighed up front
    assert cap_qty(2, None, parser.Option("0", "RLL,3_4Y", 1, "SALE")) == 2
    assert cap_qty(9, None, parser.Option("1", "X", None, "SALE", unlimited=True)) == 9


def test_not_exist_detection_from_real_pages():
    assert parser.is_not_exist(STUB_NOTEXIST, 200)
    assert parser.is_not_exist(PAGE_404, 404)
    assert parser.is_not_exist(PAGE_404, None)          # body alone is enough
    for real in (OPEN, OPEN_1LEFT, CAUTION, SOLDOUT):
        assert not parser.is_not_exist(real, 200)
    assert parser.is_soldout_page(SOLDOUT.decode("utf-8", "replace"))
    assert not parser.is_open(SOLDOUT.decode("utf-8", "replace"))


def test_checkbox_and_batch_persist(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    s = config.default_settings()
    assert s["checkout_batch"] == 3
    s["rows"] = [_row("1001", "", 2, all_stock=True), _row("1002", "RLL,2_3Y")]
    s["checkout_batch"] = 4
    config.save_settings(s)
    back = config.load_settings()
    assert [r["all_stock"] for r in back["rows"]] == [True, False]
    assert back["checkout_batch"] == 4
    # old 1.0.4 files: no all_stock, no checkout_batch
    (tmp_path / "ForetforetMacro" / "settings.json").write_text(
        json.dumps({"rows": [{"url": "u", "option": "o", "qty": 1}]}), encoding="utf-8")
    old = config.load_settings()
    assert old["rows"][0]["all_stock"] is False and old["checkout_batch"] == 3
    assert config.checkout_batch({"checkout_batch": 99}) == config.CHECKOUT_BATCH_MAX
    assert config.checkout_batch({"checkout_batch": "x"}) == 3


# ------------------------------------------------------------------ fire flow
def test_17h_drop_manual_rows_finish_at_once_and_cart_the_one_left(fast):
    """The 2026-10-02 17:00 취소분 shape: registered sizes are sold out or absent,
    one product is a sold-out page. 1.0.4 reloaded and retried these for minutes."""
    rows = [_row("1001", "CBW,2_3Y"),            # registered but SOLDOUT option
            _row("1002", "CBW,1_2Y"),            # size not on the page
            _row("10279540", "CBW,2_3Y"),        # sold-out page
            _row("1003", "CBW,4_5Y")]            # the one option still SALE
    pages = {"1001": OPEN_1LEFT, "1002": OPEN_1LEFT, "10279540": SOLDOUT, "1003": OPEN_1LEFT}
    r = _run(rows, pages, {"1003": (0.05, True, "")})
    p, logs = r["prods"], r["logs"]
    assert p["1003"].added and p["1003"].picked == [("CBW,4_5Y", 1)]
    assert [x["branduid"] for x in r["posts"]] == ["1003"]
    assert any("CBW,2_3Y 품절 (SOLDOUT" in m for m in logs), logs
    assert any("옵션 없음, 바로 건너뜀" in m for m in logs), logs
    assert p["10279540"].message == "품절" and any(m.startswith("품절: 상품 10279540") for m in logs)
    # every product page loaded exactly once, everything done in a few seconds
    assert r["loads"] == {"1001": 1, "1002": 1, "10279540": 1, "1003": 1}
    assert max(r["closed"].values()) - r["t0"] < 5.0
    # SOLDOUT option was never selected (the real site alerts and adds nothing)
    assert not any("품절된 옵션" in d for d in r["eng"].dialogs)


def test_checkbox_row_carts_every_buyable_option_capped(fast):
    rows = [_row("1001", "", 5, all_stock=True), _row("1002", "RLL,2_3Y", 2)]
    pages = {"1001": OPEN, "1002": OPEN}
    r = _run(rows, pages, {"1001": (0.05, True, ""), "1002": (0.05, True, "")})
    p = r["prods"]
    assert p["1001"].added and p["1002"].added
    # 18_24M SOLDOUT left out; qty 5 cut to the site cap 3 (1.0.6: not to the stock)
    assert p["1001"].picked == [("RLL,9_12M", 3), ("RLL,12_18M", 3), ("RLL,2_3Y", 3),
                                ("RLL,3_4Y", 3), ("RLL,4_5Y", 3)]
    # manual row unchanged: the typed option with the typed qty
    assert p["1002"].picked == [("RLL,2_3Y", 2)]
    by = {x["branduid"]: x["picks"] for x in r["posts"]}
    assert by["1001"] == "RLL,9_12Mx3;RLL,12_18Mx3;RLL,2_3Yx3;RLL,3_4Yx3;RLL,4_5Yx3"
    assert len(r["posts"]) == 2
    assert any("재고 있는 옵션 전부 담기: 구매 가능 5/6개" in m for m in r["logs"])


def test_not_exist_is_final_with_checkbox(fast):
    rows = [_row("99999999", "", 1, all_stock=True), _row("99999998", "", 1, all_stock=True),
            _row("1001", "RLL,9_12M")]
    pages = {"99999999": OPEN, "99999998": "404", "1001": OPEN}
    r = _run(rows, pages, {"1001": (0.05, True, "")}, hidden={"99999999": None})
    p, logs = r["prods"], r["logs"]
    for bu in ("99999999", "99999998"):
        assert p[bu].gone and p[bu].dropped and p[bu].message == "상품 없음"
        assert any(m.startswith(f"상품 없음: 상품 {bu}") for m in logs), logs
        assert r["loads"][bu] == 1                      # no retry
        assert r["closed"][bu] - r["t0"] < 4.0
    assert any("존재하지 않는 상품" in d for d in r["eng"].dialogs)   # the real stub's alert
    assert p["1001"].added
    assert len(r["batches"]) == 1 and len(r["batches"][0]["bus"]) == 3   # counted as attempts


def test_not_exist_keeps_retrying_without_checkbox(fast):
    rows = [_row("1001", "RLL,9_12M")]
    r = _run(rows, {"1001": OPEN}, {"1001": (0.05, True, "")}, hidden={"1001": 2.0})
    p = r["prods"]["1001"]
    assert p.added and not p.gone
    assert r["closed"]["1001"] - r["t0"] >= 2.0
    assert any("아직 비공개" in m for m in r["logs"])
    assert any("상품 페이지 공개됨" in m for m in r["logs"])


def test_caution_dropped_after_grace_only_with_checkbox(fast, monkeypatch):
    monkeypatch.setattr(config, "SOLDOUT_GRACE_SECONDS", 1.0)
    monkeypatch.setattr(config, "OPEN_WAIT_MAX_SECONDS", 3.0)
    rows = [_row("10279533", "", 1, all_stock=True), _row("10279534", "CBW,9_12M")]
    pages = {"10279533": CAUTION, "10279534": CAUTION}
    r = _run(rows, pages, {})
    p = r["prods"]
    assert p["10279533"].dropped and p["10279533"].message == "품절 상태로 건너뜀"
    assert 1.0 <= r["closed"]["10279533"] - r["t0"] < 2.5
    # the manual row keeps waiting for the official open until the open-wait limit
    assert not p["10279534"].dropped and not p["10279534"].added
    assert r["closed"]["10279534"] - r["t0"] >= 3.0
    assert r["loads"]["10279534"] == 1                  # polled by raw GET, tab not reloaded
    assert r["posts"] == []


def test_netfunnel_queue_is_waited_out_without_reload(fast):
    rows = [_row("1001", "RLL,9_12M")]
    page = OPEN.replace(b"</body>", NETFUNNEL + b"</body>")
    r = _run(rows, {"1001": page}, {"1001": (0.05, True, "")})
    p = r["prods"]["1001"]
    assert p.added
    assert r["loads"]["1001"] == 1
    assert p.t_open - p.t_fire >= 1.4
    assert any("접속 대기열(NetFunnel): 대기 1532명" in m for m in r["logs"]), r["logs"]


def test_checkout_every_3_finished_rows(fast):
    """7 rows: batches of 3, 3, 1 in finishing order; a group that carted nothing
    is skipped; the first checkout starts while a slow row is still loading."""
    rows = [_row("1001", "RLL,9_12M"),            # carted fast
            _row("1002", "RLL,7_8Y"),             # absent size, instant
            _row("1003", "", 1, all_stock=True),  # 상품 없음, instant
            _row("10279540", "CBW,2_3Y"),         # sold-out page
            _row("1005", "RLL,18_24M"),           # SOLDOUT option
            _row("99999998", "", 1, all_stock=True),  # 404, 상품 없음
            _row("1007", "RLL,2_3Y")]             # slow page, carted last
    pages = {"1001": OPEN, "1002": OPEN, "1003": OPEN, "10279540": SOLDOUT,
             "1005": OPEN, "99999998": "404", "1007": OPEN}
    basket = {"1001": (0.05, True, ""), "1007": (0.05, True, "")}
    r = _run(rows, pages, basket, hidden={"1003": None}, batch=3, slow={"1007": 2.5})
    b = r["batches"]
    assert [len(x["bus"]) for x in b] == [3, 3, 1], b
    assert [x["k"] for x in b] == [1, 2, 3]
    assert b[-1]["bus"] == ["1007"] and b[-1]["carted"] == ["1007"]
    assert sum(len(x["bus"]) for x in b) == 7
    # the first two groups are handed over before the slow row finishes
    assert b[0]["at"] < r["closed"]["1007"] and b[1]["at"] < r["closed"]["1007"]
    skipped = [x for x in b if not x["carted"]]
    assert skipped, b
    assert any("결제 묶음" in m and "담긴 상품 없음, 결제 건너뜀" in m for m in r["logs"])
    assert len(r["posts"]) == 2


def test_concurrent_cart_replies_go_to_their_own_tab(fast):
    # 1001 posts first but answers last with 품절; 1002 and 1003 answer first with
    # success. Pre-1.0.4 code read basket_responses[-1], so 1001 took a success
    # that was not its own.
    rows = [_row("1001", "RLL,9_12M"), _row("1002", "RLL,12_18M", 2), _row("1003", "RLL,2_3Y")]
    pages = {"1001": OPEN, "1002": OPEN, "1003": OPEN}
    basket = {"1001": (1.2, False, "선택된 상품/옵션은 품절입니다."),
              "1002": (0.1, True, ""),
              "1003": (0.4, True, "")}
    r = _run(rows, pages, basket)
    p = r["prods"]
    assert not p["1001"].added
    assert "품절" in p["1001"].message and p["1001"].cart_ids == []
    assert p["1002"].added and p["1002"].cart_ids == ["uid-1002"]
    assert p["1003"].added and p["1003"].cart_ids == ["uid-1003"]
    by = {}
    for x in r["posts"]:
        by.setdefault(x["branduid"], []).append(x["picks"])
    assert sorted(by) == ["1001", "1002", "1003"]
    assert all(len(v) == 1 for v in by.values()), by
    assert by["1002"][0].endswith("x2")
    assert len(r["eng"].basket_responses) == 3
    for x in r["eng"].basket_responses:
        assert x["page"] is not None


def test_tabs_close_as_each_product_resolves(fast, monkeypatch):
    monkeypatch.setattr(config, "SOLDOUT_GRACE_SECONDS", 6.0)
    rows = [_row("1001", "RLL,9_12M"), _row("10279533", "", 1, all_stock=True), _row("1002", "RLL,12_18M")]
    pages = {"1001": OPEN, "10279533": CAUTION, "1002": OPEN}
    basket = {"1001": (0.05, True, ""), "1002": (0.6, False, "선택된 상품/옵션은 품절입니다.")}
    r = _run(rows, pages, basket)
    c = r["closed"]
    assert c["1001"] < c["10279533"] and c["1002"] < c["10279533"]
    assert c["10279533"] - r["t0"] >= 6.0
    assert set(c) == {"1001", "10279533", "1002"}
    assert all(p.page is None for p in r["prods"].values())
    assert r["main_open"] and r["n_pages"] == 1


# ------------------------------------------------------------------ 1.0.6 qty -> 1
SHORT = "[RLL 후드]상품의 재고가 현재 1개 입니다."
SOLD = "선택된 상품/옵션은 품절입니다."


def _short_then(second):
    """First post: stock shortfall. Second post: (ok, msg) given its picks."""
    def answer(picks, n):
        if n == 1:
            return 0.05, False, SHORT
        return (0.05,) + second(picks)
    return answer


def test_short_stock_retries_once_with_qty_1(fast):
    rows = [_row("1001", "RLL,2_3Y", 2), _row("1002", "RLL,9_12M", 2)]
    basket = {"1001": _short_then(lambda picks: (picks.endswith("x1"), "" if picks.endswith("x1") else SHORT)),
              "1002": (0.05, True, "")}
    r = _run(rows, {"1001": OPEN, "1002": OPEN}, basket)
    p = r["prods"]["1001"]
    posts = [x["picks"] for x in r["posts"] if x["branduid"] == "1001"]
    assert posts == ["RLL,2_3Yx2", "RLL,2_3Yx1"]
    assert p.added and p.cart_ids == ["uid-1001"]
    assert p.picked == [("RLL,2_3Y", 1)]
    assert p.message == "재고 부족, 1개 담음"
    assert p.fallback == [{"options": ["RLL,2_3Y"], "requested": [2], "result": "ok-1"}]
    assert r["loads"]["1001"] == 2                   # one reload for the one retry
    assert any("재고 부족 -> 1개로 다시 담기: 성공" in m for m in r["logs"]), r["logs"]
    # the other row is untouched by the fallback
    q = r["prods"]["1002"]
    assert q.added and q.picked == [("RLL,9_12M", 2)] and q.fallback == [] and q.message == ""
    t = {x["branduid"]: x for x in r["eng"].timings(list(r["prods"].values()))}
    assert t["1001"]["fallback"][0]["result"] == "ok-1"


def test_short_stock_then_qty_1_fails_is_soldout_no_more_tries(fast):
    rows = [_row("1001", "RLL,2_3Y", 3)]
    r = _run(rows, {"1001": OPEN}, {"1001": _short_then(lambda picks: (False, SOLD))})
    p = r["prods"]["1001"]
    assert [x["picks"] for x in r["posts"]] == ["RLL,2_3Yx3", "RLL,2_3Yx1"]
    assert not p.added and p.dropped and "품절" in p.message
    assert p.message == "품절, 건너뜀"
    assert p.fallback[0]["result"] == "soldout"
    assert r["loads"]["1001"] == 2
    assert any("1개도 실패, 품절, 건너뜀" in m for m in r["logs"]), r["logs"]


def test_qty_1_short_or_soldout_answer_never_retries(fast):
    rows = [_row("1001", "RLL,2_3Y", 1), _row("1002", "RLL,9_12M", 2)]
    basket = {"1001": (0.05, False, SHORT), "1002": (0.05, False, SOLD)}
    r = _run(rows, {"1001": OPEN, "1002": OPEN}, basket)
    by = {}
    for x in r["posts"]:
        by.setdefault(x["branduid"], []).append(x["picks"])
    assert by == {"1001": ["RLL,2_3Yx1"], "1002": ["RLL,9_12Mx2"]}
    for bu in ("1001", "1002"):
        p = r["prods"][bu]
        assert not p.added and p.message == "품절, 건너뜀" and p.fallback == []
        assert r["loads"][bu] == 1


def test_checkbox_row_short_stock_retries_every_option_at_1(fast):
    rows = [_row("1001", "", 2, all_stock=True)]
    basket = {"1001": _short_then(lambda picks: (True, ""))}
    r = _run(rows, {"1001": OPEN}, basket)
    p = r["prods"]["1001"]
    posts = [x["picks"] for x in r["posts"]]
    assert len(posts) == 2
    assert posts[1] == "RLL,9_12Mx1;RLL,12_18Mx1;RLL,2_3Yx1;RLL,3_4Yx1;RLL,4_5Yx1"
    assert p.added and p.message == "재고 부족, 1개 담음"
    assert p.fallback[0]["result"] == "ok-1" and len(p.fallback[0]["options"]) == 5


def test_shop_clamp_to_1_on_page_is_reported(fast, monkeypatch):
    """The real multi_option.js set_amount alerts and resets the amount to 1 when
    the option has less stock than asked. Carted at 1, reported as the fallback."""
    import sys
    me = sys.modules[__name__]
    clamp = ("window.set_amount = function (inp) { if (inp.getAttribute('data-opt') === 'RLL,3_4Y' "
             "&& parseInt(inp.value, 10) > 2) { alert('선택하신 옵션의 재고가 부족합니다.'); inp.value = '1'; } };")
    monkeypatch.setattr(me, "STUB", STUB.replace("window.set_amount = function () {};", clamp))
    rows = [_row("1001", "RLL,3_4Y", 3)]
    r = _run(rows, {"1001": OPEN}, {"1001": (0.05, True, "")})
    p = r["prods"]["1001"]
    assert [x["picks"] for x in r["posts"]] == ["RLL,3_4Yx1"]
    assert p.added and p.picked == [("RLL,3_4Y", 1)] and p.message == "재고 부족, 1개 담음"
    assert p.fallback == [{"options": ["RLL,3_4Y"], "requested": [3], "result": "client-1"}]
