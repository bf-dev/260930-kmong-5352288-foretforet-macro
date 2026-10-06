# foretforet open-drop purchase macro (Kmong 5352288, order 7643217)

Customer 배고픈숲3094, Neoworks id f799eaed-99e4-4036-b304-ed38c3166a86, Artifacts customerId `5352288`.
Windows GUI (tkinter, Korean) that logs in to foretforet.com (MakeShop), waits for the drop
(2026-10-01 10:00 KST by default), adds each enabled row (URL, size, qty) to the cart at the exact
second, goes to `/shop/order.html`, and (since 1.0.3) in one pass selects KakaoPay, ticks every
required consent and clicks 결제하기 so the KakaoPay window/QR opens; the customer approves on the phone.
무통장입금 is never used (customer: bank-transfer orders queue behind). Pay method combo defaults to
카카오페이; the auto-click checkbox can be turned off to stop on order.html instead.

## Build / run
- Repo: `bf-dev/260930-kmong-5352288-foretforet-macro` (`main`). CI: `.github/workflows/build.yml` on
  windows-latest: pytest, PyInstaller onedir noconsole, PE check, `ci/package_zip.py`, Defender scan,
  `--selftest` (live option parse of the 6 default rows + clock sync + Artifacts upload),
  `ci/gui_screenshot.ps1` (`--guidemo`, shows unchecked row 2 and qty-0 row 5), sha256.
  Artifact `foretforet-macro` = zip + `screenshots/gui.png` + `selftest.log`.
- Local: `.venv/bin/python -m pytest tests/ -q` (34 tests). Browser runs need
  `xvfb-run -a -s "-screen 0 1400x1000x24"`, playwright 1.63.0, channel `chrome`.
- Settings/profile: `%APPDATA%\ForetforetMacro` (`~/.config/ForetforetMacro` on Linux),
  persistent Playwright profile in `browser-profile/`. Password is never persisted.

## Release / updater
- `gh run download <id> -D out/ci-<ver>` then
  `~/workspace/scripts/works-publish 5352288 out/ci-<ver>/foretforet-macro/out/foretforet-macro-<ver>.zip foretforet-macro-<ver>.zip`
- Manifest `https://static.neoworks.us/5352288/version-foretforet.json`
  `{"version","zipUrl","sha256"}`; the updater needs zip >= 5,000,000 bytes (MIN_ZIP_BYTES).
  Never overwrite a served filename: bump APP_VERSION in `foretforet/config.py` and version_info.
- 1.0.0: CI run 36664786660, sha256 7b3b4e4ba32faefe54ae22b5c38eb4622e2f38cbd1d4e78956c51dae363085e7.
- 1.0.1 (2026-09-30 13:20 KST): CI run 36667984940, sha256 4b2fca727515529cf327d49e08a6c889147051e8ec7df6957ca7edb15f986f06,
  54,356,711 bytes. Manifest updated with `FORCE=1 works-publish 5352288 <json> version-foretforet.json`
  (the manifest is the one file that is overwritten; it is served max-age=300).
- Updater (from 1.0.1) skips checks while a login test or purchase run is active (`busy_fn`),
  because the swap ends the process with `os._exit` and would kill a run waiting for the drop.
  1.0.0 has no such guard: never publish a new manifest close to a drop while 1.0.0 may be waiting.

## Site facts (verified live 2026-09-30)
- Product page: `select[name="optionlist[]"]`; `select_option` creates `#MS_amount_basic_<idx>`;
  qty via `set_amount(el,'basic')`; `send_multi('', '')` guarded by `window._is_send_multi`, POSTs
  `basket.action.html`, JSON `status` + `etc_data.basket_uid_array`.
- Closed/preview: `.shopdetailButtonTop` contains `product_caution` (all 6 rows were in this state).
- NetFunnel popup `#NetFunnel_Loading_Popup` (+ `_Count`, `_NextCnt`, `_TimeLeft`): never reload under it.
- Basket `/shop/basket.html`: `input[name=basketchks]` (chk_data_uid `pr_NORMAL_<uid>_<cart_id>`) +
  `basket_item` JSON; `multi_order('')` (guard `_is_multi_order`), `basket_multidel(1)`.
- Login state check: GET `/shop/mypage.html` with max_redirects=0, 200 and no member.html = logged in.
- **MakeShop login is a session cookie**: it does not survive closing the browser, so every run
  logs in again at preload (3 min before open). Naver device trust does persist in the profile.
