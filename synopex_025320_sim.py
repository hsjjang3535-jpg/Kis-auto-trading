"""
시노펙스 전용 시뮬 (실제 주문 없음)

- 대상: 시노펙스(025320) 1종목
- 진입(일봉, 장중 현재가로 당일 종가 대용):
  S) 거래량 급증: 전일대비 +3%↑ · 거래량≥20일평균×2 · MA20 위 · RSI≤75
  B10) 단기돌파: 전 10일 고가 돌파 · 거래량≥5일평균×1.5 · RSI 45~70 · MA20 위
  → 둘 다 충족 시 S 우선, 동시 보유 1포지션
- 청산: 손절 -2% · +3% 이후 고점 대비 -0.6% 트레일 · 15:00 시간청산
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import kis_api

KST = ZoneInfo("Asia/Seoul")
STRATEGY = "시노펙스시뮬"
TARGET_CODE = os.getenv("SYNOPEX_SIM_CODE", "025320").strip() or "025320"
TARGET_NAME = os.getenv("SYNOPEX_SIM_NAME", "시노펙스").strip() or "시노펙스"

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


ENABLED = _env_bool("ENABLE_SYNOPEX_SIM", False)
ENABLE_VOL_SPIKE = _env_bool("SYNOPEX_SIM_ENABLE_VOL_SPIKE", True)
ENABLE_B10 = _env_bool("SYNOPEX_SIM_ENABLE_B10", True)

ENTRY_START_MIN = _parse_hhmm(os.getenv("SYNOPEX_SIM_ENTRY_START", "09:00"), 9, 0)
ENTRY_END_MIN = _parse_hhmm(os.getenv("SYNOPEX_SIM_ENTRY_END", "15:00"), 15, 0)
EXIT_END_MIN = _parse_hhmm(os.getenv("SYNOPEX_SIM_EXIT_END", "15:00"), 15, 0)
SIM_AMOUNT = int(os.getenv("SYNOPEX_SIM_AMOUNT", "500000"))
MAX_API_CALLS = int(os.getenv("SYNOPEX_SIM_MAX_API_CALLS", "6"))
SCAN_INTERVAL_MIN = int(os.getenv("SYNOPEX_SIM_SCAN_INTERVAL", "3"))
POSITION_POLL_MIN = int(os.getenv("SYNOPEX_SIM_POSITION_POLL", "1"))

# S: 거래량 급증
VOL_SPIKE_MIN_DAY_PCT = float(os.getenv("SYNOPEX_SIM_VOL_SPIKE_DAY_PCT", "3.0"))
VOL_SPIKE_VOL_DAYS = int(os.getenv("SYNOPEX_SIM_VOL_SPIKE_VOL_DAYS", "20"))
VOL_SPIKE_MIN_VOL_RATIO = float(os.getenv("SYNOPEX_SIM_VOL_SPIKE_VOL_RATIO", "2.0"))
VOL_SPIKE_MAX_RSI = float(os.getenv("SYNOPEX_SIM_VOL_SPIKE_MAX_RSI", "75"))

# B10: 단기 돌파
B10_HIGH_DAYS = int(os.getenv("SYNOPEX_SIM_B10_DAYS", "10"))
B10_VOL_DAYS = int(os.getenv("SYNOPEX_SIM_B10_VOL_DAYS", "5"))
B10_MIN_VOL_RATIO = float(os.getenv("SYNOPEX_SIM_B10_VOL_RATIO", "1.5"))
B10_MIN_RSI = float(os.getenv("SYNOPEX_SIM_B10_MIN_RSI", "45"))
B10_MAX_RSI = float(os.getenv("SYNOPEX_SIM_B10_MAX_RSI", "70"))

STOP_LOSS_PCT = float(os.getenv("SYNOPEX_SIM_STOP_LOSS", "2.0"))
TAKE_PROFIT_PCT = float(os.getenv("SYNOPEX_SIM_TAKE_PROFIT", "3.0"))
TRAILING_STOP_PCT = float(os.getenv("SYNOPEX_SIM_TRAILING_STOP", "0.6"))


def is_enabled() -> bool:
    return ENABLED and (ENABLE_VOL_SPIKE or ENABLE_B10)


def is_trading_weekday() -> bool:
    return datetime.now(KST).weekday() < 5


def _now_min() -> int:
    now = datetime.now(KST)
    return now.hour * 60 + now.minute


def is_monitor_window() -> bool:
    if not is_enabled() or not is_trading_weekday():
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


def _parse_candles(candles: list[dict]) -> tuple[list[float], list[float], list[float]]:
    closes: list[float] = []
    highs: list[float] = []
    volumes: list[float] = []
    for c in candles:
        try:
            closes.append(float(c.get("stck_clpr", 0)))
            highs.append(float(c.get("stck_hgpr", 0)))
            volumes.append(float(c.get("acml_vol", 0)))
        except (TypeError, ValueError):
            continue
    return closes, highs, volumes


def _daily_context(code: str, current: float, today_volume: float) -> dict:
    candles = kis_api.get_daily_chart(code, days=120)
    closes, highs, volumes = _parse_candles(candles)
    need = max(60, VOL_SPIKE_VOL_DAYS + 1, B10_HIGH_DAYS + 1, B10_VOL_DAYS + 1)
    if len(closes) < need:
        return {}

    price_series = [current] + closes[1:60]
    ma20 = sum(price_series[:20]) / 20
    ma60 = sum(price_series[:60]) / 60
    rsi = _daily_rsi([current] + closes[1:], 14)
    prev_close = float(closes[1]) if len(closes) > 1 else 0.0
    day_pct = (current - prev_close) / prev_close * 100 if prev_close > 0 else 0.0

    prior_highs = highs[1 : B10_HIGH_DAYS + 1]
    high_10 = max(prior_highs) if prior_highs else 0.0

    vols_20 = volumes[1 : VOL_SPIKE_VOL_DAYS + 1]
    vol_avg_20 = sum(vols_20) / len(vols_20) if vols_20 else 0.0
    vol_ratio_20 = today_volume / vol_avg_20 if vol_avg_20 > 0 else 0.0

    vols_5 = volumes[1 : B10_VOL_DAYS + 1]
    vol_avg_5 = sum(vols_5) / len(vols_5) if vols_5 else 0.0
    vol_ratio_5 = today_volume / vol_avg_5 if vol_avg_5 > 0 else 0.0

    return {
        "ma20": ma20,
        "ma60": ma60,
        "rsi": rsi,
        "prev_close": prev_close,
        "day_pct": day_pct,
        "high_10": high_10,
        "vol_ratio_20": vol_ratio_20,
        "vol_ratio_5": vol_ratio_5,
    }


def _match_vol_spike(current: float, daily: dict) -> dict | None:
    if not ENABLE_VOL_SPIKE:
        return None
    ma20 = float(daily["ma20"])
    rsi = float(daily["rsi"])
    day_pct = float(daily["day_pct"])
    vol_ratio = float(daily["vol_ratio_20"])
    if day_pct < VOL_SPIKE_MIN_DAY_PCT:
        return None
    if current < ma20:
        return None
    if rsi > VOL_SPIKE_MAX_RSI:
        return None
    if vol_ratio < VOL_SPIKE_MIN_VOL_RATIO:
        return None
    return {
        "rule": "S",
        "rule_name": "거래량급증",
        "reason": (
            f"S 거래량급증 · 전일대비 {day_pct:+.1f}% · "
            f"거래량 {vol_ratio:.1f}배(20일) · MA20 위 · RSI {rsi:.0f}"
        ),
    }


def _match_b10(current: float, daily: dict) -> dict | None:
    if not ENABLE_B10:
        return None
    ma20 = float(daily["ma20"])
    rsi = float(daily["rsi"])
    high_10 = float(daily["high_10"])
    vol_ratio = float(daily["vol_ratio_5"])
    if current <= ma20:
        return None
    if not (B10_MIN_RSI <= rsi <= B10_MAX_RSI):
        return None
    if high_10 <= 0 or current < high_10:
        return None
    if vol_ratio < B10_MIN_VOL_RATIO:
        return None
    return {
        "rule": "B10",
        "rule_name": "단기돌파",
        "reason": (
            f"B10 단기돌파 · {B10_HIGH_DAYS}일 고가 {high_10:,.0f} 돌파 · "
            f"거래량 {vol_ratio:.1f}배(5일) · RSI {rsi:.0f}"
        ),
    }


def _evaluate_entry() -> tuple[dict | None, int]:
    used = 0
    try:
        info = kis_api.get_stock_info(TARGET_CODE)
        used += 1
        _throttle()
    except Exception as e:
        print(f"[시노펙스시뮬] 시세 실패: {e}")
        return None, used

    current = int(float(info.get("stck_prpr", 0)))
    if current <= 0:
        return None, used
    try:
        today_volume = float(info.get("acml_vol", 0))
    except (TypeError, ValueError):
        today_volume = 0.0

    try:
        daily = _daily_context(TARGET_CODE, float(current), today_volume)
        used += 1
        _throttle()
    except Exception as e:
        print(f"[시노펙스시뮬] 일봉 실패: {e}")
        return None, used
    if not daily:
        return None, used

    # S 우선, 없으면 B10
    matched = _match_vol_spike(float(current), daily) or _match_b10(float(current), daily)
    if not matched:
        return None, used

    return {
        "code": TARGET_CODE,
        "name": TARGET_NAME,
        "current": current,
        "ma20": daily["ma20"],
        "rsi": daily["rsi"],
        "day_pct": round(daily["day_pct"], 2),
        "rule": matched["rule"],
        "rule_name": matched["rule_name"],
        "strategy": STRATEGY,
        "reason": matched["reason"],
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
        "rule": stock.get("rule", ""),
        "rule_name": stock.get("rule_name", ""),
    }
    return {
        "action": "buy",
        "name": stock["name"],
        "code": stock["code"],
        "quantity": qty,
        "price": price,
        "rule": stock.get("rule", ""),
        "reason": f"[시노펙스시뮬] {stock.get('reason', '')}",
    }


def _evaluate_exit(pos: dict, current: int) -> tuple[bool, str]:
    buy_price = int(pos.get("buy_price", 0))
    if buy_price <= 0:
        return True, "기준가 오류 청산"
    profit_pct = (current - buy_price) / buy_price * 100
    if profit_pct <= -STOP_LOSS_PCT:
        return True, f"시노펙스 손절 ({profit_pct:.1f}%)"

    peak = int(pos.get("peak_price", buy_price))
    peak_profit = (peak - buy_price) / buy_price * 100 if buy_price > 0 else 0
    drop_from_peak = (peak - current) / peak * 100 if peak > 0 else 0
    if peak_profit >= TAKE_PROFIT_PCT and drop_from_peak >= TRAILING_STOP_PCT:
        return True, (
            f"시노펙스 트레일링 (+{profit_pct:.1f}% / "
            f"고점 +{peak_profit:.1f}%에서 -{drop_from_peak:.1f}%)"
        )

    if _now_min() >= EXIT_END_MIN:
        return True, f"시노펙스 시간청산 ({profit_pct:+.1f}%)"
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
        "rule": pos.get("rule", ""),
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
    if not is_enabled() or not is_monitor_window():
        return [], 0
    used = 0
    events: list[dict] = []

    if _open_position:
        try:
            current = int(
                kis_api.get_current_price(TARGET_CODE, fallback=_open_position["buy_price"])
            )
            used += 1
            _throttle()
        except Exception as e:
            print(f"[시노펙스시뮬] 보유 현재가 실패: {e}")
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
                f"[시노펙스시뮬] 가상매수 {stock['name']}({stock['code']}) "
                f"[{stock.get('rule_name', '')}] "
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
            f"🟪 <b>시노펙스 [시뮬] 오늘 체결 {len(_sim_trades_today)}건</b> → {sign}{net:,}원"
        )
        for t in _sim_trades_today:
            em = "📈" if t["profit_won"] >= 0 else "📉"
            s = "+" if t["profit_won"] >= 0 else ""
            rule = t.get("rule") or ""
            tag = f"[{rule}] " if rule else ""
            lines.append(
                f"   {em} {tag}{t['name']}({t['code']}) "
                f"{t['buy_price']:,}→{t['sell_price']:,}원 {s}{t['profit_pct']}%"
            )

    if _open_position:
        pos = _open_position
        rule = pos.get("rule_name") or pos.get("rule") or ""
        tag = f"[{rule}] " if rule else ""
        lines.append(
            f"🟪 시노펙스 [시뮬] 보유: {tag}{pos['name']}({pos['code']}) "
            f"{pos['buy_price']:,}원 × {pos['quantity']}주"
        )
    return lines
