# -*- coding: utf-8 -*-
"""Purchase engine for foretforet.com (Makeshop), Kmong customer 5352288, order 7643217.

Flow for one run:
  1. open a persistent browser profile (Edge -> Chrome -> Chromium) so the login sticks
  2. make sure we are logged in (Naver / Kakao / own id), the customer may finish
     2FA or a captcha by hand in the visible window
  3. preload every product page, resolve each row's option on the live page
  4. sync to the server clock (HTTP Date header) and wait for the open second
  5. reload all product pages at once; while the product is still closed poll
     lightly; while a NetFunnel queue popup is up never reload, just log the count
  6. per product: select every wanted option, set its quantity, send_multi()
  7. basket: tick only the items this run added, order them, STOP on order.html
     (card payment is done by the customer; 무통장입금 auto-complete is opt-in)

Nothing here ever submits a card payment.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import config, parser
from .clock import Clock
from .masking import register_secret

KST = timezone(timedelta(hours=9))
BASE = config.BASE_URL
LOGIN_URL = BASE + "/shop/member.html?type=login"
BASKET_URL = BASE + "/shop/basket.html"
MYPAGE_URL = BASE + "/shop/mypage.html"
LOGIN_WAIT_SECONDS = 240       # time the customer has to finish 2FA / captcha by hand

_JS_NETFUNNEL = """() => {
  const p = document.getElementById('NetFunnel_Loading_Popup');
  if (!p) return null;
  const st = window.getComputedStyle(p);
  if (st.display === 'none' || st.visibility === 'hidden') return null;
  const g = id => { const e = document.getElementById(id); return e ? e.innerText.trim() : ''; };
  return {count: g('NetFunnel_Loading_Popup_Count'), next: g('NetFunnel_Loading_Popup_NextCnt'),
          left: g('NetFunnel_Loading_Popup_TimeLeft')};
}"""

_JS_PAGE_STATE = """() => {
  const top = document.querySelector('.shopdetailButtonTop');
  const html = top ? top.innerHTML : '';
  return {
    hasSelect: !!document.querySelector('select[name="optionlist[]"]'),
    caution: html.indexOf('product_caution') >= 0,
    cartBtn: html.indexOf('send_multi(') >= 0,
    sendMulti: typeof window.send_multi === 'function',
  };
}"""

_JS_BASKET = """() => Array.from(document.querySelectorAll('input[name="basketchks"]')).map((c, i) => {
  const items = document.getElementsByName('basket_item');
  let item = {};
  try { item = JSON.parse(items[i].value); } catch (e) {}
  const row = c.closest('tr') || c.closest('li') || c.parentElement;
  return {i, uid: item.uid || '', cart_id: String(item.cart_id || ''), chk: c.getAttribute('chk_data_uid') || '',
          text: row ? row.innerText.replace(/\\s+/g, ' ').trim().slice(0, 160) : ''};
})"""


def parse_open_at(text: str) -> float:
    """'2026-10-01 10:00:00' (KST, seconds optional) -> epoch seconds."""
    t = (text or "").strip().replace("T", " ").replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(t, fmt).replace(tzinfo=KST).timestamp()
        except ValueError:
            continue
    raise ValueError(f"오픈 시각 형식이 잘못됐습니다: {text!r} (예: 2026-10-01 10:00:00)")


@dataclass
class Want:
    row_no: int
    branduid: str
    wanted: str
    qty: int
    option: parser.Option | None = None
    note: str = ""


@dataclass
class Product:
    branduid: str
    wants: list[Want] = field(default_factory=list)
    page: object = None
    title: str = ""
    added: bool = False
    cart_ids: list[str] = field(default_factory=list)
    message: str = ""


def group_rows(rows: list[dict]) -> list[Product]:
    """Active rows grouped by product, row numbers are 1-based positions in the full table."""
    products: dict[str, Product] = {}
    for i, r in enumerate(config.normalize_rows(rows), start=1):
        if not (r["enabled"] and r["qty"] > 0 and r["url"]):
            continue
        bu = parser.branduid_of(r["url"])
        if not bu:
            continue
        products.setdefault(bu, Product(bu)).wants.append(Want(i, bu, r["option"], r["qty"]))
    return list(products.values())


class Engine:
    """One run of the browser. Runs its own asyncio loop in a worker thread."""

    def __init__(self, settings: dict, password: str, diag, log, headless: bool = False,
                 on_done=None) -> None:
        self.s = dict(settings)
        self.pw = password or ""
        register_secret(self.pw)
        self.diag = diag
        self._log_cb = log
        self.headless = headless
        self.on_done = on_done or (lambda *_: None)
        self.stop_event = threading.Event()
        self.clock = Clock()
        self.result: dict = {"result": "not-started"}
        self.ctx = None
        self.browser_label = ""
        self.basket_responses: list[dict] = []
        self.dialogs: list[str] = []

    # ------------------------------------------------------------------ util
    def log(self, msg: str) -> None:
        try:
            self.diag.log(msg)
        except Exception:
            pass
        try:
            self._log_cb(msg)
        except Exception:
            pass

    def stopped(self) -> bool:
        return self.stop_event.is_set()

    def stop(self) -> None:
        self.stop_event.set()

    async def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while not self.stopped() and time.time() < end:
            await asyncio.sleep(min(0.2, max(0.0, end - time.time())))

    # --------------------------------------------------------------- browser
    async def _launch(self, pw):
        from pathlib import Path
        prof = str(Path(config.profile_dir()))
        args = dict(user_data_dir=prof, headless=self.headless, locale="ko-KR",
                    timezone_id="Asia/Seoul", viewport={"width": 1280, "height": 860},
                    args=["--disable-blink-features=AutomationControlled"])
        last = None
        for channel, label in (("msedge", "Edge"), ("chrome", "Chrome"), (None, "Chromium")):
            try:
                kw = dict(args)
                if channel:
                    kw["channel"] = channel
                ctx = await pw.chromium.launch_persistent_context(**kw)
                self.browser_label = label
                self.log(f"브라우저 실행: {label}")
                return ctx
            except Exception as exc:  # try the next browser
                last = exc
        raise RuntimeError(f"브라우저를 열 수 없습니다 (Edge/Chrome 설치 확인): {last}")

    def _attach(self, page) -> None:
        def on_dialog(d):
            msg = d.message or ""
            self.dialogs.append(msg)
            self.log(f"사이트 알림: {msg.strip()[:120]}")
            # "담겼습니다. 지금 확인하시겠습니까?" -> stay on the product page
            if d.type == "confirm" and "장바구니에 담겼습니다" in msg:
                asyncio.ensure_future(d.dismiss())
            else:
                asyncio.ensure_future(d.accept())

        async def on_response(resp):
            if "basket.action" not in resp.url:
                return
            try:
                body = await resp.text()
            except Exception:
                body = ""
            rec = {"url": resp.url, "status": resp.status, "body": body[:4000], "at": time.time()}
            self.basket_responses.append(rec)
            try:
                self.diag.add_response(resp.request.method, resp.url, resp.status, body)
            except Exception:
                pass

        page.on("dialog", on_dialog)
        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

    async def _new_page(self):
        page = await self.ctx.new_page()
        self._attach(page)
        return page

    async def _snapshot(self, page, label: str) -> None:
        try:
            self.diag.add_page(label, page.url, await page.content())
        except Exception:
            pass

    # ----------------------------------------------------------------- login
    async def is_logged_in(self) -> bool:
        try:
            r = await self.ctx.request.get(MYPAGE_URL, max_redirects=0, timeout=15000)
            loc = r.headers.get("location", "")
            ok = r.status == 200 and "member.html" not in loc
            return ok
        except Exception as exc:
            self.log(f"로그인 확인 실패: {type(exc).__name__}")
            return False

    async def ensure_login(self, page) -> bool:
        if await self.is_logged_in():
            self.log("로그인 상태 확인됨 (저장된 로그인 사용)")
            return True
        kind = self.s.get("login_type") or config.DEFAULT_LOGIN_TYPE
        uid = (self.s.get("login_id") or "").strip()
        self.log(f"로그인 시도: {kind}")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        try:
            if kind == "자체":
                if uid and self.pw:
                    await page.fill('form[name="form1"] input[name="id"]', uid)
                    await page.fill('form[name="form1"] input[name="passwd"]', self.pw)
                    await page.evaluate("check()")
            elif kind == "카카오":
                await page.evaluate("ks_login_log('')")
                await page.wait_for_load_state("domcontentloaded")
                await self._fill_kakao(page, uid)
            else:
                await page.evaluate("sns_login_log('naver')")
                await page.wait_for_url(re.compile(r"nid\.naver\.com|foretforet\.com(?!/shop/member)"),
                                        timeout=20000)
                await self._fill_naver(page, uid)
        except Exception as exc:
            self.log(f"로그인 자동 입력 중 문제: {type(exc).__name__}. 창에서 직접 로그인해 주세요.")
        return await self._wait_login_done(page, kind)

    async def _type_into(self, page, selector: str, value: str) -> bool:
        loc = page.locator(selector).first
        if await loc.count() == 0:
            return False
        await loc.click()
        await loc.fill("")
        await loc.press_sequentially(value, delay=90)
        return True

    async def _fill_naver(self, page, uid: str) -> None:
        if "nid.naver.com" not in page.url:
            return
        await page.wait_for_selector("#id", timeout=15000)
        if uid and self.pw:
            await self._type_into(page, "#id", uid)
            await asyncio.sleep(0.4)
            await self._type_into(page, "#pw", self.pw)
            await asyncio.sleep(0.4)
            btn = page.locator("#log\\.login, button[type=submit]").first
            await btn.click()
            self.log("네이버 아이디/비밀번호 입력 완료. 추가 인증이 뜨면 창에서 직접 진행해 주세요.")
        else:
            self.log("네이버 로그인 창이 열렸습니다. 창에서 직접 로그인해 주세요.")

    async def _fill_kakao(self, page, uid: str) -> None:
        if "kakao.com" not in page.url:
            return
        if uid and self.pw:
            ok1 = await self._type_into(page, "input[name=loginId], #loginId--1, input[name=email]", uid)
            ok2 = await self._type_into(page, "input[name=password], #password--2", self.pw)
            if ok1 and ok2:
                await page.locator("button[type=submit]").first.click()
                self.log("카카오 아이디/비밀번호 입력 완료. 추가 인증이 뜨면 창에서 직접 진행해 주세요.")
                return
        self.log("카카오 로그인 창이 열렸습니다. 창에서 직접 로그인해 주세요.")

    async def _wait_login_done(self, page, kind: str) -> bool:
        deadline = time.time() + LOGIN_WAIT_SECONDS
        last_url = ""
        while time.time() < deadline and not self.stopped():
            url = page.url
            if url != last_url:
                last_url = url
                host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
                self.log(f"로그인 진행 중: {host}")
            if "foretforet.com" in url and "member.html" not in url and "/list/API/" not in url:
                if await self.is_logged_in():
                    self.log(f"로그인 성공 ({kind})")
                    return True
            await self._sleep(1.0)
        if await self.is_logged_in():
            self.log(f"로그인 성공 ({kind})")
            return True
        await self._snapshot(page, "login_failed")
        self.log("로그인 확인 실패: 창에서 직접 로그인을 마친 뒤 다시 시작해 주세요.")
        return False

    # -------------------------------------------------------------- products
    async def _resolve(self, prod: Product) -> None:
        html = await prod.page.content()
        prod.title = parser.parse_title(html)
        opts = parser.parse_options(html)
        for w in prod.wants:
            opt, note = parser.match_option(w.wanted, opts)
            w.option, w.note = opt, note
            if opt:
                stock = "무제한" if opt.unlimited else (opt.stock if opt.stock is not None else "?")
                self.log(f"[{w.row_no}번] {prod.branduid} '{w.wanted}' -> {opt.text} "
                         f"(재고 {stock}, {opt.state or '-'}, {note}) x{w.qty}")
            else:
                self.log(f"[{w.row_no}번] {prod.branduid} '{w.wanted}': {note}")

    async def _goto_product(self, prod: Product) -> None:
        await prod.page.goto(parser.product_url(prod.branduid), wait_until="domcontentloaded",
                             timeout=30000)

    async def _netfunnel(self, page, tag: str, last: list) -> bool:
        """True while a NetFunnel queue popup is visible; logs the position once per change."""
        try:
            nf = await page.evaluate(_JS_NETFUNNEL)
        except Exception:
            return False
        if not nf:
            return False
        txt = f"대기 {nf.get('count') or '?'}명, 뒤 {nf.get('next') or '?'}명, 남은 {nf.get('left') or '?'}"
        if not last or txt != last[0]:
            self.log(f"[{tag}] 접속 대기열(NetFunnel): {txt} (새로고침하지 않고 기다립니다)")
            last[:] = [txt]
        return True

    async def _wait_open(self, prod: Product, deadline: float) -> bool:
        """Reload until the cart button shows. Never reload under a NetFunnel popup."""
        page = prod.page
        last_nf: list = []
        polls = 0
        while not self.stopped():
            try:
                await self._goto_product(prod)
                while not self.stopped() and await self._netfunnel(page, prod.branduid, last_nf):
                    await asyncio.sleep(0.5)
                st = await page.evaluate(_JS_PAGE_STATE)
                if st.get("hasSelect") and st.get("cartBtn") and not st.get("caution"):
                    return True
                polls += 1
                if polls == 1 or polls % 20 == 0:
                    self.log(f"[{prod.branduid}] 아직 판매 전 화면, 다시 확인 중 ({polls}회)")
            except Exception as exc:
                self.log(f"[{prod.branduid}] 페이지 확인 재시도: {type(exc).__name__}")
            if time.time() > deadline:
                self.log(f"[{prod.branduid}] 오픈 대기 시간 초과")
                return False
            await asyncio.sleep(config.OPEN_POLL_MS / 1000.0)
        return False

    async def _add_to_cart(self, prod: Product) -> None:
        page = prod.page
        html = await page.content()
        opts = parser.parse_options(html)
        picks: list[Want] = []
        for w in prod.wants:
            opt, note = parser.match_option(w.wanted, opts)
            w.option, w.note = opt, note
            if not opt:
                self.log(f"[{w.row_no}번] 옵션 못 찾음: {note}")
                continue
            if not opt.buyable:
                self.log(f"[{w.row_no}번] {opt.text} 구매 불가 ({opt.state}, 재고 {opt.stock}), 그래도 시도")
            picks.append(w)
        if not picks:
            prod.message = "담을 옵션 없음"
            return
        sel = page.locator('select[name="optionlist[]"]').first
        for idx, w in enumerate(picks):
            await sel.select_option(w.option.value)
            await page.wait_for_selector(f"#MS_amount_basic_{idx}", state="attached", timeout=5000)
            if w.qty != 1:
                await page.evaluate(
                    "([i, q]) => { const e = document.getElementById('MS_amount_basic_' + i);"
                    " e.value = String(q); if (typeof set_amount === 'function') set_amount(e, 'basic'); }",
                    [idx, w.qty])
        before = len(self.basket_responses)
        await page.evaluate("window._is_send_multi = false; send_multi('', '')")
        t_end = time.time() + 12
        last_nf: list = []
        while time.time() < t_end and len(self.basket_responses) == before and not self.stopped():
            if await self._netfunnel(page, prod.branduid, last_nf):
                t_end = time.time() + 12   # queued: keep waiting, never resend
            await asyncio.sleep(0.1)
        if len(self.basket_responses) == before:
            prod.message = "장바구니 응답 없음"
            self.log(f"[{prod.branduid}] 장바구니 응답이 없습니다")
            return
        body = self.basket_responses[-1]["body"]
        try:
            data = json.loads(body)
        except Exception:
            data = {}
        if data.get("status") is True:
            prod.added = True
            arr = ((data.get("etc_data") or {}).get("basket_uid_array")) or []
            prod.cart_ids = [str(x) for x in arr]
            names = ", ".join(f"{w.option.text} x{w.qty}" for w in picks)
            self.log(f"[{prod.branduid}] 장바구니 담기 성공: {names}")
        else:
            prod.message = str(data.get("message") or body[:200])
            self.log(f"[{prod.branduid}] 장바구니 담기 실패: {prod.message}")

    async def _fire_product(self, prod: Product, deadline: float) -> None:
        for attempt in range(3):
            if self.stopped():
                return
            if not await self._wait_open(prod, deadline):
                prod.message = prod.message or "오픈되지 않음"
                return
            try:
                await self._add_to_cart(prod)
            except Exception as exc:
                prod.message = f"{type(exc).__name__}: {exc}"
                self.log(f"[{prod.branduid}] 담기 오류: {prod.message[:160]}")
            if prod.added:
                return
            if "품절" in prod.message or "재고" in prod.message:
                return
            self.log(f"[{prod.branduid}] 다시 시도 ({attempt + 2}/3)")
            await asyncio.sleep(0.4)

    # ---------------------------------------------------------------- basket
    async def read_basket(self, page) -> list[dict]:
        await page.goto(BASKET_URL, wait_until="domcontentloaded")
        try:
            return await page.evaluate(_JS_BASKET)
        except Exception:
            return []

    async def _select_ours(self, page, items: list[dict], products: list[Product]) -> int:
        cart_ids = {c for p in products for c in p.cart_ids}
        uids = {p.branduid for p in products if p.added}
        keep = [it["i"] for it in items
                if (cart_ids and it["cart_id"] in cart_ids) or (not cart_ids and it["uid"] in uids)]
        if not keep and uids:
            keep = [it["i"] for it in items if it["uid"] in uids]
        await page.evaluate(
            "(keep) => document.querySelectorAll('input[name=\"basketchks\"]').forEach("
            "(c, i) => { c.checked = keep.indexOf(i) >= 0; })", keep)
        return len(keep)

    async def go_order(self, page, products: list[Product]) -> str:
        items = await self.read_basket(page)
        self.log(f"장바구니 {len(items)}개 항목")
        for it in items:
            self.log(f"  - {it['text'][:100]}")
        await self._snapshot(page, "basket")
        n = await self._select_ours(page, items, products)
        if n == 0:
            self.log("이번에 담은 상품을 장바구니에서 찾지 못했습니다")
            return page.url
        others = len(items) - n
        if others:
            self.log(f"기존 장바구니 상품 {others}개는 주문에서 제외 (그대로 둠)")
        async with page.expect_navigation(timeout=20000):
            await page.evaluate("window._is_multi_order = false; multi_order('')")
        await page.wait_for_load_state("domcontentloaded")
        await self._snapshot(page, "order_page")
        return page.url

    async def remove_from_cart(self, page, products: list[Product]) -> int:
        """Delete only the items this run added (test cleanup)."""
        items = await self.read_basket(page)
        n = await self._select_ours(page, items, products)
        if n:
            await page.evaluate("basket_multidel(1)")
            await asyncio.sleep(2.5)
        return n

    async def _auto_bank(self, page) -> bool:
        """Opt-in: pick 무통장입금, fill the depositor, tick the agreements, submit.
        Card/INICIS is never touched."""
        dep = (self.s.get("depositor") or "").strip()
        self.log("무통장입금 자동 완료 시도")
        ok = await page.evaluate(_JS_PICK_BANK, dep)
        self.log(f"무통장입금 선택 결과: {ok}")
        if not ok or not ok.get("picked"):
            self.log("무통장입금 선택 실패: 화면에서 직접 결제해 주세요")
            return False
        if not ok.get("submit"):
            self.log("결제 버튼을 찾지 못했습니다: 화면에서 직접 눌러 주세요")
            return False
        await page.evaluate(ok["submit"])
        await asyncio.sleep(3)
        await self._snapshot(page, "after_bank_submit")
        self.log(f"주문 제출 후 주소: {page.url}")
        return True

    # ------------------------------------------------------------------ runs
    async def _open(self, pw):
        self.ctx = await self._launch(pw)
        pages = list(self.ctx.pages)
        for p in pages:
            self._attach(p)
        return pages[0] if pages else await self._new_page()

    async def _hold_open(self, why: str) -> None:
        self.log(why)
        while not self.stopped():
            if self.ctx is None or not self.ctx.pages:
                break
            await asyncio.sleep(0.5)

    async def login_test(self) -> dict:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            page = await self._open(pw)
            ok = await self.ensure_login(page)
            await self._snapshot(page, "login_test")
            self.result = {"result": "login-ok" if ok else "login-failed", "mode": "login-test"}
            try:
                await self.ctx.close()
            except Exception:
                pass
        return self.result

    async def purchase(self, cleanup_after: bool = False, hold: bool = True) -> dict:
        from playwright.async_api import async_playwright
        open_ts = parse_open_at(self.s.get("open_at") or config.DEFAULT_OPEN_AT)
        products = group_rows(self.s.get("rows") or [])
        rows_all = config.normalize_rows(self.s.get("rows") or [])
        skipped = [i for i, r in enumerate(rows_all, 1) if not (r["enabled"] and r["qty"] > 0)]
        if skipped:
            self.log(f"건너뛰는 줄(체크 해제 또는 수량 0): {', '.join(map(str, skipped))}번")
        if not products:
            self.log("구매할 상품이 없습니다 (체크된 줄, 수량 1 이상 필요)")
            self.result = {"result": "no-rows"}
            return self.result
        self.clock.sync(self.log)
        open_txt = datetime.fromtimestamp(open_ts, KST).strftime("%Y-%m-%d %H:%M:%S")
        self.log(f"오픈 시각 {open_txt} (한국시간), 상품 {len(products)}개")
        async with async_playwright() as pw:
            page = await self._open(pw)
            # wait until the preload window
            while not self.stopped() and self.clock.now() < open_ts - config.PRELOAD_SECONDS:
                left = open_ts - self.clock.now()
                if int(left) % 60 == 0:
                    self.log(f"오픈까지 {int(left // 60)}분 {int(left % 60)}초, 대기 중")
                await asyncio.sleep(1.0)
            if self.stopped():
                return await self._finish({"result": "stopped"})
            logged = await self.ensure_login(page)
            if not logged:
                self.log("로그인 없이 진행하면 비회원 주문 화면으로 갑니다")
            for prod in products:
                prod.page = await self._new_page()
                try:
                    await self._goto_product(prod)
                    await self._resolve(prod)
                except Exception as exc:
                    self.log(f"[{prod.branduid}] 미리 열기 실패: {type(exc).__name__}")
            # resync right before the drop, then wait for the exact moment
            if open_ts - self.clock.now() > 20:
                while not self.stopped() and self.clock.now() < open_ts - 20:
                    await asyncio.sleep(0.5)
                self.clock.sync(self.log)
            fire_at = open_ts + config.PRE_FIRE_RELOAD_MS / 1000.0
            while not self.stopped():
                left = fire_at - self.clock.now()
                if left <= 0:
                    break
                await asyncio.sleep(min(0.2, left) if left > 0.25 else max(0.0, left - 0.002))
            if self.stopped():
                return await self._finish({"result": "stopped"})
            self.log("오픈! 상품 페이지 새로고침 및 담기 시작")
            t0 = time.time()
            deadline = time.time() + config.OPEN_WAIT_MAX_SECONDS
            await asyncio.gather(*(self._fire_product(p, deadline) for p in products),
                                 return_exceptions=True)
            added = [p for p in products if p.added]
            self.log(f"담기 완료 {len(added)}/{len(products)}개 상품 ({time.time() - t0:.1f}초)")
            for p in products:
                if not p.added:
                    self.log(f"  실패 {p.branduid}: {p.message or '알 수 없음'}")
            for p in products:
                await self._snapshot(p.page, f"product_{p.branduid}")
            if not added:
                return await self._finish({"result": "nothing-added"}, hold=hold)
            url = await self.go_order(page, products)
            reached = "order.html" in url
            self.log(f"주문서 도착: {url}" if reached else f"주문서가 아닌 화면: {url}")
            result = {"result": "order-page" if reached else "basket-only", "orderUrl": url,
                      "added": [p.branduid for p in added]}
            if reached and self.s.get("auto_bank") and not cleanup_after:
                result["autoBank"] = await self._auto_bank(page)
            if cleanup_after:
                n = await self.remove_from_cart(page, products)
                left = await self.read_basket(page)
                self.log(f"테스트 정리: {n}개 삭제, 남은 항목 {len(left)}개")
                result["cleanup"] = {"removed": n, "left": len(left)}
                hold = False
            elif reached:
                page_front = page
                try:
                    await page_front.bring_to_front()
                except Exception:
                    pass
            return await self._finish(result, hold=hold)

    async def _finish(self, result: dict, hold: bool = False) -> dict:
        self.result = result
        if hold and not self.stopped():
            await self._hold_open("주문서에서 결제를 진행해 주세요. 끝나면 [중지]를 누르거나 브라우저를 닫으세요.")
        try:
            if self.ctx:
                await self.ctx.close()
        except Exception:
            pass
        return self.result

    # -------------------------------------------------------------- threads
    def run_in_thread(self, mode: str) -> threading.Thread:
        def _work():
            res = {"result": "error"}
            try:
                if mode == "login":
                    res = asyncio.run(self.login_test())
                else:
                    res = asyncio.run(self.purchase())
            except Exception as exc:  # reported, never crashes the GUI
                self.log(f"오류: {type(exc).__name__}: {str(exc)[:300]}")
                try:
                    self.diag.upload_exception(exc, f"engine.{mode}")
                except Exception:
                    pass
                res = {"result": "error", "error": f"{type(exc).__name__}"}
            self.result = res
            try:
                self.on_done(mode, res)
            except Exception:
                pass

        t = threading.Thread(target=_work, daemon=True)
        t.start()
        return t


# Order page: choose 무통장입금 (bank transfer) and fill the depositor. The
# selectors come from the Makeshop order.html captured during the logged-in
# test; each lookup is defensive because the skin can differ.
_JS_PICK_BANK = """(dep) => {
  const out = {picked: false, depositor: false, agreed: 0, submit: null};
  const radios = Array.from(document.querySelectorAll('input[type=radio]'));
  let bank = radios.find(r => /^(B|bank|online)$/i.test(r.value) && /pay|paytype|pay_type|radio_paymethod/i.test(r.name));
  if (!bank) {
    bank = radios.find(r => {
      const lab = (r.closest('label') || r.parentElement || {}).innerText || '';
      const f = r.id ? document.querySelector('label[for="' + r.id + '"]') : null;
      return /무통장/.test(lab + ' ' + (f ? f.innerText : ''));
    });
  }
  if (bank) { bank.click(); bank.checked = true; bank.dispatchEvent(new Event('change', {bubbles: true})); out.picked = true; }
  const sel = document.querySelector('select[name=pay_data], select[name=bank], select[name=bankname], select[name=account]');
  if (sel && sel.options.length > 1 && !sel.value) { sel.selectedIndex = 1; sel.dispatchEvent(new Event('change', {bubbles: true})); }
  const d = document.querySelector('input[name=bankname], input[name=pay_name], input[name=bank_name], input[name=sender], input[name=deposit_name]');
  if (d && dep) { d.value = dep; d.dispatchEvent(new Event('change', {bubbles: true})); out.depositor = true; }
  document.querySelectorAll('input[type=checkbox]').forEach(c => {
    const t = ((c.closest('label') || c.parentElement || {}).innerText || '') + (c.name || '') + (c.id || '');
    if (/동의|agree/i.test(t) && !c.checked) { c.click(); out.agreed++; }
  });
  const btn = Array.from(document.querySelectorAll('a, button, input[type=button], input[type=submit]'))
    .find(b => /결제하기|주문하기|구매하기/.test((b.innerText || b.value || '').trim()));
  if (btn) { window.__ff_submit_btn = btn; out.submit = "window.__ff_submit_btn.click()"; }
  return out;
}"""
