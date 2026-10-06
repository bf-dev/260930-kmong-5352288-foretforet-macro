# -*- coding: utf-8 -*-
"""Tkinter window for the foretforet purchase macro (Kmong customer 5352288, order 7643217).

Every product row has: use checkbox, product URL, option (size) dropdown filled
from the live page, the 1.0.5 "재고 있는 옵션 전부 담기" checkbox (cart every
in-stock option instead of the typed one), quantity, and a delete button. An unchecked row or quantity
0 is skipped at the drop but keeps its URL and option. Everything except the
password is saved between runs.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import config, parser
from .clock import Clock
from .engine import KST, Engine, parse_open_at
from .reporter import Diagnostics

FONT = ("Malgun Gothic", 10)
FONT_B = ("Malgun Gothic", 10, "bold")
FONT_T = ("Malgun Gothic", 14, "bold")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"}


def fetch_options(url: str) -> tuple[str, list[parser.Option], bool]:
    """(title, options, is_open) for a product URL. Raises on network errors."""
    import requests
    bu = parser.branduid_of(url)
    if not bu:
        raise ValueError("branduid 가 없는 주소입니다")
    r = requests.get(parser.product_url(bu), headers=UA, timeout=12)
    if parser.is_hidden_stub(r.content):
        return HIDDEN_TITLE, [], False
    r.encoding = r.apparent_encoding or "euc-kr"
    html = r.text
    return parser.parse_title(html), parser.parse_options(html), parser.is_open(html)


# order.html radio_paymethod values. 무통장입금 (B) is deliberately absent.
PAY_METHODS = {"카카오페이": "KAKAOPAY", "신용카드": "C", "토스페이": "TOSS", "페이코": "PAYCO"}
HIDDEN_TITLE = "아직 비공개 상품 (오픈 시각부터 자동으로 계속 다시 확인)"


class Row:
    def __init__(self, app: "App", data: dict) -> None:
        self.app = app
        f = app.rows_frame
        self.enabled = tk.BooleanVar(value=bool(data.get("enabled", True)))
        self.url = tk.StringVar(value=data.get("url", ""))
        self.option = tk.StringVar(value=data.get("option", ""))
        self.qty = tk.StringVar(value=str(data.get("qty", 1)))
        # 1.0.5: checked = ignore the option field, cart every option in stock
        self.all_stock = tk.BooleanVar(value=bool(data.get("all_stock", False)))
        self.info = tk.StringVar(value="")
        self.w_no = ttk.Label(f, text="", width=3, anchor="center")
        self.w_chk = ttk.Checkbutton(f, variable=self.enabled, command=self.refresh_style)
        self.w_url = ttk.Entry(f, textvariable=self.url, width=44)
        self.w_opt = ttk.Combobox(f, textvariable=self.option, width=14)
        self.w_load = ttk.Button(f, text="옵션", width=5, command=self.load_options)
        self.w_all = ttk.Checkbutton(f, variable=self.all_stock, command=self.refresh_style)
        self.w_qty = ttk.Spinbox(f, from_=0, to=99, textvariable=self.qty, width=4,
                                 command=self.refresh_style)
        self.w_info = ttk.Label(f, textvariable=self.info, width=24, foreground="#555")
        self.w_del = ttk.Button(f, text="삭제", width=5, command=lambda: app.delete_row(self))
        for v in (self.enabled, self.url, self.option, self.qty, self.all_stock):
            v.trace_add("write", lambda *_: app.schedule_save())
        self.qty.trace_add("write", lambda *_: self.refresh_style())
        self.options: list[parser.Option] = []

    def widgets(self):
        return (self.w_no, self.w_chk, self.w_url, self.w_opt, self.w_load, self.w_all,
                self.w_qty, self.w_info, self.w_del)

    def grid(self, r: int) -> None:
        self.w_no.configure(text=str(r))
        for c, w in enumerate(self.widgets()):
            w.grid(row=r, column=c, padx=2, pady=2,
                   sticky="we" if c == 2 else ("" if w is self.w_all else "w"))
        self.refresh_style()

    def destroy(self) -> None:
        for w in self.widgets():
            w.destroy()

    def data(self) -> dict:
        return config.normalize_rows([{"url": self.url.get(), "option": self.option.get(),
                                       "qty": self.qty.get(), "enabled": self.enabled.get(),
                                       "all_stock": self.all_stock.get()}])[0]

    def active(self) -> bool:
        d = self.data()
        return d["enabled"] and d["qty"] > 0 and bool(d["url"])

    def refresh_style(self) -> None:
        try:
            # the option field is ignored while 전부 담기 is checked (kept, not cleared)
            if self.app.engine is None:
                self.w_opt.state(["disabled"] if self.all_stock.get() else ["!disabled"])
            skip = not self.active()
            self.w_no.configure(foreground="#aaa" if skip else "#000",
                                text=self.w_no.cget("text"))
            if skip and not self.info.get().startswith("건너뜀"):
                self._saved_info = self.info.get()
                self.info.set("건너뜀 (체크 해제/수량 0)")
            elif not skip and self.info.get().startswith("건너뜀"):
                self.info.set(getattr(self, "_saved_info", ""))
        except Exception:
            pass

    def load_options(self, quiet: bool = False) -> None:
        url = self.url.get().strip()
        if not url:
            return
        self.info.set("불러오는 중...")

        def work():
            try:
                title, opts, is_open = fetch_options(url)
                self.app.ui(lambda: self._apply(title, opts, is_open))
            except Exception as exc:
                name = type(exc).__name__
                self.app.ui(lambda: self.info.set(f"불러오기 실패: {name}"))
        threading.Thread(target=work, daemon=True).start()

    def _apply(self, title: str, opts: list[parser.Option], is_open: bool) -> None:
        self.options = opts
        self.w_opt.configure(values=[o.text for o in opts])
        cur = self.option.get().strip()
        opt, note = parser.match_option(cur, opts) if cur else (None, "옵션 선택 필요")
        if opt and opt.text != cur:
            self.option.set(opt.text)
        state = "판매중" if is_open else "오픈 전"
        if self.all_stock.get():
            n = sum(1 for o in opts if o.buyable)
            txt = f"전부 담기: 재고 {n}/{len(opts)}개 · {state}"
        elif opt:
            stock = "무제한" if opt.unlimited else (opt.stock if opt.stock is not None else "?")
            txt = f"재고 {stock} · {state}"
        else:
            txt = f"{note[:14]} · {state}"
        self._saved_info = txt
        self.info.set(txt)
        self.refresh_style()
        self.app.log(f"{self.w_no.cget('text')}번 {title[:28]}: 옵션 {len(opts)}개, "
                     f"선택 {opt.text if opt else '-'} ({txt})")


class App:
    def __init__(self, root: tk.Tk, diag: Diagnostics, demo: bool = False) -> None:
        self.root = root
        self.diag = diag
        self.demo = demo
        self.q: queue.Queue = queue.Queue()
        self.engine: Engine | None = None
        self.rows: list[Row] = []
        self._save_after = None
        self.clock = Clock()
        self.s = config.default_settings() if demo else config.load_settings()

        root.title(f"{config.APP_TITLE} v{config.APP_VERSION}")
        root.geometry("1120x820")
        root.minsize(940, 640)
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass
        for k in ("TLabel", "TButton", "TCheckbutton", "TRadiobutton", "TEntry", "TCombobox"):
            style.configure(k, font=FONT)
        style.configure("Title.TLabel", font=FONT_T)
        style.configure("Head.TLabel", font=FONT_B)
        style.configure("Big.TButton", font=FONT_B, padding=(14, 6))
        root.option_add("*TCombobox*Listbox.font", FONT)

        outer = ttk.Frame(root, padding=10)
        outer.pack(fill="both", expand=True)
        top = ttk.Frame(outer)
        top.pack(fill="x")
        ttk.Label(top, text=config.APP_TITLE, style="Title.TLabel").pack(side="left")
        self.update_var = tk.StringVar(value=f"v{config.APP_VERSION}")
        ttk.Label(top, textvariable=self.update_var, foreground="#666").pack(side="right")

        # 1. products
        box = ttk.LabelFrame(outer, text=" 1. 구매할 상품 (체크 해제 또는 수량 0 = 건너뜀, 주소/옵션은 그대로 보관) ",
                             padding=6)
        box.pack(fill="x", pady=(8, 4))
        # 1.0.4: rows live in a scrollable canvas capped at ROWS_MAX_H, so 20+ rows
        # never push [시작] and the log off the window.
        holder = ttk.Frame(box)
        holder.pack(fill="x")
        self.rows_canvas = tk.Canvas(holder, highlightthickness=0, borderwidth=0, height=60)
        self.rows_scroll = ttk.Scrollbar(holder, orient="vertical", command=self.rows_canvas.yview)
        self.rows_canvas.configure(yscrollcommand=self.rows_scroll.set)
        self.rows_scroll.pack(side="right", fill="y")
        self.rows_canvas.pack(side="left", fill="x", expand=True)
        self.rows_frame = ttk.Frame(self.rows_canvas)
        self._rows_win = self.rows_canvas.create_window(0, 0, window=self.rows_frame, anchor="nw")
        self.rows_frame.bind("<Configure>", lambda e: self._fit_rows())
        self.rows_canvas.bind("<Configure>",
                              lambda e: self.rows_canvas.itemconfigure(self._rows_win, width=e.width))
        self.rows_canvas.bind("<Enter>", lambda e: self.rows_canvas.bind_all("<MouseWheel>", self._rows_wheel))
        self.rows_canvas.bind("<Leave>", lambda e: self.rows_canvas.unbind_all("<MouseWheel>"))
        self.rows_frame.columnconfigure(2, weight=1)
        for c, t in enumerate(("번호", "사용", "상품 주소 (URL)", "옵션(사이즈)", "",
                               "재고 있는 옵션\n전부 담기", "수량", "재고/상태", "")):
            ttk.Label(self.rows_frame, text=t, style="Head.TLabel",
                      justify="center").grid(row=0, column=c, padx=2, sticky="w")
        btns = ttk.Frame(box)
        btns.pack(fill="x", pady=(4, 0))
        self.btn_add = ttk.Button(btns, text="+ 상품 추가", command=self.add_row)
        self.btn_add.pack(side="left")
        self.btn_load_all = ttk.Button(btns, text="옵션 전체 불러오기", command=self.load_all)
        self.btn_load_all.pack(side="left", padx=6)
        self.active_var = tk.StringVar()
        ttk.Label(btns, textvariable=self.active_var, foreground="#0a5").pack(side="right")

        mid = ttk.Frame(outer)
        mid.pack(fill="x", pady=4)
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=1)

        # 2. open time
        tb = ttk.LabelFrame(mid, text=" 2. 오픈 시각 (한국시간) ", padding=6)
        tb.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self.open_at = tk.StringVar(value=self.s.get("open_at") or config.DEFAULT_OPEN_AT)
        self.open_at.trace_add("write", lambda *_: self.schedule_save())
        self.w_open_at = ttk.Entry(tb, textvariable=self.open_at, width=22)
        self.w_open_at.grid(row=0, column=0, sticky="w")
        ttk.Label(tb, text="예: 2026-10-01 10:00:00").grid(row=0, column=1, padx=6, sticky="w")
        self.server_var = tk.StringVar(value="사이트 서버 시각: 확인 중")
        ttk.Label(tb, textvariable=self.server_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.left_var = tk.StringVar(value="")
        ttk.Label(tb, textvariable=self.left_var, foreground="#c40").grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Label(tb, text="3분 전에 로그인·상품 페이지를 미리 열고, 정각에 담습니다.\n상품/오픈 시각은 [시작] 전에 정하세요 (실행 중에는 잠김).",
                  foreground="#666").grid(row=3, column=0, columnspan=2, sticky="w")

        # 3. login
        lb = ttk.LabelFrame(mid, text=" 3. 로그인 ", padding=6)
        lb.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        self.login_type = tk.StringVar(value=self.s.get("login_type") or config.DEFAULT_LOGIN_TYPE)
        self.login_type.trace_add("write", lambda *_: self.schedule_save())
        rf = ttk.Frame(lb)
        rf.grid(row=0, column=0, columnspan=3, sticky="w")
        for t in config.LOGIN_TYPES:
            ttk.Radiobutton(rf, text=t if t != "자체" else "포레포레 아이디", value=t,
                            variable=self.login_type).pack(side="left", padx=(0, 8))
        ttk.Label(lb, text="아이디").grid(row=1, column=0, sticky="w", pady=2)
        self.login_id = tk.StringVar(value=self.s.get("login_id") or "")
        self.login_id.trace_add("write", lambda *_: self.schedule_save())
        ttk.Entry(lb, textvariable=self.login_id, width=24).grid(row=1, column=1, sticky="w")
        ttk.Label(lb, text="비밀번호").grid(row=2, column=0, sticky="w", pady=2)
        self.login_pw = tk.StringVar(value="")
        ttk.Entry(lb, textvariable=self.login_pw, width=24, show="*").grid(row=2, column=1, sticky="w")
        self.remember_id = tk.BooleanVar(value=bool(self.s.get("remember_id", True)))
        self.remember_id.trace_add("write", lambda *_: self.schedule_save())
        ttk.Checkbutton(lb, text="아이디 저장", variable=self.remember_id).grid(row=1, column=2, padx=6, sticky="w")
        self.btn_login = ttk.Button(lb, text="로그인 테스트", command=self.login_test)
        self.btn_login.grid(row=2, column=2, padx=6, sticky="w")
        ttk.Label(lb, text="비밀번호는 저장하지 않습니다. 추가 인증은 뜬 창에서 직접 진행하세요.",
                  foreground="#666").grid(row=3, column=0, columnspan=3, sticky="w")

        # 4. payment
        pb = ttk.LabelFrame(outer, text=" 4. 결제 ", padding=6)
        pb.pack(fill="x", pady=4)
        # 1.0.3: 무통장입금 is never used (bank-transfer orders queue behind
        # card/easy-pay orders, customer 5352288). Default KakaoPay.
        ttk.Label(pb, text="결제수단").pack(side="left")
        cur = self.s.get("pay_method") or "KAKAOPAY"
        self.pay_method = tk.StringVar(value=next((k for k, v in PAY_METHODS.items() if v == cur), "카카오페이"))
        self.pay_method.trace_add("write", lambda *_: self.schedule_save())
        ttk.Combobox(pb, textvariable=self.pay_method, values=list(PAY_METHODS), state="readonly",
                     width=10).pack(side="left", padx=(4, 10))
        self.auto_pay_click = tk.BooleanVar(value=bool(self.s.get("auto_pay_click", True)))
        self.auto_pay_click.trace_add("write", lambda *_: self.schedule_save())
        ttk.Checkbutton(pb, text="동의 자동 체크 후 [결제하기] 자동 클릭 (휴대폰 승인 창까지)",
                        variable=self.auto_pay_click).pack(side="left")
        # 1.0.5: check out what is in the cart after every N finished product rows
        self.checkout_batch = tk.StringVar(value=str(config.checkout_batch(self.s)))
        self.checkout_batch.trace_add("write", lambda *_: self.schedule_save())
        ttk.Label(pb, text="개 상품마다 결제", foreground="#000").pack(side="right")
        self.w_batch = ttk.Spinbox(pb, from_=1, to=config.CHECKOUT_BATCH_MAX,
                                   textvariable=self.checkout_batch, width=3)
        self.w_batch.pack(side="right", padx=4)
        ttk.Label(pb, text="결제 묶음").pack(side="right")

        # 5. controls + log
        cb = ttk.Frame(outer)
        cb.pack(fill="x", pady=(6, 4))
        self.btn_start = ttk.Button(cb, text="시작 (오픈 대기)", style="Big.TButton", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(cb, text="중지", style="Big.TButton", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        self.status_var = tk.StringVar(value="대기 중")
        ttk.Label(cb, textvariable=self.status_var, style="Head.TLabel").pack(side="left", padx=10)
        self.logbox = ScrolledText(outer, height=12, font=("Consolas", 9), state="disabled")
        self.logbox.pack(fill="both", expand=True)

        for d in self.s.get("rows") or []:
            self.add_row(d, save=False)
        self.update_active()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._pump)
        root.after(500, self._tick)
        threading.Thread(target=self._sync_clock, daemon=True).start()
        self.log(f"프로그램 시작 v{config.APP_VERSION} (고객 {config.CUSTOMER_ID})")

    # -------------------------------------------------------------- plumbing
    def ui(self, fn) -> None:
        self.q.put(fn)

    def log(self, msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        try:
            self.diag.log(msg)
        except Exception:
            pass
        self.q.put(("log", line))

    def _pump(self) -> None:
        try:
            while True:
                item = self.q.get_nowait()
                if callable(item):
                    try:
                        item()
                    except Exception:
                        pass
                elif item[0] == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", item[1] + "\n")
                    lines = int(self.logbox.index("end-1c").split(".")[0])
                    if lines > 3000:
                        self.logbox.delete("1.0", "1000.0")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def _sync_clock(self) -> None:
        self.clock.sync(lambda m: self.log(m))

    def _tick(self) -> None:
        try:
            now = self.clock.now()
            self.server_var.set("사이트 서버 시각: " + datetime.fromtimestamp(now, KST).strftime("%Y-%m-%d %H:%M:%S")
                                + f"  (내 PC와 차이 {self.clock.offset_ms:+.0f}ms)")
            try:
                left = parse_open_at(self.open_at.get()) - now
                if left > 0:
                    h, m, s = int(left // 3600), int(left % 3600 // 60), int(left % 60)
                    self.left_var.set(f"오픈까지 {h}시간 {m}분 {s}초")
                else:
                    self.left_var.set("오픈 시각이 지났습니다 (시작하면 바로 담기)")
            except ValueError:
                self.left_var.set("시각 형식 오류: 2026-10-01 10:00:00")
        except Exception:
            pass
        self.root.after(500, self._tick)

    # ------------------------------------------------------------------ rows
    ROWS_MAX_H = 300

    def _fit_rows(self) -> None:
        try:
            need = self.rows_frame.winfo_reqheight()
            self.rows_canvas.configure(scrollregion=self.rows_canvas.bbox("all"),
                                       height=min(need, self.ROWS_MAX_H))
        except Exception:
            pass

    def _rows_wheel(self, e) -> None:
        try:
            if self.rows_frame.winfo_reqheight() > self.rows_canvas.winfo_height():
                self.rows_canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")
        except Exception:
            pass

    def add_row(self, data: dict | None = None, save: bool = True) -> None:
        row = Row(self, data or {"url": "", "option": "", "qty": 1, "enabled": True})
        self.rows.append(row)
        row.grid(len(self.rows))
        self.update_active()
        if save:   # user pressed [+ 상품 추가]: show the new row
            self.root.after(50, lambda: self.rows_canvas.yview_moveto(1.0))
        if save:
            self.schedule_save()

    def delete_row(self, row: Row) -> None:
        d = row.data()
        if d["url"] and not self.demo:
            if not messagebox.askyesno("삭제", f"{self.rows.index(row) + 1}번 줄을 지울까요?\n"
                                             "(잠깐 빼기만 하려면 '사용' 체크를 끄세요)"):
                return
        row.destroy()
        self.rows.remove(row)
        for i, r in enumerate(self.rows, start=1):
            r.grid(i)
        self.schedule_save()

    def load_all(self) -> None:
        for r in self.rows:
            r.load_options()

    def update_active(self) -> None:
        n = sum(1 for r in self.rows if r.active())
        self.active_var.set(f"구매 대상 {n}줄 / 전체 {len(self.rows)}줄")

    # -------------------------------------------------------------- settings
    def collect(self) -> dict:
        return {
            "rows": [r.data() for r in self.rows],
            "open_at": self.open_at.get().strip(),
            "login_type": self.login_type.get(),
            "login_id": self.login_id.get().strip(),
            "pay_method": PAY_METHODS.get(self.pay_method.get(), "KAKAOPAY"),
            "auto_pay_click": bool(self.auto_pay_click.get()),
            "remember_id": bool(self.remember_id.get()),
            "checkout_batch": config.checkout_batch({"checkout_batch": self.checkout_batch.get()}),
        }

    def schedule_save(self) -> None:
        self.update_active()
        if self._save_after:
            try:
                self.root.after_cancel(self._save_after)
            except Exception:
                pass
        self._save_after = self.root.after(400, self.save)

    def save(self) -> None:
        self._save_after = None
        if not self.demo:
            config.save_settings(self.collect())

    # ------------------------------------------------------------------ runs
    def _busy(self, busy: bool, status: str) -> None:
        # the engine copies rows + open time when 시작 is pressed. In 1.0.0 they
        # stayed editable and a mid-run edit was silently ignored (customer
        # 5352288 set 13:02 and one row after 시작, the run kept 10-01 10:00 and
        # the 6 default rows). Lock every input the run depends on.
        flag = ["disabled"] if busy else ["!disabled"]
        widgets = [self.w_open_at, self.btn_add, self.btn_load_all, self.w_batch]
        for r in self.rows:
            widgets += [w for w in r.widgets() if w is not r.w_no]
        for w in widgets:
            try:
                w.state(flag)
            except Exception:
                pass
        if not busy:
            for r in self.rows:
                r.refresh_style()
        self.btn_start.configure(state="disabled" if busy else "normal")
        self.btn_login.configure(state="disabled" if busy else "normal")
        self.btn_stop.configure(state="normal" if busy else "disabled")
        self.status_var.set(status)

    def _make_engine(self) -> Engine:
        self.save()
        run_diag = Diagnostics()
        self.run_diag = run_diag
        return Engine(self.collect(), self.login_pw.get(), run_diag,
                      log=lambda m: self.q.put(("log", f"[{datetime.now().strftime('%H:%M:%S')}] {m}")),
                      on_done=lambda mode, res: self.ui(lambda: self._done(mode, res)))

    def login_test(self) -> None:
        if self.engine:
            return
        self._busy(True, "로그인 테스트 중")
        self.engine = self._make_engine()
        self.engine.run_in_thread("login")

    def start(self) -> None:
        if self.engine:
            return
        try:
            open_ts = parse_open_at(self.open_at.get())
        except ValueError as exc:
            messagebox.showerror("오픈 시각", str(exc))
            return
        if not any(r.active() for r in self.rows):
            messagebox.showwarning("상품", "체크되어 있고 수량이 1 이상인 상품이 없습니다.")
            return
        when = datetime.fromtimestamp(open_ts, KST).strftime("%m월 %d일 %H:%M:%S")
        used = [i for i, r in enumerate(self.rows, 1) if r.active()]
        self.log(f"시작: 오픈 {when}, 사용하는 줄 {', '.join(map(str, used))}번. "
                 "실행 중에는 상품/시각이 잠깁니다 (바꾸려면 [중지] 후 수정하고 다시 시작).")
        self._busy(True, f"{when} 오픈 대기 중")
        self.engine = self._make_engine()
        self.engine.run_in_thread("buy")

    def stop(self) -> None:
        if self.engine:
            self.engine.stop()
            self.status_var.set("중지하는 중...")

    def _done(self, mode: str, res: dict) -> None:
        eng, self.engine = self.engine, None
        result = res.get("result", "?")
        labels = {"login-ok": "로그인 성공", "login-failed": "로그인 실패", "order-page": "주문서 도착",
                  "basket-only": "장바구니까지 완료", "nothing-added": "담기 실패", "stopped": "중지됨",
                  "no-rows": "상품 없음", "error": "오류"}
        self._busy(False, labels.get(result, result))
        self.log(f"결과: {labels.get(result, result)}")
        if mode == "login" and result == "login-ok":
            self.log("로그인 테스트 창은 확인이 끝나면 자동으로 닫힙니다 (정상). "
                     "실제 구매는 [시작]을 누르면 새 창에서 진행됩니다.")
        try:
            meta = {"mode": "login-test" if mode == "login" else "purchase", "result": result,
                    # what the run actually used (the engine snapshot), not the GUI now
                    "openAt": (eng.s.get("open_at") if eng else self.open_at.get()),
                    "loginType": (eng.s.get("login_type") if eng else self.login_type.get()),
                    "rows": (eng.s.get("rows") if eng else [r.data() for r in self.rows]),
                    "serverOffsetMs": round(eng.clock.offset_ms if eng else 0)}
            # 1.0.6: per-row qty -> 1 fallback outcome, readable in the upload text
            fb = [f"{t['branduid']} {t.get('message', '')} {t['fallback']}"
                  for t in (res.get("timings") or []) if t.get("fallback")]
            if fb:
                meta["fallback"] = "; ".join(fb)
            self.run_diag.add_json("result.json", res)
            self.run_diag.upload(f"{meta['mode']}: {result}", meta)
        except Exception:
            pass

    def on_close(self) -> None:
        if self.engine:
            if not messagebox.askyesno("종료", "실행 중입니다. 그래도 종료할까요?"):
                return
            self.engine.stop()
        self.save()
        self.root.destroy()


def _install_tk_guard(root: tk.Tk, app: "App", diag: Diagnostics) -> None:
    """A button handler that raises must never close the window: show it in the
    log, report it once, keep running."""
    def report(exc_type, exc, tb):
        try:
            app.log(f"오류(화면): {exc_type.__name__}: {str(exc)[:200]} (프로그램은 계속 동작합니다)")
        except Exception:
            pass
        try:
            diag.upload_exception(exc, "tk-callback")
        except Exception:
            pass
    root.report_callback_exception = report


def run_gui(diag: Diagnostics) -> None:
    root = tk.Tk()
    app = App(root, diag)
    _install_tk_guard(root, app, diag)
    try:
        from .updater import UpdaterThread
        UpdaterThread(lambda m: app.ui(lambda: app.update_var.set(m)),
                      busy_fn=lambda: app.engine is not None).start()
    except Exception:
        pass
    root.mainloop()


def run_demo(hold_ms: int, diag: Diagnostics, rows: int = 0) -> None:
    """CI screenshot: default rows, row 2 unchecked, row 5 quantity 0, real option lookup.
    rows=N pads the table to N rows (cycling the defaults) to prove the scroll layout."""
    root = tk.Tk()
    app = App(root, diag, demo=True)
    i = 0
    while len(app.rows) < rows:
        app.add_row(dict(config.DEFAULT_ROWS[i % len(config.DEFAULT_ROWS)]), save=False)
        i += 1
    if rows:
        app.log(f"데모: {len(app.rows)}줄 (상품 목록은 스크롤, 시작 버튼과 로그는 항상 보임)")
    app.rows[1].enabled.set(False)
    app.rows[4].qty.set("0")
    app.rows[0].all_stock.set(True)
    app.rows[0].refresh_style()
    app.log("데모: 1번 줄 '재고 있는 옵션 전부 담기' 체크, 2번 줄 체크 해제, 5번 줄 수량 0 "
            "(건너뛴 줄도 주소/옵션은 보관)")
    root.after(800, app.load_all)
    root.after(hold_ms, root.destroy)
    root.mainloop()
