# -*- coding: utf-8 -*-
"""foretforet open-drop purchase macro, entry point (Kmong customer 5352288 / order 7643217).

The customer double-clicks the exe and gets the window. --selftest / --guidemo /
--console exist for CI and our own diagnostics only.

With --noconsole sys.stdout is None, so every print goes through _out().
"""
from __future__ import annotations

import sys

from foretforet import config
from foretforet.reporter import Diagnostics, install_excepthook

CONTROL_BRANDUID = "10254531"  # an always-on-sale product, used when every drop page is still hidden


def _out(line: str = "") -> None:
    if sys.stdout is None:
        return
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        return
    except Exception:
        pass
    try:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        sys.stdout.write((line + "\n").encode(enc, "replace").decode(enc, "replace"))
        sys.stdout.flush()
    except Exception:
        pass


def selftest(diag: Diagnostics) -> int:
    """Frozen-exe check: imports, live option parse of the default rows, clock
    sync, Artifacts upload. Exit 0 = everything the GUI needs works."""
    import tkinter  # noqa: F401
    import playwright  # noqa: F401
    from foretforet import engine, gui, parser
    from foretforet.clock import Clock

    ok = True
    rows = config.active_rows(config.default_settings()["rows"])
    _out(f"SELFTEST v{config.APP_VERSION} customer {config.CUSTOMER_ID}: {len(rows)} default rows")
    seen: dict[str, tuple] = {}
    hidden = parsed = 0
    for i, r in enumerate(rows, 1):
        bu = parser.branduid_of(r["url"])
        try:
            if bu not in seen:
                seen[bu] = gui.fetch_options(r["url"])
            title, opts, is_open = seen[bu]
            opt, note = parser.match_option(r["option"], opts)
            line = (f"row {i} {bu} want={r['option']} -> "
                    f"{opt.text if opt else None} stock={opt.stock if opt else None} "
                    f"open={is_open} options={len(opts)} {note}")
            if not opts and title == gui.HIDDEN_TITLE:
                line += " (hidden by the shop before the drop: alert stub, retried at open)"
                hidden += 1
            elif not opts:
                ok = False
            else:
                parsed += 1
        except Exception as exc:
            # a network error (site unreachable from this machine) is reported, not
            # failed: CI runners outside Korea can be blocked. A parse failure on a
            # page that did load (no options) still fails the check above.
            line = f"row {i} {bu} fetch failed (network): {type(exc).__name__}: {exc}"
        _out(line)
        diag.log(line)
    if hidden and not parsed:
        # every drop page is hidden right now: prove the option parser on an on-sale control
        try:
            title, opts, is_open = gui.fetch_options(parser.product_url(CONTROL_BRANDUID))
            line = f"control {CONTROL_BRANDUID} options={len(opts)} open={is_open}"
            if not opts:
                ok = False
        except Exception as exc:
            line = f"control {CONTROL_BRANDUID} fetch failed (network): {type(exc).__name__}: {exc}"
        _out(line)
        diag.log(line)
    info = Clock().sync(lambda m: diag.log(m))
    _out(f"clock offset {info.get('offsetMs', 0):+.0f}ms method={info.get('method')}")
    try:
        engine.parse_open_at(config.DEFAULT_OPEN_AT)
    except Exception:
        ok = False
    diag.upload("selftest " + ("OK" if ok else "FAILED"), {"mode": "selftest"}, blocking=True)
    _out("SELFTEST OK" if ok else "SELFTEST FAILED")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    diag = Diagnostics()
    install_excepthook(diag)
    try:
        if "--selftest" in argv:
            return selftest(diag)
        if "--guidemo" in argv:
            from foretforet.gui import run_demo
            hold = 150000
            for a in argv:
                if a.startswith("--hold="):
                    hold = int(a.split("=", 1)[1])
            run_demo(hold, diag)
            return 0
        from foretforet.gui import run_gui
        run_gui(diag)
        diag.upload("프로그램 정상 종료", {"mode": "exit"}, blocking=True)
        return 0
    except Exception as exc:
        diag.upload_exception(exc, "main")
        try:
            from tkinter import messagebox
            messagebox.showerror(config.APP_TITLE, f"오류가 발생했습니다: {type(exc).__name__}\n{exc}")
        except Exception:
            pass
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
