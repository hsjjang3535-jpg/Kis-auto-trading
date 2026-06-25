"""
동적 종목 선정 시스템 (종산 매매법)

선정 풀:
  1. 코스피 거래대금 상위 30개
  2. 코스닥 거래대금 상위 30개
  3. 테마주 (AI, 반도체, 2차전지) 중 당일 상승률 2% 이상

필터:
  - ETF 제외
  - 거래대금 100억 이상
  - 하락 종목 제외
  - 최소 가격 1,000원 이상

기술 조건 (종산 매매법):
  - 상단매매: 5일선 위, 200일 신고가 98%, 거래량 200%↑, 윗꼬리 30%↓
  - 하단매매: 20일선 아래, 5일선 위, RSI≤30, 거래량 150%↑, 52주고가 20%이내
  - 돌파매매: 20일 최고가 돌파, 5일선 위, 거래량 200%↑, 윗꼬리 30%↓

최종 15개 선정 (상단 > 돌파 > 하단 순)
"""
import os
import time
import kis_api

MIN_TRADING_VALUE = 10_000_000_000   # 100억
MIN_PRICE = 1_000                    # 최소 주가 1,000원
MAX_FINAL = int(os.getenv("MAX_WATCHLIST", "15"))   # 최종 워치리스트 수

# ETF 이름 필터 (포함 시 제외)
_ETF_KEYWORDS = [
    "KODEX", "TIGER", "KBSTAR", "HANARO", "ARIRANG", "KOSEF",
    "FOCUS", "TIMEFOLIO", "KTOP", "SOL", "ACE", "MASTER",
    "ETF", "레버리지", "인버스",
]

# 테마주 종목코드 (AI·반도체·2차전지 주요 종목)
_THEME_STOCKS = {
    "AI·소프트웨어": [
        "030800",  # 삼성SDS
        "035420",  # NAVER
        "035720",  # 카카오
        "259960",  # 크래프톤
        "263750",  # 펄어비스
        "293490",  # 카카오게임즈
        "042700",  # 한미반도체
        "240810",  # 원익IPS
    ],
    "반도체": [
        "005930",  # 삼성전자
        "000660",  # SK하이닉스
        "042700",  # 한미반도체
        "036830",  # 솔브레인홀딩스
        "240810",  # 원익IPS
        "357780",  # 솔브레인
        "336370",  # 솔루에타
        "112610",  # 씨에스윈드
        "071050",  # 한국금융지주
        "038540",  # 에스에너지
    ],
    "2차전지": [
        "006400",  # 삼성SDI
        "051910",  # LG화학
        "373220",  # LG에너지솔루션
        "247540",  # 에코프로비엠
        "086520",  # 에코프로
        "402340",  # SK스페셜티
        "000270",  # 기아
        "005380",  # 현대차
        "011790",  # SKC
        "096770",  # SK이노베이션
    ],
}


def _is_etf(name: str) -> bool:
    """ETF 여부 판단"""
    name_upper = name.upper()
    return any(kw.upper() in name_upper for kw in _ETF_KEYWORDS)


def _get_trading_value(stock: dict) -> int:
    try:
        return int(stock.get("acml_tr_pbmn", 0))
    except (ValueError, TypeError):
        return 0


def _fetch_market_stocks(market: str, label: str, top_n: int = 30) -> list[dict]:
    """코스피 또는 코스닥 거래대금 상위 종목 조회 (재시도 포함)"""
    for attempt in range(3):
        try:
            stocks = kis_api.get_top_trading_value(top_n, market=market)
            if stocks:
                print(f"  [{label}] {len(stocks)}개 조회 완료")
                return stocks
        except Exception as e:
            print(f"  [{label}] 조회 실패 ({attempt+1}/3): {e}")
            if attempt < 2:
                time.sleep(10)
    print(f"  [{label}] 3회 모두 실패")
    return []


