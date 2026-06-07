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


def run_trading_cycle():
    now = datetime.now(pytz.timezone(config.TIMEZONE))
    if not is_market_open(now):
        print(f"[{now}] 시장 종뢰 또는 주말. 실행 스킵.")
        return

    print(f"\n=== [트레이딩 사이클 시작] {now.strftime('%Y-%m-%d %H:%M:%S')} ===")
    state = load_state()
    today_str = now.strftime("%Y%m%d")

    # 오늘 내역 초기화
    if state.get("last_run_date") != today_str:
        state["buy_done_today"] = {}
        state["last_run_date"] = today_str

    try:
        # 1. 잔고 조회
        balance = client.get_balance()
        cash = balance["cash"]
        holdings = {h["stock_code"]: h for h in balance["holdings"]}

        print(f"지금 예수금: {cash:,}원 | 보유 종목: {len(holdings)}개")

        # CSV: 잔고 보고

        # 2. 오늘의 핫 종목 스캔 (장중 첫 1회만, 상태에 캐싱)
        if state.get("last_run_date") != today_str or not state.get("hot_stocks"):
            print("오늘의 핫 종목 스캔 중...")
            hot_stocks = fetch_top_stocks(client, limit=30, min_value=10_000_000_000)
            if not hot_stocks:
                hot_stocks = [{"code": c.strip(), "name": c.strip()} for c in config.WATCHLIST if c.strip()]
                print("평범: KIS 스캔 실패, WATCHLIST 사용")
            state["hot_stocks"] = hot_stocks
            state["last_run_date"] = today_str
            print(f"오늘 감시 종목: {len(hot_stocks)}개")
            names = [f"{h.get('name', h['code'])}({h['code']})" for h in hot_stocks[:10]]
            telegram_bot.send_info(f"*[오늘의 종가베팅 후보]* ({today_str})\n" + "\n".join(names))
            # CSV: 스캔 결과 저장

        watchlist = state["hot_stocks"]

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

                # 일등봉 데이터
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
                        telegram_bot.send_sell_alert(
                            stock_name, stock_code, current_price, sell_qty, profit_pct, sell_reason
                        )
                        # CSV: 매도 내역
                        # 내역 업데이트
                        state["buy_done_today"].pop(stock_code, None)
                    continue

                # 미보유 중이고 오늘 내역 없으면 매수 판정
                already_bought_today = state["buy_done_today"].get(stock_code, False)
                if not already_bought_today:
                    buy_flag, buy_qty, buy_reason = should_buy(
                        price_info, minute_analysis, daily_analysis, config.MAX_BUDGET_PER_STOCK
                    )
                    if buy_flag:
                        print(f"    -> 매수 신호! {buy_reason} | 수량: {buy_qty}주")
                        resp = client.order_buy(stock_code, buy_qty)
                        print(f"    -> 주문 응답: {resp}")
                        telegram_bot.send_buy_alert(
                            stock_name, stock_code, current_price, buy_qty, buy_reason
                        )
                        # CSV: 매수 내역
                        state["buy_done_today"][stock_code] = True

            except Exception as e:
                print(f"    -> 오류 [{stock_code}]: {e}")
                telegram_bot.send_error_alert(f"{stock_code} 처리 중 오류: {e}")
                continue

        save_state(state)
        print(f"=== [트레이딩 사이큐 완료] ===\n")

    except Exception as e:
        print(f"주요 실행 오류: {e}")
        telegram_bot.send_error_alert(str(e))


def run_us_trading_cycle():
    """미국장 트레이딩 사이플"""
    now = datetime.now(pytz.timezone(config.TIMEZONE))
    if not is_us_market_open(now):
        print(f"[{now}] 미국장 마감. 실행 스킵.")
        return

    print(f"\n=== [US 트레이딩 사이플 시작] {now.strftime('%Y-%m-%d %H:%M:%S')} ===")
    state = load_state()
    today_str = now.strftime("%Y%m%d")
    us_key = f"us_buy_done_{today_str}"
    us_scan_key = f"us_hot_stocks_{today_str}"

    if us_key not in state:
        state[us_key] = {}

    try:
        # 미국장 잔고
        us_balance = client.get_us_balance(exchange=config.US_EXCHANGE)
        us_cash = us_balance["cash"]
        us_holdings = {h["stock_code"]: h for h in us_balance["holdings"]}

        print(f"US 예수액: ${us_cash:,.2f} | 보유: {len(us_holdings)}개")

        # CSV: US 잔고 보고

        # 미국장 스캔 (첫 1회)
        if not state.get(us_scan_key):
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
            state[us_scan_key] = us_stocks
            print(f"US 감시 종목: {len(us_stocks)}개")
            if us_stocks:
                names = [f"{s['name']}({s['code']})" for s in us_stocks[:10]]
                telegram_bot.send_info(f"*[오늘의 US 후보]* ({today_str})\n" + "\n".join(names))
                # CSV: US 스캔 결과

        us_watchlist = state.get(us_scan_key, [])

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

                # 일등봉
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
                        telegram_bot.send_sell_alert(
                            stock_name, stock_code, current_price, sell_qty, profit_pct, sell_reason
                        )
                        # CSV: US 매도 내역
                        state[us_key].pop(stock_code, None)
                    continue

                # 미보유 중이고 오늘 내역 없으면 매수
                already_bought = state[us_key].get(stock_code, False)
                if not already_bought:
                    buy_flag, buy_qty, buy_reason = should_buy(
                        price_info, minute_analysis, daily_analysis, config.US_MAX_BUDGET_PER_STOCK
                    )
                    if buy_flag:
                        print(f"    -> US 매수! {buy_reason} | {buy_qty}주")
                        resp = client.order_us_buy(stock_code, buy_qty, exchange=config.US_EXCHANGE)
                        print(f"    -> {resp}")
                        telegram_bot.send_buy_alert(
                            stock_name, stock_code, current_price, buy_qty, buy_reason
                        )
                        # CSV: US 매수 내역
                        state[us_key][stock_code] = True

            except Exception as e:
                print(f"    -> US 오류 [{stock_code}]: {e}")
                telegram_bot.send_error_alert(f"US {stock_code} 처리 중 오류: {e}")
                continue

        save_state(state)
        print(f"=== [US 트레이딩 사이플 완료] ===\n")

    except Exception as e:
        print(f"US 주요 실행 오류: {e}")
        telegram_bot.send_error_alert(f"US: {e}")


# APScheduler: KST 기준으로 국내장 5분맛
scheduler.add_job(
    run_trading_cycle,
    trigger=CronTrigger(minute="*/5", hour="9-15", day_of_week="mon-fri", timezone=config.TIMEZONE),
    id="trading_cycle",
    replace_existing=True,
)

# 미국장: 월~토 21:30~04:00 (5분맛)
if config.US_ENABLED:
    scheduler.add_job(
        run_us_trading_cycle,
        trigger=CronTrigger(minute="*/5", hour="21-23,0-4", day_of_week="mon-fri,sat", timezone=config.TIMEZONE),
        id="us_trading_cycle",
        replace_existing=True,
    )

# 방어 후 10초 뒤 바로 한 번 실행해서 시작 점검
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
