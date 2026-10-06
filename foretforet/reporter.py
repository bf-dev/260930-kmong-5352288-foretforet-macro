# -*- coding: utf-8 -*-
"""Artifacts API 리포터 (Kmong 고객 5352288).

매 실행마다 로그 / 진단 / 페이지 내용 / 주고받은 응답을 ZIP 하나로 묶어
https://works.insu.ng/works/api 로 올린다. 성공한 실행도 올린다.
성공 실행 로그가 있어야 실패한 실행이 읽히기 때문이다.

절대 규칙
  1. 이 모듈은 어떤 경우에도 프로그램을 멈추거나 죽이지 않는다.
     네트워크 실패, 400, 방화벽, 오프라인 전부 조용히 무시한다.
  2. 업로드는 항상 백그라운드 스레드. UI 를 막지 않는다.
  3. 나가는 모든 문자열은 masking.mask() 를 통과한다.
  4. 실행당 1회 + 처리되지 않은 오류당 1회로 제한한다.
"""
from __future__ import annotations

import io
import json
import platform
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .masking import mask

MAX_ZIP_BYTES = 4 * 1024 * 1024
MAX_ENTRY_BYTES = 512 * 1024
MAX_ENTRIES = 120
MAX_LOG_CHARS = 200_000


def _install_dir() -> str:
    """설치 폴더(frozen 이면 exe 가 있는 폴더). 실패해도 절대 던지지 않는다."""
    try:
        if config.is_frozen():
            return str(Path(sys.executable).parent)
        return str(Path(__file__).resolve().parent.parent)
    except Exception:
        return "<unknown>"


def _update_state() -> dict:
    """자동 업데이트 교체가 걸려 있는지. 교체 실패를 서버에서 보기 위한 것."""
    out = {}
    try:
        from . import updater
        out["state"] = updater.read_state()
    except Exception:
        pass
    try:
        p = config.log_dir() / "update-swap.log"
        out["swapLog"] = (p.read_text(encoding="utf-8", errors="replace")[-2000:]
                          if p.exists() else None)
    except Exception:
        pass
    return out


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate_middle(text: str, limit: int) -> str:
    """가운데를 잘라내고 앞뒤를 남긴다. 앞은 시작 상황, 뒤는 실패 지점."""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    cut = len(text) - head - tail
    return text[:head] + f"\n\n... [중략 {cut}자] ...\n\n" + text[-tail:]


