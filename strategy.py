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
    """5분봉 데이터를 바탕으로 기술적 분석 (KIS API 또는 자체 기록)"""
    if not minute_data or len(minute_data) < 5:
        return {"valid": False, "reason": "데이터 부족"}

    # 자체 기록 형식 {"price": ..., "volume": ...} 또는 KIS 형식 모두 지원
    closes = []
    volumes = []
    for item in minute_data:
        if "price" in item:
            closes.append(int(item["price"]))
            volumes.append(int(item.get("volume", 0)))
        elif "stck_prpr" in item:
            closes.append(int(item["stck_prpr"]))
            volumes.append(int(item.get("cntg_vol", 0)))
        else:
            continue

    if len(closes) < 5:
        return {"valid": False, "reason": "유효 데이터 부족"}

    # 오래된 → 최신순 정렬 (자체 기록은 이미 정렬됨)
    if minute_data and "stck_prpr" in minute_data[0]:
        closes.reverse()
        volumes.reverse()

    ma5 = calc_sma(closes, 5)
    ma10 = calc_sma(closes, 10)
    ma20 = calc_sma(closes, 20)

    vol_ma5 = calc_sma(volumes, 5)
    vol_ma20 = calc_sma(volumes, 20)

    latest_close = closes[-1]
    prev_close = closes[-2] if len(closes) >= 2 else latest_close
    latest_volume = volumes[-1] if volumes else 0

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
        "volume_spike": latest_volume > vol_ma5 * 1.5 if vol_ma5 and latest_volume else False,
        "recent_low": recent_low,
        "recent_high": recent_high,
        "pullback_pct": (latest_close - recent_high) / recent_high * 100 if recent_high else 0,
        "bounce_pct": (latest_close - recent_low) / recent_low * 100 if recent_low else 0,
    }


def analyze_daily_data(daily_data: List[Dict]) -> Dict[str, Any]:
    """일봉 데이터 분석 (KIS API 또는 자체 기록)"""
    if not daily_data or len(daily_data) < 5:
        return {"valid": False, "reason": "일봉 데이터 부족"}

    # 자체 기록 형식 또는 KIS 형식 지원
    closes = []
    opens = []
    volumes = []
    trading_values = []

    for item in daily_data:
        if "close" in item:
            closes.append(int(item["close"]))
            opens.append(int(item.get("open", item["close"])))
            volumes.append(int(item.get("volume", 0)))
            trading_values.append(int(item.get("trading_value", 0)))
        elif "stck_clpr" in item:
            closes.append(int(item["stck_clpr"]))
            opens.append(int(item.get("stck_oprc", 0)))
            volumes.append(int(item.get("acml_vol", 0)))
            trading_values.append(int(item.get("acml_tr_pbmn", 0)))
        else:
            continue

    if len(closes) < 5:
        return {"valid": False, "reason": "유효 데이터 부족"}

    # 최신순 (index 0 = 최신)
    closes_desc = list(reversed(closes))
    opens_desc = list(reversed(opens))

    ma5 = calc_sma(closes_desc, 5)
    ma20 = calc_sma(closes_desc, 20)
    ma60 = calc_sma(closes_desc, 60)

    latest_close = closes_desc[0]
    latest_open = opens_desc[0]
    latest_value = trading_values[-1] if trading_values else 0
    prev_close = closes_desc[1] if len(closes_desc) > 1 else latest_close
    prev_volume = volumes[-2] if len(volumes) >= 2 else 0
    latest_volume = volumes[-1] if volumes else 0

    vol_rate = (latest_volume / prev_volume * 100) if prev_volume > 0 else 0
    is_positive = latest_close > prev_close

    # RSI
    closes_chrono = [float(c) for c in closes]  # 오래된→최신 (원본 순서)
    rsi = calc_rsi(closes_chrono, 14)

    # 볼린저밴드
    bb_upper, bb_mid, bb_lower = calc_bollinger(closes_chrono, 20)

    # 연속 하락
    consecutive_drops = count_consecutive_drops(closes_chrono)

    # 도지
    is_doji = abs(latest_close - latest_open) / max(latest_open, 1) < 0.005

    # 반전 신호
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
        "rsi": rsi,
        "bb_upper": bb_upper,
        "bb_mid": bb_mid,
        "bb_lower": bb_lower,
        "consecutive_drops": consecutive_drops,
        "reversal_signal": reversal_signal,
    }


def analyze_price_simple(price_info: Dict) -> Dict[str, Any]:
    """현재가 정보만으로 간단 분석 (분봉/일봉 없을 때 사용)"""
    current_price = price_info.get("current_price", 0)
    prev_close = price_info.get("prev_close", 0)
    open_price = price_info.get("open_price", 0)
    change_rate = price_info.get("change_rate", 0)
    volume = price_info.get("volume", 0)
    trading_value = price_info.get("trading_value", 0)

    if current_price <= 0:
        return {"valid": False, "reason": "가격 없음"}

    return {
        "valid": True,
        "current_price": current_price,
        "prev_close": prev_close,
        "open_price": open_price,
        "change_rate": change_rate,
        "volume": volume,
        "trading_value": trading_value,
        "is_positive": current_price > prev_close if prev_close > 0 else None,
        "gap_from_open": (current_price - open_price) / open_price * 100 if open_price > 0 else 0,
        "daily_change_pct": change_rate,
    }


