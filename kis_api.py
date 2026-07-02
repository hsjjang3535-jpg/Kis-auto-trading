import os
import time
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


def _market_get(path: str, tr_id: str, params: dict, retries: int = 3) -> dict:
    """시세 API GET (500 등 일시 오류 시 재시도)"""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            res = requests.get(
                f"{MARKET_URL}{path}",
                headers=_market_headers(tr_id),
                params=params,
                timeout=10,
            )
            if res.status_code in (500, 502, 503, 504):
                raise requests.HTTPError(
                    f"{res.status_code} Server Error: {res.reason} for url: {res.url}",
                    response=res,
                )
            res.raise_for_status()
            data = res.json()
            if data.get("rt_cd") not in (None, "0"):
                msg = data.get("msg1", "알 수 없는 오류")
                raise RuntimeError(f"KIS 시세 API 오류 ({tr_id}): {msg}")
            return data
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                wait = 1 + attempt
                print(f"[KIS 재시도] {path} ({attempt + 1}/{retries}) {e} → {wait}초 후")
                time.sleep(wait)
                continue
            break
    raise last_err or RuntimeError(f"KIS API 호출 실패: {path}")

def _trade_headers(tr_id: str) -> dict:
    """주문/계좌 조회용 헤더 (모드별 토큰)"""
    return {
        "Content-Type": "application/json",
        "authorization": f"Bearer {_get_trade_token()}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
    }


def get_top_trading_value(top_n: int = 20, market: str = "0000") -> list[dict]:
    """거래대금 상위 종목 조회 (거래량순위 API, fid_blng_cls_code=3)
    market: "0000"=전체, "0001"=코스피, "1001"=코스닥
    """
    headers = _market_headers("FHPST01710000")
    headers["custtype"] = "P"

    res = requests.get(
        f"{MARKET_URL}/uapi/domestic-stock/v1/quotations/volume-rank",
        headers=headers,
        timeout=10,
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": market,
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "3",       # 3=거래금액순
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "000000",
            "FID_INPUT_PRICE_1": "0",
            "FID_INPUT_PRICE_2": "0",
            "FID_VOL_CNT": "0",
            "FID_INPUT_DATE_1": "0",
        },
    )
    res.raise_for_status()
    data = res.json()
    if data.get("rt_cd") != "0":
        msg = data.get("msg1", "알 수 없는 오류")
        raise RuntimeError(f"거래대금 API 오류 (market={market}): {msg}")

    # volume-rank API는 Output(대문자) 또는 output(소문자) 사용
    items = data.get("output") or data.get("Output") or []
    return items[:top_n]


def get_stock_info(stock_code: str) -> dict:
    """주식 현재가 및 기본 정보 조회"""
    data = _market_get(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        "FHKST01010100",
        {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": stock_code},
    )
    return data.get("output", {})


def get_current_price(stock_code: str, fallback: float | None = None) -> float:
    """현재가 조회 (실패 시 fallback 사용)"""
    try:
        info = get_stock_info(stock_code)
        price = float(info.get("stck_prpr", 0))
        if price > 0:
            return price
    except Exception as e:
        print(f"[현재가 조회 실패] {stock_code}: {e}")
    if fallback and float(fallback) > 0:
        print(f"[현재가 fallback] {stock_code}: 스크리닝 가격 {fallback:.0f}원 사용")
        return float(fallback)
    raise RuntimeError(f"현재가 조회 실패 ({stock_code})")

def get_daily_chart(stock_code: str, days: int = 200) -> list[dict]:
    """일봉 데이터 조회 (최근 days일)"""
    today = datetime.now(KST).strftime("%Y%m%d")
    start = (datetime.now(KST) - timedelta(days=days + 50)).strftime("%Y%m%d")
    data = _market_get(
        "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        "FHKST03010100",
        {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": stock_code,
            "fid_input_date_1": start,
            "fid_input_date_2": today,
            "fid_period_div_code": "D",
            "fid_org_adj_prc": "0",
        },
    )
    return data.get("output2", [])

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
    if res.status_code == 404 and MODE != "실전":
        raise RuntimeError(
            f"모의투자 서버 404 오류: 계좌번호·앱키 확인 필요 (VTS 서버 {TRADE_URL})"
        )
    res.raise_for_status()
    data = res.json()
    # KIS API는 HTTP 200이어도 rt_cd != "0" 이면 오류
    if data.get("rt_cd") != "0":
        msg = data.get("msg1", "알 수 없는 오류")
        print(f"[매수 API 오류] {stock_code}: {msg}")
    return data


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
    if res.status_code == 404 and MODE != "실전":
        raise RuntimeError(
            f"모의투자 서버 404 오류: 계좌번호·앱키 확인 필요 (VTS 서버 {TRADE_URL})"
        )
    res.raise_for_status()
    data = res.json()
    if data.get("rt_cd") != "0":
        msg = data.get("msg1", "알 수 없는 오류")
        print(f"[매도 API 오류] {stock_code}: {msg}")
    return data


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


def get_cash_balance() -> int:
    """실제 계좌 예수금(주문가능금액) 조회"""
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
    data = res.json()
    # output2: 계좌 요약 정보 (예수금, 총평가금액 등)
    summary = data.get("output2", [{}])
    if isinstance(summary, list):
        summary = summary[0] if summary else {}
    # dnca_tot_amt: 예수금 총액 / prvs_rcdl_excc_amt: 전일 매매 청산 금액
    cash = int(summary.get("dnca_tot_amt", "0") or "0")
    return cash