- **Stale SNS cookies gotcha**: with leftover login_id / sns_login_service cookies,
  `member.html?type=login` renders the SNS JOIN form and `sns_login_log('naver')` throws
  (`obj.passwd`), and mypage <-> `member.html?type=reserve` loops. Fix in `ensure_login`: go straight to
  `/list/API/login_naver.html` (302 to nid.naver.com OAuth authorize).
- Naver 2026 login page: `#id`, `#pw`, visible button `#loginBtn_row` / `#loginBtn_column`
  (old `#log.login` hidden). Flow nidlogin.login -> allow_oauth -> `/list/API/login_naver.html` -> `/html/mainm.html`.
  No 2FA/captcha seen from a KR IP.
- Kakao: `/list/API/login_kakao.html` 302s to the home page without a session, so Kakao still uses
  `ks_login_log('')` on member.html. Untested live (no Kakao account); may hit the same stale-cookie issue.

## Testing
- This box is blocked by the site outside Korea for some paths: tests used an ssh SOCKS tunnel
  `ssh -f -N -D 127.0.0.1:18765 unicorn@external-2` (egress 115.68.232.141, gives occasional
  ERR_NETWORK_CHANGED). Kill it afterwards.
- Live e2e (2026-09-30, Naver login): in-stock item 10254531 RLL,2_3Y, simulated open, cart add OK,
  reached order.html, cleanup removed the test item. No order was ever placed. Over the tunnel the
  add landed 10s after open (page reload is slow over SOCKS: DOM ready 3.5 to 9.6s, JS usable 2 to 4.7s);
  a possible speed-up is `wait_until="commit"` + waiting for `send_multi`/select instead of domcontentloaded.
- Customer test credentials: only in the coordinator brief (session transcript), never in repo/notes.
  Avoid repeated typed Naver logins (account lock risk).

## Default rows (customer picks)
10279528 CBK,9_12M x1; 10279528 CBK,1_2Y x1; 10279589 MOB,9_12M x1 (customer said CBK, product only
has MOB); 10279533 CBW,1_2Y x2; 10279540 CBW,2_3Y x1; 10240351 CBB,1_2Y x1 (preview/closed item).

## Reporting
- Artifacts API source `foretforet-macro-run` (one ZIP per run, masked), devnotes `foretforet-macro-devnote`.

## 2026-09-30 customer report diagnosis (1.0.1)
Customer ran 1.0.0 at 12:57 KST: "창이 꺼져요", "blank", then at 13:03 it worked (order-page).
- Run used 2026-10-01 10:00 / 5 products although the GUI later showed 13:02 / 1 row: NOT a
  stale-read bug. `Engine(self.collect())` snapshots the GUI when 시작 is pressed (12:57:55); at that
  moment the GUI still had the default time and all 6 default rows (5 distinct branduids, rows are
  grouped by branduid). The customer then added row 7, unchecked 1-6 and typed 13:02 while the run
  was waiting; 1.0.0 left the inputs editable and ignored the edits. The stopped-run meta showed the
  edited values because `_done` read the live GUI. Fix in 1.0.1: all row widgets, + 상품 추가,
  옵션 전체 불러오기 and the open time Entry are disabled while a run is active; start logs the rows in
  use; `_done` meta now reports the engine snapshot (`eng.s`), not the live GUI.
- Login-test window closing after login-ok is by design (`login_test` closes the context). 1.0.1
  logs a line saying so.
- about:blank until 3 minutes before open is by design (preload at open-180s). 1.0.1 shows a
  local countdown page via `page.set_content` (no network). Login, cart and timing logic unchanged.
- Rejected in this release (drop is 2026-10-01 10:00): logging in early at 시작 and a keep-warm
  navigation loop. They change login/timing behaviour; not shipped.
- Tk callback errors are logged + uploaded (`_install_tk_guard`) instead of being silent.

## 2026-10-01 drop pages hidden behind "존재하지 않는 상품" (1.0.2)

- From about 09:00 KST on drop day, all 5 drop branduids (10279528, 10279589, 10279533, 10279540, 10240351)
  return 200 with only `<script>alert('존재하지 않는 상품입니다.');parent.location.href='/';</script>`
  (111 bytes), desktop and mobile. Any nonexistent branduid (e.g. 10999999) returns the same body, so it is
  the test stand-in. Control 10254531 stays a full page. Detector: `parser.is_hidden_stub(body)`.
- What 1.0.1 did (live run, tmp e2e `alert_run.py`, open +60 s): the preload logs "옵션 없음" and keeps the row.
  After open, `_wait_open` re-navigates the tab. The alert plus redirect to '/' made `goto(domcontentloaded)` hit
  30 s TimeoutErrors, so it got only about 4 to 6 checks per row in 70 s. When the fake page was un-hidden, it took
  about 42 s before the item was carted. It kept retrying (no hard fail, not stuck on '/') but was far too slow.
