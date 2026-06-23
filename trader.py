"""
자동매매 메인 로직 (장중매매)

스케줄 (한국시간 KST, TZ=Asia/Seoul):
  08:50 - 워치리스트 스크리닝 (전날 차트 기준)
  09:10 ~ 14:45 - 5분마다 진입/청산 조건 체크
  11:00 - 상태 보고
  14:50 - 잔여 포지션 강제 청산
  15:10 - 장마감 손익 보고
"""
import os
import json
import time
import schedule
from datetime import date, datetime
from dotenv import load_dotenv

import kis_api
import screener
import ai_analyzer
import notifier

load_dotenv()

MAX_BUY_AMOUNT = int(os.getenv("MAX_BUY_AMOUNT", "500000"))
MAX_TOTAL_AMOUNT = int(os.getenv("MAX_TOTAL_AMOUNT", "1000000"))
SELL_BLACKLIST = [s.strip() for s in os.getenv("SELL_BLACKLIST", "").split(",") if s.strip()]
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "2.0"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "3.0"))

_STATE_FILE = "trading_state.json"

_KR_HOLIDAYS = {
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18",
    "2026-03-01", "2026-05-05", "2026-05-25", "2026-06-06",
    "2026-08-15", "2026-09-24", "2026-09-25", "2026-09-26",
    "2026-10-03", "2026-10-09", "2026-12-25", "2026-12-31",
    "2027-01-01", "2027-03-01", "2027-05-05", "2027-06-06",
    "2027-08-15", "2027-10-03", "2027-10-09", "2027-12-25", "2027-12-31",
}

# 오전 스크리닝으로 구성된 워치리스트
_watchlist: list[dict] = []

# 현재 보유 포지션 { 종목코드: {name, quantity, buy_price, strategy} }
_positions: dict[str, dict] = {}

# 오늘 총 투자금
_total_invested_today: int = 0

# 오늘 체결된 매도 기록 (손익 보고용)
# { name, code, quantity, buy_price, sell_price, profit_pct, profit_won, reason }
_trades_today: list[dict] = []


# ── 상태 저장/복원 ─────────────────────────────────────────────────────────────

def _save_state() -> None:
    state = {
        "date": date.today().isoformat(),
        "positions": _positions,
        "total_invested_today": _total_invested_today,
    }
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[상태 저장 오류] {e}")


def _load_state() -> None:
    global _positions, _total_invested_today
    if not os.path.exists(_STATE_FILE):
        return
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if state.get("date") == date.today().isoformat():
            _positions = state.get("positions", {})
            _total_invested_today = state.get("total_invested_today", 0)
            if _positions:
                print(f"[상태 복원] 보유 포지션 {len(_positions)}개 불러옴")
    except Exception as e:
        print(f"[상태 불러오기 오류] {e}")


# ── 유틸 ──────────────────────────────────────────────────────────────────────

def is_trading_day() -> bool:
    today = datetime.now()
    if today.weekday() >= 5:
        return False
    if today.strftime("%Y-%m-%d") in _KR_HOLIDAYS:
        print(f"[공휴일] {today.strftime('%Y-%m-%d')} - 매매 건너뜀")
        return False
    return True


def _market_minutes() -> int:
    """현재 시각을 분 단위로 반환 (예: 09:35 → 575)"""
    now = datetime.now()
    return now.hour * 60 + now.minute


def is_entry_time() -> bool:
    """진입 가능 시간: 09:10 ~ 14:30"""
    t = _market_minutes()
    return 9 * 60 + 10 <= t <= 14 * 60 + 30


def is_exit_time() -> bool:
    """청산 체크 시간: 09:10 ~ 14:45"""
    t = _market_minutes()
    return 9 * 60 + 10 <= t <= 14 * 60 + 45


# ── 스케줄 함수 ───────────────────────────────────────────────────────────────

