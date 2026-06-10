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
from strategy import analyze_minute_data, analyze_daily_data, should_buy, should_sell
import telegram_bot

# ==================== KIS API 거래대금 상위 자동 스캔 ====================
ETF_KEYWORDS = ("KODEX", "TIGER", "RISE", "SOL", "PLUS", "HANARO", "KB", "KBI",
                "미래에셋", "삼성인버스", "N2", "ARIRANG", "FOCUS", "HANA", "KOSEF", "TREX",
                "KINDEX", "KBSTAR")


def fetch_top_stocks(client: KISClient, limit=30, min_value=10_000_000_000):
    """KIS API 거래대금 상위 조회 (FHPST01710000)"""
    url = f"{client.base_url}/uapi/domestic-stock/v1/ranking/volume-ranks"
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_RANK_SORT_CLS_CODE": "2",  # 2=거래대금
        "FID_INPUT_ISCD": "0000",
        "FID_DIV_CLS_CODE": "0",
        "FID_TRGT_CLS_CODE": "0",
        "FID_BLNG_CLS_CODE": "0",
        "FID_TRGT_EXLS_CLS_CODE": "0",
        "FID_INPUT_PRICE_1": "0",
        "FID_INPUT_PRICE_2": "0",
        "FID_VOL_CNT": "0",
    }
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {client._get_token()}",
        "appkey": client.app_key,
        "appsecret": client.app_secret,
        "tr_id": "FHPST01710000",
    }
    r = requests.get(url, headers=headers, params=params, timeout=15)
    d = r.json()
    if d.get("rt_cd") != "0":
        print(f"KIS 상위조회 실패: {d}")
        return []

    stocks = []
    for item in d.get("output", []):
        code = item.get("mksc_shrn_iscd", "")
        name = item.get("hts_kor_isnm", "")
        if not code or len(code) != 6:
            continue
        # ETF 필터
        if any(name.startswith(k) for k in ETF_KEYWORDS):
            continue
        # 거래대금 파싱
        try:
            value = int(item.get("acml_tr_pbmn", 0))
        except:
            continue
        if value < min_value:
            continue
        stocks.append({"code": code, "name": name, "trading_value": value})
    return stocks[:limit]


app = FastAPI()
client = KISClient()
scheduler = BackgroundScheduler(timezone=pytz.timezone(config.TIMEZONE))

# 실행 로그 (기억용)
STATE_FILE = "/tmp/trading_state.json"


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
    if weekday >= 5:  # 토/일
        return False
    hm = now.hour * 100 + now.minute
    return 900 <= hm <= 1530


def is_us_market_open(now: datetime = None) -> bool:
    """미국장 개장 여부 (DST 여름: 21:30~04:00 KST)"""
    if not config.US_ENABLED:
        return False
    if now is None:
        now = datetime.now(pytz.timezone(config.TIMEZONE))
    weekday = now.weekday()
    hm = now.hour * 100 + now.minute
    # 월~금: 21:30 ~ 다음날 04:00
    if weekday == 0:  # 월요일
        return hm >= 2130
    if weekday >= 1 and weekday <= 4:  # 화~금
        return hm >= 2130 or hm <= 400
    if weekday == 5:  # 토요일
        return hm <= 400
    return False


def _init_day_records(state: Dict, today_str: str, prefix: str = "") -> Dict:
    """일일 기록 초기화 (매수/매도/스킵)"""
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


