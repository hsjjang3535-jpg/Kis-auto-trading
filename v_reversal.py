"""
V자반등 단타 (강세주 시초 조정 후 5분봉 반등)

- ENABLE_V_REVERSAL=false (기본) → 코드만 있고 동작 없음
- 전일比 상승 + 5일선 위 + 시가 대비 조정 + V반등 → 09:15~10:30 진입
- 트레일링 익절 / 5분봉 MA 저항 / 14:20 시간청산
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import kis_api

KST = ZoneInfo("Asia/Seoul")
STRATEGY = "V자반등"

MIN_TRADING_VALUE = 10_000_000_000

_ETF_KEYWORDS = [
    "KODEX", "TIGER", "KBSTAR", "HANARO", "ARIRANG", "KOSEF",
    "FOCUS", "TIMEFOLIO", "KTOP", "SOL", "ACE", "MASTER",
    "ETF", "레버리지", "인버스",
]


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default).lower()).lower() == "true"


def _parse_hhmm(value: str, default_h: int, default_m: int) -> int:
    try:
        h, m = value.strip().split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return default_h * 60 + default_m


def _throttle() -> None:
    delay = 0.5 if os.getenv("KIS_MODE", "모의") == "모의" else 0.3
    time.sleep(delay)


# ── 설정 ──────────────────────────────────────────────────────────────────────

ENABLED = _env_bool("ENABLE_V_REVERSAL", False)
ENTRY_START_MIN = _parse_hhmm(os.getenv("V_REVERSAL_ENTRY_START", "09:15"), 9, 15)
ENTRY_END_MIN = _parse_hhmm(os.getenv("V_REVERSAL_ENTRY_END", "10:30"), 10, 30)
TIME_EXIT_MIN = _parse_hhmm(os.getenv("V_REVERSAL_TIME_EXIT", "14:20"), 14, 20)
MIN_PREV_RATE = float(os.getenv("V_REVERSAL_MIN_PREV_RATE", "1.0"))
MIN_DROP_PCT = float(os.getenv("V_REVERSAL_MIN_DROP", "2.0"))
MAX_BELOW_PREV_PCT = float(os.getenv("V_REVERSAL_MAX_BELOW_PREV", "1.0"))
MAX_AMOUNT = int(os.getenv("MAX_V_REVERSAL_AMOUNT", "500000"))
MAX_BUY = int(os.getenv("MAX_V_REVERSAL_BUY", "500000"))
MAX_POSITIONS = int(os.getenv("V_REVERSAL_MAX_POSITIONS", "1"))
MAX_API_CALLS = int(os.getenv("V_REVERSAL_MAX_API_CALLS", "20"))
TAKE_PROFIT_PCT = float(os.getenv("V_REVERSAL_TAKE_PROFIT", "3.0"))
TRAILING_STOP_PCT = float(os.getenv("V_REVERSAL_TRAILING_STOP", "1.0"))
STOP_LOSS_PCT = float(os.getenv("V_REVERSAL_STOP_LOSS", "2.0"))
MA_TOLERANCE_PCT = float(os.getenv("V_REVERSAL_MA_TOLERANCE", "0.5"))
SCAN_TOP_N = int(os.getenv("V_REVERSAL_SCAN_TOP", "30"))
MAX_CHART_CHECKS = int(os.getenv("V_REVERSAL_MAX_CHART_CHECKS", "5"))


def is_enabled() -> bool:
    return ENABLED


def is_entry_window() -> bool:
    if not ENABLED:
        return False
    t = datetime.now(KST).hour * 60 + datetime.now(KST).minute
    return ENTRY_START_MIN <= t <= ENTRY_END_MIN


def _is_etf(name: str) -> bool:
    upper = name.upper()
    return any(kw.upper() in upper for kw in _ETF_KEYWORDS)


def _get_trading_value(stock: dict) -> int:
    try:
        return int(stock.get("acml_tr_pbmn", 0))
    except (ValueError, TypeError):
        return 0


def _drop_from_open(info: dict) -> float:
    try:
        open_p = float(info.get("stck_oprc", 0))
        current = float(info.get("stck_prpr", 0))
        if open_p <= 0:
            return 0.0
        return (open_p - current) / open_p * 100
    except (ValueError, TypeError, ZeroDivisionError):
        return 0.0


def _prev_close(info: dict) -> float:
    try:
        return float(info.get("stck_prdy_clpr") or info.get("prdy_clpr") or 0)
    except (ValueError, TypeError):
        return 0.0


def _below_prev_pct(current: float, prev: float) -> float:
    if prev <= 0:
        return 0.0
    if current >= prev:
        return 0.0
    return (prev - current) / prev * 100


def _has_v_bounce(bars: list[dict]) -> bool:
    """5분봉 V자 반등: 저점 형성 + 종가 상승 전환"""
    if len(bars) < 3:
        return False
    recent = bars[-3:]
    lows = [b["low"] for b in recent]
    if lows[-1] >= lows[-2] and recent[-1]["close"] > recent[-2]["close"]:
        return True
    session_low = min(b["low"] for b in bars)
    if session_low <= 0:
        return False
    return (
        recent[-1]["close"] >= session_low * 1.003
        and recent[-1]["close"] > recent[-2]["close"]
    )


def scan_candidates(api_budget: int | None = None) -> tuple[list[dict], int]:
    """거래대금 상위 중 강세주 시초 조정 + V반등 스캔."""
    budget = api_budget if api_budget is not None else MAX_API_CALLS
    used = 0
    candidates: list[dict] = []

    try:
        kospi = kis_api.get_top_trading_value(SCAN_TOP_N, market="0001")
        used += 1
        _throttle()
        if used >= budget:
            return [], used
        kosdaq = kis_api.get_top_trading_value(SCAN_TOP_N, market="1001")
        used += 1
        _throttle()
    except Exception as e:
        print(f"[V자반등] 거래대금 조회 실패: {e}")
        return [], used

    pool: list[dict] = []
    seen: set[str] = set()
    for s in kospi + kosdaq:
        code = s.get("mksc_shrn_iscd", "")
        name = s.get("hts_kor_isnm", "")
        if not code or code in seen or _is_etf(name):
            continue
        if _get_trading_value(s) < MIN_TRADING_VALUE:
            continue
        seen.add(code)
        try:
            rate = float(s.get("prdy_ctrt", "0"))
        except ValueError:
            rate = 0.0
        if rate < MIN_PREV_RATE:
            continue
        pool.append({"code": code, "name": name, "prdy_ctrt": rate})

    pool.sort(key=lambda x: x["prdy_ctrt"], reverse=True)

    for item in pool[:MAX_CHART_CHECKS * 2]:
        if used >= budget or len(candidates) >= MAX_CHART_CHECKS:
            break
        code = item["code"]
        try:
            info = kis_api.get_stock_info(code)
            used += 1
            _throttle()
        except Exception as e:
            print(f"[V자반등] {item['name']} 시세 실패: {e}")
            continue

        drop = _drop_from_open(info)
        if drop < MIN_DROP_PCT:
            continue

        current = float(info.get("stck_prpr", 0))
        prev = _prev_close(info)
        if _below_prev_pct(current, prev) > MAX_BELOW_PREV_PCT:
            continue

        if used >= budget:
            break

        try:
            daily = kis_api.get_chart_indicators(code)
            used += 1
            _throttle()
        except Exception as e:
            print(f"[V자반등] {item['name']} 일봉 실패: {e}")
            continue

        if not daily:
            continue

        ma5 = daily.get("ma5", 0)
        if ma5 > 0 and current < ma5:
            continue

        if used >= budget:
            break

        try:
            intra = kis_api.get_intraday_5min_indicators(code)
            used += 1
            _throttle()
        except Exception as e:
            print(f"[V자반등] {item['name']} 분봉 실패: {e}")
            continue

        if not intra or intra.get("bar_count", 0) < 3:
            continue

        bars = intra.get("bars_5", [])
        if not _has_v_bounce(bars):
            continue

        candidates.append({
            "code": code,
            "name": item["name"],
            "current": current,
            "drop_from_open": round(drop, 2),
            "prdy_ctrt": item["prdy_ctrt"],
            "ma5": ma5,
            "ma60": intra.get("ma60", 0),
            "ma_period": intra.get("ma_period", 0),
            "rsi": intra.get("rsi", 50),
            "strategy": STRATEGY,
            "reason": (
                f"전일比+{item['prdy_ctrt']:.1f}% · 시가 대비 -{drop:.1f}% 조정 후 V반등"
            ),
        })

        if used >= budget:
            break

    candidates.sort(key=lambda x: x["drop_from_open"], reverse=True)
    return candidates, used


def evaluate_exit(
    pos: dict,
    current: float,
    profit_pct: float,
    intra: dict | None = None,
) -> tuple[bool, str]:
    """V자반등 전용 청산 (손절·트레일링·MA·14:20)"""
    now_min = datetime.now(KST).hour * 60 + datetime.now(KST).minute

    if profit_pct <= -STOP_LOSS_PCT:
        return True, f"V자반등 손절 ({profit_pct:.1f}%)"

    peak = pos.get("peak_price", pos["buy_price"])
    peak_profit = (peak - pos["buy_price"]) / pos["buy_price"] * 100
    drop_from_peak = (peak - current) / peak * 100 if peak > 0 else 0

    if peak_profit >= TAKE_PROFIT_PCT and drop_from_peak >= TRAILING_STOP_PCT:
        return True, (
            f"V자반등 트레일링 (+{profit_pct:.1f}% / 고점 +{peak_profit:.1f}%"
            f"에서 -{drop_from_peak:.1f}%)"
        )

    ma60 = pos.get("exit_ma60") or (intra or {}).get("ma60", 0)
    if ma60 > 0 and current >= ma60 * (1 - MA_TOLERANCE_PCT / 100):
        if profit_pct > 0:
            return True, (
                f"5분봉 MA{pos.get('ma_period', 60)} 저항 익절 (+{profit_pct:.1f}%)"
            )

    if now_min >= TIME_EXIT_MIN and profit_pct > 0:
        hh, mm = TIME_EXIT_MIN // 60, TIME_EXIT_MIN % 60
        return True, f"V자반등 시간 청산 (+{profit_pct:.1f}%, {hh:02d}:{mm:02d})"

    return False, ""
