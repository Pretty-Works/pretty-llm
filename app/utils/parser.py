# app/utils/parser.py
"""파싱 유틸.

LLM 이 뱉는 날짜 문자열과 백엔드 JSON 의 날짜 필드를 한 곳에서 정규화한다.
Validator 가 날짜 규칙을 검사하려면 우선 문자열을 date 로 안전하게 바꿀 수 있어야 한다.
"""

import re
from datetime import date, datetime

_DATE_PATTERNS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y년 %m월 %d일",
)

# "2026-08-03 ~ 2026-08-07", "2026-08-03~08-07" 같은 기간 표기
_RANGE_SPLIT = re.compile(r"\s*(?:~|-{1,2}>|부터|to)\s*")


# ─── 날짜 ─────────────────────────────────────────────────────────

def parse_date(value: object) -> date | None:
    """날짜처럼 보이는 값을 date 로. 실패하면 None (예외를 던지지 않는다)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        return None

    # ISO datetime ("2026-08-03T09:00:00", "2026-08-03T09:00:00Z")
    iso = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso).date()
    except ValueError:
        pass

    for pattern in _DATE_PATTERNS:
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def parse_date_range(value: object) -> tuple[date | None, date | None]:
    """ "2026-08-03 ~ 2026-08-07" 형태를 (시작, 종료) 로."""
    if value is None:
        return (None, None)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return (parse_date(value[0]), parse_date(value[1]))
    if isinstance(value, dict):
        return (
            parse_date(value.get("start") or value.get("from")),
            parse_date(value.get("end") or value.get("to")),
        )

    parts = _RANGE_SPLIT.split(str(value).strip())
    if len(parts) == 2:
        return (parse_date(parts[0]), parse_date(parts[1]))
    single = parse_date(value)
    return (single, single)


def days_between(start: date | None, end: date | None) -> int | None:
    if start is None or end is None:
        return None
    return (end - start).days


def overlaps(
    a_start: date | None,
    a_end: date | None,
    b_start: date | None,
    b_end: date | None,
) -> bool:
    """두 기간이 하루라도 겹치는지. 경계 포함."""
    if None in (a_start, a_end, b_start, b_end):
        return False
    return a_start <= b_end and b_start <= a_end


# ─── 금액 ─────────────────────────────────────────────────────────

def to_won(value: object) -> int | None:
    """ "12,000,000원", "1200만" 같은 표기를 정수 원 단위로."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip().replace(",", "").replace(" ", "")
    if not text:
        return None

    multiplier = 1
    for suffix, mult in (("억", 100_000_000), ("만", 10_000), ("천", 1_000)):
        if suffix in text:
            text = text.replace(suffix, "")
            multiplier = mult
            break

    digits = re.sub(r"[^0-9.\-]", "", text)
    if not digits or digits in {"-", "."}:
        return None
    try:
        return int(float(digits) * multiplier)
    except ValueError:
        return None


# ─── 텍스트 ───────────────────────────────────────────────────────

def truncate(text: str, limit: int = 300) -> str:
    """프롬프트/로그에 원문을 통째로 넣지 않기 위한 축약."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
