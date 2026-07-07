import os
import json
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

KST = ZoneInfo("Asia/Seoul")

load_dotenv()

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")


def _normalize_mode(raw: str) -> str:
    v = (raw or "모의").strip().lower()
    if v in ("실전", "prod", "real", "production"):
        return "실전"
    return "모의"


MODE = _normalize_mode(os.getenv("KIS_MODE", "모의"))
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

_last_trade_call = 0.0


def get_account_parts() -> tuple[str, str]:
    """CANO(8자리), ACNT_PRDT_CD(2자리) 반환"""
    cano = os.getenv("KIS_CANO", "").strip()
    prod = os.getenv("KIS_ACNT_PRDT_CD", "").strip()
    if cano and prod:
        return cano.zfill(8)[-8:], prod.zfill(2)[-2:]

    raw = (ACCOUNT_NO or "").strip().replace("-", "").replace(" ", "")
    if not raw:
        raise ValueError("KIS_ACCOUNT_NO 또는 KIS_CANO/KIS_ACNT_PRDT_CD가 필요합니다")

    if len(raw) >= 10:
        return raw[:8], raw[8:10]
    if len(raw) == 8:
        return raw, prod or "01"
    raise ValueError(
        f"계좌번호 형식 오류 ({raw}): 10자리(8+2) 또는 8자리+KIS_ACNT_PRDT_CD"
    )


def validate_account_for_mode() -> str | None:
    """모의/실전과 계좌번호 불일치 시 안내 문구 반환"""
    if MODE == "실전":
        return None
    try:
        cano, prod = get_account_parts()
    except ValueError as e:
        return str(e)
    if cano.startswith(("50", "4444", "00")):
        return None
    if len(cano) == 8:
        return (
            f"KIS_MODE=모의인데 KIS_ACCOUNT_NO({cano}{prod})가 실계좌로 보입니다. "
            "모의투자 계좌(보통 50xxxxxxxx)를 KIS Developers → 모의투자에서 확인 후 "
            "Railway Variables의 KIS_ACCOUNT_NO를 수정하세요. "
            "모의투자 앱키와 모의 계좌번호가 쌍으로 맞아야 합니다."
        )
    return None


def is_account_error(exc: Exception) -> bool:
    msg = str(exc)
    return any(k in msg for k in (
        "계좌번호", "계좌", "CANO", "ACNT_PRDT", "인증 시점",
    ))


def account_error_hint() -> str:
    hint = validate_account_for_mode()
    if hint:
        return hint
    if MODE != "실전":
        return (
            "모의투자: KIS Developers에서 발급한 모의 계좌번호(50xxxxxxxx)와 "
            "모의투자 앱키를 Railway에 설정했는지 확인하세요."
        )
    return "KIS_ACCOUNT_NO(8+2자리)와 실전 앱키가 일치하는지 확인하세요."


def verify_trade_account() -> tuple[bool, str]:
    """시작 시 주문/잔고 API 연결 검증"""
    warn = validate_account_for_mode()
    try:
        cano, prod = get_account_parts()
        cash = get_orderable_cash()
        msg = f"계좌 {cano}-{prod} 연결 OK (주문가능 {cash:,}원)"
        if warn:
            msg = f"{warn}\n(잔고 조회는 성공했으나 주문 시 오류 가능)"
        return True, msg
    except Exception as e:
        extra = f"\n{warn}" if warn else f"\n{account_error_hint()}"
        return False, f"계좌 API 연결 실패: {e}{extra}"


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

def _trade_headers(tr_id: str, body: dict | None = None) -> dict:
    """주문/계좌 조회용 헤더 (모드별 토큰)"""
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "authorization": f"Bearer {_get_trade_token()}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }
    if body:
        try:
            headers["hashkey"] = _get_hashkey(body)
        except Exception as e:
            print(f"[hashkey] 생성 실패 ({e}), hashkey 없이 진행")
    return headers


def _get_hashkey(body: dict) -> str:
    """주문 POST body용 hashkey 발급"""
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    _trade_throttle()
    res = requests.post(
        f"{TRADE_URL}/uapi/hashkey",
        headers={
            "Content-Type": "application/json; charset=UTF-8",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
        },
        data=payload.encode("utf-8"),
        timeout=10,
    )
    res.raise_for_status()
    data = res.json()
    h = data.get("HASH") or data.get("hash")
    if not h:
        raise RuntimeError(f"hashkey 응답 없음: {data}")
    return h


def _trade_interval() -> float:
    """모의투자 초당 2건 → 1초 간격, 실전은 여유"""
    return 1.0 if MODE != "실전" else 0.05


def trade_interval() -> float:
    return _trade_interval()


def _trade_throttle() -> None:
    global _last_trade_call
    gap = _trade_interval()
    now = time.monotonic()
    wait = gap - (now - _last_trade_call)
    if wait > 0:
        time.sleep(wait)
    _last_trade_call = time.monotonic()