def _fetch_theme_stocks() -> list[dict]:
    """테마주 중 당일 상승률 2% 이상 종목 조회"""
    theme_results = []
    checked = set()
    for theme, codes in _THEME_STOCKS.items():
        for code in codes:
            if code in checked:
                continue
            checked.add(code)
            try:
                info = kis_api.get_stock_info(code)
                rate = float(info.get("prdy_ctrt", "0"))
                if rate >= 2.0:
                    theme_results.append({
                        "mksc_shrn_iscd": code,
                        "hts_kor_isnm": info.get("hts_kor_isnm", code),
                        "acml_tr_pbmn": info.get("acml_tr_pbmn", "0"),
                        "prdy_ctrt": str(rate),
                        "_theme": theme,
                    })
                time.sleep(0.2)
            except Exception:
                pass
    print(f"  [테마주] 상승 2%↑ {len(theme_results)}개")
    return theme_results


def _apply_technical_filter(stocks: list[dict]) -> tuple[list, list, list]:
    """기술적 조건 필터링 → (상단, 돌파, 하단) 후보 반환"""
    upper, breakout, lower = [], [], []
    seen_codes = set()

    for stock in stocks:
        code = stock.get("mksc_shrn_iscd", "")
        name = stock.get("hts_kor_isnm", "")

        if not code or code in seen_codes:
            continue
        seen_codes.add(code)

        # ETF 제외
        if _is_etf(name):
            continue

        # 거래대금 100억 미만 제외
        trading_value = _get_trading_value(stock)
        if trading_value < MIN_TRADING_VALUE:
            continue

        # 등락률
        try:
            rate = float(stock.get("prdy_ctrt", "0"))
        except ValueError:
            rate = 0.0

        if rate < 0:
            continue

        # 차트 지표 조회
        try:
            ind = kis_api.get_chart_indicators(code)
            time.sleep(0.3)
        except Exception as e:
            print(f"  ⚠️ {name}({code}) 차트 실패: {e}")
            continue

        if not ind:
            continue

        current = ind["current"]
        if current < MIN_PRICE:
            continue

        ma5        = ind["ma5"]
        ma20       = ind["ma20"]
        high_200   = ind["high_200"]
        high_20    = ind.get("high_20", high_200)
        vol_ratio  = ind["vol_ratio"]
        upper_tail = ind["upper_tail_ratio"]
        rsi        = ind.get("rsi", 50.0)

        # 52주 신고가
        try:
            info = kis_api.get_stock_info(code)
            w52_high = float(info.get("w52_hgpr", 0))
            w52_gap  = (w52_high - current) / w52_high * 100 if w52_high > 0 else 100
        except Exception:
            w52_gap = 100

        base = {
            "code": code, "name": name,
            "trading_value": trading_value,
            "change_rate": rate, "current": current,
            "ma5": ma5, "ma20": ma20,
            "high_200": high_200, "high_20": high_20,
            "vol_ratio": vol_ratio, "upper_tail": upper_tail,
            "rsi": rsi,
            "source": stock.get("_theme", "거래대금"),
        }

        upper_ok = (
            current >= ma5 and
            current >= high_200 * 0.98 and
            vol_ratio >= 2.0 and
            upper_tail <= 0.3
        )
        lower_ok = (
            current < ma20 and current >= ma5 and
            w52_gap <= 20 and vol_ratio >= 1.5 and rsi <= 30
        )
        breakout_ok = (
            high_20 > 0 and current >= high_20 * 0.995 and
            current >= ma5 and vol_ratio >= 2.0 and
            upper_tail <= 0.3 and not upper_ok
        )

        if upper_ok:
            upper.append({**base, "strategy": "상단매매"})
            print(f"  🔴 상단: {name}({code}) {rate:+.1f}% 거래량{vol_ratio:.1f}x [{base['source']}]")
        elif breakout_ok:
            breakout.append({**base, "strategy": "돌파매매"})
            print(f"  🟡 돌파: {name}({code}) {rate:+.1f}% 20일고가돌파 [{base['source']}]")
        elif lower_ok:
            lower.append({**base, "strategy": "하단매매"})
            print(f"  🔵 하단: {name}({code}) {rate:+.1f}% RSI{rsi:.0f} [{base['source']}]")

    return upper, breakout, lower