def run_trading_cycle():
    now = datetime.now(pytz.timezone(config.TIMEZONE))
    if not is_market_open(now):
        print(f"[{now}] 시장 종료 또는 주말. 실행 스킵.")
        return

    print(f"\n=== [트레이딩 사이클 시작] {now.strftime('%Y-%m-%d %H:%M:%S')} ===")
    state = load_state()
    today_str = now.strftime("%Y%m%d")

    # 오늘 내역 초기화
    if state.get("last_run_date") != today_str:
        state["buy_done_today"] = {}

    state = _init_day_records(state, today_str, prefix="kr_")

    try:
        # 1. 잔고 조회
        balance = client.get_balance()
        cash = balance["cash"]
        holdings = {h["stock_code"]: h for h in balance["holdings"]}

        print(f"지금 예수금: {cash:,}원 | 보유 종목: {len(holdings)}개")

        # 2. 오늘의 핫 종목 스캔 (장중 첫 1회만)
        scan_key = f"kr_hot_stocks_{today_str}"
        if not state.get(scan_key):
            print("오늘의 핫 종목 스캔 중...")
            hot_stocks = fetch_top_stocks(client, limit=30, min_value=10_000_000_000)
            if not hot_stocks:
                hot_stocks = [{"code": c.strip(), "name": c.strip()} for c in config.WATCHLIST if c.strip()]
                print("폴백: KIS 스캔 실패, WATCHLIST 사용")
            state[scan_key] = hot_stocks
            print(f"오늘 감시 종목: {len(hot_stocks)}개")

        watchlist = state[scan_key]

        # 3. 각 종목 분석
        for stock_info in watchlist:
            stock_code = stock_info["code"].strip()
            stock_name = stock_info.get("name", stock_code)
            if not stock_code:
                continue

            try:
                # 현재가
                price_info = client.get_current_price(stock_code)
                stock_name = price_info.get("stock_name", stock_name)
                current_price = price_info["current_price"]

                # 5분봉 데이터
                minute_data = client.get_minute_candles(stock_code, period="5")
                minute_analysis = analyze_minute_data(minute_data)

                # 일봉 데이터
                daily_data = client.get_daily_candles(stock_code, count=60)
                daily_analysis = analyze_daily_data(daily_data)

                print(f"  [{stock_name}] 가: {current_price:,} | "
                      f"MA5: {minute_analysis.get('ma5')} | "
                      f"거래대금터짐: {minute_analysis.get('volume_spike')} | "
                      f"일양봉: {daily_analysis.get('is_positive')}")

                # 보유 중이면 매도 판정
                if stock_code in holdings:
                    position = holdings[stock_code]
                    sell_flag, sell_qty, sell_reason = should_sell(position, price_info, minute_analysis)
                    if sell_flag:
                        print(f"    -> 매도 신호! {sell_reason}")
                        resp = client.order_sell(stock_code, sell_qty)
                        print(f"    -> 주문 응답: {resp}")
                        profit_pct = position.get("profit_loss_rate", 0)
                        # 기록만 (알림은 장 마감 보고에서)
                        state[f"kr_sell_records_{today_str}"].append({
                            "name": stock_name,
                            "code": stock_code,
                            "price": current_price,
                            "quantity": sell_qty,
                            "profit_pct": round(profit_pct, 2),
                            "reason": sell_reason,
                            "time": now.strftime("%H:%M"),
                        })
                        state["buy_done_today"].pop(stock_code, None)
                    continue

                # 미보유 중이고 오늘 매수 없으면 매수 판정
                already_bought_today = state["buy_done_today"].get(stock_code, False)
                if not already_bought_today:
                    buy_flag, buy_qty, buy_reason = should_buy(
                        price_info, minute_analysis, daily_analysis, config.MAX_BUDGET_PER_STOCK
                    )
                    if buy_flag:
                        print(f"    -> 매수 신호! {buy_reason} | 수량: {buy_qty}주")
                        resp = client.order_buy(stock_code, buy_qty)
                        print(f"    -> 주문 응답: {resp}")
                        # 기록만 (알림은 장 마감 보고에서)
                        state[f"kr_buy_records_{today_str}"].append({
                            "name": stock_name,
                            "code": stock_code,
                            "price": current_price,
                            "quantity": buy_qty,
                            "reason": buy_reason,
                            "time": now.strftime("%H:%M"),
                        })
                        state["buy_done_today"][stock_code] = True
                    else:
                        # 미매수 사유 기록 (마지막 사유만 유지)
                        state[f"kr_skip_reasons_{today_str}"][stock_code] = {
                            "name": stock_name,
                            "reason": buy_reason,
                            "time": now.strftime("%H:%M"),
                        }

            except Exception as e:
                print(f"    -> 오류 [{stock_code}]: {e}")
                state[f"kr_skip_reasons_{today_str}"][stock_code] = {
                    "name": stock_name,
                    "reason": f"오류: {e}",
                    "time": now.strftime("%H:%M"),
                }
                continue

        save_state(state)
        print(f"=== [트레이딩 사이클 완료] ===\n")

    except Exception as e:
        print(f"주요 실행 오류: {e}")