def is_systemic_order_error(exc: Exception) -> bool:
    """서버/한도 오류 — 연속 주문 중단 권장 (계좌 오류 제외)"""
    if is_account_error(exc):
        return False
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        if exc.response.status_code in (500, 502, 503, 504):
            return True
    msg = str(exc).lower()
    return any(x in msg for x in ("500", "502", "503", "504", "server error"))


def _parse_trade_error(res: requests.Response) -> str:
    try:
        err_json = res.json()
        msg_cd = err_json.get("msg_cd", "")
        msg1 = err_json.get("msg1", "")
        if msg_cd and msg1:
            return f"{msg_cd}: {msg1}"
        return msg1 or msg_cd or res.text[:200]
    except Exception:
        return res.text[:200] or res.reason


def _should_retry_trade(exc: Exception) -> bool:
    return is_systemic_order_error(exc)


def _trade_post(path: str, tr_id: str, body: dict, retries: int = 3) -> dict:
    """주문 POST (Content-Length 명시, 500 재시도, 모의 API 간격)"""
    url = f"{TRADE_URL}{path}"
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    last_err: Exception | None = None

    for attempt in range(retries):
        _trade_throttle()
        try:
            headers = _trade_headers(tr_id, body)
            res = requests.post(
                url,
                headers=headers,
                data=payload.encode("utf-8"),
                timeout=15,
            )
            if res.status_code == 404 and MODE != "실전":
                raise RuntimeError(
                    f"모의투자 서버 404 오류: 계좌번호·앱키 확인 필요 (VTS 서버 {TRADE_URL})"
                )

            if res.status_code in (500, 502, 503, 504):
                detail = _parse_trade_error(res)
                raise requests.HTTPError(
                    f"{res.status_code} Server Error: {detail} for url: {res.url}",
                    response=res,
                )

            if res.status_code >= 400:
                detail = _parse_trade_error(res)
                raise RuntimeError(f"KIS 주문 API 오류 ({res.status_code}): {detail}")

            res.raise_for_status()
            data = res.json()
            if data.get("rt_cd") not in (None, "0"):
                msg_cd = data.get("msg_cd", "")
                msg1 = data.get("msg1", "알 수 없는 오류")
                detail = f"{msg_cd}: {msg1}" if msg_cd else msg1
                raise RuntimeError(f"KIS 주문 거부: {detail}")
            return data
        except Exception as e:
            last_err = e
            if attempt < retries - 1 and _should_retry_trade(e):
                wait = 1 + attempt * 2
                print(f"[KIS 주문 재시도] {path} ({attempt + 1}/{retries}) {e} → {wait}초 후")
                time.sleep(wait)
                continue
            break

    raise last_err or RuntimeError(f"KIS 주문 API 실패: {path}")


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

def get_minute_chart(stock_code: str, hour: str | None = None) -> list[dict]:
    """당일 1분봉 조회 (기준 시각 이전 최대 30개)"""
    if not hour:
        hour = datetime.now(KST).strftime("%H%M%S")
    data = _market_get(
        "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
        "FHKST03010200",
        {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": stock_code,
            "fid_input_hour_1": hour,
            "fid_pw_data_incu_yn": "Y",
            "fid_fake_tick_incu_yn": "N",
            "fid_adj_stck_prc": "1",
        },
    )
    return data.get("output2", [])