def run_morning_screening() -> None:
    """09:00 - 워치리스트 구성"""
    if not is_trading_day():
        return

    global _watchlist
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 오전 스크리닝 시작")
    notifier.send("⏰ 오전 8시 50분 - 장 시작 전 워치리스트 구성 시작")

    try:
        candidates = screener.screen_candidates(top_n=30)

        approved = []
        for c in candidates:
            result = ai_analyzer.analyze(c["name"], c["code"], c["change_rate"])
            c["buy"] = result["buy"]
            c["strength"] = result["strength"]
            c["reason"] = result["reason"]
            if result["buy"]:
                approved.append(c)
            time.sleep(2)  # Groq API 속도 제한 방지 (분당 30회)

        _watchlist = approved

        if approved:
            lines = [f"🔍 <b>장중매매 워치리스트 {len(approved)}개</b>\n"]
            strategy_map = {"상단매매": "🔴", "돌파매매": "🟡", "하단매매": "🔵"}
            for c in approved:
                emoji = strategy_map.get(c.get("strategy", ""), "⚪")
                lines.append(
                    f"{emoji} {c['name']}({c['code']}) - {c.get('strategy', '')}\n"
                    f"   MA5:{c.get('ma5', 0):.0f} / 현재:{c.get('current', 0):.0f} / RSI:{c.get('rsi', 0):.0f}"
                )
            notifier.send("\n".join(lines))
        else:
            notifier.send("🔍 오늘 워치리스트 없음 - 진입 대기")

        print(f"[스크리닝 완료] 워치리스트 {len(approved)}개")

    except Exception as e:
        msg = f"오전 스크리닝 오류: {e}"
        print(msg)
        notifier.notify_error(msg)


def run_market_check() -> None:
    """5분마다 - 진입/청산 조건 체크"""
    if not is_trading_day():
        return

    if is_exit_time():
        _check_exit()

    if is_entry_time():
        _check_entry()


def run_status_report() -> None:
    """11:00 - 상태 보고"""
    if not is_trading_day():
        return

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 상태 보고")
    try:
        pos_lines = []
        for code, pos in _positions.items():
            try:
                info = kis_api.get_stock_info(code)
                current = float(info.get("stck_prpr", pos["buy_price"]))
                profit_pct = (current - pos["buy_price"]) / pos["buy_price"] * 100
                pos_lines.append(f"  {pos['name']}: {profit_pct:+.1f}% ({pos['strategy']})")
                time.sleep(0.3)
            except Exception:
                pos_lines.append(f"  {pos['name']}: 조회 실패")

        lines = [
            "📊 <b>오전 11시 상태 보고</b>",
            f"모드: {os.getenv('KIS_MODE', '알 수 없음')}",
            f"워치리스트: {len(_watchlist)}개",
            f"현재 보유: {len(_positions)}개",
            f"오늘 투자금: {_total_invested_today:,}원 / {MAX_TOTAL_AMOUNT:,}원",
        ]
        if pos_lines:
            lines.append("📌 보유 종목:")
            lines.extend(pos_lines)

        notifier.send("\n".join(lines))

    except Exception as e:
        notifier.send(f"📊 오전 11시 상태 보고\n봇 정상 실행 중\n(오류: {e})")


