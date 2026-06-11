import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from typing import List, Dict

from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

import config
from kis_client import KISClient
from strategy import analyze_minute_data, analyze_daily_data, should_buy, should_buy_oversold, should_sell
import telegram_bot

# ==================== 설정 ====================
API_SLEEP = 1.0  # API 호출 간 최소 대기 (초)

ETF_KEYWORDS = ("KODEX", "TIGER", "RISE", "SOL", "PLUS", "HANARO", "KB", "KBI",
                "미래에셋", "삼성인버스", "N2", "ARIRANG", "FOCUS", "HANA", "KOSEF", "TREX",
                "KINDEX", "KBSTAR")

app = FastAPI()
client = KISClient()
scheduler = BackgroundScheduler(timezone=pytz.timezone(config.TIMEZONE))

STATE_FILE = "/tmp/trading_state.json"


def api_sleep():
    """API 호출 사이에 1초 대기"""
    time.sleep(API_SLEEP)


def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: Dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_market_open(now: datetime = None) -> bool:
    if now is None:
        now = datetime.now(pytz.timezone(config.TIMEZONE))
    weekday = now.weekday()
    if weekday >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 900 <= hm <= 1530


def is_us_market_open(now: datetime = None) -> bool:
    if not config.US_ENABLED:
        return False
    if now is None:
        now = datetime.now(pytz.timezone(config.TIMEZONE))
    weekday = now.weekday()
    hm = now.hour * 100 + now.minute
    if weekday == 0:
        return hm >= 2130
    if 1 <= weekday <= 4:
        return hm >= 2130 or hm <= 400
    if weekday == 5:
        return hm <= 400
    return False


def _init_day_records(state: Dict, today_str: str, prefix: str = "") -> Dict:
    key_buy = f"{prefix}buy_records_{today_str}"
    key_sell = f"{prefix}sell_records_{today_str}"
    key_skip = f"{prefix}skip_reasons_{today_str}"
    key_scan = f"{prefix}hot_stocks_{today_str}"
    key_buy_done = f"{prefix}buy_done_{today_str}"

    if key_buy not in state:
        state[key_buy] = []
    if key_sell not in state:
        state[key_sell] = []
    if key_skip not in state:
        state[key_skip] = {}
    if key_buy_done not in state:
        state[key_buy_done] = {}
    state[f"{prefix}last_run_date"] = today_str
    return state


# ==================== 사이클 A: 잔고 + 매도 모니터링 (5분 주기) ====================

def run_monitor_cycle():
    """잔고 확인 + 보유종목 매도 판정만 (API 호출 최소화)"""
    now = datetime.now(pytz.timezone(config.TIMEZONE))
    if not is_market_open(now):
        return

    state = load_state()
    today_str = now.strftime("%Y%m%d")
    state = _init_day_records(state, today_str, prefix="kr_")

    try:
        api_sleep()
        balance = client.get_balance()
        cash = balance["cash"]
        holdings = {h["stock_code"]: h for h in balance["holdings"]}

        print(f"[모니터] {now.strftime('%H:%M')} | 예수금: {cash:,}원 | 보유: {len(holdings)}개")

        # 보유종목 있으면 매도 판정
        for stock_code, position in holdings.items():
            try:
                api_sleep()
                price_info = client.get_current_price(stock_code)
                current_price = price_info["current_price"]

                api_sleep()
                minute_data = client.get_minute_candles(stock_code, period="5")
                minute_analysis = analyze_minute_data(minute_data)

                stock_strategy = state.get("stock_strategy", {}).get(stock_code, "regular")
                if stock_strategy == "oversold":
                    sell_flag, sell_qty, sell_reason = should_sell(
                        position, price_info, minute_analysis,
                        stop_loss_pct=config.OVERSOLD_STOP_LOSS_PCT,
                        take_profit_pct=config.OVERSOLD_TAKE_PROFIT_PCT
                    )
                else:
                    sell_flag, sell_qty, sell_reason = should_sell(position, price_info, minute_analysis)

                if sell_flag:
                    stock_name = price_info.get("stock_name", stock_code)
                    print(f"  -> 매도! {stock_name} {sell_reason}")
                    resp = client.order_sell(stock_code, sell_qty)
                    print(f"  -> 주문 응답: {resp}")
                    state[f"kr_sell_records_{today_str}"].append({
                        "name": stock_name, "code": stock_code,
                        "price": current_price, "quantity": sell_qty,
                        "profit_pct": round(position.get("profit_loss_rate", 0), 2),
                        "reason": sell_reason, "time": now.strftime("%H:%M"),
                    })
                    state["buy_done_today"].pop(stock_code, None)
            except Exception as e:
                print(f"  -> 매도판정 오류 [{stock_code}]: {e}")

        save_state(state)

    except Exception as e:
        print(f"모니터 사이클 오류: {e}")


