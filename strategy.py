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


def calc_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """RSI 계산 (closes: 오래된→최신 순서)"""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = statistics.mean(gains[:period])
    avg_loss = statistics.mean(losses[:period])
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_bollinger(closes: List[float], period: int = 20):
    """Bollinger Bands (upper, mid, lower). closes: 오래된→최신 순서"""
    if len(closes) < period:
        return None, None, None
    sma = statistics.mean(closes[-period:])
    std = statistics.stdev(closes[-period:])
    return sma + 2 * std, sma, sma - 2 * std


def count_consecutive_drops(closes: List[float]) -> int:
    """최근 연속 하락 일수 (closes: 오래된→최신 순서)"""
    count = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            count += 1
        else:
            break
    return count


def analyze_minute_data(minute_data: List[Dict]) -> Dict[str, Any]:
    """
    5분봉 데이터를 바탕으로 기술적 분석
    """
    if not minute_data or len(minute_data) < 5:
        return {"valid": False, "reason": "데이터 부족"}

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
    일봉 데이터 분석 + RSI, 볼린저밴드, 연속하락 추가
    """
    if not daily_data or len(daily_data) < 5:
        return {"valid": False, "reason": "일봉 데이터 부족"}

    # 최신순 정렬
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

    vol_rate = (latest_volume / prev_volume * 100) if prev_volume > 0 else 0
    is_positive = latest_close > prev_close

    # === 추가 지표 ===
    # RSI (시간순으로 변환)
    closes_chrono = [float(c) for c in reversed(closes)]
    rsi = calc_rsi(closes_chrono, 14)

    # 볼린저밴드
    bb_upper, bb_mid, bb_lower = calc_bollinger(closes_chrono, 20)

    # 연속 하락 일수
    consecutive_drops = count_consecutive_drops(closes_chrono)

    # 도지 캔들 (시가와 종가 차이 < 0.5%)
    is_doji = abs(latest_close - latest_open) / max(latest_open, 1) < 0.005

    # 반전 신호: N일 연속 하락 후 오늘 양봉 또는 도지
    reversal_signal = consecutive_drops >= 3 and (is_positive or is_doji)

    return {
        "valid": True,
        "latest_close": latest_close,
        "prev_close": prev_close,
        "is_positive": is_positive,
        "is_doji": is_doji,
        "latest_trading_value": latest_value,
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "price_above_ma5": latest_close > ma5 if ma5 else False,
        "price_above_ma20": latest_close > ma20 if ma20 else False,
        "ma_bullish": ma5 > ma20 if ma5 and ma20 else False,
        "vol_rate": vol_rate,
        # 새 지표
        "rsi": rsi,
        "bb_upper": bb_upper,
        "bb_mid": bb_mid,
        "bb_lower": bb_lower,
        "consecutive_drops": consecutive_drops,
        "reversal_signal": reversal_signal,
    }


def should_buy(price_info: Dict, minute_analysis: Dict, daily_analysis: Dict, budget: int) -> tuple:
    """
    매수 판정: 종산TV 3대 기법을 결합 (상승장용)
    """
    if not minute_analysis.get("valid") or not daily_analysis.get("valid"):
        return False, 0, "분석 데이터 부족"

    reasons = []
    current_price = price_info.get("current_price", 0)
    if current_price <= 0:
        return False, 0, "가격 정보 없음"

    # === 1. 종가베팅 필터 ===
    prev_close = daily_analysis["prev_close"]
    gap_pct = (current_price - prev_close) / prev_close * 100 if prev_close else 0

    if daily_analysis["latest_trading_value"] < 10_000_000_000:
        return False, 0, f"거래대금 부족 ({daily_analysis['latest_trading_value'] / 1_000_000_000:.0f}억)"

    if not daily_analysis["is_positive"]:
        if daily_analysis.get("vol_rate", 0) < 150:
            return False, 0, "음봉 (vol_rate < 150%)"

    reasons.append("거래대금 100억 이상")
    if daily_analysis["is_positive"]:
        reasons.append("양봉")
    else:
        reasons.append(f"음봉+vol_rate {daily_analysis.get('vol_rate', 0):.0f}%")

    # === 2. 이평선 + 거래대금 필터 ===
    if not minute_analysis["price_above_ma5"]:
        return False, 0, "5봉선 하위"
    if not minute_analysis["price_above_ma20"]:
        return False, 0, "20봉선 하위"
    if not minute_analysis["golden_cross"]:
        return False, 0, "골든크로스 아님"

    reasons.append("이평선 양선 유지")

    if minute_analysis["volume_spike"]:
        reasons.append("거래대금 터짐")

    # === 3. 돌파/눌림목 ===
    pullback = minute_analysis["pullback_pct"]
    bounce = minute_analysis["bounce_pct"]

    if -8 < pullback < -2:
        reasons.append(f"눌림목 반등 ({pullback:.1f}%)")
    elif bounce > 1:
        reasons.append(f"저점 돌파 반등 ({bounce:.1f}%)")

    if gap_pct > 20:
        return False, 0, f"과도한 급등 ({gap_pct:.1f}%)"

    max_qty = int(budget * 0.95 / current_price)
    quantity = max(1, max_qty)

    return True, quantity, "[상승] " + "; ".join(reasons)


def should_buy_oversold(price_info: Dict, minute_analysis: Dict, daily_analysis: Dict, budget: int) -> tuple:
    """
    매수 판정: 과매도 반등 + 눌림목 강화 (하락장용)
    조건:
    - RSI < 30 (과매도)
    - 거래대금 50억 이상
    - 반등 신호 (거래량 2배 OR BB 하단 터치 OR 3일+ 하락 후 반전)
    """
    if not minute_analysis.get("valid") or not daily_analysis.get("valid"):
        return False, 0, "분석 데이터 부족"

    current_price = price_info.get("current_price", 0)
    if current_price <= 0:
        return False, 0, "가격 정보 없음"

    reasons = []

    # === 1. RSI 과매도 (필수) ===
    rsi = daily_analysis.get("rsi")
    if rsi is None or rsi >= 30:
        return False, 0, f"RSI 과매도 아님 ({rsi:.1f})" if rsi else "RSI 데이터 없음"
    reasons.append(f"RSI 과매도 ({rsi:.1f})")

    # === 2. 거래대금 50억 이상 ===
    if daily_analysis["latest_trading_value"] < 5_000_000_000:
        return False, 0, f"거래대금 부족 ({daily_analysis['latest_trading_value'] / 1_000_000_000:.0f}억)"
    reasons.append(f"거래대금 {daily_analysis['latest_trading_value'] / 1_000_000_000:.0f}억")

    # === 3. 반등 신호 (최소 1개 필요) ===
    bounce_signals = 0

    # 3a. 거래량 2배 이상 급증
    vol_rate = daily_analysis.get("vol_rate", 0)
    if vol_rate >= 200:
        bounce_signals += 1
        reasons.append(f"거래량 급증 ({vol_rate:.0f}%)")

    # 3b. 볼린저밴드 하단 터치
    bb_lower = daily_analysis.get("bb_lower")
    if bb_lower and current_price <= bb_lower:
        bounce_signals += 1
        reasons.append("BB 하단 터치")

    # 3c. 3일+ 연속 하락 후 반전 (양봉/도지)
    if daily_analysis.get("reversal_signal"):
        drop_days = daily_analysis.get("consecutive_drops", 0)
        bounce_signals += 1
        reasons.append(f"{drop_days}일 하락 후 반전")

    # 3d. 분봉 거래량 터짐 (추가 가점)
    if minute_analysis.get("volume_spike"):
        bounce_signals += 1
        reasons.append("분봉 거래량 터짐")

    if bounce_signals == 0:
        return False, 0, "반등 신호 없음 (vol<2배, BB 위, 반전X)"

    # === 4. 과도한 급등 제외 ===
    prev_close = daily_analysis["prev_close"]
    gap_pct = (current_price - prev_close) / prev_close * 100 if prev_close else 0
    if gap_pct > 20:
        return False, 0, f"과도한 급등 ({gap_pct:.1f}%)"

    # 수량 계산
    max_qty = int(budget * 0.95 / current_price)
    quantity = max(1, max_qty)

    return True, quantity, "[과매도반등] " + "; ".join(reasons)


def should_sell(position: Dict, price_info: Dict, minute_analysis: Dict,
                stop_loss_pct: float = -2.5, take_profit_pct: float = 3.0) -> tuple:
    """
    매도 판정: 수익실현 또는 손절
    """
    avg_price = position.get("avg_price", 0)
    current_price = price_info.get("current_price", 0)
    hold_qty = position.get("quantity", 0)

    if avg_price <= 0 or hold_qty <= 0:
        return False, 0, ""

    profit_pct = (current_price - avg_price) / avg_price * 100

    # 수익실현 달성
    if profit_pct >= take_profit_pct:
        return True, hold_qty, f"수익실현 달성 ({profit_pct:+.2f}%)"

    # 손절 라인
    if profit_pct <= stop_loss_pct:
        return True, hold_qty, f"손절 라인 터치 ({profit_pct:+.2f}%)"

    # 5봉선 이하 하락: 추세 약화 시 방어
    if minute_analysis.get("valid") and not minute_analysis.get("price_above_ma5", True):
        return True, hold_qty, "5봉선 이하 하락 (추세 약화)"

    return False, 0, ""
