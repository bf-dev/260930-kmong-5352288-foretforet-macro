# -*- coding: utf-8 -*-
"""개인정보 마스킹.

진단 업로드에 들어가는 모든 문자열은 예외 없이 이 모듈을 통과한다.
텍스트 요약, 로그, 페이지 HTML, 요청/응답 본문, 쿠키, 스토리지 전부 해당한다.
한 군데만 적용하고 끝내면 다른 경로로 원문이 새기 때문에 mask() 는
zip 에 넣기 직전 단일 지점(diagnostics.add_text)에서 강제된다.

인증서 비밀번호는 마스킹 이전에 애초에 어떤 수집 경로에도 들어가지 않는다.
그래도 실수로 섞였을 때를 대비해 register_secret() 로 등록된 값은
문자열 치환으로 한 번 더 지운다.
"""
from __future__ import annotations

import re
import threading

_secrets: set[str] = set()
_lock = threading.Lock()

# 주민등록번호 (앞 6자리 = YYMMDD, 뒤 7자리 = 성별숫자 1~4 + 6자리)
#
# 앞 6자리가 **말이 되는 날짜**여야 한다는 조건이 v1.0.12 에 붙었다.
# 그전에는 `\d{6}` 이라 13자리 숫자면 무엇이든 물었고, 하필 밀리초 epoch 이
# 13자리다. 2026-09-03 진단 ZIP 의 xhr_bodies_handover_after.json 이 그렇게
# 망가졌다.
#
#     "t0": 1788393601822   →   "t0": 178839-3******
#
# JSON 이 그 자리에서 깨져 파일 전체를 파싱할 수 없게 됐고, 그날 우리는 예약
# 제출의 왕복 시간을 잃었다. 이번 판은 판정 근거를 바로 그 파일에서 읽으므로
# 고치지 않으면 의미가 없다. (09-04 파일이 멀쩡했던 것은 우연이다:
# 1788480001289 의 7번째 자리가 0 이라 성별숫자 [1-4] 에 안 걸렸을 뿐이다.)
#
# 지금 쓰는 epoch(178…)는 앞 6자리가 "178839" = 88월 이라 날짜가 될 수 없다.
# 실제 주민번호는 언제나 유효한 YYMMDD 로 시작하므로 가리는 힘은 그대로다.
_RRN_DATE = r"\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"
_RRN = re.compile(r"\b(" + _RRN_DATE + r")[-\s]?([1-4]\d{6})\b")
# 휴대폰/일반 전화
_PHONE = re.compile(r"\b(01[016789]|0\d{1,2})[-\s.]?(\d{3,4})[-\s.]?(\d{4})\b")
# 카드번호 16자리
_CARD = re.compile(r"\b(\d{4})[-\s]?(\d{4})[-\s]?(\d{4})[-\s]?(\d{4})\b")
# 이메일
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_SENSITIVE_KEYS = (
    r"uspass|passwd|password|pwd|certpw|certPassword|"
    r"aResult|aSignedMsg|aVidMsg|issacweb_data|token|authorization"
)
# 흔한 비밀번호/토큰 키=값 형태 (JSON, 쿼리스트링, 폼 인코딩)
#
# 값 부분에서 `]` `)` `;` 를 뺀 이유가 있다. 이 규칙은 CSS 선택자
# `input[type="password"]` 도 물어버렸다. `password` 다음의 `"` 를 구분자로
# 보고 그 뒤 `]` 를 값으로 잡아 `***REDACTED***` 로 바꾼 것이다.
# 그러면 CSS 문법이 그 자리에서 깨지고, 크롬은 sub.css 를 10091번째 글자에서
# 읽다 멈춘다. 그 뒤에 있던 `.popup_wrap { display:none }` 이 통째로 죽어서
# 숨어 있어야 할 확인창 사본이 화면에 '보이는' 것으로 렌더된다.
# 즉 마스킹이 증거를 훼손하고 있었다. (2026-08-25 고객 캡처에서 실측)
# `]` 하나만 뺀다. `)` 나 `;` 까지 빼면 그 글자가 든 진짜 비밀번호가 덜 지워진다.
_PWKEY = re.compile(
    r"((?:" + _SENSITIVE_KEYS + r")\s*[=:\"']{1,3}\s*)"
    r"(?!\])([^&\"'<>\s,}\]]{1,4096})",
    re.IGNORECASE,
)
# HTML 폼 속성 형태: <input name="aResult" value="...">
# 위의 _PWKEY 를 그대로 두면 name="aResult" 뒤의 value= 만 지우고 정작 값은
# 그대로 남는다. 실제로 그렇게 새던 것을 테스트가 잡았다.
_PW_ATTR = re.compile(
    r"(name\s*=\s*[\"'](?:" + _SENSITIVE_KEYS + r")[\"'][^>]*?value\s*=\s*[\"'])"
    r"([^\"']*)",
    re.IGNORECASE,
)
# 속성 순서가 반대인 경우: <input value="..." name="aResult">
_PW_ATTR_REV = re.compile(
    r"(value\s*=\s*[\"'])([^\"']*)([\"'][^>]*?name\s*=\s*[\"'](?:" + _SENSITIVE_KEYS + r")[\"'])",
    re.IGNORECASE,
)
# 한글 이름이 들어가는 흔한 필드명
_NAMEKEY = re.compile(
    r"((?:mbrnm|mbrNm|usernm|userNm|chldNm|chldnm|name|nm|성명|이름)\s*[=:\"']{1,3}\s*)"
    r"([가-힣]{2,4})",
)
# 홀로 서 있는 한글 이름 태그값 (<td>홍길동</td> 같은 것)
_NAME_TAG = re.compile(r">\s*([가-힣]{2,4})\s*(?=</(?:td|span|strong|b|dd|li|p)>)")