# ==================== 사이클 B: 종목 분석 + 매수 (10분 주기) ====================

def run_analysis_cycle():
    """종목 분석 + 매수 판정 (잔고조회 없이 상태파일 기반)"""
    now = datetime.now(pytz.timezone(config.TIMEZONE))
    if not is_market_open(now):
        return

    print(f"\n=== [분석 사이클] {now.strftime('%H:%M')} ===")
    state = load_state()
    today_str = now.strftime("%Y%m%d")

    if state.get("last_run_date") != today_str:
        state["buy_done_today"] = {}

    state = _init_day_records(state, today_str, prefix="kr_")

    try:
        # 1. 감시 종목 결정 (핫스캔 스킵, WATCHLIST 사용)
        scan_key = f"kr_hot_stocks_{today_str}"
        if not state.get(scan_key):
            watchlist = []
            for c in config.WATCHLIST:
                c = c.strip()
                if c:
                    # 종목명 조회 (1회 API 호출)
                    api_sleep()
                    try:
                        price_info = client.get_current_price(c)
                        watchlist.append({
                            "code": c,
                            "name": price_info.get("stock_name", c),
                        })
                    except Exception as e:
                        print(f"  종목명 조회 실패 [{c}]: {e}")
                        watchlist.append({"code": c, "name": c})
            state[scan_key] = watchlist
            print(f"감시 종목: {len(watchlist)}개")

        watchlist = state[scan_key]

        # 2. 잔고는 상태파일에서 (모니터 사이클이 업데이트)
        # 대신 여기서 가볍게만 확인
        api_sleep()
        balance = client.get_balance()
        cash = balance["cash"]
        holdings = {h["stock_code"]: h for h in balance["holdings"]}

        # 3. 각 종목 분석
        for stock_info in watchlist:
            stock_code = stock_info["code"].strip()
            stock_name = stock_info.get("name", stock_code)
            if not stock_code:
                continue

            # 이미 보유중이면 스킵 (매도는 모니터 사이클에서)
            if stock_code in holdings:
                continue

            # 오늘 이미 매수했으면 스킵
            if state["buy_done_today"].get(stock_code, False):
                continue

            try:
                # 현재가
                api_sleep()
                price_info = client.get_current_price(stock_code)
                stock_name = price_info.get("stock_name", stock_name)
                current_price = price_info["current_price"]

                if current_price <= 0:
                    state[f"kr_skip_reasons_{today_str}"][stock_code] = {
                        "name": stock_name, "reason": "가격 0", "time": now.strftime("%H:%M"),
                    }
                    continue

                # 5분봉
                api_sleep()
                minute_data = client.get_minute_candles(stock_code, period="5")
                minute_analysis = analyze_minute_data(minute_data)

                # 일봉
                api_sleep()
                daily_data = client.get_daily_candles(stock_code, count=60)
                daily_analysis = analyze_daily_data(daily_data)

                print(f"  [{stock_name}] {current_price:,}원 | "
                      f"MA5: {minute_analysis.get('ma5')} | "
                      f"vol_spike: {minute_analysis.get('volume_spike')} | "
                      f"일양봉: {daily_analysis.get('is_positive')} | "
                      f"RSI: {daily_analysis.get('rsi')}")

                # 매수 판정
                buy_flag, buy_qty, buy_reason = should_buy(
                    price_info, minute_analysis, daily_analysis, config.MAX_BUDGET_PER_STOCK
                )
                strategy = "regular"

                if not buy_flag:
                    buy_flag, buy_qty, buy_reason = should_buy_oversold(
                        price_info, minute_analysis, daily_analysis, config.MAX_BUDGET_PER_STOCK
                    )
                    strategy = "oversold"

                if buy_flag:
                    print(f"    -> 매수! [{strategy}] {buy_reason} | {buy_qty}주")
                    resp = client.order_buy(stock_code, buy_qty)
                    print(f"    -> 주문 응답: {resp}")
                    state[f"kr_buy_records_{today_str}"].append({
                        "name": stock_name, "code": stock_code,
                        "price": current_price, "quantity": buy_qty,
                        "reason": buy_reason, "strategy": strategy,
                        "time": now.strftime("%H:%M"),
                    })
                    state["buy_done_today"][stock_code] = True
                    if "stock_strategy" not in state:
                        state["stock_strategy"] = {}
                    state["stock_strategy"][stock_code] = strategy
                else:
                    state[f"kr_skip_reasons_{today_str}"][stock_code] = {
                        "name": stock_name, "reason": buy_reason, "time": now.strftime("%H:%M"),
                    }

            except Exception as e:
                print(f"    -> 오류 [{stock_code}]: {e}")
                state[f"kr_skip_reasons_{today_str}"][stock_code] = {
                    "name": stock_name, "reason": f"오류: {e}", "time": now.strftime("%H:%M"),
                }
                continue

        save_state(state)
        print(f"=== [분석 사이클 완료] ===\n")

    except Exception as e:
        print(f"분석 사이클 오류: {e}")


