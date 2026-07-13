"""
상한가 리바운딩 — 1단계: 텔레그램 알림 + 가상 매매 시뮬레이션

PDF 규칙:
  R0 = 상한가, R1/R2/R3 = (R0 - 상한가 이후 저점) 3등분
  R1 매수 → R0 익절, R2 1:1 추가매수, R3 손절, 매수 후 4일차 강제청산
  상한가 후 7거래일 이내 추적, 거래대금 500억+ 필터
  신규 상장주(일봉 부족·전일종가 없음)는 제외
  우선순위: K1실전 > K1플러스 > K2플러스 > K2 > 리바운딩
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import kis_api

KST = ZoneInfo("Asia/Seoul")
STRATEGY = "상한가리바운드"

_ETF_KEYWORDS = [
    "KODEX", "TIGER", "KBSTAR", "HANARO", "ARIRANG", "KOSEF",
    "FOCUS", "TIMEFOLIO", "KTOP", "SOL", "ACE", "MASTER",
    "ETF", "레버리지", "인버스",
]

_watchlist: dict[str, dict] = {}
_sim_trades_today: list[dict] = []


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default).lower()).lower() == "true"


def _parse_hhmm(value: str, default_h: int, default_m: int) -> int:
    try:
        h, m = value.strip().split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return default_h * 60 + default_m


# ── 설정 ─────────────────────────────────────────────────────────────────────

ENABLED = _env_bool("ENABLE_UL_REBOUND", False)
MONITOR_START_MIN = _parse_hhmm(os.getenv("UL_REBOUND_MONITOR_START", "09:00"), 9, 0)
MONITOR_END_MIN = _parse_hhmm(os.getenv("UL_REBOUND_MONITOR_END", "15:30"), 15, 30)
MIN_TRADING_VALUE = int(os.getenv("UL_REBOUND_MIN_TRADING_VALUE", "50000000000"))
WINDOW_DAYS = int(os.getenv("UL_REBOUND_WINDOW_DAYS", "7"))
FORCE_SELL_DAY = int(os.getenv("UL_REBOUND_FORCE_SELL_DAY", "4"))
SCAN_TOP_N = int(os.getenv("UL_REBOUND_SCAN_TOP", "30"))
MAX_API_CALLS = int(os.getenv("UL_REBOUND_MAX_API_CALLS", "25"))
MAX_CHART_CHECKS = int(os.getenv("UL_REBOUND_MAX_CHART_CHECKS", "8"))
LEVEL_TOLERANCE_PCT = float(os.getenv("UL_REBOUND_LEVEL_TOLERANCE", "0.5"))
SIM_AMOUNT = int(os.getenv("UL_REBOUND_SIM_AMOUNT", "500000"))
SIM_ENABLED = _env_bool("UL_REBOUND_SIM_ENABLED", True)
# 일봉 N일 미만(신규 상장 당일 등)은 R0~R3 불가 → 스캔 제외
MIN_HISTORY_DAYS = int(os.getenv("UL_REBOUND_MIN_HISTORY_DAYS", "2"))


def is_enabled() -> bool:
    """알림 전용 — 모의/실전 모두 동작 가능"""
    return ENABLED


def is_weekday_active() -> bool:
    """월~목(0~3)만 활성 — 금·토·일 제외"""
    return datetime.now(KST).weekday() in (0, 1, 2, 3)


def is_monitor_window() -> bool:
    if not ENABLED or not is_weekday_active():
        return False
    t = datetime.now(KST).hour * 60 + datetime.now(KST).minute
    return MONITOR_START_MIN <= t <= MONITOR_END_MIN


def get_watchlist() -> dict[str, dict]:
    return _watchlist


def load_watchlist(data: dict | None) -> None:
    global _watchlist
    _watchlist = data if isinstance(data, dict) else {}


def dump_watchlist() -> dict[str, dict]:
    return dict(_watchlist)


def get_sim_trades_today() -> list[dict]:
    return list(_sim_trades_today)


def load_sim_trades_today(data: list | None) -> None:
    global _sim_trades_today
    _sim_trades_today = data if isinstance(data, list) else []


def dump_sim_trades_today() -> list[dict]:
    return list(_sim_trades_today)


def reset_daily_sim_trades() -> None:
    global _sim_trades_today
    _sim_trades_today = []


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _is_etf(name: str) -> bool:
    upper = name.upper()
    return any(kw.upper() in upper for kw in _ETF_KEYWORDS)


def _trading_days_since(start_date: str) -> int:
    """start_date 다음 거래일부터 오늘까지 (주말 제외 근사)"""
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError:
        return 0
    today = datetime.now(KST).date()
    days = 0
    d = start + timedelta(days=1)
    while d <= today:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return days


def _is_ul_rate(rate: float) -> bool:
    return rate >= 29.0


def _is_at_upper_limit(info: dict) -> bool:
    sign = str(info.get("prdy_vrss_sign", ""))
    if sign == "1":
        return True
    try:
        current = float(info.get("stck_prpr", 0))
        upper = float(info.get("stck_mxpr", 0))
        if upper > 0 and current >= upper * 0.998:
            return True
        rate = float(info.get("prdy_ctrt", 0))
        return _is_ul_rate(rate)
    except (ValueError, TypeError):
        return False


def _get_trading_value(info_or_candle: dict) -> int:
    try:
        return int(info_or_candle.get("acml_tr_pbmn", 0))
    except (ValueError, TypeError):
        return 0


def _sort_daily(candles: list[dict]) -> list[dict]:
    return sorted(candles, key=lambda c: c.get("stck_bsop_date", ""))


def _is_new_listing(sorted_daily: list[dict]) -> bool:
    """전일 종가 없는 신규 상장(일봉 부족) — 상한가 리바운딩 대상 아님"""
    dates = {
        str(c.get("stck_bsop_date", "")).strip()
        for c in sorted_daily
        if str(c.get("stck_bsop_date", "")).strip()
    }
    return len(dates) < MIN_HISTORY_DAYS


def _levels_collapsed(levels: dict) -> bool:
    """당일 상한가 placeholder(R0=R1=R2=R3) — 매매선 무효"""
    try:
        vals = [int(levels.get(k, 0)) for k in ("r0", "r1", "r2", "r3")]
    except (TypeError, ValueError):
        return True
    if min(vals) <= 0:
        return True
    return len(set(vals)) == 1


def _find_ul_day(candles: list[dict]) -> tuple[dict | None, list[dict]]:
    """최근 WINDOW_DAYS 거래일 내 상한가 일봉 탐색. (ul_candle, 이후 캔들들)"""
    sorted_c = _sort_daily(candles)
    if not sorted_c:
        return None, []

    today_str = datetime.now(KST).strftime("%Y%m%d")
    cutoff = (datetime.now(KST) - timedelta(days=WINDOW_DAYS + 10)).strftime("%Y%m%d")

    recent = [c for c in sorted_c if cutoff <= c.get("stck_bsop_date", "") <= today_str]
    if not recent:
        return None, []

    for i in range(len(recent) - 1, -1, -1):
        candle = recent[i]
        try:
            rate = float(candle.get("prdy_ctrt", 0))
        except (ValueError, TypeError):
            rate = 0.0

        prev_close = 0.0
        if i > 0:
            try:
                prev_close = float(recent[i - 1].get("stck_clpr", 0))
            except (ValueError, TypeError):
                prev_close = 0.0

        is_ul = _is_ul_rate(rate)
        if not is_ul and prev_close > 0:
            try:
                high = float(candle.get("stck_hgpr", 0))
                is_ul = high >= prev_close * 1.29
            except (ValueError, TypeError):
                pass

        if is_ul:
            tv = _get_trading_value(candle)
            if tv < MIN_TRADING_VALUE:
                continue
            after = recent[i + 1:]
            return candle, after

    return None, []


def compute_levels(ul_candle: dict, candles_after_ul: list[dict]) -> dict:
    """R0~R3 라인 계산"""
    try:
        r0 = int(float(ul_candle.get("stck_hgpr", 0)))
    except (ValueError, TypeError):
        r0 = 0

    lows: list[float] = []
    for c in candles_after_ul:
        try:
            lows.append(float(c.get("stck_lwpr", 0)))
        except (ValueError, TypeError):
            continue

    if not lows:
        try:
            post_ul_low = int(float(ul_candle.get("stck_lwpr", 0)))
        except (ValueError, TypeError):
            post_ul_low = r0
    else:
        post_ul_low = int(min(lows))

    if post_ul_low <= 0 or post_ul_low >= r0:
        fallback = max(int(r0 * 0.05), 1)
        post_ul_low = max(r0 - fallback, 1)

    range_ = r0 - post_ul_low
    if range_ <= 0:
        range_ = max(int(r0 * 0.05), 1)
        post_ul_low = r0 - range_

    r1 = int(r0 - range_ / 3)
    r2 = int(r0 - 2 * range_ / 3)
    r3 = post_ul_low

    return {
        "r0": r0,
        "r1": r1,
        "r2": r2,
        "r3": r3,
        "post_ul_low": r3,
        "range": range_,
    }


def _format_ul_date(candle: dict) -> str:
    raw = candle.get("stck_bsop_date", "")
    if len(raw) == 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return _today()


def _alert_key(alert_type: str) -> str:
    return f"{alert_type}:{_today()}"


def _already_sent(entry: dict, alert_type: str) -> bool:
    sent = entry.setdefault("alerts_sent", [])
    return _alert_key(alert_type) in sent


def _mark_sent(entry: dict, alert_type: str) -> None:
    sent = entry.setdefault("alerts_sent", [])
    key = _alert_key(alert_type)
    if key not in sent:
        sent.append(key)


def _sim_qty(price: int) -> int:
    if price <= 0:
        return 0
    return max(SIM_AMOUNT // price, 1)


def _sim_avg_price(sim: dict) -> float:
    lots = sim.get("lots", [])
    if not lots:
        return float(sim.get("buy_price", 0))
    total_cost = sum(l["price"] * l["qty"] for l in lots)
    total_qty = sum(l["qty"] for l in lots)
    return total_cost / total_qty if total_qty else 0.0


def _sim_total_qty(sim: dict) -> int:
    return sum(l["qty"] for l in sim.get("lots", []))


def _sim_is_open(entry: dict) -> bool:
    sim = entry.get("sim")
    return bool(sim and sim.get("status") == "open")


def _open_sim_buy(entry: dict, price: int) -> dict | None:
    if not SIM_ENABLED or _sim_is_open(entry):
        return None
    sim = entry.get("sim")
    if sim and sim.get("status") == "closed":
        return None

    qty = _sim_qty(price)
    if qty < 1:
        return None

    entry["sim"] = {
        "status": "open",
        "buy_date": _today(),
        "lots": [{"price": price, "qty": qty, "type": "R1"}],
        "r2_added": False,
    }
    return {
        "action": "buy",
        "name": entry["name"],
        "code": entry["code"],
        "quantity": qty,
        "price": price,
        "reason": f"[시뮬] R1 매수구간 진입 @ {price:,}원",
    }


def _add_sim_buy(entry: dict, price: int) -> dict | None:
    if not SIM_ENABLED or not _sim_is_open(entry):
        return None
    sim = entry["sim"]
    if sim.get("r2_added"):
        return None

    qty = sim["lots"][0]["qty"]
    sim["lots"].append({"price": price, "qty": qty, "type": "R2"})
    sim["r2_added"] = True
    avg = int(_sim_avg_price(sim))
    total_qty = _sim_total_qty(sim)
    return {
        "action": "add",
        "name": entry["name"],
        "code": entry["code"],
        "quantity": qty,
        "total_quantity": total_qty,
        "price": price,
        "avg_price": avg,
        "reason": f"[시뮬] R2 추가매수 1:1 @ {price:,}원 (평단 {avg:,}원)",
    }


def _close_sim_position(entry: dict, price: int, reason: str) -> dict | None:
    if not SIM_ENABLED or not _sim_is_open(entry):
        return None

    sim = entry["sim"]
    avg = _sim_avg_price(sim)
    qty = _sim_total_qty(sim)
    if avg <= 0 or qty < 1:
        return None

    profit_pct = (price - avg) / avg * 100
    profit_won = int((price - avg) * qty)

    sim["status"] = "closed"
    sim["sell_price"] = price
    sim["sell_date"] = _today()
    sim["sell_reason"] = reason
    sim["profit_pct"] = round(profit_pct, 2)
    sim["profit_won"] = profit_won

    trade = {
        "action": "sell",
        "name": entry["name"],
        "code": entry["code"],
        "strategy": STRATEGY,
        "buy_price": int(avg),
        "sell_price": price,
        "quantity": qty,
        "buy_date": sim["buy_date"],
        "sell_date": _today(),
        "buy_reason": (
            f"R1 {entry['r1']:,}원"
            + (" + R2 추가" if sim.get("r2_added") else "")
        ),
        "sell_reason": reason,
        "profit_pct": sim["profit_pct"],
        "profit_won": profit_won,
        "r2_added": sim.get("r2_added", False),
    }
    _sim_trades_today.append(trade)
    return trade


def _apply_simulation(entry: dict, alert_type: str, current: int) -> dict | None:
    """구간 알림에 맞춰 가상 매매 이벤트 생성"""
    if not SIM_ENABLED:
        return None

    if alert_type == "R1_BUY":
        return _open_sim_buy(entry, current)

    if alert_type == "R2_ADD":
        return _add_sim_buy(entry, current)

    if alert_type == "R0_SELL":
        return _close_sim_position(entry, current, f"R0 익절 ({entry['r0']:,}원)")

    if alert_type == "R3_STOP":
        return _close_sim_position(entry, current, f"R3 손절 ({entry['r3']:,}원)")

    if alert_type == "DAY4_FORCE":
        return _close_sim_position(
            entry, current,
            f"매수 후 {FORCE_SELL_DAY}거래일 강제청산",
        )

    if alert_type == "EXPIRED" and current > 0:
        return _close_sim_position(entry, current, f"추적 {WINDOW_DAYS}거래일 만료")

    return None


def _add_to_watchlist(
    code: str,
    name: str,
    ul_date: str,
    levels: dict,
    ul_trading_value: int,
    reason: str,
) -> dict | None:
    """신규 워치리스트 등록. 이미 있으면 None"""
    if code in _watchlist:
        return None

    entry = {
        "code": code,
        "name": name,
        "ul_date": ul_date,
        "r0": levels["r0"],
        "r1": levels["r1"],
        "r2": levels["r2"],
        "r3": levels["r3"],
        "post_ul_low": levels["post_ul_low"],
        "ul_trading_value": ul_trading_value,
        "first_seen": _today(),
        "alerts_sent": [],
        "reason": reason,
    }
    _watchlist[code] = entry
    return entry


def scan_new_candidates(api_budget: int | None = None) -> tuple[list[dict], int]:
    """
    거래대금 상위 + 당일 상한가 종목에서 신규 후보 탐색.
    Returns: (신규 등록 알림 리스트, API 사용 횟수)
    """
    if not ENABLED or not is_weekday_active():
        return [], 0

    import k1_closing
    import k2_intraday
    import k1_plus
    import k2_plus
    priority_codes = set()
    if k1_closing.is_enabled():
        priority_codes |= k1_closing.get_priority_codes()
    if k1_plus.is_enabled():
        priority_codes |= k1_plus.get_priority_codes()
    if k2_plus.is_enabled():
        priority_codes |= k2_plus.get_priority_codes()
    if k2_intraday.is_enabled():
        priority_codes |= k2_intraday.get_priority_codes()

    budget = api_budget if api_budget is not None else MAX_API_CALLS
    used = 0
    new_alerts: list[dict] = []

    try:
        kospi = kis_api.get_top_trading_value(SCAN_TOP_N, market="0001")
        used += 1
        time.sleep(0.3)
        if used >= budget:
            return new_alerts, used
        kosdaq = kis_api.get_top_trading_value(SCAN_TOP_N, market="1001")
        used += 1
        time.sleep(0.3)
    except Exception as e:
        print(f"[상한가리바운드] 거래대금 조회 실패: {e}")
        return new_alerts, used

    pool: list[dict] = []
    seen: set[str] = set()
    for s in kospi + kosdaq:
        code = s.get("mksc_shrn_iscd", "")
        name = s.get("hts_kor_isnm", "")
        if not code or code in seen or _is_etf(name):
            continue
        seen.add(code)
        pool.append({"code": code, "name": name, "rank_tv": _get_trading_value(s)})

    pool.sort(key=lambda x: x["rank_tv"], reverse=True)

    chart_checks = 0
    for item in pool:
        if used >= budget or chart_checks >= MAX_CHART_CHECKS:
            break

        code = item["code"]
        name = item["name"]
        if code in _watchlist or code in priority_codes:
            continue

        try:
            info = kis_api.get_stock_info(code)
            used += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"[상한가리바운드] {name} 시세 실패: {e}")
            continue

        chart_checks += 1
        today_ul = _is_at_upper_limit(info)
        today_tv = _get_trading_value(info)

        # 일봉 이력 확인 — 신규 상장주(전일 없음)는 상한가 리바운드 제외
        try:
            daily = kis_api.get_daily_chart(code, days=30)
            used += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"[상한가리바운드] {name} 일봉 실패: {e}")
            continue

        sorted_daily = _sort_daily(daily)
        if _is_new_listing(sorted_daily):
            print(f"[상한가리바운드] {name} 신규상장(일봉<{MIN_HISTORY_DAYS}일) — 스킵")
            continue

        if today_ul and today_tv >= MIN_TRADING_VALUE:
            try:
                current = float(info.get("stck_prpr", 0))
                ul_high = float(info.get("stck_mxpr", 0)) or float(
                    info.get("stck_hgpr", 0)
                ) or current
            except (ValueError, TypeError):
                continue

            prev = sorted_daily[-2]
            try:
                prev_close = float(prev.get("stck_clpr", 0))
            except (ValueError, TypeError):
                prev_close = 0.0
            if prev_close <= 0 or prev_close >= ul_high:
                print(f"[상한가리바운드] {name} 전일종가 불가 — 스킵")
                continue

            range_ = int(ul_high) - int(prev_close)
            levels = {
                "r0": int(ul_high),
                "r1": int(ul_high - range_ / 3),
                "r2": int(ul_high - 2 * range_ / 3),
                "r3": int(prev_close),
                "post_ul_low": int(prev_close),
                "range": range_,
            }
            if _levels_collapsed(levels):
                print(f"[상한가리바운드] {name} R선 무효 — 스킵")
                continue

            entry = _add_to_watchlist(
                code, name, _today(), levels, today_tv,
                f"당일 상한가 (거래대금 {today_tv // 100_000_000:,}억)",
            )
            if entry:
                _mark_sent(entry, "NEW_UL")
                new_alerts.append({
                    "type": "NEW_UL",
                    "entry": entry,
                    "current": int(current),
                    "message": "당일 상한가 포착 — 눌림 후 R1 구간 모니터링 시작",
                })
            continue

        ul_candle, after = _find_ul_day(sorted_daily)
        if not ul_candle:
            continue

        levels = compute_levels(ul_candle, after)
        if _levels_collapsed(levels):
            print(f"[상한가리바운드] {name} R선 무효 — 스킵")
            continue
        ul_date = _format_ul_date(ul_candle)
        ul_tv = _get_trading_value(ul_candle)

        if _trading_days_since(ul_date) > WINDOW_DAYS:
            continue

        entry = _add_to_watchlist(
            code, name, ul_date, levels, ul_tv,
            f"상한가 {ul_date} (거래대금 {ul_tv // 100_000_000:,}억)",
        )
        if entry:
            _mark_sent(entry, "NEW_UL")
            new_alerts.append({
                "type": "NEW_UL",
                "entry": entry,
                "current": int(float(info.get("stck_prpr", 0))),
                "message": f"상한가 {ul_date} 포착 — R1 {levels['r1']:,}원 모니터링",
            })

    return new_alerts, used


def _level_hit(current: float, level: int, direction: str) -> bool:
    """direction: 'below' = 이하 터치, 'above' = 이상 터치"""
    if level <= 0:
        return False
    tol = LEVEL_TOLERANCE_PCT / 100
    if direction == "below":
        return current <= level * (1 + tol)
    return current >= level * (1 - tol)


def check_level_alerts(api_budget: int | None = None) -> tuple[list[dict], list[str], int]:
    """
    워치리스트 종목의 가격 vs R0~R3 비교 + 가상 매매 시뮬레이션.
    Returns: (알림 리스트, 제거된 종목코드, API 사용 횟수)
    """
    if not ENABLED or not _watchlist or not is_weekday_active():
        return [], [], 0

    import k1_closing
    import k2_intraday
    import k1_plus
    import k2_plus
    priority_codes = set()
    if k1_closing.is_enabled():
        priority_codes |= k1_closing.get_priority_codes()
    if k1_plus.is_enabled():
        priority_codes |= k1_plus.get_priority_codes()
    if k2_plus.is_enabled():
        priority_codes |= k2_plus.get_priority_codes()
    if k2_intraday.is_enabled():
        priority_codes |= k2_intraday.get_priority_codes()

    budget = api_budget if api_budget is not None else MAX_API_CALLS
    used = 0
    alerts: list[dict] = []
    removed: list[str] = []

    # 이미 등록된 신규상장/R선 붕괴 종목 정리 (레메디 등)
    for code, entry in list(_watchlist.items()):
        levels = {
            "r0": entry.get("r0", 0),
            "r1": entry.get("r1", 0),
            "r2": entry.get("r2", 0),
            "r3": entry.get("r3", 0),
        }
        if _levels_collapsed(levels):
            print(
                f"[상한가리바운드] {entry.get('name', code)} "
                f"R선 무효(신규상장 등) — 추적 제거"
            )
            removed.append(code)
            _watchlist.pop(code, None)

    for code in list(_watchlist.keys()):
        if used >= budget:
            break

        entry = _watchlist[code]
        if code in priority_codes:
            continue

        ul_date = entry.get("ul_date", "")
        ul_days = _trading_days_since(ul_date)

        try:
            current = kis_api.get_current_price(code)
            used += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"[상한가리바운드] {entry['name']} 현재가 실패: {e}")
            continue

        current_f = float(current)
        current_i = int(current_f)
        r0, r1, r2, r3 = entry["r0"], entry["r1"], entry["r2"], entry["r3"]

        # ── 가상 포지션 보유 중: 청산 우선 체크 ─────────────────────────────
        if _sim_is_open(entry):
            buy_date = entry["sim"]["buy_date"]
            buy_day_num = _trading_days_since(buy_date) + 1  # 매수일=1일차

            if buy_day_num >= FORCE_SELL_DAY and not _already_sent(entry, "DAY4_FORCE"):
                _mark_sent(entry, "DAY4_FORCE")
                alert = {
                    "type": "DAY4_FORCE",
                    "entry": entry,
                    "current": current_i,
                    "message": f"매수 {buy_day_num}일차 — {FORCE_SELL_DAY}일차 강제청산",
                }
                sim = _apply_simulation(entry, "DAY4_FORCE", current_i)
                if sim:
                    alert["sim"] = sim
                alerts.append(alert)
                continue

            if _level_hit(current_f, r3, "below") and not _already_sent(entry, "R3_STOP"):
                _mark_sent(entry, "R3_STOP")
                alert = {
                    "type": "R3_STOP",
                    "entry": entry,
                    "current": current_i,
                    "message": f"R3 손절선({r3:,}원) 터치",
                }
                sim = _apply_simulation(entry, "R3_STOP", current_i)
                if sim:
                    alert["sim"] = sim
                alerts.append(alert)
                continue

            if _level_hit(current_f, r0, "above") and not _already_sent(entry, "R0_SELL"):
                _mark_sent(entry, "R0_SELL")
                alert = {
                    "type": "R0_SELL",
                    "entry": entry,
                    "current": current_i,
                    "message": f"R0 익절 구간({r0:,}원) 도달",
                }
                sim = _apply_simulation(entry, "R0_SELL", current_i)
                if sim:
                    alert["sim"] = sim
                alerts.append(alert)
                continue

            if (
                not entry["sim"].get("r2_added")
                and _level_hit(current_f, r2, "below")
                and not _already_sent(entry, "R2_ADD")
            ):
                _mark_sent(entry, "R2_ADD")
                alert = {
                    "type": "R2_ADD",
                    "entry": entry,
                    "current": current_i,
                    "message": f"R2 추가매수 구간({r2:,}원) 터치",
                }
                sim = _apply_simulation(entry, "R2_ADD", current_i)
                if sim:
                    alert["sim"] = sim
                alerts.append(alert)

        # ── 가상 포지션 없음: R1 매수 시뮬레이션 ───────────────────────────
        elif (
            _level_hit(current_f, r1, "below")
            and not _already_sent(entry, "R1_BUY")
        ):
            _mark_sent(entry, "R1_BUY")
            alert = {
                "type": "R1_BUY",
                "entry": entry,
                "current": current_i,
                "message": f"R1 매수 구간({r1:,}원) 터치",
            }
            sim = _apply_simulation(entry, "R1_BUY", current_i)
            if sim:
                alert["sim"] = sim
            alerts.append(alert)

        # ── 추적 만료 ───────────────────────────────────────────────────────
        if ul_days > WINDOW_DAYS:
            if _sim_is_open(entry):
                alert = {
                    "type": "EXPIRED",
                    "entry": entry,
                    "current": current_i,
                    "message": f"상한가 후 {WINDOW_DAYS}거래일 초과 — 추적 종료",
                }
                sim = _apply_simulation(entry, "EXPIRED", current_i)
                if sim:
                    alert["sim"] = sim
                if not _already_sent(entry, "EXPIRED"):
                    _mark_sent(entry, "EXPIRED")
                    alerts.append(alert)
            elif not _already_sent(entry, "EXPIRED"):
                _mark_sent(entry, "EXPIRED")
                alerts.append({
                    "type": "EXPIRED",
                    "entry": entry,
                    "current": 0,
                    "message": f"상한가 후 {WINDOW_DAYS}거래일 초과 — 추적 종료",
                })
            removed.append(code)

    for code in removed:
        _watchlist.pop(code, None)

    return alerts, removed, used


def format_watchlist_summary() -> list[str]:
    """장마감 보고용 요약 라인"""
    if not _watchlist and not _sim_trades_today:
        return []

    lines: list[str] = []
    open_sims = [e for e in _watchlist.values() if _sim_is_open(e)]

    if _sim_trades_today:
        wins = [t for t in _sim_trades_today if t["profit_won"] > 0]
        loses = [t for t in _sim_trades_today if t["profit_won"] <= 0]
        net = sum(t["profit_won"] for t in _sim_trades_today)
        net_sign = "+" if net >= 0 else ""
        lines.append(
            f"🟣 <b>상한가 리바운드 [시뮬] 오늘 체결 {len(_sim_trades_today)}건</b> "
            f"(익 {len(wins)} / 손 {len(loses)}) → {net_sign}{net:,}원"
        )
        for t in _sim_trades_today:
            emoji = "📈" if t["profit_won"] >= 0 else "📉"
            sign = "+" if t["profit_won"] >= 0 else ""
            lines.append(
                f"   {emoji} {t['name']}({t['code']}) "
                f"{t['buy_price']:,}→{t['sell_price']:,}원 × {t['quantity']}주 "
                f"{sign}{t['profit_pct']}% ({sign}{t['profit_won']:,}원) "
                f"[{t['sell_reason']}]"
            )

    if open_sims:
        lines.append(f"🟣 [시뮬] 보유 중 {len(open_sims)}개")
        for entry in open_sims[:3]:
            sim = entry["sim"]
            avg = int(_sim_avg_price(sim))
            qty = _sim_total_qty(sim)
            buy_day = _trading_days_since(sim["buy_date"]) + 1
            lines.append(
                f"   {entry['name']}({entry['code']}) "
                f"평단 {avg:,}원 × {qty}주 (매수 {buy_day}일차)"
                + (" +R2" if sim.get("r2_added") else "")
            )

    tracking_only = [
        e for e in _watchlist.values()
        if not _sim_is_open(e) and (not e.get("sim") or e["sim"].get("status") != "closed")
    ]
    if tracking_only:
        lines.append(f"🟣 추적만 {len(tracking_only)}개 (R1 대기)")
        for entry in tracking_only[:3]:
            lines.append(
                f"   {entry['name']}({entry['code']}) UL {entry['ul_date']} | "
                f"R1 {entry['r1']:,}원"
            )

    return lines