_NAME_WHITELIST = {
    "어린이집", "서울시", "센터", "신청", "예약", "확인", "취소", "저장", "닫기",
    "선택", "완료", "대기", "가능", "불가", "전체", "오전", "오후", "시간제",
    "보육", "통합반", "독립반", "본인", "아동", "정보", "조회", "삭제", "등록",
    "결제", "안내", "목록", "내용", "구분", "지역", "검색", "이용", "운영",
}


def register_secret(value: str) -> None:
    """실행 중 절대 유출되면 안 되는 값(인증서 비밀번호 등)을 등록한다."""
    if not value or len(value) < 3:
        return
    with _lock:
        _secrets.add(value)


def clear_secrets() -> None:
    with _lock:
        _secrets.clear()


def _mask_rrn(m: re.Match) -> str:
    return f"{m.group(1)}-{m.group(2)[0]}******"


def _mask_phone(m: re.Match) -> str:
    return f"{m.group(1)}-****-{m.group(3)[-2:]}**"


def _mask_name(value: str) -> str:
    if len(value) <= 1:
        return value
    return value[0] + "*" * (len(value) - 1)


def mask(text) -> str:
    """어떤 값이든 마스킹된 문자열로 돌려준다. 절대 예외를 던지지 않는다."""
    if text is None:
        return ""
    try:
        if not isinstance(text, str):
            text = str(text)
    except Exception:
        return "<unprintable>"

    try:
        with _lock:
            secrets = list(_secrets)
        for s in secrets:
            if s:
                text = text.replace(s, "***REDACTED***")

        text = _RRN.sub(_mask_rrn, text)
        text = _CARD.sub(lambda m: f"{m.group(1)}-****-****-{m.group(4)}", text)
        text = _PHONE.sub(_mask_phone, text)
        text = _EMAIL.sub(
            lambda m: (m.group(0)[0] + "***@" + m.group(0).split("@", 1)[1]), text
        )
        # 속성 형태를 먼저 지운다(키=값 규칙보다 우선).
        text = _PW_ATTR.sub(lambda m: m.group(1) + "***REDACTED***", text)
        text = _PW_ATTR_REV.sub(lambda m: m.group(1) + "***REDACTED***" + m.group(3), text)
        text = _PWKEY.sub(lambda m: m.group(1) + "***REDACTED***", text)
        text = _NAMEKEY.sub(lambda m: m.group(1) + _mask_name(m.group(2)), text)
        text = _NAME_TAG.sub(
            lambda m: ">" + (m.group(1) if m.group(1) in _NAME_WHITELIST
                             else _mask_name(m.group(1))),
            text,
        )
        return text
    except Exception:
        return "<masking-failed>"
