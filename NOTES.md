# foretforet open-drop purchase macro (Kmong 5352288, order 7643217)

Customer 배고픈숲3094, Neoworks id f799eaed-99e4-4036-b304-ed38c3166a86, Artifacts customerId `5352288`.
Windows GUI (tkinter, Korean) that logs in to foretforet.com (MakeShop), waits for the drop
(2026-10-01 10:00 KST by default), adds each enabled row (URL, size, qty) to the cart at the exact
second, goes to `/shop/order.html` and STOPS so the customer pays by hand. Optional checkbox (off by
default) completes the order with 무통장입금.

## Build / run
- Repo: `bf-dev/260930-kmong-5352288-foretforet-macro` (`main`). CI: `.github/workflows/build.yml` on
  windows-latest: pytest, PyInstaller onedir noconsole, PE check, `ci/package_zip.py`, Defender scan,
  `--selftest` (live option parse of the 6 default rows + clock sync + Artifacts upload),
  `ci/gui_screenshot.ps1` (`--guidemo`, shows unchecked row 2 and qty-0 row 5), sha256.
  Artifact `foretforet-macro` = zip + `screenshots/gui.png` + `selftest.log`.
- Local: `.venv/bin/python -m pytest tests/ -q` (23 tests). Browser runs need
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
