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
  7. basket: tick only the items this run added, order them; a sold-out item is
     dropped and a short-stock item is cut to what is left, then order again
  8. order.html: KakaoPay + every required consent in one pass, press 결제하기 so
     the KakaoPay QR / approval window opens

The payment itself is always approved by the customer on the phone; nothing here
approves a payment, and 무통장입금 is never used.
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
  const form = document.getElementById('basket_form' + i) || c.closest('form');
  const q = n => { const e = form ? form.querySelector('input[name="' + n + '"]') : null; return e ? e.value : ''; };
  let name = q('product_brandname');
  if (!name) { try { name = decodeURIComponent(item.prod_name || ''); } catch (e) { name = item.prod_name || ''; } }
  return {i, uid: item.uid || '', cart_id: String(item.cart_id || ''), chk: c.getAttribute('chk_data_uid') || '',
          name, amount: parseInt(q('amount') || '0', 10) || 0,
          text: row ? row.innerText.replace(/\\s+/g, ' ').trim().slice(0, 160) : ''};
})"""

# Set a basket row quantity and save it with Makeshop's own cart_update_action().
_JS_SET_AMOUNT = """([i, n]) => {
  const f = document.getElementById('basket_form' + i);
  if (!f) return false;
  const a = f.querySelector('input[name="amount"]');
  if (!a) return false;
  a.value = String(n);
  if (typeof cart_update_action === 'function') { cart_update_action(i, 'upd'); return true; }
  return false;
}"""


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
    dropped: bool = False


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
            # tag the tab: with 10-20 products firing at once, each _add_to_cart must
            # read its own basket.action reply, never the newest one in the shared list
            rec = {"url": resp.url, "status": resp.status, "body": body[:4000], "at": time.time(),
                   "page": page}
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
                # go straight to the OAuth entry: with stale SNS cookies member.html
                # renders the SNS join form (no passwd field) and sns_login_log() throws
                await page.goto(BASE + "/list/API/login_naver.html", wait_until="domcontentloaded")
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
            await self._click_naver_login(page)
            self.log("네이버 아이디/비밀번호 입력 완료. 추가 인증이 뜨면 창에서 직접 진행해 주세요.")
        else:
            self.log("네이버 로그인 창이 열렸습니다. 창에서 직접 로그인해 주세요.")

    async def _click_naver_login(self, page) -> None:
        # Naver's 2026 login page has #loginBtn_row / #loginBtn_column (one of them
        # visible depending on width); the old page had #log.login. A plain
        # button[type=submit] hits a hidden language-switch button first.
        for sel in ("#loginBtn_row", "#loginBtn_column", "#log\\.login", "button.btn_login"):
            loc = page.locator(sel)
            for i in range(await loc.count()):
                if await loc.nth(i).is_visible():
                    await loc.nth(i).click()
                    return
        await page.locator("#pw").press("Enter")

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
        """Do not wait for domcontentloaded: at the drop the page body is there long
        before the third-party tags finish (1.0.2 lost 30 s per product on that
        TimeoutError). Return as soon as the option select and send_multi exist."""
        try:
            await prod.page.goto(parser.product_url(prod.branduid), wait_until="commit", timeout=10000)
        except Exception as exc:
            if "Timeout" not in type(exc).__name__:
                raise
        try:
            await prod.page.wait_for_function(
                "() => (typeof window.send_multi === 'function' && "
                "typeof window.change_option === 'function' && "
                "!!document.querySelector('select[name=\"optionlist[]\"]') && "
                "!!document.querySelector('.shopdetailButtonTop')) || "
                "document.readyState !== 'loading'", timeout=8000, polling=100)
        except Exception:
            pass

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

    async def _fetch_product(self, prod: Product) -> bytes | None:
        """Raw GET of the product page with the browser's cookies; None on any error."""
        try:
            if not getattr(self, "_ua", None):
                self._ua = await prod.page.evaluate("navigator.userAgent")
            r = await self.ctx.request.get(parser.product_url(prod.branduid), timeout=10000,
                                           headers={"User-Agent": self._ua})
            return await r.body()
        except Exception:
            return None

    def _drop(self, prod: Product) -> bool:
        prod.dropped = True
        prod.message = "품절 상태로 건너뜀"
        self.log(f"품절 상태로 건너뜀: {prod.title or prod.branduid} [{prod.branduid}] "
                 f"(오픈 {config.SOLDOUT_GRACE_SECONDS}초 후에도 판매 전/품절 화면, 나머지 상품으로 결제 진행)")
        return False

    async def _wait_open(self, prod: Product, deadline: float, drop_at: float | None = None) -> bool:
        """Reload until the cart button shows. Never reload under a NetFunnel popup.

        While the shop hides the product (the page body is only the
        "존재하지 않는 상품입니다" alert + redirect to '/'), poll it with a raw GET every
        HIDDEN_POLL_MS instead of navigating the tab, so the alert/redirect never
        costs a page load. As soon as the body is anything else, fall through to the
        real page load (NetFunnel aware) below.

        drop_at (1.0.4): after the open, a product still hidden or showing the
        sold-out/stopped caution past drop_at is given up so checkout is not held
        hostage by it. Time spent in a NetFunnel queue pushes drop_at back.
        """
        page = prod.page
        last_nf: list = []
        polls = 0
        hidden = 0
        while not self.stopped():
            body = await self._fetch_product(prod)
            if body is not None and parser.is_hidden_stub(body):
                hidden += 1
                if hidden == 1:
                    self.diag.add_response("GET", parser.product_url(prod.branduid), 200,
                                           body.decode("utf-8", "replace"))
                if hidden == 1 or hidden % 150 == 0:
                    self.log(f"[{prod.branduid}] 상품 페이지 아직 비공개(존재하지 않는 상품), "
                             f"계속 다시 확인 중 ({hidden}회)")
                if time.time() > deadline:
                    self.log(f"[{prod.branduid}] 오픈 대기 시간 초과 (상품 비공개 상태)")
                    return False
                if drop_at is not None and time.time() > drop_at:
                    return self._drop(prod)
                await asyncio.sleep(config.HIDDEN_POLL_MS / 1000.0)
                continue
            if hidden:
                self.log(f"[{prod.branduid}] 상품 페이지 공개됨 ({hidden}회 확인 후), 바로 엽니다")
                hidden = 0
            try:
                await self._goto_product(prod)
                queued = False
                while not self.stopped() and await self._netfunnel(page, prod.branduid, last_nf):
                    queued = True
                    await asyncio.sleep(0.5)
                if queued and drop_at is not None:
                    drop_at = max(drop_at, time.time() + config.SOLDOUT_GRACE_SECONDS)
                st = await page.evaluate(_JS_PAGE_STATE)
                if st.get("hasSelect") and st.get("cartBtn") and not st.get("caution"):
                    return True
                if st.get("caution") and drop_at is not None and time.time() > drop_at:
                    return self._drop(prod)
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
            await page.wait_for_selector(f"#MS_amount_basic_{idx}", state="attached", timeout=3000)
            if w.qty != 1:
                await page.evaluate(
                    "([i, q]) => { const e = document.getElementById('MS_amount_basic_' + i);"
                    " e.value = String(q); if (typeof set_amount === 'function') set_amount(e, 'basic'); }",
                    [idx, w.qty])
        before = len(self.basket_responses)

        def mine() -> list[dict]:
            return [r for r in self.basket_responses[before:] if r.get("page") is page]

        await page.evaluate("window._is_send_multi = false; send_multi('', '')")
        t_end = time.time() + 8
        last_nf: list = []
        while time.time() < t_end and not mine() and not self.stopped():
            if await self._netfunnel(page, prod.branduid, last_nf):
                t_end = time.time() + 8   # queued: keep waiting, never resend
            await asyncio.sleep(0.1)
        own = mine()
        if not own:
            prod.message = "장바구니 응답 없음"
            self.log(f"[{prod.branduid}] 장바구니 응답이 없습니다")
            return
        body = own[0]["body"]
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

    async def _fire_product(self, prod: Product, deadline: float, drop_at: float | None = None) -> None:
        try:
            await self._fire_product_once(prod, deadline, drop_at)
        finally:
            await self._close_product_tab(prod)

    async def _close_product_tab(self, prod: Product) -> None:
        """1.0.4: a resolved product (carted, sold out, dropped, failed) gives its tab
        back right away so 10-20 open tabs do not slow the rest. Snapshot first for
        the run ZIP. Only the product's own tab, never the order tab or the context."""
        page, prod.page = prod.page, None
        if page is None:
            return
        await self._snapshot(page, f"product_{prod.branduid}")
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass

    async def _fire_product_once(self, prod: Product, deadline: float, drop_at: float | None) -> None:
        for attempt in range(3):
            if self.stopped():
                return
            if not await self._wait_open(prod, deadline, drop_at):
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
            await asyncio.sleep(0.2)

    # ---------------------------------------------------------------- basket
    async def read_basket(self, page) -> list[dict]:
        await page.goto(BASKET_URL, wait_until="domcontentloaded", timeout=15000)
        return await self._basket_items(page)

    async def _basket_items(self, page) -> list[dict]:
        try:
            return await page.evaluate(_JS_BASKET)
        except Exception:
            return []

    def _ours(self, items: list[dict], products: list[Product]) -> list[dict]:
        """Rows this run added. basket_uid_array from the add response holds branduids,
        so match on cart_id when we have real cart ids, otherwise on branduid."""
        cart_ids = {c for p in products for c in p.cart_ids}
        uids = {p.branduid for p in products if p.added}
        keep = [it for it in items if it["cart_id"] in cart_ids]
        if not keep:
            keep = [it for it in items if it["uid"] in uids]
        return keep

    async def _select_ours(self, page, items: list[dict], products: list[Product],
                           exclude: set | None = None) -> int:
        exclude = exclude or set()
        keep = [it["i"] for it in self._ours(items, products)
                if it.get("name") not in exclude and it["cart_id"] not in exclude]
        await page.evaluate(
            "(keep) => document.querySelectorAll('input[name=\"basketchks\"]').forEach("
            "(c, i) => { c.checked = keep.indexOf(i) >= 0; })", keep)
        return len(keep)

    async def _try_order(self, page) -> None:
        """multi_order('') -> order.html. On a stock problem the shop alerts and stays on
        (or reloads) basket.html, so a missing navigation is not an error here."""
        try:
            async with page.expect_navigation(timeout=12000):
                await page.evaluate("window._is_multi_order = false; multi_order('')")
        except Exception:
            pass
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass

    async def go_order(self, page, products: list[Product]) -> str:
        """Basket -> order.html. Sold-out rows are dropped from the selection and
        rows with less stock than ordered are cut down to what is left, then the
        order is retried, so one sold-out item never blocks the rest."""
        items = await self.read_basket(page)
        self.log(f"장바구니 {len(items)}개 항목")
        for it in items:
            self.log(f"  - {it['text'][:100]}")
        await self._snapshot(page, "basket")
        exclude: set = set()
        blind = 0
        for attempt in range(1, 7):
            if self.stopped():
                break
            if attempt > 1:
                if "basket.html" not in page.url:
                    await self.read_basket(page)
                items = await self._basket_items(page)
            n = await self._select_ours(page, items, products, exclude)
            if n == 0:
                self.log("주문할 상품이 남아 있지 않습니다 (모두 품절이거나 장바구니에서 찾지 못함)")
                return page.url
            if attempt == 1:
                others = len(items) - n
                if others:
                    self.log(f"기존 장바구니 상품 {others}개는 주문에서 제외 (그대로 둠)")
            mark = len(self.dialogs)
            await self._try_order(page)
            if "order.html" in page.url:
                await self._snapshot(page, "order_page")
                if exclude:
                    self.log(f"품절 {len(exclude)}개 제외하고 주문서로 이동")
                return page.url
            alerts = self.dialogs[mark:]
            found = [x for msg in alerts for x in parser.parse_stock_alerts(msg)]
            if not found:
                blind += 1
                self.log(f"주문서로 넘어가지 않았습니다 ({attempt}회), 다시 시도")
                if blind >= 2:
                    break
                continue
            if "basket.html" not in page.url or not await self._basket_items(page):
                await self.read_basket(page)
            items = await self._basket_items(page)
            by_name = {it["name"]: it for it in self._ours(items, products)}
            for name, kind, left in found:
                it = by_name.get(name)
                if kind == "stock" and left > 0 and it and it["amount"] > left:
                    self.log(f"재고 {left}개만 남음: {name[-30:]} 수량 {it['amount']} -> {left}")
                    try:
                        await page.evaluate(_JS_SET_AMOUNT, [it["i"], left])
                        await asyncio.sleep(1.2)
                    except Exception as exc:
                        self.log(f"수량 변경 실패: {type(exc).__name__}, 이 상품은 제외")
                        exclude.add(name)
                else:
                    if name not in exclude:
                        self.log(f"품절, 주문에서 제외: {name[-30:]}")
                    exclude.add(name)
        await self._snapshot(page, "basket_final")
        return page.url

    async def remove_from_cart(self, page, products: list[Product]) -> int:
        """Delete only the items this run added (test cleanup)."""
        items = await self.read_basket(page)
        n = await self._select_ours(page, items, products)
        if n:
            await page.evaluate("basket_multidel(1)")
            await asyncio.sleep(2.5)
        return n

    # ------------------------------------------------------------ order page
    async def _fill_order(self, page) -> dict:
        try:
            return await page.evaluate(_JS_PREPARE_ORDER, self.s.get("pay_method") or "KAKAOPAY")
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}

    async def _pay_window(self, page, before: set):
        """The KakaoPay window: a new 'orderpay' popup, or the in-page #payiframe layer."""
        for p in self.ctx.pages:
            if p is not page and id(p) not in before:
                return p
        try:
            if await page.evaluate("() => { const f = document.getElementById('payiframe');"
                                   " if (!f) return false; const r = f.getBoundingClientRect();"
                                   " return r.width > 0 && r.height > 0; }"):
                return page
        except Exception:
            pass
        return None

    async def prepare_order(self, page, click: bool = True) -> dict:
        """Right after order.html loads: choose KakaoPay, tick every required consent,
        then press 결제하기 so the KakaoPay QR / approval window opens. The customer
        approves on the phone; this never approves anything itself. A missing-consent
        alert is fixed by ticking the box again and pressing once more."""
        out: dict = {"method": self.s.get("pay_method") or "KAKAOPAY", "clicked": False, "window": False}
        st = await self._fill_order(page)
        out["fill"] = st
        if st.get("error") or not st.get("radio"):
            self.log(f"카카오페이 선택 실패: {st.get('error') or '결제수단 버튼 없음'}. 화면에서 직접 골라 주세요")
        else:
            self.log(f"결제수단 카카오페이 선택, 동의 {len(st.get('checked') or [])}개 체크 "
                     f"(paymethod={st.get('paymethod')}, simplepay={st.get('simplepay')})")
        if not click:
            self.log("결제 버튼 자동 클릭 꺼짐: 화면에서 [결제하기]를 눌러 주세요")
            return out
        for attempt in range(1, 4):
            if self.stopped():
                break
            mark = len(self.dialogs)
            before = {id(p) for p in self.ctx.pages}
            try:
                await page.evaluate("() => { if (typeof send === 'function') { send(); return; }"
                                    " const b = document.querySelector('a.btn_Red.all-ok, a[href*=\"send()\"]');"
                                    " if (b) b.click(); }")
                out["clicked"] = True
            except Exception as exc:
                self.log(f"결제 버튼 실행 오류: {type(exc).__name__}")
            win = None
            end = time.time() + 6
            while time.time() < end and not self.stopped():
                win = await self._pay_window(page, before)
                if win is not None or len(self.dialogs) > mark:
                    break
                await asyncio.sleep(0.1)
            alerts = self.dialogs[mark:]
            if win is None and alerts:
                if any(re.search(r"동의|약관|환불계좌", a) for a in alerts):
                    self.log(f"동의 누락 알림, 다시 체크하고 재시도 ({attempt}/3)")
                    out["fill"] = await self._fill_order(page)
                    continue
                self.log("결제창 대신 알림이 떴습니다. 화면을 확인해 주세요")
                break
            if win is None:
                win = await self._pay_window(page, before)
            if win is not None:
                out["window"] = True
                if win is not page:
                    self._attach(win)
                    try:
                        await win.bring_to_front()
                    except Exception:
                        pass
                self.log("카카오페이 결제창이 열렸습니다. 휴대폰에서 승인해 주세요")
                await self._snapshot(page, "order_after_pay_click")
                return out
            self.log(f"결제창이 아직 안 열렸습니다 ({attempt}/3)")
        await self._snapshot(page, "order_pay_not_opened")
        return out

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

    async def _show_waiting(self, page, open_ts: float) -> None:
        """Local placeholder so the browser does not look dead (1.0.0 showed a
        blank about:blank until 3 minutes before open). No network, display only."""
        if open_ts - self.clock.now() <= config.PRELOAD_SECONDS:
            return
        fmt = lambda ts: datetime.fromtimestamp(ts, KST).strftime("%m월 %d일 %H:%M:%S")
        html = (
            "<html><head><meta charset='utf-8'><title>오픈 대기 중</title></head>"
            "<body style='font-family:sans-serif;text-align:center;padding-top:80px;color:#333'>"
            "<h2>포레포레 오픈 대기 중</h2>"
            f"<p>오픈 시각 <b>{fmt(open_ts)}</b> (한국시간)</p>"
            f"<p>{fmt(open_ts - config.PRELOAD_SECONDS)} 에 이 창에서 로그인하고 상품 페이지를 엽니다.</p>"
            "<p id='left' style='font-size:28px;color:#c40'></p>"
            "<p style='color:#888'>이 창을 닫지 마세요. 프로그램 화면에서 [중지]로 멈출 수 있습니다.</p>"
            "<script>const t=" + str(int(open_ts * 1000)) + ";"
            "function f(){let s=Math.max(0,Math.floor((t-Date.now())/1000));"
            "const h=Math.floor(s/3600),m=Math.floor(s%3600/60);s=s%60;"
            "document.getElementById('left').textContent='오픈까지 '+h+'시간 '+m+'분 '+s+'초';}"
            "f();setInterval(f,1000);</script></body></html>")
        try:
            await page.set_content(html, timeout=5000)
        except Exception:
            pass

    async def fire_all(self, products: list[Product]) -> list[Product]:
        """All product tabs at once. Returns the carted products; every product tab is
        closed when this returns."""
        t0 = time.time()
        deadline = time.time() + config.OPEN_WAIT_MAX_SECONDS
        # started late or on time, the sold-out grace runs from the moment we fire
        drop_at = time.time() + config.SOLDOUT_GRACE_SECONDS
        await asyncio.gather(*(self._fire_product(p, deadline, drop_at) for p in products),
                             return_exceptions=True)
        added = [p for p in products if p.added]
        self.log(f"담기 완료 {len(added)}/{len(products)}개 상품 ({time.time() - t0:.1f}초)")
        dropped = [p for p in products if p.dropped]
        if dropped:
            self.log(f"품절로 건너뛴 상품 {len(dropped)}개: "
                     + ", ".join(p.title or p.branduid for p in dropped))
        for p in products:
            if not p.added:
                self.log(f"  실패 {p.branduid}: {p.message or '알 수 없음'}")
        for p in products:   # normally already closed by _fire_product
            await self._close_product_tab(p)
        return added

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
            await self._show_waiting(page, open_ts)
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
                    if any("존재하지 않는" in d for d in self.dialogs[-2:]):
                        self.log(f"[{prod.branduid}] 지금은 상품 페이지가 비공개입니다. "
                                 f"오픈 시각부터 공개될 때까지 계속 다시 확인합니다")
                except Exception as exc:
                    self.log(f"[{prod.branduid}] 미리 열기 실패: {type(exc).__name__}")
            # armed heartbeat: one small JSON post so a dead PC / failed login is
            # visible before the drop, not after. Non-blocking, never raises.
            try:
                ready = [f"{p.branduid}:{'title' if p.title else 'hidden'}" for p in products]
                self.diag.heartbeat(
                    f"armed: login={'ok' if logged else 'no'}, open={open_txt} KST, "
                    f"pay={self.s.get('pay_method') or 'KAKAOPAY'}, "
                    f"autoclick={bool(self.s.get('auto_pay_click', True))}, products={', '.join(ready)}")
            except Exception:
                pass
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
            added = await self.fire_all(products)
            if not added:
                return await self._finish({"result": "nothing-added"}, hold=hold)
            url = await self.go_order(page, products)
            reached = "order.html" in url
            self.log(f"주문서 도착: {url}" if reached else f"주문서가 아닌 화면: {url}")
            result = {"result": "order-page" if reached else "basket-only", "orderUrl": url,
                      "added": [p.branduid for p in added]}
            if reached:
                # one pass, no waiting: KakaoPay + consents + 결제하기 (tests never click)
                click = bool(self.s.get("auto_pay_click", True)) and not cleanup_after
                result["pay"] = await self.prepare_order(page, click=click)
            if cleanup_after:
                n = await self.remove_from_cart(page, products)
                left = await self.read_basket(page)
                self.log(f"테스트 정리: {n}개 삭제, 남은 항목 {len(left)}개")
                result["cleanup"] = {"removed": n, "left": len(left)}
                hold = False
            elif reached and not (result.get("pay") or {}).get("window"):
                # no KakaoPay popup: show the order page. With a popup, leave it on top.
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
            return await self._finish(result, hold=hold)

    async def _finish(self, result: dict, hold: bool = False) -> dict:
        self.result = result
        if hold and not self.stopped():
            await self._hold_open("카카오페이 결제창에서 휴대폰으로 승인해 주세요 (창이 없으면 주문서에서 [결제하기]). 끝나면 [중지]를 누르거나 브라우저를 닫으세요.")
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


