"""
삼성전자 전용 시뮬 (실제 주문 없음)

- 대상: 삼성전자(005930) 1종목
- 성격: 추세 상단 돌파보다 "60일선 근처 눌림 + 장중 5분봉 반등"에 맞춘 보수적 시뮬
- 기존 자동매매와 독립 동작, 자금도 별도 가상 금액 사용
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import kis_api

KST = ZoneInfo("Asia/Seoul")
STRATEGY = "삼성전용시뮬"
TARGET_CODE = os.getenv("SAMSUNG_SIM_CODE", "005930").strip() or "005930"
TARGET_NAME = os.getenv("SAMSUNG_SIM_NAME", "삼성전자").strip() or "삼성전자"

_open_position: dict | None = None
_sim_trades_today: list[dict] = []


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default).lower()).lower() == "true"


def _parse_hhmm(value: str, default_h: int, default_m: int) -> int:
    try:
        h, m = value.strip().split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return default_h * 60 + default_m


ENABLED = _env_bool("ENABLE_SAMSUNG_SIM", False)
ENTRY_START_MIN = _parse_hhmm(os.getenv("SAMSUNG_SIM_ENTRY_START", "09:10"), 9, 10)
ENTRY_END_MIN = _parse_hhmm(os.getenv("SAMSUNG_SIM_ENTRY_END", "14:20"), 14, 20)
EXIT_END_MIN = _parse_hhmm(os.getenv("SAMSUNG_SIM_EXIT_END", "14:50"), 14, 50)
SIM_AMOUNT = int(os.getenv("SAMSUNG_SIM_AMOUNT", "500000"))
MAX_API_CALLS = int(os.getenv("SAMSUNG_SIM_MAX_API_CALLS", "6"))
SCAN_INTERVAL_MIN = int(os.getenv("SAMSUNG_SIM_SCAN_INTERVAL", "3"))
POSITION_POLL_MIN = int(os.getenv("SAMSUNG_SIM_POSITION_POLL", "1"))

MIN_DROP_PCT = float(os.getenv("SAMSUNG_SIM_MIN_DROP", "0.3"))
MAX_DROP_PCT = float(os.getenv("SAMSUNG_SIM_MAX_DROP", "1.8"))
MAX_ABOVE_MA60_PCT = float(os.getenv("SAMSUNG_SIM_MAX_ABOVE_MA60", "2.5"))
MAX_BELOW_MA60_PCT = float(os.getenv("SAMSUNG_SIM_MAX_BELOW_MA60", "1.5"))
MIN_RSI = float(os.getenv("SAMSUNG_SIM_MIN_RSI", "35"))
MAX_RSI = float(os.getenv("SAMSUNG_SIM_MAX_RSI", "58"))
MIN_VOLUME_RATIO = float(os.getenv("SAMSUNG_SIM_MIN_VOLUME_RATIO", "1.1"))

STOP_LOSS_PCT = float(os.getenv("SAMSUNG_SIM_STOP_LOSS", "2.0"))
TAKE_PROFIT_PCT = float(os.getenv("SAMSUNG_SIM_TAKE_PROFIT", "3.0"))
TRAILING_STOP_PCT = float(os.getenv("SAMSUNG_SIM_TRAILING_STOP", "0.6"))


def is_enabled() -> bool:
    return ENABLED


def is_trading_weekday() -> bool:
    return datetime.now(KST).weekday() < 5


def _now_min() -> int:
    now = datetime.now(KST)
    return now.hour * 60 + now.minute


def is_monitor_window() -> bool:
    if not ENABLED or not is_trading_weekday():
        return False
    t = _now_min()
    if _open_position:
        return t <= EXIT_END_MIN
    return ENTRY_START_MIN <= t <= ENTRY_END_MIN


def get_poll_interval_min() -> int:
    return POSITION_POLL_MIN if _open_position else SCAN_INTERVAL_MIN


def get_open_position() -> dict | None:
    return _open_position


def get_sim_trades_today() -> list[dict]:
    return list(_sim_trades_today)


def load_open_position(data: dict | None) -> None:
    global _open_position
    _open_position = data if isinstance(data, dict) else None


def load_sim_trades_today(data: list | None) -> None:
    global _sim_trades_today
    _sim_trades_today = data if isinstance(data, list) else []


def dump_open_position() -> dict | None:
    return dict(_open_position) if _open_position else None


def dump_sim_trades_today() -> list[dict]:
    return list(_sim_trades_today)


def reset_daily_sim_trades() -> None:
    global _open_position, _sim_trades_today
    _open_position = None
    _sim_trades_today = []


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _throttle() -> None:
    time.sleep(0.3)


def _sim_qty(price: int) -> int:
    return max(SIM_AMOUNT // price, 1) if price > 0 else 0


def _daily_rsi(closes_latest_first: list[float], period: int = 14) -> float:
    if len(closes_latest_first) < period + 1:
        return 50.0
    prices = list(reversed(closes_latest_first[:period + 1]))
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _daily_context(code: str) -> dict:
    candles = kis_api.get_daily_chart(code, days=120)
    closes: list[float] = []
    for c in candles:
        try:
            closes.append(float(c.get("stck_clpr", 0)))
        except (TypeError, ValueError):
            continue
    if len(closes) < 60:
        return {}
    ma20 = sum(closes[:20]) / 20
    ma60 = sum(closes[:60]) / 60
    rsi = _daily_rsi(closes, 14)
    return {
        "ma20": ma20,
        "ma60": ma60,
        "rsi": rsi,
    }


def _drop_from_open(info: dict) -> float:
    try:
        open_p = float(info.get("stck_oprc", 0))
        current = float(info.get("stck_prpr", 0))
        if open_p <= 0:
            return 0.0
        return (open_p - current) / open_p * 100
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _ma60_gap_pct(current: float, ma60: float) -> float:
    if ma60 <= 0:
        return 0.0
    return (current - ma60) / ma60 * 100


def _has_rebound_signal(bars: list[dict]) -> tuple[bool, float]:
    if len(bars) < 6:
        return False, 0.0
    recent = bars[-6:]
    signal = recent[-1]
    prev = recent[-2]
    recent_low = min(b["low"] for b in recent[:-1])
    if signal["close"] <= signal["open"]:
        return False, 0.0
    if signal["close"] <= prev["close"]:
        return False, 0.0
    if signal["close"] < recent_low * 1.002:
        return False, 0.0
    prior_volumes = [b.get("volume", 0) for b in recent[:-1]]
    avg_vol = sum(prior_volumes) / len(prior_volumes) if prior_volumes else 0
    vol_ratio = signal.get("volume", 0) / avg_vol if avg_vol > 0 else 0
    return vol_ratio >= MIN_VOLUME_RATIO, vol_ratio


def _evaluate_entry() -> tuple[dict | None, int]:
    used = 0
    try:
        info = kis_api.get_stock_info(TARGET_CODE)
        used += 1
        _throttle()
    except Exception as e:
        print(f"[삼성시뮬] 시세 실패: {e}")
        return None, used

    current = int(float(info.get("stck_prpr", 0)))
    if current <= 0:
        return None, used
    drop = _drop_from_open(info)
    if drop < MIN_DROP_PCT or drop > MAX_DROP_PCT:
        return None, used

    try:
        daily = _daily_context(TARGET_CODE)
        used += 1
        _throttle()
    except Exception as e:
        print(f"[삼성시뮬] 일봉 실패: {e}")
        return None, used
    if not daily:
        return None, used

    ma20 = float(daily.get("ma20", 0))
    ma60 = float(daily.get("ma60", 0))
    rsi = float(daily.get("rsi", 50))
    ma60_gap = _ma60_gap_pct(current, ma60)
    if ma60_gap > MAX_ABOVE_MA60_PCT or ma60_gap < -MAX_BELOW_MA60_PCT:
        return None, used
    if ma20 < ma60 * 0.985:
        return None, used
    if not (MIN_RSI <= rsi <= MAX_RSI):
        return None, used

    try:
        intra = kis_api.get_intraday_5min_indicators(TARGET_CODE)
        used += 1
        _throttle()
    except Exception as e:
        print(f"[삼성시뮬] 분봉 실패: {e}")
        return None, used
    if not intra or intra.get("bar_count", 0) < 6:
        return None, used

    has_bounce, volume_ratio = _has_rebound_signal(intra.get("bars_5", []))
    if not has_bounce:
        return None, used

    return {
        "code": TARGET_CODE,
        "name": TARGET_NAME,
        "current": current,
        "ma20": ma20,
        "ma60": ma60,
        "rsi": rsi,
        "drop_from_open": round(drop, 2),
        "strategy": STRATEGY,
        "reason": (
            f"60일선 근처 눌림({ma60_gap:+.1f}%) · 시가대비 -{drop:.1f}% · "
            f"일봉 RSI {rsi:.0f} · 5분봉 반등 거래량 {volume_ratio:.1f}배"
        ),
    }, used


def _open_sim(stock: dict, price: int) -> dict | None:
    global _open_position
    if _open_position:
        return None
    qty = _sim_qty(price)
    if qty < 1:
        return None
    _open_position = {
        "code": stock["code"],
        "name": stock["name"],
        "quantity": qty,
        "buy_price": price,
        "peak_price": price,
        "buy_date": _today(),
        "buy_reason": stock.get("reason", ""),
    }
    return {
        "action": "buy",
        "name": stock["name"],
        "code": stock["code"],
        "quantity": qty,
        "price": price,
        "reason": f"[삼성전용시뮬] {stock.get('reason', '')}",
    }


def _evaluate_exit(pos: dict, current: int) -> tuple[bool, str]:
    buy_price = int(pos.get("buy_price", 0))
    if buy_price <= 0:
        return True, "기준가 오류 청산"
    profit_pct = (current - buy_price) / buy_price * 100
    if profit_pct <= -STOP_LOSS_PCT:
        return True, f"삼성전용 손절 ({profit_pct:.1f}%)"

    peak = int(pos.get("peak_price", buy_price))
    peak_profit = (peak - buy_price) / buy_price * 100 if buy_price > 0 else 0
    drop_from_peak = (peak - current) / peak * 100 if peak > 0 else 0
    if peak_profit >= TAKE_PROFIT_PCT and drop_from_peak >= TRAILING_STOP_PCT:
        return True, (
            f"삼성전용 트레일링 (+{profit_pct:.1f}% / "
            f"고점 +{peak_profit:.1f}%에서 -{drop_from_peak:.1f}%)"
        )

    if _now_min() >= EXIT_END_MIN:
        return True, f"삼성전용 시간청산 ({profit_pct:+.1f}%)"
    return False, ""


def _close_sim(price: int, reason: str) -> dict | None:
    global _open_position
    if not _open_position:
        return None
    pos = _open_position
    buy_price = int(pos["buy_price"])
    qty = int(pos["quantity"])
    profit_pct = (price - buy_price) / buy_price * 100 if buy_price > 0 else 0
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


def run_check(api_budget: int | None = None) -> tuple[list[dict], int]:
    if not ENABLED or not is_monitor_window():
        return [], 0
    budget = api_budget if api_budget is not None else MAX_API_CALLS
    used = 0
    events: list[dict] = []

    if _open_position:
        try:
            current = int(kis_api.get_current_price(TARGET_CODE, fallback=_open_position["buy_price"]))
            used += 1
            _throttle()
        except Exception as e:
            print(f"[삼성시뮬] 보유 현재가 실패: {e}")
            return events, used
        if current > int(_open_position.get("peak_price", _open_position["buy_price"])):
            _open_position["peak_price"] = current
        should_sell, reason = _evaluate_exit(_open_position, current)
        if should_sell:
            sim = _close_sim(current, reason)
            if sim:
                events.append(sim)
        return events, used

    if _sim_trades_today or not (ENTRY_START_MIN <= _now_min() <= ENTRY_END_MIN):
        return events, used

    stock, scan_used = _evaluate_entry()
    used += scan_used
    if stock:
        sim = _open_sim(stock, int(stock["current"]))
        if sim:
            events.append(sim)
            print(
                f"[삼성시뮬] 가상매수 {stock['name']}({stock['code']}) "
                f"{sim['quantity']}주 @ {int(stock['current']):,}"
            )
    return events, used


def format_summary() -> list[str]:
    if not _sim_trades_today and not _open_position:
        return []

    lines: list[str] = []
    if _sim_trades_today:
        net = sum(t["profit_won"] for t in _sim_trades_today)
        sign = "+" if net >= 0 else ""
        lines.append(
            f"🟦 <b>삼성전용 [시뮬] 오늘 체결 {len(_sim_trades_today)}건</b> → {sign}{net:,}원"
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
            f"🟦 삼성전용 [시뮬] 보유: {pos['name']}({pos['code']}) "
            f"{pos['buy_price']:,}원 × {pos['quantity']}주"
        )
    return lines
