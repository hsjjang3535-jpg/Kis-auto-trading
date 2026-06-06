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
        state["last_run_date"] = today_str

    try:
        # 1. 잔고 조회
        balance = client.get_balance()
        cash = balance["cash"]
        holdings = {h["stock_code"]: h for h in balance["holdings"]}

        print(f"지금 예수금: {cash:,}원 | 보유 종목: {len(holdings)}개")

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


# APScheduler: KST 기준으로 매수 5분마다
# 장 중 09:05 ~ 15:25 동안만 실행 (시잤 관리 선조를 망함)
scheduler.add_job(
    run_trading_cycle,
    trigger=CronTrigger(minute="*/5", hour="9-15", day_of_week="mon-fri", timezone=config.TIMEZONE),
    id="trading_cycle",
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
    return {"status": "ok", "market_open": is_market_open()}


@app.get("/balance")
def api_balance():
    try:
        bal = client.get_balance()
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
