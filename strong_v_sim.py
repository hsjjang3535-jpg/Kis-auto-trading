"""
강세주 급락 V — 시뮬만 (SK하이닉스형 장중 급락 후 V반등)

- ENABLE_STRONG_V_SIM=false (기본) → 코드만 있고 동작 없음
- 전일比 +2%+ 강세주 · 시가 대비 -3%+ 급락 · 전일종가 -5% 이내 · 5분봉 V반등
- MA5 필터 완화: MA5 아래 N%까지 허용 (기본 3%)
- 실제 주문 없음 — 가상 매수·청산 + 텔레그램 알림
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import kis_api
from v_reversal import _has_v_bounce

KST = ZoneInfo("Asia/Seoul")
STRATEGY = "강세V시뮬"

_ETF_KEYWORDS = [
    "KODEX", "TIGER", "KBSTAR", "HANARO", "ARIRANG", "KOSEF",
    "FOCUS", "TIMEFOLIO", "KTOP", "SOL", "ACE", "MASTER",
    "ETF", "레버리지", "인버스",
]

_open_position: dict | None = None
_focus_target: dict | None = None
_sim_trades_today: list[dict] = []


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

ENABLED = _env_bool("ENABLE_STRONG_V_SIM", False)
ENTRY_START_MIN = _parse_hhmm(os.getenv("STRONG_V_ENTRY_START", "09:15"), 9, 15)
ENTRY_END_MIN = _parse_hhmm(os.getenv("STRONG_V_ENTRY_END", "14:30"), 14, 30)
EXIT_END_MIN = _parse_hhmm(os.getenv("STRONG_V_EXIT_END", "14:50"), 14, 50)
TIME_EXIT_MIN = _parse_hhmm(os.getenv("STRONG_V_TIME_EXIT", "14:20"), 14, 20)
MIN_PREV_RATE = float(os.getenv("STRONG_V_MIN_PREV_RATE", "2.0"))
MIN_DROP_PCT = float(os.getenv("STRONG_V_MIN_DROP", "3.0"))
MAX_BELOW_PREV_PCT = float(os.getenv("STRONG_V_MAX_BELOW_PREV", "5.0"))
MA5_BELOW_TOLERANCE_PCT = float(os.getenv("STRONG_V_MA5_TOLERANCE", "3.0"))
MIN_TRADING_VALUE = int(os.getenv("STRONG_V_MIN_TRADING_VALUE", "10000000000"))
SIM_AMOUNT = int(os.getenv("STRONG_V_SIM_AMOUNT", "500000"))
MAX_POSITIONS = 1
MAX_API_CALLS = int(os.getenv("STRONG_V_MAX_API_CALLS", "20"))
SCAN_TOP_N = int(os.getenv("STRONG_V_SCAN_TOP", "30"))
MAX_CHART_CHECKS = int(os.getenv("STRONG_V_MAX_CHART_CHECKS", "5"))
TAKE_PROFIT_PCT = float(os.getenv("STRONG_V_TAKE_PROFIT", "3.0"))
TRAILING_STOP_PCT = float(os.getenv("STRONG_V_TRAILING_STOP", "1.0"))
STOP_LOSS_PCT = float(os.getenv("STRONG_V_STOP_LOSS", "2.0"))
MA_TOLERANCE_PCT = float(os.getenv("STRONG_V_MA_TOLERANCE", "0.5"))
SCAN_START_MIN = _parse_hhmm(os.getenv("STRONG_V_SCAN_START", "09:00"), 9, 0)
FAST_SCAN_END_MIN = _parse_hhmm(os.getenv("STRONG_V_FAST_SCAN_END", "10:00"), 10, 0)
POLL_INTERVAL_MIN = int(os.getenv("STRONG_V_POLL_INTERVAL", "5"))
FAST_SCAN_INTERVAL_MIN = int(os.getenv("STRONG_V_FAST_SCAN_INTERVAL", "2"))
FOCUS_INTERVAL_MIN = int(os.getenv("STRONG_V_FOCUS_INTERVAL", "1"))


def is_enabled() -> bool:
    return ENABLED


def is_trading_weekday() -> bool:
    return datetime.now(KST).weekday() < 5


def _now_min() -> int:
    now = datetime.now(KST)
    return now.hour * 60 + now.minute


def is_entry_window() -> bool:
    if not ENABLED or not is_trading_weekday():
        return False
    t = _now_min()
    return ENTRY_START_MIN <= t <= ENTRY_END_MIN


def is_scan_window() -> bool:
    """넓은 스캔 구간 (09:00~ 진입종료, 후보 탐색)"""
    if not ENABLED or not is_trading_weekday():
        return False
    t = _now_min()
    if _open_position and t <= EXIT_END_MIN:
        return False
    if _focus_target:
        return False
    return SCAN_START_MIN <= t <= ENTRY_END_MIN


def is_monitor_window() -> bool:
    """진입·후보추적·보유 청산 구간"""
    if not ENABLED or not is_trading_weekday():
        return False
    t = _now_min()
    if _open_position and t <= EXIT_END_MIN:
        return True
    if _focus_target and t <= ENTRY_END_MIN:
        return True
    return SCAN_START_MIN <= t <= ENTRY_END_MIN


def has_focus_or_position() -> bool:
    return _focus_target is not None or _open_position is not None


def get_poll_interval_min() -> int:
    """1분=후보/보유 추적, 09~10시=2분, 그 외=5분"""
    if has_focus_or_position():
        return FOCUS_INTERVAL_MIN
    t = _now_min()
    if SCAN_START_MIN <= t < FAST_SCAN_END_MIN:
        return FAST_SCAN_INTERVAL_MIN
    return POLL_INTERVAL_MIN


def get_open_position() -> dict | None:
    return _open_position


def get_focus_target() -> dict | None:
    return _focus_target


def load_open_position(data: dict | None) -> None:
    global _open_position
    _open_position = data if isinstance(data, dict) else None


def load_focus_target(data: dict | None) -> None:
    global _focus_target
    _focus_target = data if isinstance(data, dict) else None


def dump_open_position() -> dict | None:
    return dict(_open_position) if _open_position else None


def dump_focus_target() -> dict | None:
    return dict(_focus_target) if _focus_target else None


def get_sim_trades_today() -> list[dict]:
    return list(_sim_trades_today)


def load_sim_trades_today(data: list | None) -> None:
    global _sim_trades_today
    _sim_trades_today = data if isinstance(data, list) else []


def dump_sim_trades_today() -> list[dict]:
    return list(_sim_trades_today)


def reset_daily_sim_trades() -> None:
    global _sim_trades_today, _open_position, _focus_target
    _sim_trades_today = []
    _open_position = None
    _focus_target = None


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


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
    if prev <= 0 or current >= prev:
        return 0.0
    return (prev - current) / prev * 100


def _ma5_ok(current: float, ma5: float) -> bool:
    """MA5 완화: MA5 아래 MA5_BELOW_TOLERANCE_PCT%까지 허용"""
    if ma5 <= 0:
        return True
    floor = ma5 * (1 - MA5_BELOW_TOLERANCE_PCT / 100)
    return current >= floor


def scan_candidates(api_budget: int | None = None) -> tuple[list[dict], int]:
    """거래대금 상위 중 강세주 장중 급락 + V반등 스캔."""
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
        print(f"[강세V시뮬] 거래대금 조회 실패: {e}")
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
            print(f"[강세V시뮬] {item['name']} 시세 실패: {e}")
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
            print(f"[강세V시뮬] {item['name']} 일봉 실패: {e}")
            continue

        if not daily:
            continue

        ma5 = daily.get("ma5", 0)
        if not _ma5_ok(current, ma5):
            continue

        if used >= budget:
            break

        try:
            intra = kis_api.get_intraday_5min_indicators(code)
            used += 1
            _throttle()
        except Exception as e:
            print(f"[강세V시뮬] {item['name']} 분봉 실패: {e}")
            continue

        if not intra or intra.get("bar_count", 0) < 3:
            continue

        bars = intra.get("bars_5", [])
        if not _has_v_bounce(bars):
            continue

        below_ma5 = ""
        if ma5 > 0 and current < ma5:
            gap = (ma5 - current) / ma5 * 100
            below_ma5 = f" · MA5 아래 {gap:.1f}% (허용 {MA5_BELOW_TOLERANCE_PCT}%)"

        candidates.append({
            "code": code,
            "name": item["name"],
            "current": current,
            "drop_from_open": round(drop, 2),
            "prdy_ctrt": item["prdy_ctrt"],
            "below_prev_pct": round(_below_prev_pct(current, prev), 2),
            "ma5": ma5,
            "ma60": intra.get("ma60", 0),
            "ma_period": intra.get("ma_period", 0),
            "strategy": STRATEGY,
            "reason": (
                f"전일比+{item['prdy_ctrt']:.1f}% · "
                f"시가 -{drop:.1f}% · 전일종가 -{_below_prev_pct(current, prev):.1f}% "
                f"V반등{below_ma5}"
            ),
        })

        if used >= budget:
            break

    candidates.sort(key=lambda x: x["drop_from_open"], reverse=True)
    return candidates, used


def _evaluate_single(
    code: str,
    name: str,
    api_budget: int,
    prdy_ctrt: float = 0.0,
    require_v: bool = True,
) -> tuple[dict | None, int]:
    """단일 종목 재검증 (후보 1분 추적·진입 확인용)."""
    used = 0
    if used >= api_budget:
        return None, used
    try:
        info = kis_api.get_stock_info(code)
        used += 1
        _throttle()
    except Exception as e:
        print(f"[강세V시뮬] {name} 시세 실패: {e}")
        return None, used

    drop = _drop_from_open(info)
    if drop < MIN_DROP_PCT:
        return None, used

    current = float(info.get("stck_prpr", 0))
    prev = _prev_close(info)
    if _below_prev_pct(current, prev) > MAX_BELOW_PREV_PCT:
        return None, used

    if used >= api_budget:
        return None, used

    try:
        daily = kis_api.get_chart_indicators(code)
        used += 1
        _throttle()
    except Exception as e:
        print(f"[강세V시뮬] {name} 일봉 실패: {e}")
        return None, used

    if not daily:
        return None, used

    ma5 = daily.get("ma5", 0)
    if not _ma5_ok(current, ma5):
        return None, used

    if used >= api_budget:
        return None, used

    try:
        intra = kis_api.get_intraday_5min_indicators(code)
        used += 1
        _throttle()
    except Exception as e:
        print(f"[강세V시뮬] {name} 분봉 실패: {e}")
        return None, used

    if not intra or intra.get("bar_count", 0) < 3:
        return None, used

    bars = intra.get("bars_5", [])
    if require_v and not _has_v_bounce(bars):
        return None, used

    below_ma5 = ""
    if ma5 > 0 and current < ma5:
        gap = (ma5 - current) / ma5 * 100
        below_ma5 = f" · MA5 아래 {gap:.1f}% (허용 {MA5_BELOW_TOLERANCE_PCT}%)"

    prdy_ctrt = prdy_ctrt or (_focus_target.get("prdy_ctrt", 0) if _focus_target else 0)
    if require_v:
        reason_suffix = "V반등"
    else:
        reason_suffix = "V대기"
    return {
        "code": code,
        "name": name,
        "current": current,
        "drop_from_open": round(drop, 2),
        "prdy_ctrt": prdy_ctrt,
        "below_prev_pct": round(_below_prev_pct(current, prev), 2),
        "ma5": ma5,
        "ma60": intra.get("ma60", 0),
        "ma_period": intra.get("ma_period", 0),
        "strategy": STRATEGY,
        "reason": (
            f"전일比+{prdy_ctrt:.1f}% · "
            f"시가 -{drop:.1f}% · 전일종가 -{_below_prev_pct(current, prev):.1f}% "
            f"{reason_suffix}{below_ma5}"
        ),
    }, used


def _prefilter_single(
    code: str,
    name: str,
    api_budget: int,
) -> tuple[bool, int]:
    """후보 추적 유지 여부 — 급락·MA5 등 사전조건만 확인."""
    used = 0
    if used >= api_budget:
        return False, used
    try:
        info = kis_api.get_stock_info(code)
        used += 1
        _throttle()
    except Exception as e:
        print(f"[강세V시뮬] {name} 시세 실패: {e}")
        return False, used

    drop = _drop_from_open(info)
    if drop < MIN_DROP_PCT:
        return False, used

    current = float(info.get("stck_prpr", 0))
    prev = _prev_close(info)
    if _below_prev_pct(current, prev) > MAX_BELOW_PREV_PCT:
        return False, used

    if used >= api_budget:
        return False, used

    try:
        daily = kis_api.get_chart_indicators(code)
        used += 1
        _throttle()
    except Exception as e:
        print(f"[강세V시뮬] {name} 일봉 실패: {e}")
        return False, used

    if not daily:
        return False, used

    ma5 = daily.get("ma5", 0)
    if not _ma5_ok(current, ma5):
        return False, used

    return True, used


def _sim_qty(price: int) -> int:
    return max(SIM_AMOUNT // price, 1) if price > 0 else 0


def _evaluate_exit(
    pos: dict,
    current: float,
    profit_pct: float,
    intra: dict | None = None,
) -> tuple[bool, str]:
    now_min = _now_min()

    if profit_pct <= -STOP_LOSS_PCT:
        return True, f"강세V 손절 ({profit_pct:.1f}%)"

    peak = pos.get("peak_price", pos["buy_price"])
    peak_profit = (peak - pos["buy_price"]) / pos["buy_price"] * 100
    drop_from_peak = (peak - current) / peak * 100 if peak > 0 else 0

    if peak_profit >= TAKE_PROFIT_PCT and drop_from_peak >= TRAILING_STOP_PCT:
        return True, (
            f"강세V 트레일링 (+{profit_pct:.1f}% / 고점 +{peak_profit:.1f}%"
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
        return True, f"강세V 시간 청산 (+{profit_pct:.1f}%, {hh:02d}:{mm:02d})"

    if now_min >= EXIT_END_MIN:
        return True, f"강세V 장마감 청산 ({profit_pct:+.1f}%)"

    return False, ""


def _open_sim(stock: dict, price: int) -> dict | None:
    global _open_position, _focus_target
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
        "exit_ma60": stock.get("ma60", 0),
        "ma_period": stock.get("ma_period", 60),
    }
    _focus_target = None
    return {
        "action": "buy",
        "name": stock["name"],
        "code": stock["code"],
        "quantity": qty,
        "price": price,
        "reason": f"[강세V시뮬] {stock.get('reason', '')}",
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


def run_check(api_budget: int | None = None) -> tuple[list[dict], int]:
    """보유 1분 청산 → 후보 1분 추적 → 넓은 스캔(5/2분). Returns (events, api_used)."""
    global _focus_target
    if not ENABLED or not is_monitor_window():
        return [], 0

    budget = api_budget if api_budget is not None else MAX_API_CALLS
    used = 0
    events: list[dict] = []

    if _open_position:
        code = _open_position["code"]
        try:
            current = float(kis_api.get_current_price(code))
            used += 1
            _throttle()
        except Exception as e:
            print(f"[강세V시뮬] {_open_position['name']} 현재가 실패: {e}")
            return events, used

        current_i = int(current)
        buy_price = _open_position["buy_price"]
        if current_i > _open_position.get("peak_price", buy_price):
            _open_position["peak_price"] = current_i

        profit_pct = (current - buy_price) / buy_price * 100

        intra = None
        if used < budget:
            try:
                intra = kis_api.get_intraday_5min_indicators(code)
                used += 1
                _throttle()
            except Exception as e:
                print(f"[강세V시뮬] {_open_position['name']} 분봉 실패: {e}")

        should_sell, reason = _evaluate_exit(_open_position, current, profit_pct, intra)
        if should_sell:
            sim = _close_sim(current_i, reason)
            if sim:
                events.append(sim)
        return events, used

    if _focus_target:
        if not is_entry_window() or _sim_trades_today:
            return events, used

        code = _focus_target["code"]
        name = _focus_target["name"]
        pre_ok, pre_used = _prefilter_single(code, name, budget - used)
        used += pre_used
        if not pre_ok:
            print(f"[강세V시뮬] 후보 {name}({code}) 사전조건 이탈 — 추적 해제")
            _focus_target = None
            return events, used

        stock, eval_used = _evaluate_single(
            code, name, budget - used, _focus_target.get("prdy_ctrt", 0), require_v=True,
        )
        used += eval_used

        if not stock:
            print(f"[강세V시뮬] 후보 {name}({code}) V미형성 — 1분 후 재확인")
            return events, used

        try:
            current = kis_api.get_current_price(code, fallback=stock.get("current"))
            used += 1
            _throttle()
            if current == 0:
                return events, used
            sim = _open_sim(stock, int(current))
            if sim:
                events.append(sim)
                print(
                    f"[강세V시뮬] 가상매수 {name}({code}) "
                    f"{sim['quantity']}주 @ {int(current):,}"
                )
        except Exception as e:
            print(f"[강세V시뮬] {name} 진입 오류: {e}")
        return events, used

    if not is_scan_window():
        return events, used

    if _sim_trades_today:
        return events, used

    remaining_budget = budget - used
    try:
        candidates, scan_used = scan_candidates(api_budget=remaining_budget)
        used += scan_used
        print(f"[강세V시뮬] 넓은 스캔 {len(candidates)}개 후보 (API {used}회)")
    except Exception as e:
        print(f"[강세V시뮬] 스캔 오류: {e}")
        return events, used

    if candidates:
        _focus_target = candidates[0]
        print(
            f"[강세V시뮬] 후보 포착 {_focus_target['name']}({_focus_target['code']}) "
            f"— 1분 추적 시작"
        )

    return events, used


def format_summary() -> list[str]:
    if not _sim_trades_today and not _open_position and not _focus_target:
        return []

    lines: list[str] = []
    if _sim_trades_today:
        net = sum(t["profit_won"] for t in _sim_trades_today)
        sign = "+" if net >= 0 else ""
        lines.append(
            f"🟢 <b>강세V [시뮬] 오늘 체결 {len(_sim_trades_today)}건</b> → {sign}{net:,}원"
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
            f"🟢 강세V [시뮬] 보유: {pos['name']}({pos['code']}) "
            f"{pos['buy_price']:,}원 × {pos['quantity']}주"
        )
    elif _focus_target:
        ft = _focus_target
        lines.append(
            f"🟢 강세V [시뮬] 후보 추적: {ft['name']}({ft['code']}) 1분"
        )
    return lines
