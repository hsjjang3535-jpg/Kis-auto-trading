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
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# schedule 라이브러리 사용 안 함 - KST 직접 체크 루프로 대체
KST = ZoneInfo("Asia/Seoul")

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
# 트레일링 스탑: 3% 이상 수익 후 고점에서 이만큼 내리면 매도
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "1.0"))

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

def _today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _save_state() -> None:
    state = {
        "date": _today_kst(),
        "positions": _positions,
        "total_invested_today": _total_invested_today,
        "trades_today": _trades_today,
    }
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[상태 저장 오류] {e}")


def _load_state() -> None:
    global _positions, _total_invested_today, _trades_today
    if not os.path.exists(_STATE_FILE):
        return
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if state.get("date") == _today_kst():
            _positions = state.get("positions", {})
            _total_invested_today = state.get("total_invested_today", 0)
            _trades_today = state.get("trades_today", [])
            if _positions:
                print(f"[상태 복원] 보유 포지션 {len(_positions)}개 불러옴")
            if _trades_today:
                print(f"[상태 복원] 오늘 체결 {len(_trades_today)}건 불러옴")
    except Exception as e:
        print(f"[상태 불러오기 오류] {e}")


# ── 유틸 ──────────────────────────────────────────────────────────────────────

def is_trading_day() -> bool:
    today = datetime.now(KST)
    if today.weekday() >= 5:
        return False
    if today.strftime("%Y-%m-%d") in _KR_HOLIDAYS:
        print(f"[공휴일] {today.strftime('%Y-%m-%d')} - 매매 건너뜀")
        return False
    return True


def _market_minutes() -> int:
    """현재 KST 시각을 분 단위로 반환 (예: 09:35 → 575)"""
    now = datetime.now(KST)
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
    print(f"\n[{datetime.now(KST).strftime('%H:%M:%S')} KST] 오전 스크리닝 시작")
    notifier.send("⏰ 오전 9시 05분 - 장 시작 후 워치리스트 구성 시작")

    try:
        candidates = screener.screen_candidates(top_n=30)

        approved = []
        ai_fail_count = 0
        for c in candidates:
            result = ai_analyzer.analyze(c["name"], c["code"], c["change_rate"])
            c["buy"] = result["buy"]
            c["strength"] = result["strength"]
            c["reason"] = result["reason"]
            if result["reason"] == "분석 실패":
                ai_fail_count += 1
            if result["buy"]:
                approved.append(c)
            time.sleep(2)  # Groq API 속도 제한 방지 (분당 30회)

        # AI가 전부 실패한 경우 → 기술적 조건 통과 종목만으로 진행
        ai_unavailable = candidates and ai_fail_count == len(candidates)
        if ai_unavailable:
            notifier.send(
                "⚠️ <b>AI 분석 서버 일시 불가</b>\n"
                "Groq API 전체 다운 → 기술적 조건 통과 종목으로 진행합니다."
            )
            approved = candidates
            for c in approved:
                c.setdefault("reason", "AI 분석 불가 (기술적 조건 통과)")

        _watchlist = approved

        if approved:
            ai_note = " (AI 미적용)" if ai_unavailable else ""
            lines = [f"🔍 <b>장중매매 워치리스트 {len(approved)}개{ai_note}</b>\n"]
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

    print(f"\n[{datetime.now(KST).strftime('%H:%M:%S')} KST] 상태 보고")
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

    print(f"\n[{datetime.now(KST).strftime('%H:%M:%S')} KST] 장마감 손익 보고")
    today = datetime.now(KST).strftime("%Y-%m-%d")

    if not _trades_today:
        notifier.send(
            f"📋 <b>오늘 장마감 보고 ({today})</b>\n"
            "매매 없음 (체결 종목 없음)"
        )
        return

    win_trades  = [t for t in _trades_today if t["profit_won"] > 0]
    lose_trades = [t for t in _trades_today if t["profit_won"] <= 0]
    total_profit = sum(t["profit_won"] for t in win_trades)
    total_loss   = sum(t["profit_won"] for t in lose_trades)
    net = total_profit + total_loss

    lines = [f"📋 <b>오늘 장마감 보고 ({today})</b>\n"]

    for t in _trades_today:
        is_profit = t["profit_won"] > 0
        emoji = "📈" if is_profit else "📉"
        sign  = "+" if is_profit else ""

        lines.append(
            f"{emoji} <b>{t['name']} ({t['code']})</b>\n"
            f"   전략: {t.get('strategy', '-')}\n"
            f"   매수가: {t['buy_price']:,}원 | 수량: {t['quantity']}주\n"
            f"   매도가: {t['sell_price']:,}원\n"
            f"   매수사유: {t.get('buy_reason', '-')}\n"
            f"   결과: {sign}{t['profit_pct']}% ({sign}{t['profit_won']:,}원)\n"
            f"   매도사유: {t.get('sell_reason', '-')}"
        )

    lines.append("")
    lines.append("─────────────────────")
    lines.append(f"총 매매: {len(_trades_today)}건")
    lines.append(f"  익절 {len(win_trades)}건: +{total_profit:,}원")
    lines.append(f"  손절 {len(lose_trades)}건: {total_loss:,}원")
    net_sign = "+" if net >= 0 else ""
    lines.append(f"\n🏁 오늘 순손익: <b>{net_sign}{net:,}원</b>")

    notifier.send("\n".join(lines))


