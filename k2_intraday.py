"""
K2 장중 매매 — 시뮬만 (4장 K2매매)

- 피보 0.5선(K2) 훼손 시 가상 매수
- 고점갱신일 = 상한가일 (Day1), 상한가일 포함 4일까지 매수 가능
- 청산 가정: 4일차 강제 / 피보 고점 익절 / 피보 저점 손절
- 실제 주문 없음. 우선순위: K1 > K2 > 상한가 리바운딩
- API: 등록 시 일봉 1회, 장중은 현재가만
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import kis_api
import k1_closing

KST = ZoneInfo("Asia/Seoul")
STRATEGY = "K2시뮬"

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


ENABLED = _env_bool("ENABLE_K2_SIM", False)
MONITOR_START_MIN = _parse_hhmm(os.getenv("K2_MONITOR_START", "09:10"), 9, 10)
MONITOR_END_MIN = _parse_hhmm(os.getenv("K2_MONITOR_END", "14:45"), 14, 45)
MIN_TRADING_VALUE = int(os.getenv("K2_MIN_TRADING_VALUE", "50000000000"))
# 상한가일=Day1 포함 4거래일까지 매수 가능
MAX_DAYS_FROM_UL = int(os.getenv("K2_MAX_DAYS_FROM_HIGH", "4"))
FORCE_SELL_DAY = int(os.getenv("K2_FORCE_SELL_DAY", "4"))
SCAN_TOP_N = int(os.getenv("K2_SCAN_TOP", "20"))
MAX_API_CALLS = int(os.getenv("K2_MAX_API_CALLS", "20"))
MAX_CHART_CHECKS = int(os.getenv("K2_MAX_CHART_CHECKS", "5"))
MAX_WATCH = int(os.getenv("K2_MAX_WATCH", "5"))
LEVEL_TOLERANCE_PCT = float(os.getenv("K2_LEVEL_TOLERANCE", "0.5"))
SIM_AMOUNT = int(os.getenv("K2_SIM_AMOUNT", "500000"))


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
    """상한가 리바운딩보다 우선 (추적·시뮬 보유)"""
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


def _ul_day_number(ul_date: str) -> int:
    """상한가일=Day1"""
    return _trading_days_since(ul_date) + 1


def _get_trading_value(info_or_candle: dict) -> int:
    try:
        return int(info_or_candle.get("acml_tr_pbmn", 0))
    except (ValueError, TypeError):
        return 0


def _is_ul_rate(rate: float) -> bool:
    return rate >= 29.0


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


def _format_ul_date(candle: dict) -> str:
    raw = candle.get("stck_bsop_date", "")
    if len(raw) == 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return _today()


def _k2_breached(current: float, k2: int) -> bool:
    if k2 <= 0:
        return False
    return current <= k2 * (1 + LEVEL_TOLERANCE_PCT / 100)


def _alert_key(alert_type: str) -> str:
    return f"{alert_type}:{_today()}"


def _already_sent(entry: dict, alert_type: str) -> bool:
    return _alert_key(alert_type) in entry.setdefault("alerts_sent", [])


def _mark_sent(entry: dict, alert_type: str) -> None:
    sent = entry.setdefault("alerts_sent", [])
    key = _alert_key(alert_type)
    if key not in sent:
        sent.append(key)


def _sim_qty(price: int) -> int:
    return max(SIM_AMOUNT // price, 1) if price > 0 else 0


def _sim_is_open(entry: dict) -> bool:
    sim = entry.get("sim")
    return bool(sim and sim.get("status") == "open")


def _open_sim(entry: dict, price: int) -> dict | None:
    if _sim_is_open(entry):
        return None
    if entry.get("sim", {}).get("status") == "closed":
        return None
    qty = _sim_qty(price)
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
        "reason": f"[K2시뮬] K2 {entry.get('k2', 0):,}원 훼손 @ {price:,}원",
    }


def _close_sim(entry: dict, price: int, reason: str) -> dict | None:
    if not _sim_is_open(entry):
        return None
    sim = entry["sim"]
    buy_price = sim["buy_price"]
    qty = sim["quantity"]
    if buy_price <= 0 or qty < 1:
        return None
    profit_pct = (price - buy_price) / buy_price * 100
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
        "sell_reason": reason,
        "profit_pct": sim["profit_pct"],
        "profit_won": profit_won,
    }
    _sim_trades_today.append(trade)
    return trade


def scan_new_candidates(api_budget: int | None = None) -> tuple[list[dict], int]:
    """상한가 후보를 워치리스트에 등록 (K2 미이탈도 추적)"""
    if not ENABLED or not is_trading_weekday():
        return [], 0

    budget = api_budget if api_budget is not None else MAX_API_CALLS
    used = 0
    new_alerts: list[dict] = []
    k1_codes = k1_closing.get_priority_codes() if k1_closing.is_enabled() else set()

    try:
        kospi = kis_api.get_top_trading_value(SCAN_TOP_N, market="0001")
        used += 1
        time.sleep(0.3)
        kosdaq = kis_api.get_top_trading_value(SCAN_TOP_N, market="1001")
        used += 1
        time.sleep(0.3)
    except Exception as e:
        print(f"[K2시뮬] 거래대금 조회 실패: {e}")
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
        if len(_watchlist) >= MAX_WATCH:
            break

        code, name = item["code"], item["name"]
        if code in _watchlist or code in k1_codes:
            continue

        checks += 1
        try:
            daily = kis_api.get_daily_chart(code, days=30)
            used += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"[K2시뮬] {name} 일봉 실패: {e}")
            continue

        sorted_c = _sort_daily(daily)
        ul_candle, after, ul_idx = _find_ul_day(sorted_c)
        if not ul_candle:
            continue

        ul_date = _format_ul_date(ul_candle)
        day_num = _ul_day_number(ul_date)
        if day_num < 1 or day_num > MAX_DAYS_FROM_UL:
            continue

        levels = k1_closing.compute_fib_levels(ul_candle, after, ul_idx, sorted_c)
        ul_tv = _get_trading_value(ul_candle)

        entry = {
            "code": code,
            "name": name,
            "ul_date": ul_date,
            "day_num": day_num,
            "k1": levels["k1"],
            "k2": levels["k2"],
            "fib_high": levels["fib_high"],
            "fib_low": levels["fib_low"],
            "ul_trading_value": ul_tv,
            "first_seen": _today(),
            "alerts_sent": [],
            "reason": (
                f"상한가 {ul_date} (D{day_num}/{MAX_DAYS_FROM_UL}) "
                f"K2 {levels['k2']:,}원 추적"
            ),
        }
        _watchlist[code] = entry
        _mark_sent(entry, "NEW")
        new_alerts.append({
            "type": "NEW",
            "entry": entry,
            "current": 0,
            "message": entry["reason"],
            "sim": None,
        })

    return new_alerts, used


def check_alerts(api_budget: int | None = None) -> tuple[list[dict], list[str], int]:
    """현재가만으로 K2 이탈·시뮬 청산 체크"""
    if not ENABLED or not _watchlist or not is_trading_weekday():
        return [], [], 0

    budget = api_budget if api_budget is not None else MAX_API_CALLS
    used = 0
    alerts: list[dict] = []
    removed: list[str] = []
    k1_codes = k1_closing.get_priority_codes() if k1_closing.is_enabled() else set()

    for code in list(_watchlist.keys()):
        if used >= budget:
            break

        entry = _watchlist[code]
        if code in k1_codes:
            # K1 우선 — 추적만 유지하되 시뮬 매수는 하지 않음 (이미 open이면 청산만)
            pass

        ul_date = entry.get("ul_date", "")
        day_num = _ul_day_number(ul_date)
        entry["day_num"] = day_num

        try:
            current = float(kis_api.get_current_price(code))
            used += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"[K2시뮬] {entry['name']} 현재가 실패: {e}")
            continue

        current_i = int(current)
        k2 = int(entry.get("k2", 0))
        fib_high = int(entry.get("fib_high", 0))
        fib_low = int(entry.get("fib_low", 0))

        # ── 보유 중: 가정 청산 ─────────────────────────────────────────────
        if _sim_is_open(entry):
            buy_day = _trading_days_since(entry["sim"]["buy_date"]) + 1

            if buy_day >= FORCE_SELL_DAY and not _already_sent(entry, "DAY4"):
                _mark_sent(entry, "DAY4")
                sim = _close_sim(
                    entry, current_i,
                    f"매수 {buy_day}일차 — {FORCE_SELL_DAY}일차 강제청산 (가정)",
                )
                alerts.append({
                    "type": "DAY4",
                    "entry": entry,
                    "current": current_i,
                    "message": f"매수 {buy_day}일차 강제청산 (가정)",
                    "sim": sim,
                })
                continue

            if fib_high > 0 and current >= fib_high * (1 - LEVEL_TOLERANCE_PCT / 100):
                if not _already_sent(entry, "TP_HIGH"):
                    _mark_sent(entry, "TP_HIGH")
                    sim = _close_sim(
                        entry, current_i,
                        f"피보 고점({fib_high:,}원) 재도달 익절 (가정)",
                    )
                    alerts.append({
                        "type": "TP_HIGH",
                        "entry": entry,
                        "current": current_i,
                        "message": f"피보 고점 {fib_high:,}원 재도달 (가정 익절)",
                        "sim": sim,
                    })
                    continue

            if fib_low > 0 and current <= fib_low * (1 + LEVEL_TOLERANCE_PCT / 100):
                if not _already_sent(entry, "SL_LOW"):
                    _mark_sent(entry, "SL_LOW")
                    sim = _close_sim(
                        entry, current_i,
                        f"피보 저점({fib_low:,}원) 손절 (가정)",
                    )
                    alerts.append({
                        "type": "SL_LOW",
                        "entry": entry,
                        "current": current_i,
                        "message": f"피보 저점 {fib_low:,}원 (가정 손절)",
                        "sim": sim,
                    })
                    continue

        # ── 미보유: K2 훼손 매수 (K1 우선 종목은 스킵) ─────────────────────
        elif (
            code not in k1_codes
            and day_num <= MAX_DAYS_FROM_UL
            and _k2_breached(current, k2)
            and not _already_sent(entry, "K2_BUY")
        ):
            _mark_sent(entry, "K2_BUY")
            sim = _open_sim(entry, current_i)
            alerts.append({
                "type": "K2_BUY",
                "entry": entry,
                "current": current_i,
                "message": f"K2 {k2:,}원 훼손 — 장중 매수 구간 (시뮬)",
                "sim": sim,
            })

        # ── 매수 기한 만료 (미보유) ─────────────────────────────────────────
        if day_num > MAX_DAYS_FROM_UL and not _sim_is_open(entry):
            if not _already_sent(entry, "EXPIRED"):
                _mark_sent(entry, "EXPIRED")
                alerts.append({
                    "type": "EXPIRED",
                    "entry": entry,
                    "current": current_i,
                    "message": (
                        f"상한가일 기준 D{day_num} — "
                        f"매수 기한({MAX_DAYS_FROM_UL}일) 초과"
                    ),
                    "sim": None,
                })
            removed.append(code)
        elif day_num > MAX_DAYS_FROM_UL and _sim_is_open(entry):
            # 보유 중이면 4일차 규칙이 청산 — 만료로 제거하지 않음
            pass

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
            f"🔶 <b>K2 [시뮬] 오늘 체결 {len(_sim_trades_today)}건</b> → {sign}{net:,}원"
        )
        for t in _sim_trades_today:
            em = "📈" if t["profit_won"] >= 0 else "📉"
            s = "+" if t["profit_won"] >= 0 else ""
            lines.append(
                f"   {em} {t['name']}({t['code']}) "
                f"{t['buy_price']:,}→{t['sell_price']:,}원 {s}{t['profit_pct']}%"
            )

    open_sims = [e for e in _watchlist.values() if _sim_is_open(e)]
    if open_sims:
        lines.append(f"🔶 K2 [시뮬] 보유 {len(open_sims)}개")
        for e in open_sims[:3]:
            sim = e["sim"]
            day = _trading_days_since(sim["buy_date"]) + 1
            lines.append(
                f"   {e['name']}({e['code']}) {sim['buy_price']:,}원 × "
                f"{sim['quantity']}주 (매수 D{day})"
            )

    waiting = [
        e for e in _watchlist.values()
        if not _sim_is_open(e) and e.get("sim", {}).get("status") != "closed"
    ]
    if waiting:
        lines.append(f"🔶 K2 추적 {len(waiting)}개 (K2 이탈 대기, 상한가일=D1)")
        for e in waiting[:3]:
            lines.append(
                f"   {e['name']}({e['code']}) UL {e['ul_date']} D{e.get('day_num', '?')} "
                f"| K2 {e['k2']:,}원"
            )
    return lines
