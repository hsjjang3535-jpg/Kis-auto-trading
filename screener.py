import kis_api


def screen_candidates(top_n: int = 20, high_threshold_pct: float = 5.0) -> list[dict]:
    """
    종산 매매법 기준으로 종목 필터링:
    1. 거래대금 상위 top_n 종목 조회
    2. 52주 신고가 대비 threshold% 이내인 종목만 통과
    """
    print(f"[스크리너] 거래대금 상위 {top_n}개 종목 조회 중...")
    top_stocks = kis_api.get_top_trading_value(top_n)

    candidates = []
    for stock in top_stocks:
        code = stock.get("mksc_shrn_iscd", "")
        name = stock.get("hts_kor_isnm", "")
        trading_value = stock.get("acml_tr_pbmn", "0")
        rate = stock.get("prdy_ctrt", "0")

        if not code:
            continue

        try:
            rate_f = float(rate)
        except ValueError:
            rate_f = 0.0

        # 하락 종목 제외
        if rate_f < 0:
            continue

        # 52주 신고가 근처인지 확인
        if kis_api.is_near_high(code, high_threshold_pct):
            candidates.append({
                "code": code,
                "name": name,
                "trading_value": int(trading_value),
                "change_rate": rate_f,
            })
            print(f"  ✅ 통과: {name}({code}) 등락률 {rate_f:+.2f}%")
        else:
            print(f"  ❌ 제외: {name}({code}) - 신고가 조건 미충족")

    print(f"[스크리너] 최종 후보 {len(candidates)}개")
    return candidates