# ==================== 미국장 ====================

def run_us_trading_cycle():
    """미국장 트레이딩 사이클 (잔고+분석 분리)"""
    now = datetime.now(pytz.timezone(config.TIMEZONE))
    if not is_us_market_open(now):
        return

    print(f"\n=== [US 트레이딩 사이클] {now.strftime('%H:%M')} ===")
    state = load_state()
    today_str = now.strftime("%Y%m%d")
    state = _init_day_records(state, today_str, prefix="us_")

    try:
        # 잔고 조회
        api_sleep()
        us_balance = client.get_us_balance(exchange=config.US_EXCHANGE)
        us_cash = us_balance["cash"]
        us_holdings = {h["stock_code"]: h for h in us_balance["holdings"]}
        print(f"US 예수액: ${us_cash:,.2f} | 보유: {len(us_holdings)}개")

        # 스캔 (첫 1회만)
        scan_key = f"us_hot_stocks_{today_str}"
        if not state.get(scan_key):
            us_stocks = []
            for code in config.US_WATCHLIST:
                code = code.strip()
                if not code:
                    continue
                api_sleep()
                try:
                    price = client.get_us_price(code, exchange=config.US_EXCHANGE)
                    us_stocks.append({
                        "code": code,
                        "name": price.get("stock_name", code),
                        "price": price["current_price"],
                        "change_rate": price.get("change_rate", 0),
                        "value": price.get("trading_value", 0),
                    })
                except Exception as e:
                    print(f"  US 스캔 오류 [{code}]: {e}")
                    us_stocks.append({"code": code, "name": code})

            if not us_stocks:
                us_stocks = [{"code": c.strip(), "name": c.strip()} for c in config.US_WATCHLIST if c.strip()]
            state[scan_key] = us_stocks
            print(f"US 감시 종목: {len(us_stocks)}개")

        us_watchlist = state.get(scan_key, [])

        for stock_info in us_watchlist:
            stock_code = stock_info["code"].strip()
            stock_name = stock_info.get("name", stock_code)
            if not stock_code:
                continue

            try:
                # 보유 중이면 매도 판정
                if stock_code in us_holdings:
                    position = us_holdings[stock_code]

                    api_sleep()
                    price_info = client.get_us_price(stock_code, exchange=config.US_EXCHANGE)
                    current_price = price_info["current_price"]

                    api_sleep()
                    minute_data = client.get_us_minute(stock_code, exchange=config.US_EXCHANGE, period="5")
                    minute_analysis = analyze_minute_data(minute_data)

                    stock_strategy = state.get("stock_strategy", {}).get(stock_code, "regular")
                    if stock_strategy == "oversold":
                        sell_flag, sell_qty, sell_reason = should_sell(
                            position, price_info, minute_analysis,
                            stop_loss_pct=config.OVERSOLD_STOP_LOSS_PCT,
                            take_profit_pct=config.OVERSOLD_TAKE_PROFIT_PCT
                        )
                    else:
                        sell_flag, sell_qty, sell_reason = should_sell(
                            position, price_info, minute_analysis,
                            stop_loss_pct=config.US_STOP_LOSS_PCT,
                            take_profit_pct=config.US_TAKE_PROFIT_PCT
                        )

                    if sell_flag:
                        print(f"  -> US 매도! {stock_name} {sell_reason}")
                        resp = client.order_us_sell(stock_code, sell_qty, exchange=config.US_EXCHANGE)
                        print(f"  -> {resp}")
                        state[f"us_sell_records_{today_str}"].append({
                            "name": stock_name, "code": stock_code,
                            "price": current_price, "quantity": sell_qty,
                            "profit_pct": round(position.get("profit_loss_rate", 0), 2),
                            "reason": sell_reason, "time": now.strftime("%H:%M"),
                        })
                        state[f"us_buy_done_{today_str}"].pop(stock_code, None)
                    continue

                # 미보유 → 매수 판정
                already_bought = state.get(f"us_buy_done_{today_str}", {}).get(stock_code, False)
                if already_bought:
                    continue

                api_sleep()
                price_info = client.get_us_price(stock_code, exchange=config.US_EXCHANGE)
                stock_name = price_info.get("stock_name", stock_name)
                current_price = price_info["current_price"]

                api_sleep()
                minute_data = client.get_us_minute(stock_code, exchange=config.US_EXCHANGE, period="5")
                minute_analysis = analyze_minute_data(minute_data)

                api_sleep()
                daily_data = client.get_us_daily(stock_code, exchange=config.US_EXCHANGE, count=60)
                daily_analysis = analyze_daily_data(daily_data)

                print(f"  [US {stock_name}] ${current_price:.2f} | "
                      f"MA5: {minute_analysis.get('ma5')} | "
                      f"vol_spike: {minute_analysis.get('volume_spike')}")

                buy_flag, buy_qty, buy_reason = should_buy(
                    price_info, minute_analysis, daily_analysis, config.US_MAX_BUDGET_PER_STOCK
                )
                strategy = "regular"

                if not buy_flag:
                    buy_flag, buy_qty, buy_reason = should_buy_oversold(
                        price_info, minute_analysis, daily_analysis, config.US_MAX_BUDGET_PER_STOCK
                    )
                    strategy = "oversold"

                if buy_flag:
                    print(f"  -> US 매수! [{strategy}] {buy_reason} | {buy_qty}주")
                    resp = client.order_us_buy(stock_code, buy_qty, exchange=config.US_EXCHANGE)
                    print(f"  -> {resp}")
                    state[f"us_buy_records_{today_str}"].append({
                        "name": stock_name, "code": stock_code,
                        "price": current_price, "quantity": buy_qty,
                        "reason": buy_reason, "strategy": strategy,
                        "time": now.strftime("%H:%M"),
                    })
                    if f"us_buy_done_{today_str}" not in state:
                        state[f"us_buy_done_{today_str}"] = {}
                    state[f"us_buy_done_{today_str}"][stock_code] = True
                    if "stock_strategy" not in state:
                        state["stock_strategy"] = {}
                    state["stock_strategy"][stock_code] = strategy
                else:
                    state[f"us_skip_reasons_{today_str}"][stock_code] = {
                        "name": stock_name, "reason": buy_reason, "time": now.strftime("%H:%M"),
                    }

            except Exception as e:
                print(f"  -> US 오류 [{stock_code}]: {e}")
                state[f"us_skip_reasons_{today_str}"][stock_code] = {
                    "name": stock_name, "reason": f"오류: {e}", "time": now.strftime("%H:%M"),
                }
                continue

        save_state(state)
        print(f"=== [US 사이클 완료] ===\n")

    except Exception as e:
        print(f"US 주요 오류: {e}")


