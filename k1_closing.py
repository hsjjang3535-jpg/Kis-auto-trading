"""
K1 종가베팅 (3장 K1매매)

- 금·월: 실전 종가 매수 (기존 금요일 AI 종가 대체)
- 피보 K1(0.236) 이탈 + 국민1음봉/양봉종배/2음봉종배
- 상한가 후 2거래일까지만, 매수 4일차 전량 매도
- 상한가 리바운딩보다 동일 종목 우선
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import kis_api

KST = ZoneInfo("Asia/Seoul")
STRATEGY = "K1종가"

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


ENABLED = _env_bool("ENABLE_K1_CLOSING", False)
ENTRY_START_MIN = _parse_hhmm(os.getenv("K1_CLOSING_ENTRY_START", "14:20"), 14, 20)
ENTRY_END_MIN = _parse_hhmm(os.getenv("K1_CLOSING_ENTRY_END", "14:50"), 14, 50)
MIN_TRADING_VALUE = int(os.getenv("K1_MIN_TRADING_VALUE", "50000000000"))
MAX_BUY_DAYS_AFTER_UL = int(os.getenv("K1_MAX_BUY_DAYS_AFTER_UL", "2"))
FORCE_SELL_DAY = int(os.getenv("K1_FORCE_SELL_DAY", "4"))
SCAN_TOP_N = int(os.getenv("K1_SCAN_TOP", "30"))
MAX_API_CALLS = int(os.getenv("K1_MAX_API_CALLS", "25"))
MAX_CHART_CHECKS = int(os.getenv("K1_MAX_CHART_CHECKS", "8"))
LEVEL_TOLERANCE_PCT = float(os.getenv("K1_LEVEL_TOLERANCE", "0.5"))
SIM_ENABLED = _env_bool("K1_SIM_ENABLED", True)
SIM_AMOUNT = int(os.getenv("K1_SIM_AMOUNT", "500000"))


def is_enabled() -> bool:
    return ENABLED


def _weekday() -> int:
    return datetime.now(KST).weekday()


def is_k1_closing_day() -> bool:
    """금(4)·월(0) — K1 실전 종가"""
    return _weekday() in (0, 4)


def is_closing_entry_window() -> bool:
    if not ENABLED or not is_k1_closing_day():
        return False
    t = datetime.now(KST).hour * 60 + datetime.now(KST).minute
    return ENTRY_START_MIN <= t <= ENTRY_END_MIN


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
    """상한가 리바운딩보다 우선할 종목"""
    codes = set(_watchlist.keys())
    for entry in _watchlist.values():
        sim = entry.get("sim")
        if sim and sim.get("status") == "open":
            codes.add(entry["code"])
    return codes


def has_priority(code: str) -> bool:
    return code in get_priority_codes()


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


def _days_after_ul(ul_date: str) -> int:
    return _trading_days_since(ul_date)


def _is_ul_rate(rate: float) -> bool:
    return rate >= 29.0


def _get_trading_value(info_or_candle: dict) -> int:
    try:
        return int(info_or_candle.get("acml_tr_pbmn", 0))
    except (ValueError, TypeError):
        return 0


def _sort_daily(candles: list[dict]) -> list[dict]:
    return sorted(candles, key=lambda c: c.get("stck_bsop_date", ""))


def _find_ul_day(candles: list[dict]) -> tuple[dict | None, list[dict], int]:
    sorted_c = _sort_daily(candles)
    if not sorted_c:
        return None, [], -1

    today_str = datetime.now(KST).strftime("%Y%m%d")
    cutoff = (datetime.now(KST) - timedelta(days=20)).strftime("%Y%m%d")
    recent = [c for c in sorted_c if cutoff <= c.get("stck_bsop_date", "") <= today_str]
    if not recent:
        return None, [], -1

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
                pass

        is_ul = _is_ul_rate(rate)
        if not is_ul and prev_close > 0:
            try:
                high = float(candle.get("stck_hgpr", 0))
                is_ul = high >= prev_close * 1.29
            except (ValueError, TypeError):
                pass

        if is_ul and _get_trading_value(candle) >= MIN_TRADING_VALUE:
            return candle, recent[i + 1:], i

    return None, [], -1


def compute_fib_levels(ul_candle: dict, candles_after_ul: list[dict], ul_index: int,
                         all_candles: list[dict]) -> dict:
    """피보 0.236(K1), 0.5(K2) — 상한가 고점 ~ 상한가 이전 저점"""
    try:
        fib_high = int(float(ul_candle.get("stck_hgpr", 0)))
    except (ValueError, TypeError):
        fib_high = 0

    lows: list[float] = []
    if ul_index > 0:
        try:
            lows.append(float(all_candles[ul_index - 1].get("stck_lwpr", 0)))
        except (ValueError, TypeError):
            pass
    try:
        lows.append(float(ul_candle.get("stck_lwpr", 0)))
    except (ValueError, TypeError):
        pass
    for c in candles_after_ul:
        try:
            lows.append(float(c.get("stck_lwpr", 0)))
        except (ValueError, TypeError):
            continue

    valid_lows = [l for l in lows if l > 0]
    fib_low = int(min(valid_lows)) if valid_lows else max(fib_high - int(fib_high * 0.05), 1)

    if fib_low >= fib_high:
        fib_low = max(fib_high - int(fib_high * 0.05), 1)

    range_ = fib_high - fib_low
    if range_ <= 0:
        range_ = max(int(fib_high * 0.05), 1)
        fib_low = fib_high - range_

    k1 = int(fib_high - 0.236 * range_)
    k2 = int(fib_high - 0.5 * range_)

    return {
        "fib_high": fib_high,
        "fib_low": fib_low,
        "k1": k1,
        "k2": k2,
        "range": range_,
    }


def _format_ul_date(candle: dict) -> str:
    raw = candle.get("stck_bsop_date", "")
    if len(raw) == 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return _today()


def _level_breached(current: float, level: int) -> bool:
    if level <= 0:
        return False
    tol = LEVEL_TOLERANCE_PCT / 100
    return current <= level * (1 + tol)


def _classify_pattern(daily: dict, current: float, k1: int, days_after: int) -> str | None:
    if not _level_breached(current, k1):
        return None
    try:
        open_p = float(daily.get("stck_oprc", 0))
        close_p = float(daily.get("stck_clpr", current))
    except (ValueError, TypeError):
        return None

    is_bear = close_p < open_p
    is_bull = close_p >= open_p

    if days_after == 1:
        if is_bear:
            return "국민1음봉"
        if is_bull:
            return "양봉종배"
    elif days_after == 2 and is_bear:
        return "2음봉종배"
    return None


def _is_long_n_shape(candles_after_ul: list[dict]) -> bool:
    """긴 N자 상한가 근사: 상한가 후 고점-저점-재상승 반복"""
    if len(candles_after_ul) < 3:
        return False
    try:
        highs = [float(c.get("stck_hgpr", 0)) for c in candles_after_ul[:5]]
        lows = [float(c.get("stck_lwpr", 0)) for c in candles_after_ul[:5]]
    except (ValueError, TypeError):
        return False
    if len(highs) < 3:
        return False
    return highs[0] > highs[1] < highs[2] and (max(highs) - min(lows)) / max(highs) > 0.15


def evaluate_stock(code: str, name: str, api_budget: int) -> tuple[dict | None, int]:
    """단일 종목 K1 적합도 평가. (entry dict, api_used)"""
    used = 0
    if api_budget <= 0:
        return None, 0

    try:
        daily = kis_api.get_daily_chart(code, days=30)
        used += 1
        time.sleep(0.3)
    except Exception as e:
        print(f"[K1종가] {name} 일봉 실패: {e}")
        return None, used

    sorted_c = _sort_daily(daily)
    ul_candle, after, ul_idx = _find_ul_day(sorted_c)
    if not ul_candle:
        return None, used

    ul_date = _format_ul_date(ul_candle)
    days_after = _days_after_ul(ul_date)
    if days_after < 1 or days_after > MAX_BUY_DAYS_AFTER_UL:
        return None, used

    if _is_long_n_shape(after):
        return None, used

    levels = compute_fib_levels(ul_candle, after, ul_idx, sorted_c)
    k1, k2 = levels["k1"], levels["k2"]

    if api_budget - used <= 0:
        return None, used
    try:
        info = kis_api.get_stock_info(code)
        used += 1
        time.sleep(0.3)
    except Exception as e:
        print(f"[K1종가] {name} 시세 실패: {e}")
        return None, used

    current = float(info.get("stck_prpr", 0))
    today_candle = sorted_c[-1] if sorted_c else {}
    pattern = _classify_pattern(today_candle, current, k1, days_after)
    if not pattern:
        return None, used

    ul_tv = _get_trading_value(ul_candle)
    return {
        "code": code,
        "name": name,
        "ul_date": ul_date,
        "days_after_ul": days_after,
        "pattern": pattern,
        "k1": k1,
        "k2": k2,
        "fib_high": levels["fib_high"],
        "fib_low": levels["fib_low"],
        "current": int(current),
        "change_rate": float(info.get("prdy_ctrt", 0)),
        "ul_trading_value": ul_tv,
        "reason": (
            f"K1 {pattern} — 상한가 {ul_date} D+{days_after} "
            f"K1 {k1:,}원 이탈 (거래대금 {ul_tv // 100_000_000:,}억)"
        ),
        "priority_score": days_after * 10 + (k2 - int(current)) / max(k2, 1) * 100,
    }, used


def scan_closing_candidates(api_budget: int | None = None) -> tuple[list[dict], int]:
    if not ENABLED or not is_k1_closing_day():
        return [], 0

    budget = api_budget if api_budget is not None else MAX_API_CALLS
    used = 0
    candidates: list[dict] = []

    try:
        kospi = kis_api.get_top_trading_value(SCAN_TOP_N, market="0001")
        used += 1
        time.sleep(0.3)
        kosdaq = kis_api.get_top_trading_value(SCAN_TOP_N, market="1001")
        used += 1
        time.sleep(0.3)
    except Exception as e:
        print(f"[K1종가] 거래대금 조회 실패: {e}")
        return [], used

    pool: list[dict] = []
    seen: set[str] = set()
    for s in kospi + kosdaq:
        code = s.get("mksc_shrn_iscd", "")
        name = s.get("hts_kor_isnm", "")
        if not code or code in seen or _is_etf(name):
            continue
        seen.add(code)
        pool.append({"code": code, "name": name})

    checks = 0
    for item in pool:
        if used >= budget or checks >= MAX_CHART_CHECKS:
            break
        checks += 1
        entry, u = evaluate_stock(item["code"], item["name"], budget - used)
        used += u
        if entry:
            candidates.append(entry)
            _watchlist[item["code"]] = {
                **entry,
                "first_seen": _today(),
                "alerts_sent": [],
            }

    candidates.sort(key=lambda x: x.get("priority_score", 0), reverse=True)
    return candidates, used


def _sim_qty(price: int) -> int:
    return max(SIM_AMOUNT // price, 1) if price > 0 else 0


def record_sim_buy(entry: dict, price: int) -> dict | None:
    if not SIM_ENABLED:
        return None
    code = entry["code"]
    wl = _watchlist.setdefault(code, {**entry, "alerts_sent": []})
    if wl.get("sim", {}).get("status") == "open":
        return None

    qty = _sim_qty(price)
    wl["sim"] = {
        "status": "open",
        "buy_date": _today(),
        "quantity": qty,
        "buy_price": price,
        "pattern": entry.get("pattern", ""),
    }
    return {
        "action": "buy",
        "name": entry["name"],
        "code": code,
        "quantity": qty,
        "price": price,
        "pattern": entry.get("pattern", ""),
        "reason": entry.get("reason", ""),
    }


def record_sim_sell(entry: dict, price: int, reason: str) -> dict | None:
    sim = entry.get("sim")
    if not sim or sim.get("status") != "open":
        return None

    buy_price = sim["buy_price"]
    qty = sim["quantity"]
    profit_pct = (price - buy_price) / buy_price * 100 if buy_price else 0
    profit_won = int((price - buy_price) * qty)

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
        "buy_price": buy_price,
        "sell_price": price,
        "quantity": qty,
        "buy_date": sim["buy_date"],
        "sell_date": _today(),
        "buy_reason": sim.get("pattern", ""),
        "sell_reason": reason,
        "profit_pct": sim["profit_pct"],
        "profit_won": profit_won,
        "pattern": sim.get("pattern", ""),
    }
    _sim_trades_today.append(trade)
    return trade


def check_open_sim_day4(current_prices: dict[str, float]) -> list[dict]:
    """보유 K1 시뮬 4일차 청산"""
    sells: list[dict] = []
    for code, entry in list(_watchlist.items()):
        sim = entry.get("sim")
        if not sim or sim.get("status") != "open":
            continue
        buy_day = _trading_days_since(sim["buy_date"]) + 1
        if buy_day < FORCE_SELL_DAY:
            continue
        price = int(current_prices.get(code, sim.get("buy_price", 0)))
        if price <= 0:
            continue
        result = record_sim_sell(entry, price, f"매수 {buy_day}일차 — {FORCE_SELL_DAY}일차 강제청산")
        if result:
            sells.append(result)
    return sells


def format_summary() -> list[str]:
    lines: list[str] = []
    if _sim_trades_today:
        net = sum(t["profit_won"] for t in _sim_trades_today)
        sign = "+" if net >= 0 else ""
        lines.append(
            f"🔷 <b>K1 종가 [시뮬] 오늘 {len(_sim_trades_today)}건</b> → {sign}{net:,}원"
        )
        for t in _sim_trades_today:
            em = "📈" if t["profit_won"] >= 0 else "📉"
            s = "+" if t["profit_won"] >= 0 else ""
            lines.append(
                f"   {em} {t['name']}({t['code']}) [{t.get('pattern', '')}] "
                f"{t['buy_price']:,}→{t['sell_price']:,}원 {s}{t['profit_pct']}%"
            )

    open_sims = [e for e in _watchlist.values() if e.get("sim", {}).get("status") == "open"]
    if open_sims:
        lines.append(f"🔷 K1 [시뮬] 보유 {len(open_sims)}개 (4일차 청산)")
        for e in open_sims[:3]:
            sim = e["sim"]
            day = _trading_days_since(sim["buy_date"]) + 1
            lines.append(
                f"   {e['name']}({e['code']}) [{sim.get('pattern', '')}] "
                f"{sim['buy_price']:,}원 × {sim['quantity']}주 (D{day})"
            )
    return lines


def evaluate_force_sell_day(pos: dict) -> tuple[bool, str]:
    """실전 K1 포지션 4일차 청산 여부"""
    buy_date = pos.get("buy_date", "")
    buy_day = _trading_days_since(buy_date) + 1
    if buy_day >= FORCE_SELL_DAY:
        return True, f"K1 {FORCE_SELL_DAY}일차 강제청산 (매수 D{buy_day})"
    return False, ""