def screen_closing_bet_candidates(top_n: int = 20) -> list[dict]:
    """종가베팅 후보 선정 (14:00 스크리닝)

    조건:
    - 당일 상승률 2% 이상
    - 5일선 위 (상승 추세)
    - 거래량 2배 이상 (모멘텀 확인)
    - RSI 40~75 (적정 모멘텀, 과열 아님)
    - 최대 5개 선정
    """
    print("\n[종가베팅 스크리너] 후보 선정 시작")

    kospi  = _fetch_market_stocks("0001", "코스피(종가)", top_n)
    time.sleep(0.5)
    kosdaq = _fetch_market_stocks("1001", "코스닥(종가)", top_n)

    all_stocks: list[dict] = []
    seen: set[str] = set()
    for s in kospi + kosdaq:
        code = s.get("mksc_shrn_iscd", "")
        if code and code not in seen:
            seen.add(code)
            all_stocks.append(s)

    candidates = []
    for stock in all_stocks:
        code = stock.get("mksc_shrn_iscd", "")
        name = stock.get("hts_kor_isnm", "")

        if not code or _is_etf(name):
            continue
        if _get_trading_value(stock) < MIN_TRADING_VALUE:
            continue

        try:
            rate = float(stock.get("prdy_ctrt", "0"))
        except ValueError:
            rate = 0.0

        if rate < 2.0:  # 당일 2% 이상 상승만
            continue

        try:
            ind = kis_api.get_chart_indicators(code)
            time.sleep(0.3)
        except Exception as e:
            print(f"  ⚠️ {name}({code}) 차트 실패: {e}")
            continue

        if not ind:
            continue

        current = ind["current"]
        if current < MIN_PRICE:
            continue

        ma5 = ind["ma5"]
        vol_ratio = ind["vol_ratio"]
        rsi = ind.get("rsi", 50.0)

        if current >= ma5 and vol_ratio >= 2.0 and 40 <= rsi <= 75:
            candidates.append({
                "code": code,
                "name": name,
                "change_rate": rate,
                "current": current,
                "ma5": ma5,
                "vol_ratio": vol_ratio,
                "rsi": rsi,
                "strategy": "종가베팅",
            })
            print(f"  🌙 종가베팅: {name}({code}) {rate:+.1f}% RSI{rsi:.0f} 거래량{vol_ratio:.1f}x")

    candidates.sort(key=lambda x: x["change_rate"], reverse=True)
    result = candidates[:5]
    print(f"\n[종가베팅 스크리너 완료] 최종 {len(result)}개 선정")
    return result


def screen_candidates(top_n: int = 30) -> list[dict]:
    """동적 종목 선정 (코스피30 + 코스닥30 + 테마주 → 최종 15개)"""
    print(f"\n[스크리너] 동적 종목 선정 시작")

    # 1. 코스피 + 코스닥 + 테마주 수집
    kospi  = _fetch_market_stocks("0001", "코스피", top_n)
    time.sleep(0.5)
    kosdaq = _fetch_market_stocks("1001", "코스닥", top_n)
    time.sleep(0.5)
    theme  = _fetch_theme_stocks()

    # 중복 제거 후 통합 (코스피 → 코스닥 → 테마 순)
    all_stocks: list[dict] = []
    seen = set()
    for s in kospi + kosdaq + theme:
        code = s.get("mksc_shrn_iscd", "")
        if code and code not in seen:
            seen.add(code)
            all_stocks.append(s)

    print(f"[스크리너] 총 {len(all_stocks)}개 후보 (코스피{len(kospi)} + 코스닥{len(kosdaq)} + 테마{len(theme)})")

    if not all_stocks:
        raise RuntimeError("종목 조회 실패 - 모든 소스에서 빈 결과")

    # 2. 기술적 조건 필터링
    upper, breakout, lower = _apply_technical_filter(all_stocks)

    # 3. 우선순위 정렬 후 최종 MAX_FINAL개 선정
    combined = upper + breakout + lower
    final = combined[:MAX_FINAL]

    print(
        f"\n[스크리너 완료] 상단 {len(upper)}개 / 돌파 {len(breakout)}개 / 하단 {len(lower)}개 "
        f"→ 최종 {len(final)}개 선정"
    )
    return final
