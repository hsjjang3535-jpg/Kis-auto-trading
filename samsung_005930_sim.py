"""
삼성전자 전용 시뮬 (실제 주문 없음)

- 대상: 삼성전자(005930) 1종목
- 진입(B): 종가>MA20>MA60 · RSI≥50 · 전 20일 고가 돌파 · 당일 거래량 ≥ 직전 5일 평균×배수
- 청산: 손절 -2% · +3% 이후 고점 대비 -0.6% 트레일 · 14:50 시간청산
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

MIN_RSI = float(os.getenv("SAMSUNG_SIM_MIN_RSI", "50"))
BREAKOUT_HIGH_DAYS = int(os.getenv("SAMSUNG_SIM_BREAKOUT_DAYS", "20"))
MIN_VOLUME_RATIO = float(os.getenv("SAMSUNG_SIM_MIN_VOLUME_RATIO", "1.1"))
VOLUME_AVG_DAYS = int(os.getenv("SAMSUNG_SIM_VOLUME_AVG_DAYS", "5"))

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


def _daily_breakout_context(code: str, current: float, today_volume: float) -> dict:
    """일봉 B규칙 지표 (최신봉=오늘, current·당일 거래량으로 당일치 대체)."""
    candles = kis_api.get_daily_chart(code, days=120)
    closes, highs, volumes = _parse_candles(candles)
    need = max(60, BREAKOUT_HIGH_DAYS + 1, VOLUME_AVG_DAYS + 1)
    if len(closes) < need:
        return {}

    price_series = [current] + closes[1:60]
    ma20 = sum(price_series[:20]) / 20
    ma60 = sum(price_series[:60]) / 60
    rsi_closes = [current] + closes[1:]
    rsi = _daily_rsi(rsi_closes, 14)

    prior_highs = highs[1 : BREAKOUT_HIGH_DAYS + 1]
    high_n = max(prior_highs) if prior_highs else 0.0

    prior_vols = volumes[1 : VOLUME_AVG_DAYS + 1]
    vol_avg = sum(prior_vols) / len(prior_vols) if prior_vols else 0.0
    vol_ratio = today_volume / vol_avg if vol_avg > 0 else 0.0

    return {
        "ma20": ma20,
        "ma60": ma60,
        "rsi": rsi,
        "high_n": high_n,
        "vol_ratio": vol_ratio,
    }


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
    try:
        today_volume = float(info.get("acml_vol", 0))
    except (TypeError, ValueError):
        today_volume = 0.0

    try:
        daily = _daily_breakout_context(TARGET_CODE, float(current), today_volume)
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
    high_n = float(daily.get("high_n", 0))
    vol_ratio = float(daily.get("vol_ratio", 0))

    if current <= ma20 or ma20 <= ma60:
        return None, used
    if rsi < MIN_RSI:
        return None, used
    if high_n <= 0 or current < high_n:
        return None, used
    if vol_ratio < MIN_VOLUME_RATIO:
        return None, used

    return {
        "code": TARGET_CODE,
        "name": TARGET_NAME,
        "current": current,
        "ma20": ma20,
        "ma60": ma60,
        "rsi": rsi,
        "high_n": high_n,
        "vol_ratio": round(vol_ratio, 2),
        "strategy": STRATEGY,
        "reason": (
            f"B 돌파 · {BREAKOUT_HIGH_DAYS}일 고가 {high_n:,.0f} 돌파 · "
            f"가격>{ma20:,.0f}(MA20)>{ma60:,.0f}(MA60) · RSI {rsi:.0f} · 거래량 {vol_ratio:.1f}배"
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