# Order page (Makeshop order.html, captured 2026-10-01): choose the payment radio
# through the shop's own jQuery click handler (it sets paymethod=C and
# simplepay_type=KKP for KAKAOPAY and resets pay_agree via pay_agree_init()), then
# tick the consents sendok2() checks. Untouched on purpose: same / modify_address
# (address copy, fires a confirm), reserve / coupon boxes.
_JS_PREPARE_ORDER = """(method) => {
  const out = {radio: false, checked: [], paymethod: '', simplepay: ''};
  const f = document.forms['form1'] || document.getElementById('order_form') || document;
  const r = f.querySelector('input[name="radio_paymethod"][value="' + method + '"]');
  if (r) {
    if (!r.checked || !window.__ff_paid_once) {
      try {
        if (window.jQuery) { jQuery(r).prop('checked', true).trigger('click'); }
        else { r.click(); }
      } catch (e) { try { r.click(); } catch (e2) {} }
      window.__ff_paid_once = true;
    }
    f.querySelectorAll('input[name="radio_paymethod"]').forEach(x => { x.checked = (x === r); });
    out.radio = true;
  }
  // The same aborted ready chain also skips "place=S; setTimeout(addrclick, 1000)", which
  // fills the member's default address; without it send() stops at "받는분의 성함을 입력하세요".
  // Run the shop's own addrclick() (it reads the shop-side stored address, we copy nothing).
  const rcv = document.querySelector('input[name="receiver"]');
  const def = document.querySelector('input[name="place"][value="S"]');
  if (rcv && !String(rcv.value || '').trim() && def && typeof addrclick === 'function') {
    try {
      def.checked = true;
      addrclick();
      out.address = String(rcv.value || '').trim() ? 'default' : 'empty';
    } catch (e) { out.address = 'error: ' + String(e && e.message || e).slice(0, 80); }
  }
  const ids = ['new_privacy_ok', 'privacy_ok', 'provider_privacy_agree_ok', 'recall_policy_ok',
               'contract_ok', 'pay_agree', 'user_age_check', 'before_pay_agree', 'refund_info_agree'];
  const tick = () => {
    ids.forEach(n => {
      const els = document.querySelectorAll('#' + n + ', input[type=checkbox][name="' + n + '"]');
      els.forEach(c => {
        if (c.type === 'checkbox' && !c.disabled && !c.checked) {
          c.checked = true;
          try { c.dispatchEvent(new Event('change', {bubbles: true})); } catch (e) {}
          if (out.checked.indexOf(n) < 0) out.checked.push(n);
        }
      });
    });
  };
  tick();
  const all = document.getElementById('all_ok');
  if (all && !all.disabled) {
    all.checked = true;
    try { if (typeof all_check === 'function') all_check(); } catch (e) {}
    if (out.checked.indexOf('all_ok') < 0) out.checked.push('all_ok');
  }
  try { if (typeof all_entire_agree === 'function') all_entire_agree(); } catch (e) {}
  tick();
  if (all) all.checked = true;
  const pm = f.querySelector ? f.querySelector('input[name="paymethod"]') : null;
  const sp = f.querySelector ? f.querySelector('input[name="simplepay_type"]') : null;
  // Makeshop binds the radio handler inside a jQuery ready callback; if an earlier
  // ready callback throws (a tracker script failing), the handler never binds and
  // paymethod stays empty. Apply the same mapping the handler would.
  const simple = {KAKAOPAY: 'KKP', PAYCO: 'PC', TOSS: 'TOS'};
  if (r && pm) {
    const want = simple[method] ? 'C' : method;
    if (pm.value !== want) { pm.value = want; out.fixed = true; }
    if (sp && simple[method] && sp.value !== simple[method]) { sp.value = simple[method]; out.fixed = true; }
  }
  out.paymethod = pm ? pm.value : '';
  out.simplepay = sp ? sp.value : '';
  out.selected = (f.querySelector('input[name="radio_paymethod"]:checked') || {}).value || '';
  return out;
}"""