def should_buy(price_info: Dict, minute_analysis: Dict, daily_analysis: Dict, budget: int) -> tuple:
    """
    매수 판정: 상승장 기법 (분석 데이터 있을 때)
    """
    if not minute_analysis.get("valid") or not daily_analysis.get("valid"):
        return False, 0, "분석 데이터 부족"

    reasons = []
    current_price = price_info.get("current_price", 0)
    if current_price <= 0:
        return False, 0, "가격 정보 없음"

    # === 1. 거래대금 필터 ===
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

    prev_close = daily_analysis["prev_close"]
    gap_pct = (current_price - prev_close) / prev_close * 100 if prev_close else 0
    if gap_pct > 20:
        return False, 0, f"과도한 급등 ({gap_pct:.1f}%)"

    max_qty = int(budget * 0.95 / current_price)
    quantity = max(1, max_qty)

    return True, quantity, "[상승] " + "; ".join(reasons)


def should_buy_simple(price_info: Dict, budget: int) -> tuple:
    """
    매수 판정: 간단 버전 (분봉/일봉 없을 때 - 모의투자 폴백)
    조건:
    - 상승률 +1% ~ +10% (적당한 상승)
    - 거래대금 50억 이상
    - 시가 대비 양수
    """
    current_price = price_info.get("current_price", 0)
    if current_price <= 0:
        return False, 0, "가격 없음"

    change_rate = price_info.get("change_rate", 0)
    trading_value = price_info.get("trading_value", 0)
    open_price = price_info.get("open_price", 0)
    prev_close = price_info.get("prev_close", 0)

    reasons = []

    # 상승률 체크
    if change_rate < 1.0:
        return False, 0, f"상승률 낮음 ({change_rate:+.1f}%)"
    if change_rate > 10.0:
        return False, 0, f"과급등 위험 ({change_rate:+.1f}%)"
    reasons.append(f"상승률 {change_rate:+.1f}%")

    # 거래대금 체크
    if trading_value < 5_000_000_000:
        return False, 0, f"거래대금 부족 ({trading_value / 1_000_000_000:.0f}억)"
    reasons.append(f"거래대금 {trading_value / 1_000_000_000:.0f}억")

    # 시가 대비 상승 (장중 흐름 확인)
    gap_from_open = (current_price - open_price) / open_price * 100 if open_price > 0 else 0
    if gap_from_open < -1.0:
        return False, 0, f"시가 대비 하락 ({gap_from_open:+.1f}%)"
    reasons.append(f"시가대비 {gap_from_open:+.1f}%")

    max_qty = int(budget * 0.95 / current_price)
    if max_qty <= 0:
        return False, 0, f"예산 부족 (가격: {current_price:,}원)"
    quantity = max(1, max_qty)

    return True, quantity, "[간이] " + "; ".join(reasons)


def should_buy_oversold(price_info: Dict, minute_analysis: Dict, daily_analysis: Dict, budget: int) -> tuple:
    """
    매수 판정: 과매도 반등 (하락장용)
    """
    if not minute_analysis.get("valid") or not daily_analysis.get("valid"):
        return False, 0, "분석 데이터 부족"

    current_price = price_info.get("current_price", 0)
    if current_price <= 0:
        return False, 0, "가격 정보 없음"

    reasons = []

    rsi = daily_analysis.get("rsi")
    if rsi is None or rsi >= 30:
        return False, 0, f"RSI 과매도 아님 ({rsi:.1f})" if rsi else "RSI 데이터 없음"
    reasons.append(f"RSI 과매도 ({rsi:.1f})")

    if daily_analysis["latest_trading_value"] < 5_000_000_000:
        return False, 0, f"거래대금 부족 ({daily_analysis['latest_trading_value'] / 1_000_000_000:.0f}억)"
    reasons.append(f"거래대금 {daily_analysis['latest_trading_value'] / 1_000_000_000:.0f}억")

    bounce_signals = 0
    vol_rate = daily_analysis.get("vol_rate", 0)
    if vol_rate >= 200:
        bounce_signals += 1
        reasons.append(f"거래량 급증 ({vol_rate:.0f}%)")

    bb_lower = daily_analysis.get("bb_lower")
    if bb_lower and current_price <= bb_lower:
        bounce_signals += 1
        reasons.append("BB 하단 터치")

    if daily_analysis.get("reversal_signal"):
        drop_days = daily_analysis.get("consecutive_drops", 0)
        bounce_signals += 1
        reasons.append(f"{drop_days}일 하락 후 반전")

    if minute_analysis.get("volume_spike"):
        bounce_signals += 1
        reasons.append("분봉 거래량 터짐")

    if bounce_signals == 0:
        return False, 0, "반등 신호 없음"

    prev_close = daily_analysis["prev_close"]
    gap_pct = (current_price - prev_close) / prev_close * 100 if prev_close else 0
    if gap_pct > 20:
        return False, 0, f"과도한 급등 ({gap_pct:.1f}%)"

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

    if profit_pct >= take_profit_pct:
        return True, hold_qty, f"수익실현 달성 ({profit_pct:+.2f}%)"

    if profit_pct <= stop_loss_pct:
        return True, hold_qty, f"손절 라인 터치 ({profit_pct:+.2f}%)"

    # 5봉선 이하 하락 (분석 데이터 있을 때만)
    if minute_analysis.get("valid") and not minute_analysis.get("price_above_ma5", True):
        return True, hold_qty, "5봉선 이하 하락 (추세 약화)"

    return False, 0, ""
