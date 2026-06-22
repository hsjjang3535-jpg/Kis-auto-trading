"""
자동매매 메인 로직

스케줄:
  14:50 - 종목 스크리닝 + AI 분석
  15:00 - 매수 실행 (AI 승인 종목)
  09:01 - 다음날 시초가 매도
"""
import os
import time
import schedule
from datetime import datetime
from dotenv import load_dotenv

import kis_api
import screener
import ai_analyzer
import notifier

load_dotenv()

MAX_BUY_AMOUNT = int(os.getenv("MAX_BUY_AMOUNT", "100000"))

# 매수한 종목 기록 (당일 유지)
_bought_today: list[dict] = []


def is_trading_day() -> bool:
    """주말이면 매매 건너뜀"""
    return datetime.now().weekday() < 5


def run_screening() -> None:
    """14:50 - 종목 스크리닝 및 AI 분석"""
    if not is_trading_day():
        return

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 스크리닝 시작")
    notifier.send("⏰ 오후 2시 50분 - 종가베팅 후보 분석 시작")

    try:
        candidates = screener.screen_candidates(top_n=20, high_threshold_pct=5.0)

        approved = []
        for c in candidates:
            result = ai_analyzer.analyze(c["name"], c["code"], c["change_rate"])
            c["buy"] = result["buy"]
            c["strength"] = result["strength"]
            c["reason"] = result["reason"]
            if result["buy"]:
                approved.append(c)

        notifier.notify_screening_result(approved)

        # 전역 저장 (매수 로직에서 사용)
        global _screening_result
        _screening_result = approved
        print(f"[스크리닝 완료] AI 승인 종목: {len(approved)}개")

    except Exception as e:
        msg = f"스크리닝 오류: {e}"
        print(msg)
        notifier.notify_error(msg)


_screening_result: list[dict] = []


def run_buy() -> None:
    """15:00 - 매수 실행"""
    if not is_trading_day():
        return

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 매수 실행")

    if not _screening_result:
        notifier.send("ℹ️ 오늘 매수 대상 없음")
        return

    # 안전장치: 최대 2종목만 매수
    targets = _screening_result[:2]

    for stock in targets:
        try:
            price_info = kis_api.get_stock_info(stock["code"])
            current_price = int(price_info.get("stck_prpr", 0))
            if current_price == 0:
                continue

            quantity = MAX_BUY_AMOUNT // current_price
            if quantity < 1:
                notifier.send(f"⚠️ {stock['name']}: 잔고 부족 (현재가 {current_price:,}원)")
                continue

            result = kis_api.buy_stock(stock["code"], quantity)
            rt_cd = result.get("rt_cd", "")

            if rt_cd == "0":
                _bought_today.append({
                    "code": stock["code"],
                    "name": stock["name"],
                    "quantity": quantity,
                    "buy_price": current_price,
                })
                notifier.notify_buy(stock["name"], stock["code"], quantity, current_price, stock["reason"])
            else:
                msg = result.get("msg1", "알 수 없는 오류")
                notifier.notify_error(f"{stock['name']} 매수 실패: {msg}")

        except Exception as e:
            notifier.notify_error(f"{stock['name']} 매수 오류: {e}")

        time.sleep(0.5)


def run_sell() -> None:
    """09:01 - 보유 종목 시초가 매도"""
    if not is_trading_day():
        return

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 매도 실행")

    try:
        holdings = kis_api.get_holdings()
    except Exception as e:
        notifier.notify_error(f"잔고 조회 오류: {e}")
        return

    for h in holdings:
        code = h.get("pdno", "")
        name = h.get("prdt_name", code)
        quantity = int(h.get("hldg_qty", 0))
        avg_price = float(h.get("pchs_avg_pric", 0))

        if quantity < 1:
            continue

        try:
            result = kis_api.sell_stock(code, quantity)
            rt_cd = result.get("rt_cd", "")

            if rt_cd == "0":
                price_info = kis_api.get_stock_info(code)
                current = float(price_info.get("stck_prpr", avg_price))
                profit_pct = (current - avg_price) / avg_price * 100 if avg_price else 0
                notifier.notify_sell(name, code, quantity, profit_pct)
            else:
                msg = result.get("msg1", "알 수 없는 오류")
                notifier.notify_error(f"{name} 매도 실패: {msg}")

        except Exception as e:
            notifier.notify_error(f"{name} 매도 오류: {e}")

        time.sleep(0.5)


def main():
    print("=== KIS 자동매매 시작 (종산 종가베팅) ===")
    notifier.send("🤖 자동매매 봇 시작됨\n매일 14:50 스크리닝 → 15:00 매수 → 익일 09:01 매도")

    schedule.every().day.at("14:50").do(run_screening)
    schedule.every().day.at("15:00").do(run_buy)
    schedule.every().day.at("09:01").do(run_sell)

    print("스케줄 등록 완료: 14:50 스크리닝 / 15:00 매수 / 09:01 매도")

    while True:
        schedule.run_pending()
        time.sleep(10)


if __name__ == "__main__":
    main()