def run_us_trading_cycle():
    """미국장 트레이딩 사이클"""
    now = datetime.now(pytz.timezone(config.TIMEZONE))
    if not is_us_market_open(now):
        print(f"[{now}] 미국장 마감. 실행 스킵.")
        return

    print(f"\n=== [US 트레이딩 사이클 시작] {now.strftime('%Y-%m-%d %H:%M:%S')} ===")
    state = load_state()
    today_str = now.strftime("%Y%m%d")

    state = _init_day_records(state, today_str, prefix="us_")

    try:
        # 미국장 잔고
        us_balance = client.get_us_balance(exchange=config.US_EXCHANGE)
        us_cash = us_balance["cash"]
        us_holdings = {h["stock_code"]: h for h in us_balance["holdings"]}

        print(f"US 예수액: ${us_cash:,.2f} | 보유: {len(us_holdings)}개")

        # 미국장 스캔 (첫 1회)
        scan_key = f"us_hot_stocks_{today_str}"
        if not state.get(scan_key):
            print("US 핫 종목 스캔...")
            us_stocks = []
            for code in config.US_WATCHLIST:
                code = code.strip()
                if not code:
                    continue
                try:
                    price = client.get_us_price(code, exchange=config.US_EXCHANGE)
                    if price["trading_value"] >= 10_000_000:  # $10M
                        us_stocks.append({
                            "code": code,
                            "name": price["stock_name"],
                            "price": price["current_price"],
                            "change_rate": price["change_rate"],
                            "value": price["trading_value"],
                        })
                except Exception as e:
                    continue
            state[scan_key] = us_stocks
            print(f"US 감시 종목: {len(us_stocks)}개")

        us_watchlist = state.get(scan_key, [])

        for stock_info in us_watchlist:
            stock_code = stock_info["code"].strip()
            stock_name = stock_info.get("name", stock_code)
            if not stock_code:
                continue

            try:
                price_info = client.get_us_price(stock_code, exchange=config.US_EXCHANGE)
                stock_name = price_info.get("stock_name", stock_name)
                current_price = price_info["current_price"]

                # 5분봉
                minute_data = client.get_us_minute(stock_code, exchange=config.US_EXCHANGE, period="5")
                minute_analysis = analyze_minute_data(minute_data)

                # 일봉
                daily_data = client.get_us_daily(stock_code, exchange=config.US_EXCHANGE, count=60)
                daily_analysis = analyze_daily_data(daily_data)

                print(f"  [US {stock_name}] ${current_price:.2f} | "
                      f"MA5: {minute_analysis.get('ma5')} | "
                      f"vol_spike: {minute_analysis.get('volume_spike')}")

                # 보유 중이면 매도
                if stock_code in us_holdings:
                    position = us_holdings[stock_code]
                    sell_flag, sell_qty, sell_reason = should_sell(
                        position, price_info, minute_analysis,
                        stop_loss_pct=config.US_STOP_LOSS_PCT,
                        take_profit_pct=config.US_TAKE_PROFIT_PCT
                    )
                    if sell_flag:
                        print(f"    -> US 매도! {sell_reason}")
                        resp = client.order_us_sell(stock_code, sell_qty, exchange=config.US_EXCHANGE)
                        print(f"    -> {resp}")
                        profit_pct = position.get("profit_loss_rate", 0)
                        state[f"us_sell_records_{today_str}"].append({
                            "name": stock_name,
                            "code": stock_code,
                            "price": current_price,
                            "quantity": sell_qty,
                            "profit_pct": round(profit_pct, 2),
                            "reason": sell_reason,
                            "time": now.strftime("%H:%M"),
                        })
                        state[f"us_buy_done_{today_str}"].pop(stock_code, None)
                    continue

                # 미보유 중이고 오늘 매수 없으면 매수
                already_bought = state.get(f"us_buy_done_{today_str}", {}).get(stock_code, False)
                if not already_bought:
                    buy_flag, buy_qty, buy_reason = should_buy(
                        price_info, minute_analysis, daily_analysis, config.US_MAX_BUDGET_PER_STOCK
                    )
                    if buy_flag:
                        print(f"    -> US 매수! {buy_reason} | {buy_qty}주")
                        resp = client.order_us_buy(stock_code, buy_qty, exchange=config.US_EXCHANGE)
                        print(f"    -> {resp}")
                        state[f"us_buy_records_{today_str}"].append({
                            "name": stock_name,
                            "code": stock_code,
                            "price": current_price,
                            "quantity": buy_qty,
                            "reason": buy_reason,
                            "time": now.strftime("%H:%M"),
                        })
                        if f"us_buy_done_{today_str}" not in state:
                            state[f"us_buy_done_{today_str}"] = {}
                        state[f"us_buy_done_{today_str}"][stock_code] = True
                    else:
                        state[f"us_skip_reasons_{today_str}"][stock_code] = {
                            "name": stock_name,
                            "reason": buy_reason,
                            "time": now.strftime("%H:%M"),
                        }

            except Exception as e:
                print(f"    -> US 오류 [{stock_code}]: {e}")
                state[f"us_skip_reasons_{today_str}"][stock_code] = {
                    "name": stock_name,
                    "reason": f"오류: {e}",
                    "time": now.strftime("%H:%M"),
                }
                continue

        save_state(state)
        print(f"=== [US 트레이딩 사이클 완료] ===\n")

    except Exception as e:
        print(f"US 주요 실행 오류: {e}")


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

    # 매도 내역
    if sells:
        lines.append(f"🔴 *매도 ({len(sells)}건)*")
        for s in sells:
            emoji = "📈" if s["profit_pct"] >= 0 else "📉"
            lines.append(f"  {emoji} {s['name']}({s['code']})")
            lines.append(f"    {s['price']:,}원 × {s['quantity']}주 | {s['profit_pct']:+.2f}%")
            lines.append(f"    사유: {s['reason']} ({s['time']})")
        lines.append("")

    # 매수 내역
    if buys:
        lines.append(f"🟢 *매수 ({len(buys)}건)*")
        for b in buys:
            lines.append(f"  ▷ {b['name']}({b['code']})")
            lines.append(f"    {b['price']:,}원 × {b['quantity']}주")
            lines.append(f"    사유: {b['reason']} ({b['time']})")
        lines.append("")

    # 매수 안 한 이유
    if skips:
        lines.append(f"⏭ *미매수 종목 ({len(skips)}개)*")
        for code, info in skips.items():
            lines.append(f"  · {info['name']}({code}): {info['reason']}")
        lines.append("")

    # 요약
    if not buys and not sells:
        lines.append("📝 오늘 매수/매도 없음")
    else:
        lines.append(f"총 매수 {len(buys)}건 | 매도 {len(sells)}건 | 스킵 {len(skips)}개")

    # 잔고 추가
    try:
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
    # 미국장은 전날 날짜 기준 (04:05에 실행하면 전날 날짜)
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

    # 미국 잔고
    try:
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