def run_closing_report() -> None:
    """15:10 - 장마감 손익 보고"""
    if not is_trading_day():
        return

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 장마감 손익 보고")

    if not _trades_today:
        notifier.send("📋 <b>오늘 장마감 보고</b>\n매매 없음 (체결 종목 없음)")
        return

    total_profit_won = sum(t["profit_won"] for t in _trades_today)
    win_trades = [t for t in _trades_today if t["profit_won"] > 0]
    lose_trades = [t for t in _trades_today if t["profit_won"] <= 0]

    lines = ["📋 <b>오늘 장마감 손익 보고</b>\n"]

    for t in _trades_today:
        emoji = "📈" if t["profit_won"] > 0 else "📉"
        sign = "+" if t["profit_won"] > 0 else ""
        lines.append(
            f"{emoji} {t['name']}({t['code']})\n"
            f"   매수 {t['buy_price']:,}원 → 매도 {t['sell_price']:,}원\n"
            f"   {t['quantity']}주 | {sign}{t['profit_pct']}% | {sign}{t['profit_won']:,}원\n"
            f"   사유: {t['reason']}"
        )

    lines.append("")
    lines.append(f"─────────────────")
    lines.append(f"총 매매: {len(_trades_today)}건 (익절 {len(win_trades)}건 / 손절 {len(lose_trades)}건)")
    sign = "+" if total_profit_won > 0 else ""
    lines.append(f"오늘 손익: <b>{sign}{total_profit_won:,}원</b>")

    notifier.send("\n".join(lines))


def run_force_close() -> None:
    """14:50 - 잔여 포지션 강제 청산"""
    if not is_trading_day():
        return
    if not _positions:
        notifier.send("✅ 14:50 - 보유 포지션 없음, 청산 불필요")
        return

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 강제 청산 시작 ({len(_positions)}개)")
    notifier.send(f"⏰ 14시 50분 - 잔여 포지션 {len(_positions)}개 강제 청산")

    for code, pos in list(_positions.items()):
        if pos["name"] in SELL_BLACKLIST:
            notifier.send(f"🚫 {pos['name']} - 매도 금지 종목, 보유 유지")
            continue
        _execute_sell(code, pos, "장 마감 전 강제 청산")
        time.sleep(0.5)


# ── 진입 로직 ─────────────────────────────────────────────────────────────────

def _check_entry() -> None:
    """워치리스트 종목 진입 조건 체크 및 매수"""
    if not _watchlist:
        return

    global _total_invested_today
    remaining = MAX_TOTAL_AMOUNT - _total_invested_today
    if remaining <= 0:
        return

    already_held = set(_positions.keys())

    for stock in _watchlist:
        code = stock["code"]
        name = stock["name"]
        strategy = stock.get("strategy", "")

        if code in already_held:
            continue

        try:
            info = kis_api.get_stock_info(code)
            current = float(info.get("stck_prpr", 0))
            if current == 0:
                continue

            # 오전 스크리닝에서 계산된 지표 활용 (재호출 없이 빠른 체크)
            ma5 = stock.get("ma5", 0)
            ma20 = stock.get("ma20", 0)
            high_200 = stock.get("high_200", 0)
            high_20 = stock.get("high_20", 0)
            rsi = stock.get("rsi", 50.0)

            entry_ok = False
            if strategy == "상단매매":
                entry_ok = (
                    current >= ma5 and
                    high_200 > 0 and current >= high_200 * 0.98
                )
            elif strategy == "하단매매":
                entry_ok = (
                    current >= ma5 and
                    ma20 > 0 and current < ma20 and
                    rsi <= 30  # screener와 동일 기준
                )
            elif strategy == "돌파매매":
                entry_ok = (
                    high_20 > 0 and current >= high_20 * 0.995 and
                    current >= ma5
                )

            if not entry_ok:
                continue

            # 매수 실행
            buy_amount = min(MAX_BUY_AMOUNT, remaining)
            quantity = buy_amount // int(current)
            if quantity < 1:
                notifier.send(f"⚠️ {name}: 금액 부족 (현재가 {int(current):,}원)")
                continue

            result = kis_api.buy_stock(code, quantity)
            if result.get("rt_cd") == "0":
                invested = quantity * int(current)
                _total_invested_today += invested
                _positions[code] = {
                    "name": name,
                    "quantity": quantity,
                    "buy_price": int(current),
                    "strategy": strategy,
                }
                _save_state()
                already_held.add(code)
                remaining = MAX_TOTAL_AMOUNT - _total_invested_today
                notifier.notify_buy(name, code, quantity, int(current), stock.get("reason", ""))

                if remaining <= 0:
                    break
            else:
                msg = result.get("msg1", "알 수 없는 오류")
                notifier.notify_error(f"{name} 매수 실패: {msg}")

            time.sleep(0.5)

        except Exception as e:
            notifier.notify_error(f"{name} 진입 체크 오류: {e}")