- 1.0.2: `_wait_open` first polls with a raw GET through `ctx.request` (browser cookies plus the real UA) every
  `HIDDEN_POLL_MS`=400. While the body is the stub, it never touches the tab. Once the body is anything else,
  it falls through to the original goto + NetFunnel + `_JS_PAGE_STATE` loop. `OPEN_WAIT_MAX_SECONDS` went from 600 to 900.
  The preload logs that the page is hidden. The GUI shows the title "아직 비공개 상품 ...". The selftest accepts hidden
  rows and proves the parser on control 10254531 when every drop row is hidden (otherwise CI fails today).
- 1.0.2 live run: about 21 raw checks in 25 s, detected 1 s after un-hide, carted 14 s later (slow through the SOCKS
  tunnel), test item removed, Artifacts upload 200 matched=True. CI run 36794432008, zip sha256 d401b491...
- Open question: whether NetFunnel fronts the raw GET at open. If NetFunnel serves its own page, the body is no
  longer the stub, so we fall through to the NetFunnel-aware tab loop, which is the safe direction.

## 2026-10-01 drop ended basket-only (1.0.3)
Evidence: 1.0.2 run ZIP of the 10:00 drop. Cart had 6 items, then 전체상품주문 alerted sold out / "재고가 현재
1개", the app stopped on basket.html; on order.html the customer then hit "환불계좌 수집/설정 동의" and
"개인정보 수집/이용 약관" alerts with the pay button doing nothing while stock sold out.
- Basket: `parser.parse_stock_alerts` reads Makeshop's multi-line stock alerts (product names contain
  brackets, so greedy match to the last `]`). The engine drops sold-out rows / lowers qty to the remaining
  stock, then retries 전체상품주문 until order.html or nothing is left.
- Order page: `engine.prepare_order(page, click)` runs `_JS_PREPARE_ORDER` once right after load:
  default address, KakaoPay radio, every required consent checkbox (환불계좌, 개인정보, 주문/결제 동의,
  전체동의), then calls the shop's own `send()`. Up to 3 tries; each waits 6 s for the KakaoPay popup or
  an alert, and on a 동의/약관/환불계좌 alert it re-ticks and retries.
- ROOT CAUSE of the dead pay button: order.html runs jQuery 1.7.2, where one throwing
  `$(document).ready` callback aborts all later ones. A tracker (ChannelIO in footer.1.js, kakaoPixel,
  google_tag_manager, MSLOG_code, request_init_spm_iframe) throws first, so (a) the pay-method radio
  handler never binds and hidden `paymethod`/`simplepay_type` stay empty, (b) the ready callback that
  checks `place[value=S]` and calls `addrclick()` (default address) is skipped, so send() alerts
  "받는분의 성함을 입력하세요." Hardening: the prepare JS sets form1 `paymethod=C` +
  `simplepay_type` (KAKAOPAY=KKP, PAYCO=PC, TOSS=TOS) itself, and calls `addrclick()` BEFORE ticking
  consents (calling it after cleared pay_agree).
- Offline replay (scratch, `~/workspace/kmong/tmp/ff103/replay/`, disposable): the real captured
  order.html served via Playwright route with all POSTs mocked/blocked. With tracker stubs and with
  `NOSTUB=1` (real tracker failure) both reach `form1` submit to `/ssllogin/order.html` target
  HIDDEN_PROCESS with pm C / sp KKP. In the NOSTUB case addrclick still throws "Cannot convert
  undefined or null to object" but receiver is filled first and send() submits.
- Not done: a live logged-in end-to-end on foretforet (no test creds in env, and never approve KakaoPay).
  Safe stop point for any future live test: the KakaoPay approval window.
- Latency: product goto 10 s (`commit`), readyState wait 8 s, option select wait 3 s, no 40 s retry
  stalls. Armed heartbeat: one JSON post (`[heartbeat] ...`) when armed, via `reporter.heartbeat`.
- Tests: `tests/test_order_flow.py` covers unbound radio handler and aborted-ready default address.
- CI run 36803894157 (commit 88fe236). Zip 54,374,087 bytes, sha256
  68131ea6d654373fad25d1fac0f3bb8a709614cd4191522dc46e5b468f0c431a, published to
  https://static.neoworks.us/5352288/foretforet-macro-1.0.3.zip, manifest updated with FORCE=1.
  Artifacts: CI selftest row and engineer verification row arrived (matched=true).