# 국내장: 평일 5분 주기 트레이딩 (알림 없이 실행만)
scheduler.add_job(
    run_trading_cycle,
    trigger=CronTrigger(minute="*/5", hour="9-15", day_of_week="mon-fri", timezone=config.TIMEZONE),
    id="trading_cycle",
    replace_existing=True,
)

# 국내장 마감 보고: 평일 15:35
scheduler.add_job(
    send_kr_end_of_day_report,
    trigger=CronTrigger(minute="35", hour="15", day_of_week="mon-fri", timezone=config.TIMEZONE),
    id="kr_eod_report",
    replace_existing=True,
)

# 미국장: 월~토 21:30~04:00 (5분 주기)
if config.US_ENABLED:
    scheduler.add_job(
        run_us_trading_cycle,
        trigger=CronTrigger(minute="*/5", hour="21-23,0-4", day_of_week="mon-fri,sat", timezone=config.TIMEZONE),
        id="us_trading_cycle",
        replace_existing=True,
    )

    # 미국장 마감 보고: 화~토 04:05 KST
    scheduler.add_job(
        send_us_end_of_day_report,
        trigger=CronTrigger(minute="5", hour="4", day_of_week="tue-sat", timezone=config.TIMEZONE),
        id="us_eod_report",
        replace_existing=True,
    )

# 시작 후 10초 뒤 한 번 실행해서 점검
scheduler.add_job(
    run_trading_cycle,
    trigger="date",
    run_date=datetime.now() + timedelta(seconds=10),
    id="initial_run",
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