# ── 청산 로직 ─────────────────────────────────────────────────────────────────

def _check_exit() -> None:
    """보유 포지션 익절/손절 조건 체크"""
    if not _positions:
        return

    for code, pos in list(_positions.items()):
        if pos["name"] in SELL_BLACKLIST:
            continue

        try:
            info = kis_api.get_stock_info(code)
            current = float(info.get("stck_prpr", pos["buy_price"]))
            profit_pct = (current - pos["buy_price"]) / pos["buy_price"] * 100

            if profit_pct >= TAKE_PROFIT_PCT:
                _execute_sell(code, pos, f"익절 (+{profit_pct:.1f}%)", current, profit_pct)
            elif profit_pct <= -STOP_LOSS_PCT:
                _execute_sell(code, pos, f"손절 ({profit_pct:.1f}%)", current, profit_pct)

            time.sleep(0.3)

        except Exception as e:
            print(f"[청산 체크 오류] {pos['name']}: {e}")


def _execute_sell(code: str, pos: dict, reason: str,
                  current: float = 0, profit_pct: float = 0) -> None:
    name = pos["name"]
    quantity = pos["quantity"]

    try:
        # 현재가가 전달되지 않은 경우 (강제청산 등)에만 재조회
        if current == 0:
            info = kis_api.get_stock_info(code)
            current = float(info.get("stck_prpr", pos["buy_price"]))
            profit_pct = (current - pos["buy_price"]) / pos["buy_price"] * 100

        result = kis_api.sell_stock(code, quantity)
        if result.get("rt_cd") == "0":
            profit_won = int((current - pos["buy_price"]) * quantity)
            _trades_today.append({
                "name": name,
                "code": code,
                "quantity": quantity,
                "buy_price": pos["buy_price"],
                "sell_price": int(current),
                "profit_pct": round(profit_pct, 2),
                "profit_won": profit_won,
                "reason": reason,
            })
            del _positions[code]
            _save_state()
            notifier.notify_sell(name, code, quantity, profit_pct, reason)
        else:
            msg = result.get("msg1", "알 수 없는 오류")
            notifier.notify_error(f"{name} 매도 실패: {msg}")

    except Exception as e:
        notifier.notify_error(f"{name} 매도 오류: {e}")


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    print("=== KIS 자동매매 시작 (종산 장중매매) ===")
    _load_state()
    notifier.send(
        "🤖 자동매매 봇 시작 (장중매매)\n"
        "08:50 워치리스트 → 09:10~14:30 5분마다 진입\n"
        "익절 +3% / 손절 -2% / 14:50 강제청산 / 15:10 손익보고"
    )

    # 한국시간(KST) 기준 스케줄
    schedule.every().day.at("08:50").do(run_morning_screening)  # 장 시작 10분 전 (전날 데이터 기준)
    schedule.every(5).minutes.do(run_market_check)
    schedule.every().day.at("11:00").do(run_status_report)
    schedule.every().day.at("14:50").do(run_force_close)
    schedule.every().day.at("15:10").do(run_closing_report)

    print("스케줄 등록 완료 (한국시간 KST):")
    print("  08:50       - 워치리스트 스크리닝 (장 시작 전)")
    print("  09:10~14:30 - 5분마다 진입 체크")
    print("  09:10~14:45 - 5분마다 청산 체크 (익절/손절)")
    print("  11:00       - 상태 보고")
    print("  14:50       - 강제 청산")
    print("  15:10       - 장마감 손익 보고")

    while True:
        schedule.run_pending()
        time.sleep(10)


if __name__ == "__main__":
    main()