# ==================== 장 마감 보고 ====================

def send_kr_end_of_day_report():
    """국내장 마감 보고 (15:35 실행)"""
    now = datetime.now(pytz.timezone(config.TIMEZONE))
    today_str = now.strftime("%Y%m%d")
    state = load_state()

    buys = state.get(f"kr_buy_records_{today_str}", [])
    sells = state.get(f"kr_sell_records_{today_str}", [])
    skips = state.get(f"kr_skip_reasons_{today_str}", {})

    lines = [f"📊 *[국내장] 일일 보고* ({now.strftime('%Y-%m-%d')})"]
    lines.append("")

    if sells:
        lines.append(f"🔴 *매도 ({len(sells)}건)*")
        for s in sells:
            emoji = "📈" if s["profit_pct"] >= 0 else "📉"
            lines.append(f"  {emoji} {s['name']}({s['code']})")
            lines.append(f"    {s['price']:,}원 × {s['quantity']}주 | {s['profit_pct']:+.2f}%")
            lines.append(f"    사유: {s['reason']} ({s['time']})")
        lines.append("")

    if buys:
        lines.append(f"🟢 *매수 ({len(buys)}건)*")
        for b in buys:
            lines.append(f"  ▷ {b['name']}({b['code']})")
            lines.append(f"    {b['price']:,}원 × {b['quantity']}주")
            lines.append(f"    사유: {b['reason']} ({b['time']})")
        lines.append("")

    if skips:
        lines.append(f"⏭ *미매수 종목 ({len(skips)}개)*")
        for code, info in skips.items():
            lines.append(f"  · {info['name']}({code}): {info['reason']}")
        lines.append("")

    if not buys and not sells:
        lines.append("📝 오늘 매수/매도 없음")
    else:
        lines.append(f"총 매수 {len(buys)}건 | 매도 {len(sells)}건 | 스킵 {len(skips)}개")

    try:
        api_sleep()
        balance = client.get_balance()
        cash = balance["cash"]
        holdings = balance["holdings"]
        total_eval = sum(h.get("eval_amount", 0) for h in holdings)
        lines.append("")
        lines.append(f"💰 예수금: {cash:,}원 | 평가: {total_eval:,}원")
        if holdings:
            lines.append("*보유:*")
            for h in holdings:
                name = h.get("stock_name", "")
                code = h.get("stock_code", "")
                qty = h.get("quantity", 0)
                profit = h.get("profit_loss_rate", 0)
                emoji = "📈" if profit >= 0 else "📉"
                lines.append(f"  {emoji} {name}({code}) {qty}주 {profit:+.2f}%")
    except Exception as e:
        lines.append(f"잔고 조회 실패: {e}")

    telegram_bot.send_info("\n".join(lines))
    print(f"[국내장 마감 보고 전송 완료]")