## 2026-10-02 drop-time fixes for 10-20 rows (1.0.4)
Context: customer registers 10-20 rows at 17:00 KST, many stay sold out. 1.0.3 waited for every product
until the deadline and read `basket_responses[-1]`, so with many tabs one product could take another's reply.
- Fix 1, sold-out grace drop: `engine.fire_all` sets `drop_at = fire + config.SOLDOUT_GRACE_SECONDS` (30).
  `_wait_open` returns `_drop(prod)` once past drop_at while the page is still hidden (raw-GET stub) or
  shows `product_caution`. `prod.dropped=True`, message "품절 상태로 건너뜀", log line
  `품절 상태로 건너뜀: <title> [bu] ...`. Time spent under a NetFunnel popup pushes drop_at back by the
  grace. Pre-fire polling and arming are unchanged (drop_at only exists inside fire_all).
- Fix 2, per-tab attribution: `_attach` on_response stores `"page": page` in each basket record;
  `_add_to_cart` only accepts records after its own `before` index whose page is its own tab.
- Fix 3, close resolved tabs: `_fire_product` wraps `_fire_product_once` in `finally: _close_product_tab`
  (snapshot `product_<bu>` into the run ZIP, then close). fire_all calls it again for every product
  (idempotent, `prod.page` set to None). Never touches the main/order tab, the KakaoPay popup or ctx.
- Fix 4, GUI: rows live in a Canvas + Scrollbar (`rows_canvas`, `rows_frame`, `ROWS_MAX_H=300`, wheel
  scrolls only when overflowing). `--guidemo --rows=N` pads to N rows; CI step "GUI screenshot (20 rows)"
  writes `screenshots/gui20.png`.
- Tests: `tests/test_drop_flow.py`, fully offline (Playwright route serves `c_10279533` / `p_10254531`
  fixtures with a stub send_multi, basket.action answered locally with crossing delays, everything else
  aborted, `_fetch_product` monkeypatched). Headless page load of the fixture takes ~1 s on the build box,
  so timing assertions use grace >= 1.5 s (6 s in the tab-close test).
- CI run 36948337875 (commit 9618b14). Zip 54,376,870 bytes, sha256
  676539baf5654ebc392b5b97c432c139c1b7a65113cc235b03bab60d4ee8b0b2, published to
  https://static.neoworks.us/5352288/foretforet-macro-1.0.4.zip, manifest at 1.0.4 (FORCE=1).
  Verified with the 1.0.3 updater code (git archive f2a78d5): choose_download picks the 1.0.4 zip,
  `_download` passes MIN_ZIP_BYTES, sha matches. Artifacts: CI selftest v1.0.4 row and engineer
  verification row (matched=true), devnote posted.
- Note: urllib default UA gets 403 from static.neoworks.us (Cloudflare); `requests` and curl are fine.

## 2026-10-02 17:00 취소분 drop carted 0/10 -> 1.0.5

Root cause of 0/10 (1.0.4): after the drop the product page showed the soldout_area, which 1.0.4
read as "not open" and reloaded; on registered SOLDOUT options it still selected them, the site
alerted, no amount input appeared, and the 3 s wait was retried 3x per row; unregistered sizes
ended as "담을 옵션 없음". At 17:00 every registered target option was SOLDOUT or absent; the only
stock was the unregistered CBW,4_5Y (SALE, stock 1) on 10279535. 10 tabs were not the cause.

1.0.5 behavior (customer-confirmed spec):
- All rows open in parallel; each row is checked ONCE after open. Sold out after open is final.
  Missing size = instant skip ("옵션 없음, 바로 건너뜀"). Only "not open yet" retries.
- One cart try per row (`send_multi` once, wait for its own basket.action reply), no reload.
- Checkout in groups of `결제 묶음` (default 3, UI setting) finished attempts; a group with 0 carted
  is skipped; last group may be smaller. `checkout_group(k, group, page, click)`.
- Per-row checkbox "재고 있는 옵션 전부 담기" (`all_stock` in the saved row). ON: every cartable
  option at row qty, capped by the site's per-option max_amount / stock, one pass. OFF: 1.0.4 manual.
- Checkbox ON + page does not exist -> "상품 없음", no retry, counts as a finished attempt.
  Checkbox OFF keeps retrying (official open hides pages as "존재하지 않는 상품" until open).
  Live detection: `branduid=99999999` returns HTTP 200, 111-byte stub with
  `alert('존재하지 않는 상품입니다.')` + redirect to "/"; a bad path is HTTP 404 Makeshop 403page.
  Fixtures: tests/fixtures/n_notexist_stub.html, n_makeshop404.html.
- Product tabs block images/fonts/media and trackers.

