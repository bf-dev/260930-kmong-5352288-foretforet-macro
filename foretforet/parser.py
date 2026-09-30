# -*- coding: utf-8 -*-
"""Makeshop product-page parsing and option matching (foretforet.com, customer 5352288)."""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from . import config


@dataclass
class Option:
    value: str          # <option value="..."> (index used by the select)
    text: str           # e.g. "CBK,9_12M"
    stock: int | None   # sto_real_stock, None if unknown
    state: str          # SALE / SOLDOUT / ...
    unlimited: bool = False

    @property
    def color(self) -> str:
        return self.text.split(",", 1)[0].strip() if "," in self.text else ""

    @property
    def size(self) -> str:
        return self.text.split(",", 1)[1].strip() if "," in self.text else self.text.strip()

    @property
    def buyable(self) -> bool:
        return self.state.upper() == "SALE" and (self.unlimited or self.stock is None or self.stock > 0)


def branduid_of(url: str) -> str | None:
    """Extract branduid from any product URL form, or a bare number."""
    if not url:
        return None
    url = url.strip()
    if re.fullmatch(r"\d{5,}", url):
        return url
    try:
        q = parse_qs(urlparse(url).query)
        if q.get("branduid"):
            v = q["branduid"][0].strip()
            if v.isdigit():
                return v
    except Exception:
        pass
    m = re.search(r"branduid=(\d+)", url)
    return m.group(1) if m else None


def product_url(branduid: str) -> str:
    """Canonical desktop product URL (same option markup as /m/product.html)."""
    return f"{config.BASE_URL}/shop/shopdetail.html?branduid={branduid}"


def parse_title(page: str) -> str:
    m = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
    return _html.unescape(m.group(1).strip()) if m else ""


def parse_stock(page: str) -> dict[str, dict]:
    """opt_values -> {stock, state, unlimited} from the optionJsonData JS literal."""
    out: dict[str, dict] = {}
    m = re.search(r"(?<!pre_)optionJsonData\s*=\s*\{", page)
    if not m:
        return out
    end = page.find("};", m.end())
    blob = page[m.end(): end if end > 0 else m.end() + 200000]
    for block in re.finditer(r"\{adminuser:'[^']*'(.*?)\}", blob, re.S):
        b = block.group(1)
        v = re.search(r"opt_values:'([^']*)'", b)
        st = re.search(r"sto_real_stock:'(-?\d+)'", b)
        state = re.search(r"sto_state:'(\w+)'", b)
        unl = re.search(r"sto_unlimit:'(\w)'", b)
        if v is None:
            continue
        out[_html.unescape(v.group(1))] = {
            "stock": int(st.group(1)) if st else None,
            "state": state.group(1) if state else "",
            "unlimited": bool(unl and unl.group(1) == "Y"),
        }
    return out


def parse_options(page: str) -> list[Option]:
    """Options of the first basic option select, merged with stock data."""
    stock = parse_stock(page)
    sel = re.search(r'<select[^>]*name="optionlist\[\]"[^>]*>(.*?)</select>', page, re.S | re.I)
    opts: list[Option] = []
    if sel:
        for om in re.finditer(r"<option([^>]*)>(.*?)</option>", sel.group(1), re.S | re.I):
            attrs, label = om.group(1), om.group(2)
            vm = re.search(r'value="([^"]*)"', attrs)
            if not vm or vm.group(1) == "":
                continue
            tm = re.search(r'title="([^"]*)"', attrs)
            text = _html.unescape((tm.group(1) if tm else re.sub(r"<[^>]+>", "", label)).strip())
            sm = re.search(r'sto_state="(\w+)"', attrs)
            info = stock.get(text, {})
            opts.append(Option(
                value=vm.group(1), text=text,
                stock=info.get("stock"),
                state=info.get("state") or (sm.group(1) if sm else ""),
                unlimited=info.get("unlimited", False),
            ))
    return opts


def is_open(page: str) -> bool:
    """On sale = cart button present and no 'sold out / stopped' caution."""
    btn = re.search(r'class="shopdetailButtonTop"(.*?)</div>', page, re.S)
    area = btn.group(1) if btn else page
    if "product_caution" in area:
        return False
    return "send_multi(" in area and 'class="cart"' in area


def normalize(s: str) -> str:
    return re.sub(r"[\s,\-_./]+", "", (s or "")).lower()


def _norm_size(s: str) -> str:
    return normalize(s)


def match_option(wanted: str, options: list[Option]) -> tuple[Option | None, str]:
    """Pick the option the customer meant.

    1. exact / normalized full match ("cbk 9-12m" == "CBK,9_12M")
    2. size-first: if the typed colour does not exist and the product has exactly
       one colour, match on size alone ("cbk 9-12m" -> "MOB,9_12M")
    3. size-only input ("9-12m") when it is unique
    Returns (option or None, note).
    """
    if not options:
        return None, "옵션 없음"
    w = normalize(wanted)
    if not w:
        return None, "옵션 미입력"
    for o in options:
        if normalize(o.text) == w or o.value == wanted.strip():
            return o, "정확히 일치"

    colors = sorted({o.color.lower() for o in options if o.color})
    # split typed text into colour + size when it starts with a known colour or any letters
    typed_color = ""
    typed_size = w
    for c in sorted(colors, key=len, reverse=True):
        if c and w.startswith(normalize(c)):
            typed_color, typed_size = c, w[len(normalize(c)):]
            break
    if typed_color:
        hits = [o for o in options if o.color.lower() == typed_color and _norm_size(o.size) == typed_size]
        if len(hits) == 1:
            return hits[0], "색상+사이즈 일치"
        return None, f"'{wanted}' 사이즈 없음"

    # typed colour unknown: try to peel off a leading alpha token as a colour
    m = re.match(r"^([a-z]+)(\d.*)$", w)
    size_part = m.group(2) if m else w
    size_hits = [o for o in options if _norm_size(o.size) == size_part]
    if m and len(colors) == 1 and len(size_hits) == 1:
        return size_hits[0], f"색상 '{m.group(1).upper()}' 없음, 단일 색상이라 사이즈로 매칭"
    if not m and len(size_hits) == 1:
        return size_hits[0], "사이즈로 매칭"
    if len(size_hits) > 1:
        return None, f"'{wanted}' 여러 색상에 있음, 색상을 적어주세요"
    return None, f"'{wanted}' 일치하는 옵션 없음"
