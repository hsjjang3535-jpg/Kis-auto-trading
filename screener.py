"""
종산 매매법 스크리너

상단매매 조건:
  - 거래대금 100억 이상
  - 현재가 5일 이동평균선 위
  - 200일 신고가 돌파 (현재가 >= 200일 최고가의 98%)
  - 거래량 최근 5일 평균 대비 200% 이상
  - 윗꼬리 비율 30% 이하 (종가가 고가권)

하단매매 조건:
  - 거래대금 100억 이상
  - 현재가 20일 이동평균선 아래 (눌림목)
  - 현재가 5일 이동평균선 위 (단기 반등 시작)
  - 52주 신고가 대비 20% 이내 (너무 먼 종목 제외)
  - 거래량 최근 5일 평균 대비 150% 이상
"""
import time
import kis_api

MIN_TRADING_VALUE = 10_000_000_000  # 100억


def _get_trading_value_won(stock: dict) -> int:
    try:
        return int(stock.get("acml_tr_pbmn", 0))
    except ValueError:
        return 0


def screen_candidates(top_n: int = 30) -> list[dict]:
    """상단매매 + 하단매매 후보 통합 스크리닝"""
    print(f"[스크리너] 거래대금 상위 {top_n}개 종목 조회 중...")
    top_stocks = kis_api.get_top_trading_value(top_n)

    upper_candidates = []
    lower_candidates = []

    for stock in top_stocks:
        code = stock.get("mksc_shrn_iscd", "")
        name = stock.get("hts_kor_isnm", "")
        trading_value = _get_trading_value_won(stock)

        if not code:
            continue

        # 거래대금 100억 미만 제외
        if trading_value < MIN_TRADING_VALUE:
            print(f"  ❌ {name}({code}) - 거래대금 부족 ({trading_value/1e8:.0f}억)")
            continue

        try:
            rate = float(stock.get("prdy_ctrt", "0"))
        except ValueError:
            rate = 0.0

        # 하락 종목 제외
        if rate < 0:
            continue

        # 차트 지표 계산 (API 호출)
        try:
            ind = kis_api.get_chart_indicators(code)
            time.sleep(0.3)  # API rate limit
        except Exception as e:
            print(f"  ⚠️ {name}({code}) 차트 조회 실패: {e}")
            continue

        if not ind:
            continue

        current = ind["current"]
        ma5 = ind["ma5"]
        ma20 = ind["ma20"]
        high_200 = ind["high_200"]
        vol_ratio = ind["vol_ratio"]
        upper_tail = ind["upper_tail_ratio"]

        base = {
            "code": code,
            "name": name,
            "trading_value": trading_value,
            "change_rate": rate,
            "ma5": ma5,
            "ma20": ma20,
            "vol_ratio": vol_ratio,
        }

        # ── 상단매매 조건 체크 ──
        upper_ok = (
            current >= ma5 and                    # 5일선 위
            current >= high_200 * 0.98 and        # 200일 신고가 근접/돌파
            vol_ratio >= 2.0 and                  # 거래량 200% 이상
            upper_tail <= 0.3                     # 윗꼬리 30% 이하
        )

        # ── 하단매매 조건 체크 ──
        w52_high = kis_api.get_stock_info(code).get("w52_hgpr", 0)
        try:
            w52_gap = (float(w52_high) - current) / float(w52_high) * 100 if float(w52_high) > 0 else 100
        except (ValueError, ZeroDivisionError):
            w52_gap = 100

        lower_ok = (
            current < ma20 and                    # 20일선 아래 (눌림목)
            current >= ma5 and                    # 5일선 위 (단기 반등)
            w52_gap <= 20 and                     # 52주 신고가 대비 20% 이내
            vol_ratio >= 1.5                      # 거래량 150% 이상
        )

        if upper_ok:
            entry = {**base, "strategy": "상단매매"}
            upper_candidates.append(entry)
            print(f"  🔴 상단매매: {name}({code}) 등락률 {rate:+.1f}% 거래량비 {vol_ratio:.1f}x")
        elif lower_ok:
            entry = {**base, "strategy": "하단매매"}
            lower_candidates.append(entry)
            print(f"  🔵 하단매매: {name}({code}) 등락률 {rate:+.1f}% MA20 아래 눌림목")
        else:
            print(f"  ❌ 제외: {name}({code}) - 조건 미충족")

    # 상단매매 우선, 그 다음 하단매매
    all_candidates = upper_candidates + lower_candidates
    print(f"\n[스크리너] 상단매매 {len(upper_candidates)}개 / 하단매매 {len(lower_candidates)}개")
    return all_candidates