def send_us_end_of_day_report():
    """미국장 마감 보고 (04:05 KST 실행)"""
    now = datetime.now(pytz.timezone(config.TIMEZONE))
    yesterday = now - timedelta(days=1) if now.hour <= 4 else now
    today_str = yesterday.strftime("%Y%m%d")
    state = load_state()

    buys = state.get(f"us_buy_records_{today_str}", [])
    sells = state.get(f"us_sell_records_{today_str}", [])
    skips = state.get(f"us_skip_reasons_{today_str}", {})

    lines = [f"📊 *[미국장] 일일 보고* ({yesterday.strftime('%Y-%m-%d')})"]
    lines.append("")

    if sells:
        lines.append(f"🔴 *매도 ({len(sells)}건)*")
        for s in sells:
            emoji = "📈" if s["profit_pct"] >= 0 else "📉"
            lines.append(f"  {emoji} {s['name']}({s['code']})")
            lines.append(f"    ${s['price']:.2f} × {s['quantity']}주 | {s['profit_pct']:+.2f}%")
            lines.append(f"    사유: {s['reason']} ({s['time']})")
        lines.append("")

    if buys:
        lines.append(f"🟢 *매수 ({len(buys)}건)*")
        for b in buys:
            lines.append(f"  ▷ {b['name']}({b['code']})")
            lines.append(f"    ${b['price']:.2f} × {b['quantity']}주")
            lines.append(f"    사유: {b['reason']} ({b['time']})")
        lines.append("")

    if skips:
        lines.append(f"⏭ *미매수 종목 ({len(skips)}개)*")
        for code, info in skips.items():
            lines.append(f"  · {info['name']}({code}): {info['reason']}")
        lines.append("")

    if not buys and not sells:
        lines.append("📝 오늘 매수/매도 없음")
    else:
        lines.append(f"총 매수 {len(buys)}건 | 매도 {len(sells)}건 | 스킵 {len(skips)}개")

    try:
        api_sleep()
        us_balance = client.get_us_balance(exchange=config.US_EXCHANGE)
        us_cash = us_balance["cash"]
        us_holdings = us_balance["holdings"]
        total_eval = sum(h.get("eval_amount", 0) for h in us_holdings)
        lines.append("")
        lines.append(f"💰 예수금: ${us_cash:,.2f} | 평가: ${total_eval:,.2f}")
        if us_holdings:
            lines.append("*보유:*")
            for h in us_holdings:
                name = h.get("stock_name", "")
                code = h.get("stock_code", "")
                qty = h.get("quantity", 0)
                profit = h.get("profit_loss_rate", 0)
                emoji = "📈" if profit >= 0 else "📉"
                lines.append(f"  {emoji} {name}({code}) {qty}주 {profit:+.2f}%")
    except Exception as e:
        lines.append(f"잔고 조회 실패: {e}")

    telegram_bot.send_info("\n".join(lines))
    print(f"[미국장 마감 보고 전송 완료]")


