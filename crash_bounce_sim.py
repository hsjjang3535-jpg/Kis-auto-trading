"""
낙폭반등 — 시뮬만 (실제 주문 없음)

- ENABLE_CRASH_BOUNCE_SIM=true, ENABLE_CRASH_BOUNCE=false 로 실전 매수 중단
- 진입·청산 규칙은 crash_bounce와 동일 (낙폭 상한·5분봉 반등 등)
- 빠른손절(QUICK_STOP) 미적용
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import crash_bounce
import kis_api

KST = ZoneInfo("Asia/Seoul")
STRATEGY = "낙폭반등시뮬"

_open_position: dict | None = None
_sim_trades_today: list[dict] = []
_sim_invested_today: int = 0


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default).lower()).lower() == "true"


ENABLED = _env_bool("ENABLE_CRASH_BOUNCE_SIM", False)
SIM_AMOUNT = int(os.getenv("CRASH_BOUNCE_SIM_AMOUNT", "500000"))
POLL_INTERVAL_MIN = int(os.getenv("CRASH_BOUNCE_SIM_POLL_INTERVAL", "5"))
POSITION_POLL_MIN = int(os.getenv("CRASH_BOUNCE_SIM_POSITION_POLL", "1"))


def is_enabled() -> bool:
    return ENABLED


def is_trading_weekday() -> bool:
    return datetime.now(KST).weekday() < 5


def _now_min() -> int:
    now = datetime.now(KST)
    return now.hour * 60 + now.minute


def is_monitor_window() -> bool:
    """보유 청산·오전 진입·오후 청산 구간"""
    if not ENABLED or not is_trading_weekday():
        return False
    t = _now_min()
    if _open_position:
        exit_min = (
            crash_bounce.AFTERNOON_TIME_EXIT_MIN
            if _open_position.get("entry_session") == "afternoon"
            else crash_bounce.TIME_EXIT_MIN
        )
        return t <= exit_min + 30
    return crash_bounce.is_entry_window() or t == crash_bounce.ENTRY_END_MIN + 5


def get_poll_interval_min() -> int:
    return POSITION_POLL_MIN if _open_position else POLL_INTERVAL_MIN


def get_open_position() -> dict | None:
    return _open_position


def get_sim_trades_today() -> list[dict]:
    return list(_sim_trades_today)


def get_sim_invested_today() -> int:
    return _sim_invested_today


def load_open_position(data: dict | None) -> None:
    global _open_position
    _open_position = data if isinstance(data, dict) else None


def load_sim_trades_today(data: list | None) -> None:
    global _sim_trades_today
    _sim_trades_today = data if isinstance(data, list) else []


def load_sim_invested_today(value: int) -> None:
    global _sim_invested_today
    _sim_invested_today = max(0, int(value))


def dump_open_position() -> dict | None:
    return dict(_open_position) if _open_position else None


def dump_sim_trades_today() -> list[dict]:
    return list(_sim_trades_today)


def dump_sim_invested_today() -> int:
    return _sim_invested_today


def reset_daily_sim_trades() -> None:
    global _open_position, _sim_trades_today, _sim_invested_today
    _open_position = None
    _sim_trades_today = []
    _sim_invested_today = 0


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _throttle() -> None:
    time.sleep(0.3)


def _sim_qty(price: int) -> int:
    return max(SIM_AMOUNT // price, 1) if price > 0 else 0


def _open_sim(stock: dict, price: int, session: str) -> dict | None:
    global _open_position, _sim_invested_today
    if _open_position:
        return None
    remaining = crash_bounce.MAX_AMOUNT - _sim_invested_today
    if remaining <= 0:
        return None
    buy_cap = min(crash_bounce.MAX_BUY, remaining, SIM_AMOUNT)
    qty = max(buy_cap // price, 1) if price > 0 else 0
    if qty < 1:
        return None
    invested = qty * price
    if invested > remaining:
        qty = remaining // price
        if qty < 1:
            return None
        invested = qty * price

    _open_position = {
        "code": stock["code"],
        "name": stock["name"],
        "quantity": qty,
        "buy_price": price,
        "buy_date": _today(),
        "buy_reason": stock.get("reason", ""),
        "entry_session": session,
        "exit_ma60": stock.get("ma60", 0),
        "ma_period": stock.get("ma_period", 60),
    }
    _sim_invested_today += invested
    return {
        "action": "buy",
        "name": stock["name"],
        "code": stock["code"],
        "quantity": qty,
        "price": price,
        "reason": f"[낙폭반등시뮬] {stock.get('reason', '')}",
    }


def _close_sim(price: int, reason: str) -> dict | None:
    global _open_position
    if not _open_position:
        return None
    pos = _open_position
    buy_price = pos["buy_price"]
    qty = pos["quantity"]
    if buy_price <= 0 or qty < 1:
        _open_position = None
        return None
    profit_pct = (price - buy_price) / buy_price * 100
    profit_won = int((price - buy_price) * qty)
    trade = {
        "action": "sell",
        "name": pos["name"],
        "code": pos["code"],
        "strategy": STRATEGY,
        "buy_price": buy_price,
        "sell_price": price,
        "quantity": qty,
        "buy_date": pos["buy_date"],
        "sell_date": _today(),
        "sell_reason": reason,
        "profit_pct": round(profit_pct, 2),
        "profit_won": profit_won,
    }
    _sim_trades_today.append(trade)
    _open_position = None
    return trade


def _try_entries(afternoon: bool, api_budget: int) -> tuple[list[dict], int]:
    global _sim_invested_today
    events: list[dict] = []
    used = 0
    if _open_position:
        return events, used
    if _sim_invested_today >= crash_bounce.MAX_AMOUNT:
        return events, used

    candidates, scan_used = crash_bounce.scan_candidates(
        api_budget=api_budget, afternoon=afternoon,
    )
    used += scan_used
    session = "afternoon" if afternoon else "morning"
    label = "오후" if afternoon else "오전"
    print(f"[낙폭반등시뮬/{label}] 스캔 {len(candidates)}개 후보 (API {used}회)")

    remaining = crash_bounce.MAX_AMOUNT - _sim_invested_today
    for stock in candidates:
        if remaining <= 0:
            break
        code = stock["code"]
        name = stock["name"]
        try:
            current = int(stock.get("current") or 0)
            if current <= 0:
                info = kis_api.get_stock_info(code)
                used += 1
                _throttle()
                current = int(float(info.get("stck_prpr", 0)))
            if current <= 0:
                continue
            sim = _open_sim(stock, current, session)
            if sim:
                events.append(sim)
                remaining = crash_bounce.MAX_AMOUNT - _sim_invested_today
                print(
                    f"[낙폭반등시뮬] 가상매수 {name}({code}) "
                    f"{sim['quantity']}주 @ {current:,}"
                )
                break
        except Exception as e:
            print(f"[낙폭반등시뮬] {name} 진입 오류: {e}")
    return events, used


def run_check(api_budget: int | None = None) -> tuple[list[dict], int]:
    """보유 청산 → 오전 진입 스캔"""
    if not ENABLED or not is_monitor_window():
        return [], 0

    budget = api_budget if api_budget is not None else crash_bounce.MAX_API_CALLS
    used = 0
    events: list[dict] = []

    if _open_position:
        code = _open_position["code"]
        try:
            info = kis_api.get_stock_info(code)
            used += 1
            _throttle()
            current = float(info.get("stck_prpr", _open_position["buy_price"]))
        except Exception as e:
            print(f"[낙폭반등시뮬] {_open_position['name']} 시세 실패: {e}")
            return events, used

        buy_price = _open_position["buy_price"]
        profit_pct = (current - buy_price) / buy_price * 100
        intra = None
        if used < budget:
            try:
                intra = kis_api.get_intraday_5min_indicators(code)
                used += 1
                _throttle()
            except Exception as e:
                print(f"[낙폭반등시뮬] {_open_position['name']} 분봉 실패: {e}")

        should_sell, reason = crash_bounce.evaluate_exit(
            _open_position, current, profit_pct, intra,
        )
        if should_sell:
            sim = _close_sim(int(current), reason)
            if sim:
                events.append(sim)
        return events, used

    if crash_bounce.is_entry_window():
        entry_events, entry_used = _try_entries(afternoon=False, api_budget=budget)
        events.extend(entry_events)
        used += entry_used

    return events, used


def run_afternoon_entry(api_budget: int | None = None) -> tuple[list[dict], int]:
    """13:15 오후 필터 1회 — 오전 미체결 시만"""
    if not ENABLED or not is_trading_weekday():
        return [], 0
    if _open_position or _sim_invested_today > 0:
        return [], 0
    budget = api_budget if api_budget is not None else crash_bounce.MAX_API_CALLS
    return _try_entries(afternoon=True, api_budget=budget)


def format_summary() -> list[str]:
    if not _sim_trades_today and not _open_position:
        return []

    lines: list[str] = []
    if _sim_trades_today:
        net = sum(t["profit_won"] for t in _sim_trades_today)
        sign = "+" if net >= 0 else ""
        lines.append(
            f"🔶 <b>낙폭반등 [시뮬] 오늘 체결 {len(_sim_trades_today)}건</b> → {sign}{net:,}원"
        )
        for t in _sim_trades_today:
            em = "📈" if t["profit_won"] >= 0 else "📉"
            s = "+" if t["profit_won"] >= 0 else ""
            lines.append(
                f"   {em} {t['name']}({t['code']}) "
                f"{t['buy_price']:,}→{t['sell_price']:,}원 {s}{t['profit_pct']}%"
            )

    if _open_position:
        pos = _open_position
        lines.append(
            f"🔶 낙폭반등 [시뮬] 보유: {pos['name']}({pos['code']}) "
            f"{pos['buy_price']:,}원 × {pos['quantity']}주"
        )
    return lines
