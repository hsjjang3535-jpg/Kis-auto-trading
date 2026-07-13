"""
K2플러스 — 시뮬만 (6장)

- 세력봉(거래대금 500억+) 기준, 상한가는 K1 실전에 양보
- 피보: 0.236=K1, 0.5=K2 — K2 훼손 시 장중 가상 매수
- 고점갱신일=세력봉일(D1) 포함 4일까지 매수
- 청산 가정: 매수 4일차 / 고점 익절 / 저점 손절
- 실제 주문 없음
- 우선순위: K1실전 > K1플러스 > K2플러스 > K2 > 리바운딩
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import kis_api
import k1_closing

KST = ZoneInfo("Asia/Seoul")
STRATEGY = "K2플러스시뮬"

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


ENABLED = _env_bool("ENABLE_K2_PLUS_SIM", False)
MONITOR_START_MIN = _parse_hhmm(os.getenv("K2_PLUS_MONITOR_START", "09:10"), 9, 10)
MONITOR_END_MIN = _parse_hhmm(os.getenv("K2_PLUS_MONITOR_END", "14:45"), 14, 45)
MIN_TRADING_VALUE = int(os.getenv("K2_PLUS_MIN_TRADING_VALUE", "50000000000"))
MIN_DAY_RATE = float(os.getenv("K2_PLUS_MIN_DAY_RATE", "5.0"))
MAX_DAYS_FROM_POWER = int(os.getenv("K2_PLUS_MAX_DAYS_FROM_HIGH", "4"))
FORCE_SELL_DAY = int(os.getenv("K2_PLUS_FORCE_SELL_DAY", "4"))
SCAN_TOP_N = int(os.getenv("K2_PLUS_SCAN_TOP", "20"))
MAX_API_CALLS = int(os.getenv("K2_PLUS_MAX_API_CALLS", "20"))
MAX_CHART_CHECKS = int(os.getenv("K2_PLUS_MAX_CHART_CHECKS", "5"))
MAX_WATCH = int(os.getenv("K2_PLUS_MAX_WATCH", "5"))
LEVEL_TOLERANCE_PCT = float(os.getenv("K2_PLUS_LEVEL_TOLERANCE", "0.5"))
SIM_AMOUNT = int(os.getenv("K2_PLUS_SIM_AMOUNT", "500000"))
REQUIRE_NEW_HIGH = _env_bool("K2_PLUS_REQUIRE_NEW_HIGH", True)


def is_enabled() -> bool:
    return ENABLED


def is_trading_weekday() -> bool:
    return datetime.now(KST).weekday() < 5


def is_monitor_window() -> bool:
    if not ENABLED or not is_trading_weekday():
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


def get_priority_codes() -> set[str]:
    return set(_watchlist.keys())


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _is_etf(name: str) -> bool:
    upper = name.upper()
    return any(kw.upper() in upper for kw in _ETF_KEYWORDS)


def _trading_days_since(start_date: str) -> int:
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


def _power_day_number(power_date: str) -> int:
    """세력봉일=D1"""
    try:
        start = datetime.strptime(power_date, "%Y-%m-%d").date()
    except ValueError:
        return 0
    today = datetime.now(KST).date()
    if today < start:
        return 0
    days = 0
    d = start
    while d <= today:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return days


def _get_trading_value(d: dict) -> int:
    try:
        return int(d.get("acml_tr_pbmn", 0))
    except (ValueError, TypeError):
        return 0


def _is_ul_like(info: dict) -> bool:
    sign = str(info.get("prdy_vrss_sign", ""))
    if sign == "1":
        return True
    try:
        current = float(info.get("stck_prpr", 0))
        upper = float(info.get("stck_mxpr", 0))
        if upper > 0 and current >= upper * 0.998:
            return True
        return float(info.get("prdy_ctrt", 0)) >= 29.0
    except (ValueError, TypeError):
        return False


def _sort_daily(candles: list[dict]) -> list[dict]:
    return sorted(candles, key=lambda c: c.get("stck_bsop_date", ""))


def _is_new_high(sorted_daily: list[dict], lookback: int = 20) -> bool:
    if len(sorted_daily) < 2:
        return False
    try:
        today_high = float(sorted_daily[-1].get("stck_hgpr", 0))
    except (ValueError, TypeError):
        return False
    if today_high <= 0:
        return False
    prev = sorted_daily[-(lookback + 1):-1] if len(sorted_daily) > 1 else []
    highs = []
    for c in prev:
        try:
            highs.append(float(c.get("stck_hgpr", 0)))
        except (ValueError, TypeError):
            continue
    if not highs:
        return True
    return today_high >= max(highs)


def _fib_from_power_day(sorted_daily: list[dict]) -> dict | None:
    if not sorted_daily:
        return None
    today = sorted_daily[-1]
    try:
        fib_high = int(float(today.get("stck_hgpr", 0)))
        today_low = float(today.get("stck_lwpr", 0))
    except (ValueError, TypeError):
        return None
    prev_low = today_low
    if len(sorted_daily) >= 2:
        try:
            prev_low = float(sorted_daily[-2].get("stck_lwpr", today_low))
        except (ValueError, TypeError):
            pass
    fib_low = int(min(x for x in (today_low, prev_low) if x > 0) or max(fib_high - 1, 1))
    if fib_low >= fib_high:
        fib_low = max(fib_high - max(int(fib_high * 0.05), 1), 1)
    range_ = fib_high - fib_low
    if range_ <= 0:
        return None
    return {
        "fib_high": fib_high,
        "fib_low": fib_low,
        "k1": int(fib_high - 0.236 * range_),
        "k2": int(fib_high - 0.5 * range_),
        "range": range_,
    }


def _k2_breached(current: float, k2: int) -> bool:
    if k2 <= 0:
        return False
    return current <= k2 * (1 + LEVEL_TOLERANCE_PCT / 100)


def _higher_priority_codes() -> set[str]:
    codes = set()
    if k1_closing.is_enabled():
        codes |= k1_closing.get_priority_codes()
    try:
        import k1_plus
        if k1_plus.is_enabled():
            codes |= k1_plus.get_priority_codes()
    except Exception:
        pass
    return codes


def _alert_key(t: str) -> str:
    return f"{t}:{_today()}"


def _already_sent(entry: dict, t: str) -> bool:
    return _alert_key(t) in entry.setdefault("alerts_sent", [])


def _mark_sent(entry: dict, t: str) -> None:
    sent = entry.setdefault("alerts_sent", [])
    key = _alert_key(t)
    if key not in sent:
        sent.append(key)


def _sim_is_open(entry: dict) -> bool:
    sim = entry.get("sim")
    return bool(sim and sim.get("status") == "open")


def _open_sim(entry: dict, price: int) -> dict | None:
    if _sim_is_open(entry) or entry.get("sim", {}).get("status") == "closed":
        return None
    qty = max(SIM_AMOUNT // price, 1) if price > 0 else 0
    if qty < 1:
        return None
    entry["sim"] = {
        "status": "open",
        "buy_date": _today(),
        "quantity": qty,
        "buy_price": price,
    }
    return {
        "action": "buy",
        "name": entry["name"],
        "code": entry["code"],
        "quantity": qty,
        "price": price,
        "k2": entry.get("k2", 0),
        "reason": f"[K2+시뮬] K2 {entry.get('k2', 0):,}원 훼손 @ {price:,}원",
    }


def _close_sim(entry: dict, price: int, reason: str) -> dict | None:
    if not _sim_is_open(entry):
        return None
    sim = entry["sim"]
    buy_price = sim["buy_price"]
    qty = sim["quantity"]
    profit_pct = (price - buy_price) / buy_price * 100 if buy_price else 0
    profit_won = int((price - buy_price) * qty)
    sim.update({
        "status": "closed",
        "sell_price": price,
        "sell_date": _today(),
        "sell_reason": reason,
        "profit_pct": round(profit_pct, 2),
        "profit_won": profit_won,
    })
    trade = {
        "action": "sell",
        "name": entry["name"],
        "code": entry["code"],
        "strategy": STRATEGY,
        "buy_price": buy_price,
        "sell_price": price,
        "quantity": qty,
        "buy_date": sim["buy_date"],
        "sell_date": _today(),
        "sell_reason": reason,
        "profit_pct": sim["profit_pct"],
        "profit_won": profit_won,
    }
    _sim_trades_today.append(trade)
    return trade


def scan_new_candidates(api_budget: int | None = None) -> tuple[list[dict], int]:
    """당일 세력봉(비상한가) 후보 등록 — K1/K1+ 우선 종목 제외"""
    if not ENABLED or not is_trading_weekday():
        return [], 0

    budget = api_budget if api_budget is not None else MAX_API_CALLS
    used = 0
    alerts: list[dict] = []
    skip_codes = _higher_priority_codes()

    try:
        kospi = kis_api.get_top_trading_value(SCAN_TOP_N, market="0001")
        used += 1
        time.sleep(0.3)
        kosdaq = kis_api.get_top_trading_value(SCAN_TOP_N, market="1001")
        used += 1
        time.sleep(0.3)
    except Exception as e:
        print(f"[K2+시뮬] 거래대금 조회 실패: {e}")
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
            rate = float(s.get("prdy_ctrt", 0))
        except (ValueError, TypeError):
            rate = 0.0
        tv = _get_trading_value(s)
        if tv < MIN_TRADING_VALUE or rate < MIN_DAY_RATE:
            continue
        pool.append({"code": code, "name": name, "rate": rate, "tv": tv})

    pool.sort(key=lambda x: x["tv"], reverse=True)
    checks = 0
    for item in pool:
        if used >= budget or checks >= MAX_CHART_CHECKS or len(_watchlist) >= MAX_WATCH:
            break
        code, name = item["code"], item["name"]
        if code in _watchlist or code in skip_codes:
            continue

        checks += 1
        try:
            info = kis_api.get_stock_info(code)
            used += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"[K2+시뮬] {name} 시세 실패: {e}")
            continue

        if _is_ul_like(info):
            continue
        if _get_trading_value(info) < MIN_TRADING_VALUE:
            continue

        try:
            daily = kis_api.get_daily_chart(code, days=30)
            used += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"[K2+시뮬] {name} 일봉 실패: {e}")
            continue

        sorted_c = _sort_daily(daily)
        if REQUIRE_NEW_HIGH and not _is_new_high(sorted_c):
            continue

        levels = _fib_from_power_day(sorted_c)
        if not levels:
            continue

        entry = {
            "code": code,
            "name": name,
            "power_date": _today(),
            "day_num": 1,
            "k1": levels["k1"],
            "k2": levels["k2"],
            "fib_high": levels["fib_high"],
            "fib_low": levels["fib_low"],
            "day_rate": float(info.get("prdy_ctrt", item["rate"])),
            "trading_value": _get_trading_value(info),
            "first_seen": _today(),
            "alerts_sent": [],
            "reason": (
                f"세력봉 D1 +{float(info.get('prdy_ctrt', 0)):.1f}% "
                f"대금 {_get_trading_value(info) // 100_000_000:,}억 "
                f"K2 {levels['k2']:,}"
            ),
        }
        _watchlist[code] = entry
        _mark_sent(entry, "NEW")
        alerts.append({
            "type": "NEW",
            "entry": entry,
            "current": int(float(info.get("stck_prpr", 0))),
            "message": entry["reason"],
            "sim": None,
        })

    return alerts, used


def check_alerts(api_budget: int | None = None) -> tuple[list[dict], list[str], int]:
    """장중 K2 훼손 매수 + 보유 청산"""
    if not ENABLED or not _watchlist or not is_trading_weekday():
        return [], [], 0

    budget = api_budget if api_budget is not None else MAX_API_CALLS
    used = 0
    alerts: list[dict] = []
    removed: list[str] = []
    skip_codes = _higher_priority_codes()

    for code in list(_watchlist.keys()):
        if used >= budget:
            break
        entry = _watchlist[code]
        power_date = entry.get("power_date", "")
        day_num = _power_day_number(power_date)
        entry["day_num"] = day_num

        try:
            info = kis_api.get_stock_info(code)
            used += 1
            time.sleep(0.3)
            current = float(info.get("stck_prpr", 0))
        except Exception as e:
            print(f"[K2+시뮬] {entry['name']} 시세 실패: {e}")
            continue

        current_i = int(current)
        k2 = int(entry.get("k2", 0))
        fib_high = int(entry.get("fib_high", 0))
        fib_low = int(entry.get("fib_low", 0))

        if _sim_is_open(entry):
            buy_day = _trading_days_since(entry["sim"]["buy_date"]) + 1
            if buy_day >= FORCE_SELL_DAY and not _already_sent(entry, "DAY4"):
                _mark_sent(entry, "DAY4")
                sim = _close_sim(
                    entry, current_i,
                    f"매수 {buy_day}일차 — {FORCE_SELL_DAY}일차 올매도",
                )
                alerts.append({
                    "type": "DAY4",
                    "entry": entry,
                    "current": current_i,
                    "message": f"매수 {buy_day}일차 올매도",
                    "sim": sim,
                })
                continue
            if fib_high > 0 and current >= fib_high * (1 - LEVEL_TOLERANCE_PCT / 100):
                if not _already_sent(entry, "TP"):
                    _mark_sent(entry, "TP")
                    sim = _close_sim(entry, current_i, f"고점({fib_high:,}) 재도달 익절 (가정)")
                    alerts.append({
                        "type": "TP", "entry": entry, "current": current_i,
                        "message": "피보 고점 재도달 (가정)", "sim": sim,
                    })
                    continue
            if fib_low > 0 and current <= fib_low * (1 + LEVEL_TOLERANCE_PCT / 100):
                if not _already_sent(entry, "SL"):
                    _mark_sent(entry, "SL")
                    sim = _close_sim(entry, current_i, f"저점({fib_low:,}) 손절 (가정)")
                    alerts.append({
                        "type": "SL", "entry": entry, "current": current_i,
                        "message": "피보 저점 (가정 손절)", "sim": sim,
                    })
                    continue
            continue

        # 미보유: K1/K1+ 우선 종목은 매수 스킵
        if code in skip_codes:
            continue

        if day_num < 1 or day_num > MAX_DAYS_FROM_POWER:
            if not _already_sent(entry, "EXPIRED"):
                _mark_sent(entry, "EXPIRED")
                alerts.append({
                    "type": "EXPIRED",
                    "entry": entry,
                    "current": current_i,
                    "message": f"세력봉 후 {MAX_DAYS_FROM_POWER}일 초과 — 미매수 추적 종료",
                    "sim": None,
                })
            removed.append(code)
            continue

        if _is_ul_like(info):
            removed.append(code)
            continue

        if _k2_breached(current, k2) and not _already_sent(entry, "BUY"):
            _mark_sent(entry, "BUY")
            sim = _open_sim(entry, current_i)
            alerts.append({
                "type": "BUY",
                "entry": entry,
                "current": current_i,
                "message": f"K2 {k2:,} 훼손 (D{day_num}) → 장중매수 (시뮬)",
                "sim": sim,
            })

    for code in removed:
        _watchlist.pop(code, None)
    return alerts, removed, used


def format_summary() -> list[str]:
    if not _watchlist and not _sim_trades_today:
        return []
    lines: list[str] = []
    if _sim_trades_today:
        net = sum(t["profit_won"] for t in _sim_trades_today)
        lines.append(
            f"🔷 <b>K2플러스 [시뮬] 오늘 체결 {len(_sim_trades_today)}건</b> "
            f"순손익 {_format_won(net)}"
        )
        for t in _sim_trades_today:
            em = "📈" if t["profit_won"] >= 0 else "📉"
            s = "+" if t["profit_won"] >= 0 else ""
            lines.append(
                f"  {em} {t['name']}: {s}{t['profit_pct']}% "
                f"({s}{t['profit_won']:,}원) — {t.get('sell_reason', '')}"
            )
    open_sims = [e for e in _watchlist.values() if _sim_is_open(e)]
    tracking = [e for e in _watchlist.values() if not _sim_is_open(e)]
    if open_sims:
        lines.append(f"🔷 K2+ [시뮬] 보유 {len(open_sims)}개 (4일차 청산)")
        for e in open_sims:
            day = _trading_days_since(e["sim"]["buy_date"]) + 1
            lines.append(
                f"  · {e['name']} D{day} @ {e['sim']['buy_price']:,} "
                f"K2 {e.get('k2', 0):,}"
            )
    if tracking:
        lines.append(f"🔷 K2+ 추적만 {len(tracking)}개 (K2 대기)")
        for e in tracking[:5]:
            lines.append(
                f"  · {e['name']} D{e.get('day_num', '?')} "
                f"K2 {e.get('k2', 0):,}"
            )
    return lines


def _format_won(amount: int) -> str:
    sign = "+" if amount > 0 else ""
    return f"{sign}{amount:,}원"