# ==================== 스케줄러 ====================

# 국내장: 잔고+매도 모니터링 (5분 주기)
scheduler.add_job(
    run_monitor_cycle,
    trigger=CronTrigger(minute="*/5", hour="9-15", day_of_week="mon-fri", timezone=config.TIMEZONE),
    id="monitor_cycle",
    replace_existing=True,
)

# 국내장: 종목 분석+매수 (10분 주기, 정각에만)
scheduler.add_job(
    run_analysis_cycle,
    trigger=CronTrigger(minute="0,10,20,30,40,50", hour="9-15", day_of_week="mon-fri", timezone=config.TIMEZONE),
    id="analysis_cycle",
    replace_existing=True,
)

# 국내장 마감 보고: 평일 15:35
scheduler.add_job(
    send_kr_end_of_day_report,
    trigger=CronTrigger(minute="35", hour="15", day_of_week="mon-fri", timezone=config.TIMEZONE),
    id="kr_eod_report",
    replace_existing=True,
)

# 미국장: 5분 주기 (잔고+매도+분석 통합)
if config.US_ENABLED:
    scheduler.add_job(
        run_us_trading_cycle,
        trigger=CronTrigger(minute="*/5", hour="21-23,0-4", day_of_week="mon-fri,sat", timezone=config.TIMEZONE),
        id="us_trading_cycle",
        replace_existing=True,
    )
    scheduler.add_job(
        send_us_end_of_day_report,
        trigger=CronTrigger(minute="5", hour="4", day_of_week="tue-sat", timezone=config.TIMEZONE),
        id="us_eod_report",
        replace_existing=True,
    )

# 시작 후 초기화
scheduler.add_job(
    run_monitor_cycle,
    trigger="date",
    run_date=datetime.now() + timedelta(seconds=10),
    id="initial_monitor",
)
scheduler.add_job(
    run_analysis_cycle,
    trigger="date",
    run_date=datetime.now() + timedelta(seconds=20),
    id="initial_analysis",
)

scheduler.start()


@app.get("/")
def root():
    return {"status": "KIS Trading Bot is running", "time": datetime.now(pytz.timezone(config.TIMEZONE)).isoformat()}


@app.get("/health")
def health():
    return {"status": "ok", "market_open": is_market_open(), "us_market_open": is_us_market_open()}


@app.get("/balance")
def api_balance():
    try:
        bal = client.get_balance()
        return bal
    except Exception as e:
        return {"error": str(e)}


@app.get("/us_balance")
def api_us_balance():
    try:
        if not config.US_ENABLED:
            return {"error": "US 시장 비활성화"}
        bal = client.get_us_balance(exchange=config.US_EXCHANGE)
        return bal
    except Exception as e:
        return {"error": str(e)}


@app.get("/price/{stock_code}")
def api_price(stock_code: str):
    try:
        return client.get_current_price(stock_code)
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
