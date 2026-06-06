from typing import List, Dict, Any, Optional
from datetime import datetime, timezone, timedelta
import statistics


def calc_sma(values: List[float], period: int) -> Optional[float]:
    """Simple Moving Average"""
    if len(values) < period:
        return None
    return statistics.mean(values[-period:])


def calc_ema(values: List[float], period: int) -> Optional[float]:
    """Exponential Moving Average"""
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = values[0]
    for price in values[1:]:
        ema = (price - ema) * multiplier + ema
    return ema


def analyze_minute_data(minute_data: List[Dict]) -> Dict[str, Any]:
    """
    5분봉 데이터를 바탕으로 기술적 분석
    minute_data: [{stck_bsop_date, stck_cntg_hour, stck_prpr, stck_oprc, stck_hgpr, stck_lwpr, cntg_vol}, ...]
    """
    if not minute_data or len(minute_data) < 5:
        return {"valid": False, "reason": "데이터 부조"}

    # 가격 리스트 (최신순)
    closes = [int(item.get("stck_prpr", 0)) for item in minute_data]
    closes.reverse()  # 오래된 → 최신순

    volumes = [int(item.get("cntg_vol", 0)) for item in minute_data]
    volumes.reverse()

    ma5 = calc_sma(closes, 5)
    ma10 = calc_sma(closes, 10)
    ma20 = calc_sma(closes, 20)

    vol_ma5 = calc_sma(volumes, 5)
    vol_ma20 = calc_sma(volumes, 20)

    latest_close = closes[-1]
    prev_close = closes[-2]
    latest_volume = volumes[-1]

    # 역배열: 최신순
    recent_closes = closes[-10:]
    recent_low = min(recent_closes)
    recent_high = max(recent_closes)

    return {
        "valid": True,
        "latest_close": latest_close,
        "prev_close": prev_close,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "price_above_ma5": latest_close > ma5 if ma5 else False,
        "price_above_ma20": latest_close > ma20 if ma20 else False,
        "golden_cross": ma5 > ma20 if ma5 and ma20 else False,
        "latest_volume": latest_volume,
        "vol_ma5": vol_ma5,
        "vol_ma20": vol_ma20,
        "volume_spike": latest_volume > vol_ma5 * 1.5 if vol_ma5 else False,
        "recent_low": recent_low,
        "recent_high": recent_high,
        "pullback_pct": (latest_close - recent_high) / recent_high * 100 if recent_high else 0,
        "bounce_pct": (latest_close - recent_low) / recent_low * 100 if recent_low else 0,
    }


def analyze_daily_data(daily_data: List[Dict]) -> Dict[str, Any]:
    """
    일등봉 데이터를 바탕으로 종가베팅 분석
    daily_data: [{stck_bsop_date, stck_clpr, stck_oprc, stck_hgpr, stck_lwpr, acml_vol, acml_tr_pbmn}, ...]
    """
    if not daily_data or len(daily_data) < 5:
        return {"valid": False, "reason": "일등봉 데이터 부조"}

    # 최신숝 정렬
    daily_data.sort(key=lambda x: x.get("stck_bsop_date", ""), reverse=True)

    closes = [int(item.get("stck_clpr", 0)) for item in daily_data]
    opens = [int(item.get("stck_oprc", 0)) for item in daily_data]
    volumes = [int(item.get("acml_vol", 0)) for item in daily_data]
    trading_values = [int(item.get("acml_tr_pbmn", 0)) for item in daily_data]

    ma5 = calc_sma(closes, 5)
    ma20 = calc_sma(closes, 20)
    ma60 = calc_sma(closes, 60)

    latest_close = closes[0]
    latest_open = opens[0]
    latest_value = trading_values[0]
    prev_close = closes[1] if len(closes) > 1 else latest_close
    prev_volume = volumes[1] if len(volumes) > 1 else 0
    latest_volume = volumes[0]

    # vol_rate: 전일대비 거래량 비율
    vol_rate = (latest_volume / prev_volume * 100) if prev_volume > 0 else 0

    # 양봉/음봉
    is_positive = latest_close > prev_close

    return {
        "valid": True,
        "latest_close": latest_close,
        "prev_close": prev_close,
        "is_positive": is_positive,
        "latest_trading_value": latest_value,
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "price_above_ma5": latest_close > ma5 if ma5 else False,
        "price_above_ma20": latest_close > ma20 if ma20 else False,
        "ma_bullish": ma5 > ma20 if ma5 and ma20 else False,
        "vol_rate": vol_rate,
    }


