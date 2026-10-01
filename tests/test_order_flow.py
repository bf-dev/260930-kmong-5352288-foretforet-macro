# -*- coding: utf-8 -*-
"""1.0.3 basket/order flow tests (Kmong customer 5352288).

The order-page test runs against a local fixture that mirrors the real
foretforet order.html behaviour: clicking a payment radio runs pay_agree_init()
which clears every consent, send() alerts on the first missing consent, and a
complete form opens window.open('about:blank', 'orderpay'). Nothing here talks
to the live site, so no order or payment can ever be made.
"""
import asyncio

import pytest

from foretforet import config, parser
from foretforet.engine import Engine

# real alert texts from the 2026-10-01 10:00 drop log
A_SOLDOUT = ("[FALL26[던스스웨덴]블랙 클로버 후드수트-DS26KABDS0010CBK]선택된 상품/옵션은 품절입니다.\n"
             "수량/상품 체크를 다시하시기 바랍니다.\n \n감사합니다.")
A_OTHER = "[FALL26[던스스웨덴]블랙 클로버 후드수트-DS26KABDS0010CBK]상품은 다른 고객의 주문으로 품절입니다.\n \n감사합니다."
A_STOCK = "[FALL26[던스스웨덴]브라운 클로버 배기팬츠-DS26KAPAN0021CBW]상품의 재고가 현재 1개 입니다.\n \n감사합니다."
A_MULTI = ("[FALL26[던스스웨덴]브라운 클로버 배기팬츠-DS26KAPAN0021CBW]상품의 재고가 현재 1개 입니다.\n"
           "[FALL26[던스스웨덴]마더어스 후드수트-DS26KABDS0083MOB]상품은 다른 고객의 주문으로 ")


def test_stock_alert_soldout_keeps_nested_brackets():
    assert parser.parse_stock_alerts(A_SOLDOUT) == [
        ("FALL26[던스스웨덴]블랙 클로버 후드수트-DS26KABDS0010CBK", "soldout", 0)]
    assert parser.parse_stock_alerts(A_OTHER)[0][1] == "soldout"


def test_stock_alert_remaining_count():
    assert parser.parse_stock_alerts(A_STOCK) == [
        ("FALL26[던스스웨덴]브라운 클로버 배기팬츠-DS26KAPAN0021CBW", "stock", 1)]


def test_stock_alert_multi_line_and_truncated():
    got = parser.parse_stock_alerts(A_MULTI)
    assert got == [("FALL26[던스스웨덴]브라운 클로버 배기팬츠-DS26KAPAN0021CBW", "stock", 1),
                   ("FALL26[던스스웨덴]마더어스 후드수트-DS26KABDS0083MOB", "soldout", 0)]


def test_unrelated_alerts_are_not_stock_alerts():
    assert parser.parse_stock_alerts("환불계좌 수집/설정 동의에 체크해주시기 바랍니다.") == []
    assert parser.parse_stock_alerts("") == []


def test_defaults_are_kakaopay_and_never_bank_transfer():
    s = config.default_settings()
    assert s["pay_method"] == "KAKAOPAY"
    assert s["auto_pay_click"] is True
    assert "auto_bank" not in s and "depositor" not in s