Gotchas found live (2026-10-02, guest run through the KR tunnel):
- **Blocking daumcdn kp.js breaks carting.** The site's `send_multi` (/js/jquery.multi_option.js)
  calls `insert_kakao_pixel_basket()` -> `kakaoPixel(id).addToCart()` unguarded BEFORE
  `common_basket_send`; with kakaoPixel undefined it throws and nothing is carted. Fix:
  `TRACKER_STUBS` init script (Proxy no-op for kakaoPixel, fbq, Kakao, ChannelIO) on product tabs.
  The STUB in tests/test_drop_flow.py calls kakaoPixel too, so the regression is covered.
- **Guest cart cookie.** Only basket.html sets the guest `tempid` cookie (product pages and the
  home page do not). Without it, parallel basket.action posts can each open their own guest
  cart. purchase() now loads BASKET_URL on the main page before firing when not logged in.
- Guest checkout: multi_order without login lands on /shop/qmember.html; engine jumps to
  /shop/order.html?type=guest. 10254531 is 회원전용 (member-only): the guest order is refused
  with an alert, engine logs it and stops that batch instead of looping.

Live evidence method (do NOT use the customer account, never pay): a guest-only script
(kept outside the repo in ~/workspace/kmong/tmp/ff105/live_run.py, disposable) builds an
Engine with fixed rows, routes Playwright through `ssh -D 18765 unicorn@external-2`, runs
`fire_all(batch=3, on_batch=checkout_group(click=False))`, reads the basket, compares each row's
basket.action result with actual basket rows, then `remove_from_cart` to empty it.
In-stock test products used on 2026-10-02: 10254531 (RLL sizes, member-only), 10275092 (20D,10Y).

1.0.5 release: commit 610c3b7, CI run 36993873871 (tests, PE, Defender, selftest, gui.png all green),
zip sha256 14cde837c25478742c22062e39ca49090e488504553794cf691d803ca51a5205, manifest bumped 2026-10-02.

## 2026-10-06 stock shortfall -> retry once with qty 1 (1.0.6)

Customer request (2026-10-06): "남은 수량 말고 그냥 한 개로 해서 담아 주세요 ... 1개 담고 품절일경우 패스".
So the macro NEVER computes remaining stock any more. `cap_qty` only applies the site per-option
max (`max_amount`), never the stock number.

Behaviour, per option (checkbox rows apply it to every option they cart):
- qty > 1 and the cart attempt fails for a stock shortfall -> ONE retry at qty 1, then stop.
  Success status: "재고 부족, 1개 담음". qty 1 also fails, or the answer is sold out: "품절, 건너뜀"
  (dropped). Any other failure keeps the raw shop message. No loops.
- Two shortfall signals, both verified:
  1. Client clamp: shop JS `set_amount(inp,'basic')` alerts
     "선택하신 상품의 옵션은 수량이 부족합니다.\n수량을 조절해주세요." and resets the input to 1.
     `_pick_send` reports `clamped`; result "client-1", the row is carted x1 in the same send.
  2. Server reply from basket.action: "[name]상품의 재고가 현재 N개 입니다." (`stock_short(msg)`:
     has 재고/부족/수량 and no 품절). Engine reloads the page once (`_load`), re-picks at qty 1,
     one `send_multi`. Result "ok-1" or "soldout". Sold out text: "선택된 상품/옵션은 품절입니다."
- Checkout (go_order): a basket stock alert lowers the item to 1 (was: to the remaining count).
- `timings()` entries carry `fallback: [{options, requested, result}]`; gui.py puts them in
  meta.json `fallback` and reporter.summary_text includes it in the upload text.
- Tests: tests/test_drop_flow.py has 5 new cases (retry once ok, retry fails -> soldout, qty 1
  never retries, checkbox row retries every option, client clamp reported). 53 pass locally (.venv).

Live evidence 2026-10-06 06:27 KST (guest, no payment, ~/workspace/kmong/tmp/ff106/live_run.py,
disposable): 10254531 "RLL,9_12M" qty 2 with live stock 1 -> shop alert above ->
"재고 부족, 1개 담음: RLL,9_12M (사이트가 1개로 줄임)", basket 1개 MATCH; 10275092 "20D,10Y" carted
normally; guest order refused as 회원전용 (expected); both test items removed, basket 0 rows.
Option 3_4Y on 10254531 no longer exists. Port 18765 may already be held by an older
`ssh -D` tunnel; check `ss -ltnp | grep 18765` and kill the stale one after testing.

1.0.6 release: commit 1994918, CI run 37413929098 (tests, selftest, gui.png green),
zip sha256 e66568e9dc14e04be92401e99248cc986550afd7faa1966bbc47852586ab5ace, manifest bumped 2026-10-06.
