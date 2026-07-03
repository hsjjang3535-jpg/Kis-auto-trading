"""
낙폭반등 단타 (급락 후 5분봉 반등 매매)

- ENABLE_CRASH_BOUNCE=false (기본) → 코드만 있고 동작 없음
- 09:10~10:30 진입 / 5분봉 MA 저항·손익·시간 청산
- AI 미사용, 전용 자금·API 호출 상한
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import kis_api

KST = ZoneInfo("Asia/Seoul")
STRATEGY = "낙폭반등"

_ETF_KEYWORDS = [
    "KODEX", "TIGER", "KBSTAR", "HANARO", "ARIRANG", "KOSEF",
    "FOCUS", "TIMEFOLIO", "KTOP", "SOL", "ACE", "MASTER",
    "ETF", "레버리지", "인버스",
]


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default).lower()).lower() == "true"


def _parse_hhmm(value: str, default_h: int, default_m: int) -> int:
    """HH:MM → 자정 기준 분"""
    try:
        h, m = value.strip().split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return default_h * 60 + default_m


# ── 설정 (환경변수) ────────────────────────────────────────────────────────────

ENABLED = _env_bool("ENABLE_CRASH_BOUNCE", False)
ENTRY_START_MIN = _parse_hhmm(os.getenv("CRASH_BOUNCE_ENTRY_START", "09:10"), 9, 10)
ENTRY_END_MIN = _parse_hhmm(os.getenv("CRASH_BOUNCE_ENTRY_END", "10:30"), 10, 30)
TIME_EXIT_MIN = _parse_hhmm(os.getenv("CRASH_BOUNCE_TIME_EXIT", "11:00"), 11, 0)
MIN_DROP_PCT = float(os.getenv("CRASH_BOUNCE_MIN_DROP", "3.5"))
MAX_AMOUNT = int(os.getenv("MAX_CRASH_BOUNCE_AMOUNT", "500000"))
MAX_BUY = int(os.getenv("MAX_CRASH_BOUNCE_BUY", "500000"))
MAX_POSITIONS = int(os.getenv("CRASH_BOUNCE_MAX_POSITIONS", "1"))
MAX_API_CALLS = int(os.getenv("CRASH_BOUNCE_MAX_API_CALLS", "20"))
TAKE_PROFIT_PCT = float(os.getenv("CRASH_BOUNCE_TAKE_PROFIT", "3.0"))
STOP_LOSS_PCT = float(os.getenv("CRASH_BOUNCE_STOP_LOSS", "2.0"))
MA_TOLERANCE_PCT = float(os.getenv("CRASH_BOUNCE_MA_TOLERANCE", "0.5"))
SCAN_TOP_N = int(os.getenv("CRASH_BOUNCE_SCAN_TOP", "20"))
MAX_CHART_CHECKS = int(os.getenv("CRASH_BOUNCE_MAX_CHART_CHECKS", "5"))


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


def _drop_from_open(info: dict) -> float:
    try:
        open_p = float(info.get("stck_oprc", 0))
        current = float(info.get("stck_prpr", 0))
        if open_p <= 0:
            return 0.0
        return (open_p - current) / open_p * 100
    except (ValueError, TypeError, ZeroDivisionError):
        return 0.0


def _has_bounce_pattern(bars: list[dict]) -> bool:
    """5분봉 더블바텀 또는 저점 대비 반등"""
    if len(bars) < 3:
        return False
    recent = bars[-3:]
    lows = [b["low"] for b in recent]
    # 저점 형성 후 반등
    if lows[-1] >= lows[-2] and recent[-1]["close"] > recent[-2]["close"]:
        return True
    session_low = min(b["low"] for b in bars)
    if session_low <= 0:
        return False
    return recent[-1]["close"] >= session_low * 1.003


def scan_candidates(api_budget: int | None = None) -> tuple[list[dict], int]:
    """
    거래대금 상위 중 시가 대비 급락 종목 스캔.
    Returns: (후보 리스트, 사용한 API 호출 수)
    """
    budget = api_budget if api_budget is not None else MAX_API_CALLS
    used = 0
    candidates: list[dict] = []

    try:
        kospi = kis_api.get_top_trading_value(SCAN_TOP_N, market="0001")
        used += 1
        time.sleep(0.3)
        if used >= budget:
            return [], used
        kosdaq = kis_api.get_top_trading_value(SCAN_TOP_N, market="1001")
        used += 1
    except Exception as e:
        print(f"[낙폭반등] 거래대금 조회 실패: {e}")
        return [], used

    pool: list[dict] = []
    seen: set[str] = set()
    for s in kospi + kosdaq:
        code = s.get("mksc_shrn_iscd", "")
        name = s.get("hts_kor_isnm", "")
        if not code or code in seen or _is_etf(name):
            continue
        seen.add(code)
        try:
            rate = float(s.get("prdy_ctrt", "0"))
        except ValueError:
            rate = 0.0
        if rate >= 0:
            continue
        pool.append({"code": code, "name": name, "prdy_ctrt": rate})

    pool.sort(key=lambda x: x["prdy_ctrt"])

    for item in pool[:MAX_CHART_CHECKS * 2]:
        if used >= budget or len(candidates) >= MAX_CHART_CHECKS:
            break
        code = item["code"]
        try:
            info = kis_api.get_stock_info(code)
            used += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"[낙폭반등] {item['name']} 시세 실패: {e}")
            continue

        drop = _drop_from_open(info)
        if drop < MIN_DROP_PCT:
            continue

        try:
            daily = kis_api.get_chart_indicators(code)
            used += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"[낙폭반등] {item['name']} 일봉 실패: {e}")
            continue

        if not daily:
            continue

        current = float(info.get("stck_prpr", 0))
        ma5 = daily.get("ma5", 0)
        # 급락 반등: 일봉 5일선 아래 (하단 눌림목과 구분)
        if ma5 > 0 and current >= ma5:
            continue

        if used >= budget:
            break

        try:
            intra = kis_api.get_intraday_5min_indicators(code)
            used += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"[낙폭반등] {item['name']} 분봉 실패: {e}")
            continue

        if not intra or intra.get("bar_count", 0) < 3:
            continue

        bars = intra.get("bars_5", [])
        if not _has_bounce_pattern(bars):
            continue

        candidates.append({
            "code": code,
            "name": item["name"],
            "current": current,
            "drop_from_open": round(drop, 2),
            "ma5": ma5,
            "ma60": intra.get("ma60", 0),
            "ma_period": intra.get("ma_period", 0),
            "rsi": intra.get("rsi", 50),
            "strategy": STRATEGY,
            "reason": f"시가 대비 -{drop:.1f}% 급락 후 5분봉 반등",
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
    """낙폭반등 전용 청산 조건"""
    now_min = datetime.now(KST).hour * 60 + datetime.now(KST).minute

    if profit_pct <= -STOP_LOSS_PCT:
        return True, f"낙폭반등 손절 ({profit_pct:.1f}%)"

    if profit_pct >= TAKE_PROFIT_PCT:
        return True, f"낙폭반등 익절 (+{profit_pct:.1f}%)"

    ma60 = pos.get("exit_ma60") or (intra or {}).get("ma60", 0)
    if ma60 > 0 and current >= ma60 * (1 - MA_TOLERANCE_PCT / 100):
        if profit_pct > 0:
            return True, f"5분봉 MA{pos.get('ma_period', 60)} 저항 익절 (+{profit_pct:.1f}%)"

    if now_min >= TIME_EXIT_MIN and profit_pct > 0:
        return True, f"낙폭반등 시간 청산 (+{profit_pct:.1f}%, {TIME_EXIT_MIN // 60:02d}:{TIME_EXIT_MIN % 60:02d})"

    return False, ""