def test_old_bank_settings_are_dropped_on_load(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(config, "settings_path", lambda: tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        json.dumps({"auto_bank": True, "depositor": "x"}), encoding="utf-8")
    s = config.load_settings()
    assert s["pay_method"] == "KAKAOPAY" and "auto_bank" not in s


ORDER_HTML = """<html><head><meta charset="utf-8">
<script>
var CONSENTS = [['new_privacy_ok','개인정보 수집/이용 약관에 동의하셔야 구매가 가능합니다.'],
                ['refund_info_agree','환불계좌 수집/설정 동의에 체크해주시기 바랍니다.'],
                ['pay_agree','결제 진행 동의에 체크해 주세요.'],
                ['before_pay_agree','구매조건 확인 및 결제진행 동의에 체크해 주세요.']];
window.__sends = 0;
function pay_agree_init() { CONSENTS.forEach(function(c){ document.getElementById(c[0]).checked = false; });
  document.getElementById('all_ok').checked = false; }
function all_check() { var v = document.getElementById('all_ok').checked;
  CONSENTS.forEach(function(c){ document.getElementById(c[0]).checked = v; }); }
function all_entire_agree() {}
function pick(r) { pay_agree_init();
  document.form1.paymethod.value = r.value == 'KAKAOPAY' ? 'C' : r.value;
  document.form1.simplepay_type.value = r.value == 'KAKAOPAY' ? 'KKP' : ''; }
function send() { window.__sends++;
  for (var i = 0; i < CONSENTS.length; i++) {
    if (!document.getElementById(CONSENTS[i][0]).checked) { alert(CONSENTS[i][1]); return; } }
  if (document.form1.paymethod.value == 'B') { alert('무통장입금 사용 금지 (test)'); return; }
  window.open('about:blank', 'orderpay'); }
</script></head><body>
<form name="form1" id="order_form">
<input type="hidden" name="paymethod" value="B"><input type="hidden" name="simplepay_type" value="">
<input type="radio" name="radio_paymethod" value="B" checked onclick="pick(this)">
<input type="radio" name="radio_paymethod" value="C" onclick="pick(this)">
<input type="radio" name="radio_paymethod" value="KAKAOPAY" onclick="pick(this)">
<input type="checkbox" id="all_ok">
<input type="checkbox" id="new_privacy_ok"><input type="checkbox" id="refund_info_agree">
<input type="checkbox" id="pay_agree"><input type="checkbox" id="before_pay_agree">
</form><a class="btn_Red all-ok" href="javascript:send()">결제하기</a>
</body></html>"""


class _Diag:
    def log(self, *_):
        pass

    def add_page(self, *_):
        pass

    def add_response(self, *_):
        pass


def _run_order(html_extra: str = "", click: bool = True, html: str = ORDER_HTML):
    pw_api = pytest.importorskip("playwright.async_api")

    async def go():
        async with pw_api.async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except Exception as exc:  # no browser on this machine
                pytest.skip(f"no chromium: {exc}")
            eng = Engine(config.default_settings(), "", _Diag(), log=lambda m: None, headless=True)
            eng.ctx = await browser.new_context()
            page = await eng.ctx.new_page()
            eng._attach(page)
            await page.set_content(html.replace("</body>", html_extra + "</body>"))
            res = await eng.prepare_order(page, click=click)
            state = await page.evaluate(
                "() => ({pm: document.form1.paymethod.value, sp: document.form1.simplepay_type.value,"
                " sends: window.__sends, pages: 0})")
            state["pages"] = len(eng.ctx.pages)
            state["dialogs"] = list(eng.dialogs)
            await browser.close()
            return res, state
    return asyncio.run(go())


def test_order_page_kakaopay_consents_and_pay_window():
    res, st = _run_order()
    assert res["fill"]["selected"] == "KAKAOPAY"
    assert st["pm"] == "C" and st["sp"] == "KKP"          # KakaoPay, never bank transfer
    assert st["sends"] == 1 and st["dialogs"] == []       # all consents ticked in one pass
    assert res["window"] is True and st["pages"] == 2     # orderpay popup opened, nothing approved


def test_order_page_consent_alert_is_fixed_and_retried():
    # a late script clears refund consent once after we tick it (like a re-render)
    extra = ("<script>var _s = send; var _once = false; send = function(){ if (!_once) {"
             " _once = true; document.getElementById('refund_info_agree').checked = false; } _s(); };</script>")
    res, st = _run_order(extra)
    assert st["sends"] == 2
    assert any("환불계좌" in d for d in st["dialogs"])
    assert res["window"] is True


def test_order_page_click_off_only_prepares():
    res, st = _run_order(click=False)
    assert st["sends"] == 0 and st["pages"] == 1
    assert st["pm"] == "C" and st["sp"] == "KKP"


def test_order_page_unbound_radio_handler_still_sets_kakaopay():
    # Live root cause: jQuery 1.7 stops running ready callbacks when one throws (a tracker
    # script), so Makeshop's radio_paymethod handler never binds and paymethod stays empty.
    html = ORDER_HTML.replace(' onclick="pick(this)"', '').replace(
        'name="paymethod" value="B"', 'name="paymethod" value=""')
    res, st = _run_order(html=html)
    assert st["pm"] == "C" and st["sp"] == "KKP"
    assert st["sends"] == 1 and st["dialogs"] == [] and res["window"] is True


def test_order_page_default_address_filled_when_ready_chain_aborted():
    # addrclick() normally runs from the same aborted ready callback; send() then stops at
    # "받는분의 성함을 입력하세요". prepare_order runs the shop's own addrclick() first.
    html = ORDER_HTML.replace(
        "function send() { window.__sends++;",
        "function addrclick() { if (document.querySelector('input[name=place]:checked'))"
        " document.form1.receiver.value = 'stored'; pay_agree_init(); }\n"
        "function send() { window.__sends++; if (!document.form1.receiver.value) {"
        " alert('받는분의 성함을 입력하세요.'); return; }").replace(
        '<input type="checkbox" id="all_ok">',
        '<input type="text" name="receiver" value=""><input type="radio" name="place" value="S">'
        '<input type="checkbox" id="all_ok">')
    res, st = _run_order(html=html)
    assert res["fill"]["address"] == "default"
    assert st["sends"] == 1 and st["dialogs"] == [] and res["window"] is True
