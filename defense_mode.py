"""방어 모드 — 연속 손절·일/주 손실 한도 초과 시 신규매수 중단.

보유 청산(손절·익절·강제청산)은 그대로 동작.
ENABLE_DEFENSE_MODE=false 이면 항상 통과.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

ENABLED = os.getenv("ENABLE_DEFENSE_MODE", "true").lower() == "true"
MAX_CONSEC_LOSSES = int(os.getenv("DEFENSE_MAX_CONSEC_LOSSES", "2"))
DAILY_LOSS_LIMIT = int(os.getenv("DEFENSE_DAILY_LOSS_LIMIT", "-40000"))
WEEKLY_LOSS_LIMIT = int(os.getenv("DEFENSE_WEEKLY_LOSS_LIMIT", "-50000"))
BLOCK_CLOSING = os.getenv("DEFENSE_BLOCK_CLOSING", "true").lower() == "true"

# 상태 (trading_state에 저장)
_pause_until: str = ""  # YYYY-MM-DD inclusive
_pause_reason: str = ""
_notified_key: str = ""


def is_enabled() -> bool:
    return ENABLED


def dump_state() -> dict:
    return {
        "pause_until": _pause_until,
        "pause_reason": _pause_reason,
        "notified_key": _notified_key,
    }


def load_state(data: dict | None) -> None:
    global _pause_until, _pause_reason, _notified_key
    if not isinstance(data, dict):
        return
    _pause_until = str(data.get("pause_until") or "")
    _pause_reason = str(data.get("pause_reason") or "")
    _notified_key = str(data.get("notified_key") or "")


def clear_pause() -> None:
    global _pause_until, _pause_reason
    _pause_until = ""
    _pause_reason = ""


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _week_mon_fri(ref: datetime | None = None) -> list[str]:
    now = ref or datetime.now(KST)
    monday = (now - timedelta(days=now.weekday())).date()
    return [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(5)]


def _week_friday(ref: datetime | None = None) -> str:
    return _week_mon_fri(ref)[4]


def _consec_losses(trades: list[dict]) -> int:
    """최근 연속 손실 건수 (실전 체결만, 끝에서부터)."""
    n = 0
    for t in reversed(trades or []):
        try:
            won = int(t.get("profit_won", 0))
        except (TypeError, ValueError):
            won = 0
        if won <= 0:
            n += 1
        else:
            break
    return n


def _today_pnl(trades: list[dict]) -> int:
    total = 0
    for t in trades or []:
        try:
            total += int(t.get("profit_won", 0))
        except (TypeError, ValueError):
            pass
    return total


def _weekly_pnl(ledger: list[dict], trades_today: list[dict], today: str) -> int:
    week = set(_week_mon_fri())
    total = 0
    for row in ledger or []:
        d = str(row.get("date") or "")
        if d in week and d != today:
            try:
                total += int(row.get("profit_won", 0))
            except (TypeError, ValueError):
                pass
    total += _today_pnl(trades_today)
    return total


def _set_pause(until: str, reason: str) -> bool:
    """Pause 갱신. 기간이 늘거나 사유가 바뀌면 True."""
    global _pause_until, _pause_reason
    if not _pause_until or until > _pause_until:
        _pause_until = until
        _pause_reason = reason
        return True
    if until == _pause_until and reason != _pause_reason:
        _pause_reason = reason
        return True
    return False


def refresh(
    *,
    trades_today: list[dict],
    daily_ledger: list[dict],
) -> dict:
    """현재 한도·연속손절을 반영해 pause 갱신. 청산 직후 호출."""
    if not ENABLED:
        return {"blocked": False, "reason": "방어OFF", "changed": False}

    today = _today()
    # 만료된 pause 정리
    global _pause_until, _pause_reason
    if _pause_until and today > _pause_until:
        _pause_until = ""
        _pause_reason = ""

    consec = _consec_losses(trades_today)
    day_pnl = _today_pnl(trades_today)
    week_pnl = _weekly_pnl(daily_ledger, trades_today, today)
    changed = False

    if WEEKLY_LOSS_LIMIT < 0 and week_pnl <= WEEKLY_LOSS_LIMIT:
        until = _week_friday()
        reason = (
            f"주간손실 {week_pnl:,}원 ≤ {WEEKLY_LOSS_LIMIT:,}원 "
            f"→ {until}까지 신규매수 중단"
        )
        changed = _set_pause(until, reason) or changed

    if DAILY_LOSS_LIMIT < 0 and day_pnl <= DAILY_LOSS_LIMIT:
        reason = (
            f"일손실 {day_pnl:,}원 ≤ {DAILY_LOSS_LIMIT:,}원 "
            f"→ 오늘 신규매수 중단"
        )
        changed = _set_pause(today, reason) or changed

    if MAX_CONSEC_LOSSES > 0 and consec >= MAX_CONSEC_LOSSES:
        reason = (
            f"연속손실 {consec}건 ≥ {MAX_CONSEC_LOSSES}건 "
            f"→ 오늘 신규매수 중단"
        )
        changed = _set_pause(today, reason) or changed

    blocked = bool(_pause_until and today <= _pause_until)
    return {
        "blocked": blocked,
        "reason": _pause_reason if blocked else "방어OK",
        "consec": consec,
        "day_pnl": day_pnl,
        "week_pnl": week_pnl,
        "pause_until": _pause_until,
        "changed": changed,
    }


def allow_new_buy(
    *,
    for_closing: bool = False,
    trades_today: list[dict] | None = None,
    daily_ledger: list[dict] | None = None,
) -> tuple[bool, str]:
    if not ENABLED:
        return True, "방어OFF"
    status = refresh(
        trades_today=trades_today or [],
        daily_ledger=daily_ledger or [],
    )
    if not status["blocked"]:
        return True, status["reason"]
    if for_closing and not BLOCK_CLOSING:
        return True, f"방어활성(종가허용) · {status['reason']}"
    return False, status["reason"]


def notify_if_triggered(send_fn, status: dict | None = None) -> None:
    """새로 pause 걸렸을 때 1회 알림."""
    global _notified_key
    if not ENABLED or not callable(send_fn):
        return
    if status is None:
        return
    if not status.get("blocked") or not status.get("changed"):
        return
    key = f"{_pause_until}|{_pause_reason}"
    if key == _notified_key:
        return
    _notified_key = key
    scope = "장중+종가" if BLOCK_CLOSING else "장중만"
    send_fn(
        "🛡️ <b>방어모드 — 신규매수 일시중단</b>\n"
        f"{status.get('reason', _pause_reason)}\n"
        f"대상: {scope} 신규매수\n"
        "보유 청산(손절·익절·강제)은 정상 동작"
    )


def format_status_line() -> str:
    if not ENABLED:
        return "방어모드: OFF"
    today = _today()
    active = bool(_pause_until and today <= _pause_until)
    scope = "종가포함" if BLOCK_CLOSING else "장중만"
    base = (
        f"방어모드: ON ({scope} · 연속≥{MAX_CONSEC_LOSSES} · "
        f"일≤{DAILY_LOSS_LIMIT:,} · 주≤{WEEKLY_LOSS_LIMIT:,})"
    )
    if active:
        return f"{base} · ⛔{_pause_until}까지"
    return f"{base} · 가동중"