def run_force_close() -> None:
    """14:50 - 잔여 포지션 강제 청산"""
    if not is_trading_day():
        return
    if not _positions:
        notifier.send("✅ 14:50 - 보유 포지션 없음, 청산 불필요")
        return

    print(f"\n[{datetime.now(KST).strftime('%H:%M:%S')} KST] 강제 청산 시작 ({len(_positions)}개)")
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
                    "peak_price": int(current),  # 트레일링 스탑용 고점 추적
                    "strategy": strategy,
                    "buy_reason": stock.get("reason", ""),  # AI 매수사유
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
    """보유 포지션 익절(트레일링 스탑)/손절 조건 체크"""
    if not _positions:
        return

    for code, pos in list(_positions.items()):
        if pos["name"] in SELL_BLACKLIST:
            continue

        try:
            info = kis_api.get_stock_info(code)
            current = float(info.get("stck_prpr", pos["buy_price"]))
            profit_pct = (current - pos["buy_price"]) / pos["buy_price"] * 100

            # 고점 갱신 (트레일링 스탑용)
            peak = pos.get("peak_price", pos["buy_price"])
            if current > peak:
                peak = current
                _positions[code]["peak_price"] = peak
                _save_state()

            peak_profit_pct = (peak - pos["buy_price"]) / pos["buy_price"] * 100
            drop_from_peak = (peak - current) / peak * 100

            if profit_pct <= -STOP_LOSS_PCT:
                # 손절: -2% 이하
                _execute_sell(code, pos, f"손절 ({profit_pct:.1f}%)", current, profit_pct)

            elif peak_profit_pct >= TAKE_PROFIT_PCT and drop_from_peak >= TRAILING_STOP_PCT:
                # 트레일링 스탑: 3% 이상 도달 후 고점에서 1% 이상 내려오면 매도
                _execute_sell(
                    code, pos,
                    f"트레일링 익절 (현재 +{profit_pct:.1f}% / 고점 +{peak_profit_pct:.1f}%에서 -{drop_from_peak:.1f}%)",
                    current, profit_pct,
                )

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
                "sell_reason": reason,
                "buy_reason": pos.get("buy_reason", ""),
                "strategy": pos.get("strategy", ""),
            })
            del _positions[code]
            _save_state()
            notifier.notify_sell(name, code, quantity, profit_pct, reason)
        else:
            msg = result.get("msg1", "알 수 없는 오류")
            notifier.notify_error(f"{name} 매도 실패: {msg}")

    except Exception as e:
        notifier.notify_error(f"{name} 매도 오류: {e}")


