import os
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

KST = ZoneInfo("Asia/Seoul")

load_dotenv()

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
MODE = os.getenv("KIS_MODE", "실전")
ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO", "")

# 시장 데이터(시세/차트)는 항상 실전 서버 사용 (VTS 서버는 시세 API 미지원)
MARKET_URL = "https://openapi.koreainvestment.com:9443"
# 주문/계좌 조회는 모드에 따라 구분
TRADE_URL = "https://openapi.koreainvestment.com:9443" if MODE == "실전" else "https://openapivts.koreainvestment.com:29443"

# 서버별 토큰 캐시 분리 (실전 서버 토큰 / VTS 서버 토큰)
_token_cache = {
    "market": {"token": None, "expires_at": None},  # 시세 조회용 (항상 실전 서버)
    "trade":  {"token": None, "expires_at": None},  # 주문/계좌용 (모드에 따라)
}


def _fetch_token(server_url: str) -> str:
    res = requests.post(
        f"{server_url}/oauth2/tokenP",
        json={"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET},
        timeout=10,
    )
    res.raise_for_status()
    return res.json()["access_token"]


def _get_market_token() -> str:
    """시세/차트 조회용 토큰 (항상 실전 서버)"""
    cache = _token_cache["market"]
    now = datetime.now(KST)
    if cache["token"] and cache["expires_at"] > now:
        return cache["token"]
    cache["token"] = _fetch_token(MARKET_URL)
    cache["expires_at"] = now + timedelta(hours=23)
    return cache["token"]


def _get_trade_token() -> str:
    """주문/계좌 조회용 토큰 (모드에 따라 실전 또는 VTS)"""
    cache = _token_cache["trade"]
    now = datetime.now(KST)
    if cache["token"] and cache["expires_at"] > now:
        return cache["token"]
    cache["token"] = _fetch_token(TRADE_URL)
    cache["expires_at"] = now + timedelta(hours=23)
    return cache["token"]


def _market_headers(tr_id: str) -> dict:
    """시세/차트 조회용 헤더 (실전 서버 토큰)"""
    return {
        "Content-Type": "application/json",
        "authorization": f"Bearer {_get_market_token()}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
    }


def _trade_headers(tr_id: str) -> dict:
    """주문/계좌 조회용 헤더 (모드별 토큰)"""
    return {
        "Content-Type": "application/json",
        "authorization": f"Bearer {_get_trade_token()}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
    }


def get_top_trading_value(top_n: int = 20) -> list[dict]:
    """거래대금 상위 종목 조회"""
    res = requests.get(
        f"{MARKET_URL}/uapi/domestic-stock/v1/ranking/trading-value",
        headers=_market_headers("FHPST01700000"),
        timeout=10,
        params={
            "fid_cond_mrkt_div_code": "J",
            "fid_cond_scr_div_code": "20171",
            "fid_input_iscd": "0000",
            "fid_rank_sort_cls_code": "0",
            "fid_input_cnt_1": str(top_n),
            "fid_prc_cls_code": "0",
            "fid_input_price_1": "",
            "fid_input_price_2": "",
            "fid_vol_cnt": "",
            "fid_trgt_cls_code": "111111111",
            "fid_trgt_exls_cls_code": "000000",
            "fid_div_cls_code": "0",
            "fid_rsfl_rate1": "",
            "fid_rsfl_rate2": "",
        },
    )
    res.raise_for_status()
    return res.json().get("output", [])


def get_stock_info(stock_code: str) -> dict:
    """주식 현재가 및 기본 정보 조회"""
    res = requests.get(
        f"{MARKET_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
        headers=_market_headers("FHKST01010100"),
        params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": stock_code},
        timeout=10,
    )
    res.raise_for_status()
    return res.json().get("output", {})


def get_daily_chart(stock_code: str, days: int = 200) -> list[dict]:
    """일봉 데이터 조회 (최근 days일)"""
    today = datetime.now(KST).strftime("%Y%m%d")
    start = (datetime.now(KST) - timedelta(days=days + 50)).strftime("%Y%m%d")
    res = requests.get(
        f"{MARKET_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        headers=_market_headers("FHKST03010100"),
        timeout=15,
        params={
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": stock_code,
            "fid_input_date_1": start,
            "fid_input_date_2": today,
            "fid_period_div_code": "D",
            "fid_org_adj_prc": "0",
        },
    )
    res.raise_for_status()
    return res.json().get("output2", [])


def get_chart_indicators(stock_code: str) -> dict:
    """
    차트 지표 계산:
    - ma5: 5일 이동평균
    - ma20: 20일 이동평균
    - high_200: 200일 최고가
    - vol_ratio: 오늘 거래량 / 최근 5일 평균 거래량 비율
    - current: 현재가
    - upper_tail_ratio: 윗꼬리 비율 (낮을수록 좋음)
    """
    candles = get_daily_chart(stock_code, days=210)
    if len(candles) < 20:
        return {}

    closes = []
    volumes = []
    highs = []
    for c in candles:
        try:
            closes.append(float(c.get("stck_clpr", 0)))
            volumes.append(float(c.get("acml_vol", 0)))
            highs.append(float(c.get("stck_hgpr", 0)))
        except ValueError:
            continue

    if len(closes) < 20:
        return {}

    current = closes[0]
    ma5 = sum(closes[:5]) / 5
    ma20 = sum(closes[:20]) / 20
    high_200 = max(highs[:min(200, len(highs))])
    vol_today = volumes[0]
    vol_avg5 = sum(volumes[1:6]) / 5 if len(volumes) >= 6 else vol_today

    # 오늘 봉의 윗꼬리 비율
    try:
        today_high = highs[0]
        today_candle = candles[0]
        today_open = float(today_candle.get("stck_oprc", current))
        body_top = max(current, today_open)
        upper_tail = today_high - body_top
        candle_range = today_high - float(today_candle.get("stck_lwpr", today_high))
        upper_tail_ratio = upper_tail / candle_range if candle_range > 0 else 0
    except (ValueError, ZeroDivisionError):
        upper_tail_ratio = 0

    high_20 = max(highs[1:min(21, len(highs))])  # 전일 기준 20일 최고가 (오늘 제외)

    # RSI(14) 계산
    rsi = _calc_rsi(closes, period=14)

    return {
        "current": current,
        "ma5": ma5,
        "ma20": ma20,
        "high_200": high_200,
        "high_20": high_20,
        "vol_ratio": vol_today / vol_avg5 if vol_avg5 > 0 else 0,
        "upper_tail_ratio": upper_tail_ratio,
        "rsi": rsi,
    }


def _calc_rsi(closes: list[float], period: int = 14) -> float:
    """RSI 계산 (closes[0]이 최신)"""
    if len(closes) < period + 1:
        return 50.0
    # 최신→과거 순서이므로 역순으로 변환
    prices = list(reversed(closes[:period + 1]))
    gains, losses = [], []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def is_near_high(stock_code: str, threshold_pct: float = 5.0) -> bool:
    """52주 신고가 대비 threshold_pct% 이내인지 확인"""
    info = get_stock_info(stock_code)
    try:
        current = float(info.get("stck_prpr", 0))
        high_52w = float(info.get("w52_hgpr", 0))
        if high_52w == 0:
            return False
        gap = (high_52w - current) / high_52w * 100
        return gap <= threshold_pct
    except (ValueError, ZeroDivisionError):
        return False


def buy_stock(stock_code: str, quantity: int) -> dict:
    """시장가 매수"""
    acc_no = ACCOUNT_NO[:8]
    acc_prod = ACCOUNT_NO[8:] if len(ACCOUNT_NO) > 8 else "01"
    tr_id = "TTTC0802U" if MODE == "실전" else "VTTC0802U"

    res = requests.post(
        f"{TRADE_URL}/uapi/domestic-stock/v1/trading/order-cash",
        headers=_trade_headers(tr_id),
        json={
            "CANO": acc_no,
            "ACNT_PRDT_CD": acc_prod,
            "PDNO": stock_code,
            "ORD_DVSN": "01",  # 시장가
            "ORD_QTY": str(quantity),
            "ORD_UNPR": "0",
        },
        timeout=10,
    )
    res.raise_for_status()
    return res.json()


def sell_stock(stock_code: str, quantity: int) -> dict:
    """시장가 매도"""
    acc_no = ACCOUNT_NO[:8]
    acc_prod = ACCOUNT_NO[8:] if len(ACCOUNT_NO) > 8 else "01"
    tr_id = "TTTC0801U" if MODE == "실전" else "VTTC0801U"

    res = requests.post(
        f"{TRADE_URL}/uapi/domestic-stock/v1/trading/order-cash",
        headers=_trade_headers(tr_id),
        json={
            "CANO": acc_no,
            "ACNT_PRDT_CD": acc_prod,
            "PDNO": stock_code,
            "ORD_DVSN": "01",  # 시장가
            "ORD_QTY": str(quantity),
            "ORD_UNPR": "0",
        },
        timeout=10,
    )
    res.raise_for_status()
    return res.json()


def get_holdings() -> list[dict]:
    """보유 종목 조회"""
    acc_no = ACCOUNT_NO[:8]
    acc_prod = ACCOUNT_NO[8:] if len(ACCOUNT_NO) > 8 else "01"
    tr_id = "TTTC8434R" if MODE == "실전" else "VTTC8434R"

    res = requests.get(
        f"{TRADE_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
        headers=_trade_headers(tr_id),
        timeout=10,
        params={
            "CANO": acc_no,
            "ACNT_PRDT_CD": acc_prod,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        },
    )
    res.raise_for_status()
    return res.json().get("output1", [])