def should_buy(price_info: Dict, minute_analysis: Dict, daily_analysis: Dict, budget: int) -> tuple:
    """
    매수 판정: 종산TV 3대 기법을 결합
    반환: (should_buy: bool, quantity: int, reason: str)
    """
    if not minute_analysis.get("valid") or not daily_analysis.get("valid"):
        return False, 0, "분석 데이터 부조"

    reasons = []
    current_price = price_info.get("current_price", 0)
    if current_price <= 0:
        return False, 0, "가격 정보 없음"

    # === 1. 종가베팅 필터 ===
    # 전일 종가 대비 현재가 저렴하거나 도담선이면 선별
    prev_close = daily_analysis["prev_close"]
    gap_pct = (current_price - prev_close) / prev_close * 100 if prev_close else 0

    # 거래대금 100억 이상 필수
    if daily_analysis["latest_trading_value"] < 10_000_000_000:
        return False, 0, f"거래대금 부족 ({daily_analysis['latest_trading_value'] / 1_000_000_000:.0f}억)"

    # 양봉/음봉 + vol_rate 체크
    if not daily_analysis["is_positive"]:
        if daily_analysis.get("vol_rate", 0) < 150:
            return False, 0, "음봉 (vol_rate < 150%)"

    reasons.append("거래대금 100억 이상")
    if daily_analysis["is_positive"]:
        reasons.append("양봉")
    else:
        reasons.append(f"음봉+vol_rate {daily_analysis.get('vol_rate',0):.0f}%")

    # === 2. 이평선 + 거래대금 필터 ===
    if not minute_analysis["price_above_ma5"]:
        return False, 0, "5봉선 하위"
    if not minute_analysis["price_above_ma20"]:
        return False, 0, "20봉선 하위"
    if not minute_analysis["golden_cross"]:
        return False, 0, "데드구스 아님"

    reasons.append("이평선 양선 유지")

    # 거래대금 터짐
    if not minute_analysis["volume_spike"]:
        # 거래대금 터짐은 강제 조건은 아니고 가점 요인
        pass
    else:
        reasons.append("거래대금 터짐")

    # === 3. 돌파/눈름목 ===
    # 최근 저점에서 반등한 경우 = 눈름목 매수 타이밍
    pullback = minute_analysis["pullback_pct"]
    bounce = minute_analysis["bounce_pct"]

    # 최근 고점대비 -3% ~ -8% 눈름목에서 반등
    if -8 < pullback < -2:
        reasons.append(f"눈름목 반등 ({pullback:.1f}%)")
    elif bounce > 1:
        reasons.append(f"저점 돌파 반등 ({bounce:.1f}%)")
    else:
        # 돌파/눈름목 패턴이 부저하면 방어
        pass

    # 매수 거래: 전일 종가 대비 과도한 과다 급등 제외
    if gap_pct > 20:
        return False, 0, f"과도한 급등 ({gap_pct:.1f}%)"

    # 수량 계산: 방역 최대 예산 / 현재가의 95%
    max_qty = int(budget * 0.95 / current_price)
    quantity = max(1, max_qty)

    return True, quantity, "; ".join(reasons)


def should_sell(position: Dict, price_info: Dict, minute_analysis: Dict, stop_loss_pct: float = -2.5, take_profit_pct: float = 2.0) -> tuple:
    """
    매도 판정: 수익부 또는 손절 만족 시
    반환: (should_sell: bool, quantity: int, reason: str)
    """
    avg_price = position.get("avg_price", 0)
    current_price = price_info.get("current_price", 0)
    hold_qty = position.get("quantity", 0)

    if avg_price <= 0 or hold_qty <= 0:
        return False, 0, ""

    profit_pct = (current_price - avg_price) / avg_price * 100

    # 수익부 달성
    if profit_pct >= take_profit_pct:
        return True, hold_qty, f"수익부 달성 ({profit_pct:+.2f}%)"

    # 손절 라인
    if profit_pct <= stop_loss_pct:
        return True, hold_qty, f"손절 라인 추진 ({profit_pct:+.2f}%)"

    # 5봉선 이하 도람: 추세 약화 시 방어
    if minute_analysis.get("valid") and not minute_analysis.get("price_above_ma5", True):
        return True, hold_qty, "5봉선 이하 도람 (추세 약화)"

    return False, 0, ""