# ── 일별 실행 추적 (오늘 날짜별로 각 작업이 한 번씩만 실행되도록) ────────────────
_last_ran: dict[str, str] = {}


def _reset_daily_state() -> None:
    """자정이 지나 날짜가 바뀌면 일별 데이터 초기화"""
    global _watchlist, _trades_today, _total_invested_today
    _watchlist = []
    _trades_today = []
    _total_invested_today = 0
    _save_state()
    print(f"[일별 초기화] {_today_kst()} 새 거래일 시작")


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    print("=== KIS 자동매매 시작 (종산 장중매매) ===")
    _load_state()

    # HTTP API 서버를 별도 스레드로 시작 (텔레그램 봇 연동)
    import api_server, threading
    api_thread = threading.Thread(target=api_server.start_api_server, daemon=True)
    api_thread.start()

    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    notifier.send(
        f"🤖 자동매매 봇 시작 (장중매매) - {now_kst}\n"
        "⏰ 09:05 스크리닝 → 09:10~14:30 5분마다 진입\n"
        "✅ 익절 +3% / 손절 -2% / 14:50 강제청산 / 15:10 손익보고"
    )

    print("KST 직접 체크 루프 시작 (schedule 라이브러리 미사용)")
    print(f"현재 KST: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}")

    # ── 봇 시작 시 스크리닝 시간대(09:05~09:30)에 재시작됐으면 즉시 실행 ──
    # (장마감 후 재배포 시 불필요한 API 호출 방지)
    _init_t = datetime.now(KST)
    _init_min = _init_t.hour * 60 + _init_t.minute
    if is_trading_day() and 9 * 60 + 5 <= _init_min <= 9 * 60 + 30 and not _watchlist:
        notifier.send("▶️ 봇 재시작 감지 (스크리닝 시간) - 즉시 스크리닝 실행")
        _last_ran["screening"] = _init_t.strftime("%Y-%m-%d")
        run_morning_screening()

    last_5min_slot = -1  # 마지막으로 장중 체크한 5분 슬롯

    while True:
        now = datetime.now(KST)
        today = now.strftime("%Y-%m-%d")
        t = now.hour * 60 + now.minute  # 자정 기준 분 단위 (예: 09:10 → 550)

        # 날짜가 바뀌면 일별 상태 초기화
        if _last_ran.get("date") and _last_ran["date"] != today:
            _reset_daily_state()
            last_5min_slot = -1
        _last_ran["date"] = today

        # ── 09:05~09:30 KST - 워치리스트 스크리닝 (장 개시 후 데이터 안정화) ──
        if 9 * 60 + 5 <= t <= 9 * 60 + 30 and _last_ran.get("screening") != today:
            _last_ran["screening"] = today
            run_morning_screening()

        # ── 09:10~14:45 KST - 5분마다 장중 진입/청산 체크 ───────────────────
        if 9 * 60 + 10 <= t <= 14 * 60 + 45:
            slot = t // 5  # 5분 단위 슬롯 번호 (분이 바뀌기 전까지 같은 값)
            if slot != last_5min_slot:
                last_5min_slot = slot
                run_market_check()

        # ── 11:00~11:10 KST - 상태 보고 ─────────────────────────────────────
        if 11 * 60 <= t <= 11 * 60 + 10 and _last_ran.get("status") != today:
            _last_ran["status"] = today
            run_status_report()

        # ── 14:50~15:00 KST - 강제 청산 ─────────────────────────────────────
        if 14 * 60 + 50 <= t <= 15 * 60 and _last_ran.get("force_close") != today:
            _last_ran["force_close"] = today
            run_force_close()

        # ── 15:10~15:30 KST - 장마감 손익 보고 ──────────────────────────────
        if 15 * 60 + 10 <= t <= 15 * 60 + 30 and _last_ran.get("closing") != today:
            _last_ran["closing"] = today
            run_closing_report()

        time.sleep(20)  # 20초마다 KST 시간 직접 체크


if __name__ == "__main__":
    main()
