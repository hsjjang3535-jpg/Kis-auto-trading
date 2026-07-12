"""
K1플러스 — 시뮬만 (5장)

- 세력봉(거래대금 500억+) 당일, 상한가 케이스는 K1 실전에 양보
- 5분/일봉 피보: 0.236=K1, 0.5=K2
- K1 훼손 + 당일 양봉 → 종가대 가상 매수
- 매수 4일차 전량 가상 청산
- 실제 주문 없음. 우선순위: K1실전 > K1플러스 > K2 > 리바운딩
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import kis_api
import k1_closing

KST = ZoneInfo("Asia/Seoul")
STRATEGY = "K1플러스시뮬"

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


ENABLED = _env_bool("ENABLE_K1_PLUS_SIM", False)
ENTRY_START_MIN = _parse_hhmm(os.getenv("K1_PLUS_ENTRY_START", "14:20"), 14, 20)
ENTRY_END_MIN = _parse_hhmm(os.getenv("K1_PLUS_ENTRY_END", "14:50"), 14, 50)
MONITOR_START_MIN = _parse_hhmm(os.getenv("K1_PLUS_MONITOR_START", "09:10"), 9, 10)
MONITOR_END_MIN = _parse_hhmm(os.getenv("K1_PLUS_MONITOR_END", "14:50"), 14, 50)
MIN_TRADING_VALUE = int(os.getenv("K1_PLUS_MIN_TRADING_VALUE", "50000000000"))
MIN_DAY_RATE = float(os.getenv("K1_PLUS_MIN_DAY_RATE", "5.0"))  # 세력봉 근사: 당일 +5%↑
FORCE_SELL_DAY = int(os.getenv("K1_PLUS_FORCE_SELL_DAY", "4"))
SCAN_TOP_N = int(os.getenv("K1_PLUS_SCAN_TOP", "20"))
MAX_API_CALLS = int(os.getenv("K1_PLUS_MAX_API_CALLS", "20"))
MAX_CHART_CHECKS = int(os.getenv("K1_PLUS_MAX_CHART_CHECKS", "5"))
MAX_WATCH = int(os.getenv("K1_PLUS_MAX_WATCH", "5"))
LEVEL_TOLERANCE_PCT = float(os.getenv("K1_PLUS_LEVEL_TOLERANCE", "0.5"))
SIM_AMOUNT = int(os.getenv("K1_PLUS_SIM_AMOUNT", "500000"))
REQUIRE_NEW_HIGH = _env_bool("K1_PLUS_REQUIRE_NEW_HIGH", True)


def is_enabled() -> bool:
    return ENABLED


def is_trading_weekday() -> bool:
    return datetime.now(KST).weekday() < 5


def is_entry_window() -> bool:
    if not ENABLED or not is_trading_weekday():
        return False
    t = datetime.now(KST).hour * 60 + datetime.now(KST).minute
    return ENTRY_START_MIN <= t <= ENTRY_END_MIN


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


def _get_trading_value(d: dict) -> int:
    try:
        return int(d.get("acml_tr_pbmn", 0))
    except (ValueError, TypeError):
        return 0


def _is_ul_like(info: dict) -> bool:
    if str(info.get("prdy_vrss_sign", "")) == "1":
        return True
    try:
        rate = float(info.get("prdy_ctrt", 0))
        if rate >= 29.0:
            return True
        current = float(info.get("stck_prpr", 0))
        upper = float(info.get("stck_mxpr", 0))
        return upper > 0 and current >= upper * 0.998
    except (ValueError, TypeError):
        return False


def _sort_daily(candles: list[dict]) -> list[dict]:
    return sorted(candles, key=lambda c: c.get("stck_bsop_date", ""))


def _format_date(raw: str) -> str:
    if len(raw) == 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return _today()


def _is_new_high(sorted_daily: list[dict], lookback: int = 20) -> bool:
    """신고가 근사: 오늘 고가 >= 최근 lookback일 고가"""
    if len(sorted_daily) < 2:
        return True
    try:
        today_high = float(sorted_daily[-1].get("stck_hgpr", 0))
        prev_highs = [
            float(c.get("stck_hgpr", 0))
            for c in sorted_daily[-(lookback + 1):-1]
        ]
        if not prev_highs or today_high <= 0:
            return True
        return today_high >= max(prev_highs) * 0.995
    except (ValueError, TypeError):
        return True


def _fib_from_power_day(sorted_daily: list[dict]) -> dict | None:
    """당일 세력봉 기준: 전일 저~당일 고 (또는 당일 저~고)"""
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


def _k1_breached(current: float, k1: int) -> bool:
    if k1 <= 0:
        return False
    return current <= k1 * (1 + LEVEL_TOLERANCE_PCT / 100)


def _is_bullish(info_or_candle: dict, current: float | None = None) -> bool:
    try:
        open_p = float(info_or_candle.get("stck_oprc", 0))
        close_p = float(
            current if current is not None
            else info_or_candle.get("stck_clpr", info_or_candle.get("stck_prpr", 0))
        )
        return close_p >= open_p and open_p > 0
    except (ValueError, TypeError):
        return False


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
        "k1": entry.get("k1", 0),
        "reason": f"[K1+시뮬] 세력봉 당일 양봉종배 K1 {entry.get('k1', 0):,} 훼손",
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
    """당일 세력봉(비상한가) 후보 등록"""
    if not ENABLED or not is_trading_weekday():
        return [], 0

    budget = api_budget if api_budget is not None else MAX_API_CALLS
    used = 0
    alerts: list[dict] = []
    k1_codes = k1_closing.get_priority_codes() if k1_closing.is_enabled() else set()

    try:
        kospi = kis_api.get_top_trading_value(SCAN_TOP_N, market="0001")
        used += 1
        time.sleep(0.3)
        kosdaq = kis_api.get_top_trading_value(SCAN_TOP_N, market="1001")
        used += 1
        time.sleep(0.3)
    except Exception as e:
        print(f"[K1+시뮬] 거래대금 조회 실패: {e}")
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
        if code in _watchlist or code in k1_codes:
            continue

        checks += 1
        try:
            info = kis_api.get_stock_info(code)
            used += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"[K1+시뮬] {name} 시세 실패: {e}")
            continue

        # 상한가 → K1 실전 영역, 플러스 스킵
        if _is_ul_like(info):
            continue
        if _get_trading_value(info) < MIN_TRADING_VALUE:
            continue

        try:
            daily = kis_api.get_daily_chart(code, days=30)
            used += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"[K1+시뮬] {name} 일봉 실패: {e}")
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
            "k1": levels["k1"],
            "k2": levels["k2"],
            "fib_high": levels["fib_high"],
            "fib_low": levels["fib_low"],
            "day_rate": float(info.get("prdy_ctrt", item["rate"])),
            "trading_value": _get_trading_value(info),
            "first_seen": _today(),
            "alerts_sent": [],
            "reason": (
                f"세력봉 당일 +{float(info.get('prdy_ctrt', 0)):.1f}% "
                f"대금 {_get_trading_value(info) // 100_000_000:,}억 "
                f"K1 {levels['k1']:,}"
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
    """종가대 매수 + 보유 청산 (현재가/시세만)"""
    if not ENABLED or not _watchlist or not is_trading_weekday():
        return [], [], 0

    budget = api_budget if api_budget is not None else MAX_API_CALLS
    used = 0
    alerts: list[dict] = []
    removed: list[str] = []
    k1_codes = k1_closing.get_priority_codes() if k1_closing.is_enabled() else set()
    in_entry = is_entry_window()

    for code in list(_watchlist.keys()):
        if used >= budget:
            break
        entry = _watchlist[code]

        try:
            info = kis_api.get_stock_info(code)
            used += 1
            time.sleep(0.3)
            current = float(info.get("stck_prpr", 0))
        except Exception as e:
            print(f"[K1+시뮬] {entry['name']} 시세 실패: {e}")
            continue

        current_i = int(current)
        k1 = int(entry.get("k1", 0))
        fib_high = int(entry.get("fib_high", 0))
        fib_low = int(entry.get("fib_low", 0))

        # ── 보유: 가정 청산 ────────────────────────────────────────────────
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

        # ── 미보유: 당일만, K1 실전 종목 스킵 ───────────────────────────────
        if code in k1_codes:
            continue
        if entry.get("power_date") != _today():
            if not _already_sent(entry, "EXPIRED"):
                _mark_sent(entry, "EXPIRED")
                alerts.append({
                    "type": "EXPIRED",
                    "entry": entry,
                    "current": current_i,
                    "message": "세력봉 당일 종료 — 미매수 추적 종료",
                    "sim": None,
                })
            removed.append(code)
            continue

        if _is_ul_like(info):
            removed.append(code)
            continue

        if (
            in_entry
            and _k1_breached(current, k1)
            and _is_bullish(info, current)
            and not _already_sent(entry, "BUY")
        ):
            _mark_sent(entry, "BUY")
            sim = _open_sim(entry, current_i)
            alerts.append({
                "type": "BUY",
                "entry": entry,
                "current": current_i,
                "message": f"K1 {k1:,} 훼손 + 당일 양봉 → 종가베팅 (시뮬)",
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
        sign = "+" if net >= 0 else ""
        lines.append(
            f"💠 <b>K1플러스 [시뮬] 오늘 {len(_sim_trades_today)}건</b> → {sign}{net:,}원"
        )
        for t in _sim_trades_today:
            em = "📈" if t["profit_won"] >= 0 else "📉"
            s = "+" if t["profit_won"] >= 0 else ""
            lines.append(
                f"   {em} {t['name']}({t['code']}) "
                f"{t['buy_price']:,}→{t['sell_price']:,} {s}{t['profit_pct']}%"
            )
    open_sims = [e for e in _watchlist.values() if _sim_is_open(e)]
    if open_sims:
        lines.append(f"💠 K1+ [시뮬] 보유 {len(open_sims)}개 (4일차 청산)")
        for e in open_sims[:3]:
            sim = e["sim"]
            day = _trading_days_since(sim["buy_date"]) + 1
            lines.append(
                f"   {e['name']}({e['code']}) {sim['buy_price']:,}×{sim['quantity']} (D{day})"
            )
    waiting = [
        e for e in _watchlist.values()
        if not _sim_is_open(e) and e.get("power_date") == _today()
    ]
    if waiting:
        lines.append(f"💠 K1+ 추적 {len(waiting)}개 (세력봉 당일·종가대)")
        for e in waiting[:3]:
            lines.append(f"   {e['name']}({e['code']}) K1 {e['k1']:,}")
    return lines