class Diagnostics:
    """한 번의 실행에서 모은 것들. 스레드 안전."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[tuple[str, bytes]] = []
        self._log: list[str] = []
        self.started_at = _now()
        self.customer_id = config.CUSTOMER_ID
        self._uploaded_error = False

    # -- 수집 --------------------------------------------------------
    def log(self, line: str) -> None:
        try:
            stamp = datetime.now().strftime("%H:%M:%S")
            with self._lock:
                self._log.append(f"[{stamp}][cust {self.customer_id}] {line}")
                if len(self._log) > 6000:
                    del self._log[1000:3000]
        except Exception:
            pass

    def add_text(self, name: str, text: str) -> None:
        """ZIP 에 텍스트 항목 추가. 마스킹은 여기 한 지점에서 강제된다."""
        try:
            safe = mask(text)
            data = _truncate_middle(safe, MAX_ENTRY_BYTES).encode("utf-8", "replace")
            with self._lock:
                # 같은 이름이면 덮어쓴다. 주기적으로 갱신되는 스냅샷
                # (clock_resync.json 처럼)이 ZIP 에 중복 항목으로 쌓이면
                # 압축이 깨진 것처럼 보이고 마지막 값도 찾기 어렵다.
                for i, (existing, _) in enumerate(self._entries):
                    if existing == name:
                        self._entries[i] = (name, data)
                        return
                if len(self._entries) >= MAX_ENTRIES:
                    return
                self._entries.append((name, data))
        except Exception:
            pass

    def add_json(self, name: str, obj) -> None:
        try:
            self.add_text(name, json.dumps(obj, ensure_ascii=False, indent=2, default=str))
        except Exception:
            pass

    def add_page(self, label: str, url: str, html: str) -> None:
        try:
            idx = len([1 for n, _ in self._entries if n.startswith("page_source/")])
            self.add_text(f"page_source/{idx:04d}_{label}.html", f"<!-- {mask(url)} -->\n{html}")
        except Exception:
            pass

    def add_response(self, method: str, url: str, status, body: str) -> None:
        try:
            idx = len([1 for n, _ in self._entries if n.startswith("requests/")])
            self.add_json(
                f"requests/{idx:04d}.json",
                {
                    "method": method,
                    "url": mask(url),
                    "status": status,
                    "body": mask(_truncate_middle(body or "", MAX_ENTRY_BYTES // 2)),
                    "at": _now(),
                },
            )
        except Exception:
            pass

    # -- 메타 --------------------------------------------------------
    def meta(self, extra: dict | None = None) -> dict:
        info = {
            "customerId": self.customer_id,
            "kmongOrderId": config.ORDER_ID,
            "app": config.APP_SLUG,
            "appVersion": config.APP_VERSION,
            "startedAt": self.started_at,
            "finishedAt": _now(),
            "frozen": config.is_frozen(),
            "python": sys.version.split()[0],
            "os": f"{platform.system()} {platform.release()} ({platform.version()})",
            "machine": platform.machine(),
            "targetSite": config.BASE_URL,
            # 설치 위치. 2026-09-02 자동 업데이트 사고 때 고객 PC 의 설치
            # 경로를 우리가 한 번도 받아본 적이 없어서, 교체가 왜 실패했는지
            # 서버에서 좁힐 수가 없었다. 비밀이 아니고 진단에 결정적이다.
            "installDir": _install_dir(),
            "updateState": _update_state(),
        }
        if extra:
            for k, v in extra.items():
                info[k] = v
        return info

    # -- 업로드 ------------------------------------------------------
    def build_zip(self, extra_meta: dict | None = None) -> bytes:
        buf = io.BytesIO()
        try:
            with self._lock:
                entries = list(self._entries)
                logtext = "\n".join(self._log)
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("meta.json", json.dumps(self.meta(extra_meta),
                                                   ensure_ascii=False, indent=2))
                z.writestr("run.log", mask(_truncate_middle(logtext, MAX_LOG_CHARS)))
                for name, data in entries:
                    if buf.tell() > MAX_ZIP_BYTES:
                        break
                    z.writestr(name, data)
        except Exception:
            return b""
        return buf.getvalue()

    def summary_text(self, headline: str, extra_meta: dict | None = None) -> str:
        m = self.meta(extra_meta)
        lines = [
            f"[{config.APP_SLUG} v{config.APP_VERSION}] 고객 {self.customer_id} / 주문 {config.ORDER_ID}",
            headline,
            f"OS: {m['os']} / frozen={m['frozen']}",
            f"시작 {m['startedAt']} → 종료 {m['finishedAt']}",
        ]
        for k in ("mode", "openAt", "rows", "serverOffsetMs", "result", "loginType", "fallback"):
            if extra_meta and k in extra_meta:
                lines.append(f"{k}: {extra_meta[k]}")
        with self._lock:
            tail = self._log[-25:]
        lines.append("--- 최근 로그 ---")
        lines.extend(tail)
        return mask("\n".join(lines))

    def upload(self, headline: str, extra_meta: dict | None = None,
               blocking: bool = False) -> None:
        """실행 결과를 올린다. 실패해도 절대 예외를 밖으로 내보내지 않는다."""
        def _work():
            try:
                import requests  # 지연 임포트: 없더라도 앱은 돌아야 한다
                blob = self.build_zip(extra_meta)
                text = self.summary_text(headline, extra_meta)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                fname = f"{config.APP_SLUG}-{self.customer_id}-{stamp}.zip"
                payload = {
                    "customerId": self.customer_id,
                    "source": config.ARTIFACT_SOURCE,
                    "text": text,
                }
                if blob:
                    r = requests.post(
                        config.WORKS_API, data=payload,
                        files={"file": (fname, blob, "application/zip")}, timeout=25)
                else:
                    # 첨부가 없으면 multipart 가 아니라 JSON 으로 보내야 한다.
                    # 폼 인코딩 + 파일 없음은 게이트웨이가 400 으로 거절한다.
                    r = requests.post(config.WORKS_API, json=payload, timeout=25)
                self.log(f"진단 업로드 status={r.status_code}")
                try:
                    body = r.json()
                    self.log(f"진단 업로드 matched={body.get('data', {}).get('matched')}")
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001 - 무슨 일이 있어도 조용히
                try:
                    self.log(f"진단 업로드 실패(무시): {type(exc).__name__}")
                except Exception:
                    pass

        try:
            if blocking:
                _work()
            else:
                threading.Thread(target=_work, daemon=True).start()
        except Exception:
            pass

    def heartbeat(self, text: str) -> None:
        """Short JSON-only status post (no zip), in a daemon thread. Never raises."""
        def _work():
            try:
                import requests
                r = requests.post(config.WORKS_API, json={
                    "customerId": self.customer_id, "source": config.ARTIFACT_SOURCE,
                    "text": f"[heartbeat] cust {self.customer_id} v{config.APP_VERSION} {text}"}, timeout=15)
                self.log(f"heartbeat status={r.status_code}")
            except Exception as exc:  # noqa: BLE001
                try:
                    self.log(f"heartbeat 실패(무시): {type(exc).__name__}")
                except Exception:
                    pass
        try:
            threading.Thread(target=_work, daemon=True).start()
        except Exception:
            pass

    def upload_exception(self, exc: BaseException, where: str = "") -> None:
        """처리되지 않은 오류당 1회."""
        try:
            if self._uploaded_error:
                return
            self._uploaded_error = True
            import traceback
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            self.add_text("error/traceback.txt", tb)
            self.log(f"오류 발생({where}): {type(exc).__name__}: {exc}")
            self.upload(f"실행 중 오류: {type(exc).__name__} @ {where}",
                        {"result": "error"})
        except Exception:
            pass


def install_excepthook(diag: "Diagnostics") -> None:
    """처리되지 않은 예외도 반드시 보고되게 한다."""
    try:
        prev = sys.excepthook

        def _hook(etype, value, tb):
            try:
                diag.upload_exception(value, "excepthook")
                time.sleep(2)
            except Exception:
                pass
            try:
                prev(etype, value, tb)
            except Exception:
                pass

        sys.excepthook = _hook
    except Exception:
        pass