def _hour_before(hhmmss: int) -> str:
    """HHMMSS 정수에서 1분 전 시각 문자열"""
    h = hhmmss // 10000
    m = (hhmmss // 100) % 100
    s = hhmmss % 100
    total = h * 3600 + m * 60 + s - 60
    if total < 9 * 3600:
        return "090000"
    nh, rem = divmod(total, 3600)
    nm, ns = divmod(rem, 60)
    return f"{nh:02d}{nm:02d}{ns:02d}"


def get_intraday_minute_bars(stock_code: str, max_minutes: int = 90) -> list[dict]:
    """당일 1분봉 수집 (과거→현재 순)"""
    all_candles: list[dict] = []
    seen: set[str] = set()
    hour = datetime.now(KST).strftime("%H%M%S")

    for _ in range(4):
        chunk = get_minute_chart(stock_code, hour)
        if not chunk:
            break
        for c in chunk:
            key = c.get("stck_cntg_hour", "")
            if key and key not in seen:
                seen.add(key)
                all_candles.append(c)
        if len(all_candles) >= max_minutes:
            break
        times = []
        for c in chunk:
            try:
                times.append(int(c.get("stck_cntg_hour", "0")))
            except ValueError:
                continue
        if not times:
            break
        hour = _hour_before(min(times))

    all_candles.sort(key=lambda x: x.get("stck_cntg_hour", ""))
    return all_candles[-max_minutes:]


def aggregate_5min_bars(minute_candles: list[dict]) -> list[dict]:
    """1분봉 → 5분봉 OHLCV"""
    if not minute_candles:
        return []

    parsed = []
    for c in minute_candles:
        try:
            t = int(c.get("stck_cntg_hour", "0"))
            parsed.append({
                "time": t,
                "open": float(c.get("stck_oprc", 0)),
                "high": float(c.get("stck_hgpr", 0)),
                "low": float(c.get("stck_lwpr", 0)),
                "close": float(c.get("stck_prpr", 0)),
                "volume": float(c.get("cntg_vol", 0)),
            })
        except (ValueError, TypeError):
            continue

    if not parsed:
        return []

    bars: list[dict] = []
    bucket: list[dict] = []
    bucket_start: int | None = None

    for row in parsed:
        minute_of_day = (row["time"] // 10000) * 60 + ((row["time"] // 100) % 100)
        slot = minute_of_day // 5
        if bucket_start is None:
            bucket_start = slot
        if slot != bucket_start and bucket:
            bars.append(_merge_ohlcv(bucket))
            bucket = []
            bucket_start = slot
        bucket.append(row)

    if bucket:
        bars.append(_merge_ohlcv(bucket))
    return bars


def _merge_ohlcv(rows: list[dict]) -> dict:
    return {
        "open": rows[0]["open"],
        "high": max(r["high"] for r in rows),
        "low": min(r["low"] for r in rows),
        "close": rows[-1]["close"],
        "volume": sum(r["volume"] for r in rows),
    }


def get_intraday_5min_indicators(stock_code: str) -> dict:
    """5분봉 기반 단기 지표 (MA60·RSI·세션 저점)"""
    minutes = get_intraday_minute_bars(stock_code, max_minutes=90)
    bars = aggregate_5min_bars(minutes)
    if not bars:
        return {}

    closes = [b["close"] for b in bars]
    lows = [b["low"] for b in bars]
    period = min(60, len(closes))
    ma60 = sum(closes[-period:]) / period
    rsi = _calc_rsi(closes, min(14, len(closes) - 1) if len(closes) > 1 else 14)
    session_low = min(lows)

    return {
        "current": closes[-1],
        "ma60": ma60,
        "ma_period": period,
        "rsi": rsi,
        "session_low": session_low,
        "bars_5": bars,
        "bar_count": len(bars),
    }


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
    acc_no, acc_prod = get_account_parts()
    tr_id = "TTTC0802U" if MODE == "실전" else "VTTC0802U"

    body = {
        "CANO": acc_no,
        "ACNT_PRDT_CD": acc_prod,
        "PDNO": stock_code,
        "ORD_DVSN": "01",  # 시장가
        "ORD_QTY": str(quantity),
        "ORD_UNPR": "0",
        "EXCG_ID_DVSN_CD": "KRX",
    }
    try:
        data = _trade_post(
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id,
            body,
        )
    except Exception as e:
        if is_account_error(e):
            raise RuntimeError(f"{e}\n{account_error_hint()}") from e
        raise
    if data.get("rt_cd") != "0":
        msg = data.get("msg1", "알 수 없는 오류")
        print(f"[매수 API 오류] {stock_code}: {msg}")
    return data


def sell_stock(stock_code: str, quantity: int) -> dict:
    """시장가 매도"""
    acc_no, acc_prod = get_account_parts()
    tr_id = "TTTC0801U" if MODE == "실전" else "VTTC0801U"

    body = {
        "CANO": acc_no,
        "ACNT_PRDT_CD": acc_prod,
        "PDNO": stock_code,
        "ORD_DVSN": "01",  # 시장가
        "ORD_QTY": str(quantity),
        "ORD_UNPR": "0",
        "EXCG_ID_DVSN_CD": "KRX",
        "SLL_TYPE": "01",
    }
    try:
        data = _trade_post(
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id,
            body,
        )
    except Exception as e:
        if is_account_error(e):
            raise RuntimeError(f"{e}\n{account_error_hint()}") from e
        raise
    if data.get("rt_cd") != "0":
        msg = data.get("msg1", "알 수 없는 오류")
        print(f"[매도 API 오류] {stock_code}: {msg}")
    return data


def get_holdings() -> list[dict]:
    """보유 종목 조회"""
    acc_no, acc_prod = get_account_parts()
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
    return get_orderable_cash()


def get_orderable_cash() -> int:
    """주문 가능 현금 (매수가능금액 우선)"""
    acc_no, acc_prod = get_account_parts()
    tr_id = "TTTC8434R" if MODE == "실전" else "VTTC8434R"

    _trade_throttle()
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
    if data.get("rt_cd") not in (None, "0"):
        msg_cd = data.get("msg_cd", "")
        msg1 = data.get("msg1", "알 수 없는 오류")
        detail = f"{msg_cd}: {msg1}" if msg_cd else msg1
        err = RuntimeError(f"KIS 잔고 조회 오류: {detail}")
        if is_account_error(err):
            raise RuntimeError(f"{detail}\n{account_error_hint()}") from err
        raise err
    summary = data.get("output2", [{}])
    if isinstance(summary, list):
        summary = summary[0] if summary else {}

    for key in ("ord_psbl_cash", "nrcvb_buy_amt", "dnca_tot_amt"):
        try:
            val = int(summary.get(key, "0") or "0")
        except (ValueError, TypeError):
            val = 0
        if val > 0:
            return val
    return 0
