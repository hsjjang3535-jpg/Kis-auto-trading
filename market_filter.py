"""시장(지수) 필터 — 약세장에서 신규 매수 차단.

기본 규칙:
  - 코스닥 등락 ≤ MARKET_FILTER_KOSDAQ_MIN (기본 -0.8%) 이면 장중·V자·낙폭 신규 OFF
  - 또는 코스피 ≤ MARKET_FILTER_KOSPI_MIN (기본 -1.0%) 이면 동일 (REQUIRE=any)
  - 종가베팅은 MARKET_FILTER_BLOCK_CLOSING=true 일 때만 차단
  - 조회 실패 시에는 차단하지 않음 (fail-open)
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import kis_api

KST = ZoneInfo("Asia/Seoul")

ENABLED = os.getenv("ENABLE_MARKET_FILTER", "true").lower() == "true"
KOSDAQ_MIN = float(os.getenv("MARKET_FILTER_KOSDAQ_MIN", "-0.5"))
KOSPI_MIN = float(os.getenv("MARKET_FILTER_KOSPI_MIN", "-1.0"))
# any = 둘 중 하나라도 약하면 차단 / both = 둘 다 약해야만 차단
REQUIRE = os.getenv("MARKET_FILTER_REQUIRE", "any").strip().lower()
BLOCK_CLOSING = os.getenv("MARKET_FILTER_BLOCK_CLOSING", "true").lower() == "true"
CACHE_SEC = int(os.getenv("MARKET_FILTER_CACHE_SEC", "60"))

_cache: dict | None = None
_cache_at: float = 0.0
_notified_block_day: str = ""
_notified_clear_day: str = ""
_was_blocked: bool = False


def is_enabled() -> bool:
    return ENABLED


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _snapshot() -> dict:
    """코스피·코스닥 등락률 스냅샷 (캐시)."""
    global _cache, _cache_at
    now = time.monotonic()
    if _cache is not None and (now - _cache_at) < CACHE_SEC:
        return _cache

    kospi = kis_api.get_index_change_pct("0001")
    kosdaq = kis_api.get_index_change_pct("1001")
    snap = {
        "kospi": kospi,
        "kosdaq": kosdaq,
        "at": datetime.now(KST).strftime("%H:%M"),
        "ok": kospi is not None and kosdaq is not None,
    }
    _cache = snap
    _cache_at = now
    return snap


def _weak(rate: float | None, floor: float) -> bool:
    if rate is None:
        return False
    return rate <= floor


def evaluate(*, for_closing: bool = False) -> dict:
    """필터 판정.

    Returns:
      blocked: bool
      reason: str
      kospi / kosdaq: float | None
    """
    if not ENABLED:
        return {
            "blocked": False,
            "reason": "필터OFF",
            "kospi": None,
            "kosdaq": None,
        }
    if for_closing and not BLOCK_CLOSING:
        snap = _snapshot()
        return {
            "blocked": False,
            "reason": "종가필터OFF",
            "kospi": snap.get("kospi"),
            "kosdaq": snap.get("kosdaq"),
        }

    snap = _snapshot()
    kospi = snap.get("kospi")
    kosdaq = snap.get("kosdaq")
    if not snap.get("ok"):
        return {
            "blocked": False,
            "reason": "지수조회실패(통과)",
            "kospi": kospi,
            "kosdaq": kosdaq,
        }

    kospi_weak = _weak(kospi, KOSPI_MIN)
    kosdaq_weak = _weak(kosdaq, KOSDAQ_MIN)

    if REQUIRE == "both":
        blocked = kospi_weak and kosdaq_weak
    else:
        blocked = kospi_weak or kosdaq_weak

    if not blocked:
        return {
            "blocked": False,
            "reason": (
                f"지수OK 코스피{kospi:+.1f}% 코스닥{kosdaq:+.1f}% "
                f"(한도 코스피≤{KOSPI_MIN:g}/코스닥≤{KOSDAQ_MIN:g})"
            ),
            "kospi": kospi,
            "kosdaq": kosdaq,
        }

    parts = []
    if kospi_weak:
        parts.append(f"코스피{kospi:+.1f}%≤{KOSPI_MIN:g}%")
    if kosdaq_weak:
        parts.append(f"코스닥{kosdaq:+.1f}%≤{KOSDAQ_MIN:g}%")
    return {
        "blocked": True,
        "reason": " · ".join(parts),
        "kospi": kospi,
        "kosdaq": kosdaq,
    }


def allow_new_buy(*, for_closing: bool = False) -> tuple[bool, str]:
    """신규 매수 허용 여부. (허용, 사유)"""
    result = evaluate(for_closing=for_closing)
    if result["blocked"]:
        return False, result["reason"]
    return True, result["reason"]


def format_status_line() -> str:
    """시작/상태 보고용 한 줄."""
    if not ENABLED:
        return "지수필터: OFF"
    mode = "종가포함" if BLOCK_CLOSING else "장중만"
    req = "둘다" if REQUIRE == "both" else "하나라도"
    return (
        f"지수필터: ON ({mode} · {req} 약세) "
        f"코스피≤{KOSPI_MIN:g}% / 코스닥≤{KOSDAQ_MIN:g}%"
    )


def notify_if_changed(send_fn) -> None:
    """차단↔해제 전환 시 텔레그램 1회 알림."""
    global _notified_block_day, _notified_clear_day, _was_blocked
    if not ENABLED or not callable(send_fn):
        return
    result = evaluate(for_closing=False)
    today = _today()
    blocked = bool(result["blocked"])
    kospi = result.get("kospi")
    kosdaq = result.get("kosdaq")
    idx = (
        f"코스피 {kospi:+.1f}% · 코스닥 {kosdaq:+.1f}%"
        if kospi is not None and kosdaq is not None
        else "지수 조회 중"
    )

    if blocked and (not _was_blocked or _notified_block_day != today):
        _notified_block_day = today
        _was_blocked = True
        send_fn(
            "🛑 <b>지수필터 — 신규매수 일시중단</b>\n"
            f"{idx}\n"
            f"사유: {result['reason']}\n"
            "대상: 장중·V자·낙폭 신규매수\n"
            "보유 청산(손절·익절)은 정상 동작"
        )
    elif not blocked and _was_blocked and _notified_clear_day != today:
        _notified_clear_day = today
        _was_blocked = False
        send_fn(
            "✅ <b>지수필터 — 신규매수 재개</b>\n"
            f"{idx}\n"
            f"{result['reason']}"
        )
    elif not blocked:
        _was_blocked = False
