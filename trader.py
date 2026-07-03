"""
자동매매 메인 로직 (장중매매 + 종가베팅)

스케줄 (한국시간 KST):
  09:00 - 종가베팅 포지션 시초가 매도 (전일 보유분)
  09:05 - 장중매매 워치리스트 스크리닝
  09:10 ~ 14:45 - 5분마다 장중매매 진입/청산 체크
  09:10 ~ 10:30 - 5분마다 낙폭반등 체크 (ENABLE_CRASH_BOUNCE=true 시)
  11:00 - 상태 보고
  14:00 - 종가베팅 스크리닝
  14:20 ~ 14:50 - 5분마다 종가베팅 매수 체크
  14:50 - 장중매매 잔여 포지션 강제 청산 (종가베팅 제외)
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
import crash_bounce

load_dotenv()

MAX_BUY_AMOUNT = int(os.getenv("MAX_BUY_AMOUNT", "500000"))
MAX_TOTAL_AMOUNT = int(os.getenv("MAX_TOTAL_AMOUNT", "1000000"))
SELL_BLACKLIST = [s.strip() for s in os.getenv("SELL_BLACKLIST", "").split(",") if s.strip()]
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "2.0"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "3.0"))
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "1.0"))
# 동적 자금 관리: True면 매일 실제 예수금으로 한도 자동 조절
DYNAMIC_CAPITAL = os.getenv("DYNAMIC_CAPITAL", "true").lower() == "true"
# 1회 매수금액 = 예수금의 이 비율 (기본 50%)
BUY_RATIO = float(os.getenv("BUY_RATIO", "0.5"))
# 종가베팅 자금 한도 (별도 관리)
MAX_CLOSING_AMOUNT = int(os.getenv("MAX_CLOSING_AMOUNT", "500000"))  # 종가베팅 총 한도
MAX_CLOSING_BUY = int(os.getenv("MAX_CLOSING_BUY", "250000"))        # 종가베팅 1회 매수

_STATE_FILE = "trading_state.json"

_KR_HOLIDAYS = {
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18",
    "2026-03-01", "2026-05-05", "2026-05-25", "2026-06-06",
    "2026-08-15", "2026-09-24", "2026-09-25", "2026-09-26",
    "2026-10-03", "2026-10-09", "2026-12-25", "2026-12-31",
    "2027-01-01", "2027-03-01", "2027-05-05", "2027-06-06",
    "2027-08-15", "2027-10-03", "2027-10-09", "2027-12-25", "2027-12-31",
}

# 오전 스크리닝으로 구성된 워치리스트 (장중매매)
_watchlist: list[dict] = []

# 현재 보유 포지션 (장중매매) { 종목코드: {name, quantity, buy_price, strategy} }
_positions: dict[str, dict] = {}

# 오늘 장중매매 총 투자금 (낙폭반등 제외)
_total_invested_today: int = 0

# 오늘 낙폭반등 투자금 (전용 한도)
_crash_bounce_invested_today: int = 0

# 종가베팅 워치리스트 (14:00 스크리닝)
_closing_watchlist: list[dict] = []

# 종가베팅 오버나이트 포지션 { 종목코드: {name, quantity, buy_price, buy_date} }
_closing_positions: dict[str, dict] = {}

# 오늘 종가베팅 투자금
_closing_invested_today: int = 0

# 오늘 체결된 매도 기록 (장중 + 종가베팅 모두 포함, 손익 보고용)
# { name, code, quantity, buy_price, sell_price, profit_pct, profit_won, reason, strategy }
_trades_today: list[dict] = []

# 오늘 스크리닝 요약 (0개일 때 이유 보고용)
_last_morning_summary: dict = {}
_last_closing_summary: dict = {}


# ── 상태 저장/복원 ─────────────────────────────────────────────────────────────

def _today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _save_state() -> None:
    state = {
        "date": _today_kst(),
        "positions": _positions,
        "total_invested_today": _total_invested_today,
        "crash_bounce_invested_today": _crash_bounce_invested_today,
        "trades_today": _trades_today,
        "closing_positions": _closing_positions,       # 오버나이트 유지
        "closing_invested_today": _closing_invested_today,
    }
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[상태 저장 오류] {e}")


def _load_state() -> None:
    global _positions, _total_invested_today, _trades_today
    global _closing_positions, _closing_invested_today, _crash_bounce_invested_today
    if not os.path.exists(_STATE_FILE):
        return
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        # 종가베팅 포지션은 날짜와 무관하게 항상 불러옴 (오버나이트 포지션)
        _closing_positions = state.get("closing_positions", {})
        if _closing_positions:
            print(f"[상태 복원] 종가베팅 포지션 {len(_closing_positions)}개 불러옴 (오버나이트)")

        # 장중 포지션은 오늘 날짜인 경우만
        if state.get("date") == _today_kst():
            _positions = state.get("positions", {})
            _total_invested_today = state.get("total_invested_today", 0)
            _crash_bounce_invested_today = state.get("crash_bounce_invested_today", 0)
            _trades_today = state.get("trades_today", [])
            _closing_invested_today = state.get("closing_invested_today", 0)
            if _positions:
                print(f"[상태 복원] 장중 포지션 {len(_positions)}개 불러옴")
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

def _format_empty_watchlist_msg(kind: str = "장중") -> str:
    """워치리스트 0개일 때 상세 이유 메시지"""
    if kind == "장중":
        stats = screener.get_last_screen_stats()
        summary = _last_morning_summary
        header = "🔍 <b>오늘 장중 워치리스트 없음</b>\n"
    else:
        stats = screener.get_last_closing_stats()
        summary = _last_closing_summary
        header = "🌙 <b>종가베팅 워치리스트 없음</b>\n"

    lines = [header]
    pool = stats.get("pool", 0)
    tech = stats.get("technical_pass", 0)

    if pool == 0:
        lines.append("📭 종목 조회 실패 또는 후보 풀 0개")
        return "\n".join(lines)

    if kind == "장중":
        lines.append(
            f"📊 후보 {pool}개 수집 (코스피{stats.get('kospi',0)}+"
            f"코스닥{stats.get('kosdaq',0)}+테마{stats.get('theme',0)})"
        )
        lines.append(
            f"   기술조건: 상단 {stats.get('upper',0)} / "
            f"돌파 {stats.get('breakout',0)} / 하단 {stats.get('lower',0)} "
            f"→ 통과 {tech}개"
        )
    else:
        lines.append(f"📊 후보 {pool}개 → 종가베팅 조건 통과 {tech}개")

    if tech == 0:
        lines.append("❌ 원인: 종산 기술조건(거래량·신고가·RSI 등) 충족 종목 없음")
    elif summary.get("ai_rejected"):
        lines.append(f"❌ 원인: AI 매수 거절 {len(summary['ai_rejected'])}개")
        for r in summary["ai_rejected"][:3]:
            lines.append(f"   · {r['name']}: {r['reason']}")
    else:
        lines.append("❌ 원인: 조건 통과 후보 없음")

    return "\n".join(lines)


def _update_capital() -> None:
    """실제 예수금으로 MAX_TOTAL_AMOUNT, MAX_BUY_AMOUNT 자동 조절"""
    global MAX_TOTAL_AMOUNT, MAX_BUY_AMOUNT
    if not DYNAMIC_CAPITAL:
        return
    try:
        cash = kis_api.get_cash_balance()
        if cash <= 0:
            print("[자금관리] 예수금 조회 실패 또는 0원 - 기존 한도 유지")
            return
        # 예수금 전액을 총 한도로 설정 (단, 환경변수 최솟값 이상 유지)
        old_total = MAX_TOTAL_AMOUNT
        old_buy   = MAX_BUY_AMOUNT
        MAX_TOTAL_AMOUNT = cash
        MAX_BUY_AMOUNT   = int(cash * BUY_RATIO)
        print(f"[자금관리] 예수금 {cash:,}원 → 총한도 {MAX_TOTAL_AMOUNT:,}원 / 1회매수 {MAX_BUY_AMOUNT:,}원")
        if old_total != MAX_TOTAL_AMOUNT:
            notifier.send(
                f"💰 <b>자금 한도 자동 조절</b>\n"
                f"예수금: {cash:,}원\n"
                f"총 투자 한도: {old_total:,}원 → {MAX_TOTAL_AMOUNT:,}원\n"
                f"1회 매수 한도: {old_buy:,}원 → {MAX_BUY_AMOUNT:,}원 (예수금의 {int(BUY_RATIO*100)}%)"
            )
    except Exception as e:
        print(f"[자금관리] 예수금 조회 오류: {e}")


def run_morning_sell_closing_bet() -> None:
    """09:00 - 종가베팅 포지션 시초가 매도 (전일 매수분)"""
    if not is_trading_day():
        return
    if not _closing_positions:
        return

    count = len(_closing_positions)
    print(f"\n[{datetime.now(KST).strftime('%H:%M:%S')} KST] 종가베팅 시초가 매도 ({count}개)")
    notifier.send(f"🌅 09:00 - 종가베팅 포지션 {count}개 시초가 매도 시작")

    for code, pos in list(_closing_positions.items()):
        _execute_closing_sell(code, pos, "종가베팅 시초가 매도")
        time.sleep(0.5)


def run_closing_bet_screening() -> None:
    """14:00 - 종가베팅 워치리스트 구성"""
    if not is_trading_day():
        return

    global _closing_watchlist, _last_closing_summary
    print(f"\n[{datetime.now(KST).strftime('%H:%M:%S')} KST] 종가베팅 스크리닝 시작")
    notifier.send("⏰ 오후 2시 - 종가베팅 후보 스크리닝 시작")

    try:
        candidates = screener.screen_closing_bet_candidates(top_n=20)

        approved = []
        ai_rejected = []
        ai_fail_count = 0
        for c in candidates:
            result = ai_analyzer.analyze_closing_bet(
                c["name"], c["code"], c["change_rate"],
                rsi=c.get("rsi"), vol_ratio=c.get("vol_ratio"),
                current=c.get("current"), ma5=c.get("ma5"),
            )
            c["buy"] = ai_analyzer.is_approved(result)
            c["strength"] = result["strength"]
            c["reason"] = result["reason"]
            if result["reason"] == "분석 실패":
                ai_fail_count += 1
            if c["buy"]:
                approved.append(c)
            else:
                ai_rejected.append({
                    "name": c["name"], "reason": result["reason"],
                })
            time.sleep(2)

        if candidates and ai_fail_count == len(candidates):
            approved = candidates
            ai_rejected = []
            for c in approved:
                c.setdefault("reason", "AI 분석 불가 (기술적 조건 통과)")

        _last_closing_summary = {
            "stats": screener.get_last_closing_stats(),
            "candidates": len(candidates),
            "approved": len(approved),
            "ai_rejected": ai_rejected,
        }
        _closing_watchlist = approved

        if approved:
            lines = [f"🌙 <b>종가베팅 워치리스트 {len(approved)}개</b>\n"]
            for c in approved:
                lines.append(
                    f"🟣 {c['name']}({c['code']}) {c['change_rate']:+.1f}%\n"
                    f"   RSI:{c.get('rsi', 0):.0f} / 거래량{c.get('vol_ratio', 0):.1f}x\n"
                    f"   사유: {c.get('reason', '-')}"
                )
            notifier.send("\n".join(lines))
        else:
            notifier.send(_format_empty_watchlist_msg("종가"))

        print(f"[종가베팅 스크리닝 완료] {len(approved)}개")

    except Exception as e:
        msg = f"종가베팅 스크리닝 오류: {e}"
        print(msg)
        notifier.notify_error(msg)


def run_morning_screening() -> bool:
    """09:05 - 워치리스트 구성. 성공 True / 실패 False"""
    if not is_trading_day():
        return False

    global _watchlist, _last_morning_summary
    print(f"\n[{datetime.now(KST).strftime('%H:%M:%S')} KST] 오전 스크리닝 시작")
    _update_capital()  # 실제 예수금으로 투자 한도 자동 조절
    notifier.send("⏰ 오전 9시 05분 - 장 시작 후 워치리스트 구성 시작")

    try:
        candidates = screener.screen_candidates(top_n=30)
        stats = screener.get_last_screen_stats()

        approved = []
        ai_rejected = []
        ai_fail_count = 0
        for c in candidates:
            result = ai_analyzer.analyze(
                c["name"], c["code"], c["change_rate"],
                strategy=c.get("strategy", ""),
                rsi=c.get("rsi"), vol_ratio=c.get("vol_ratio"),
                current=c.get("current"), ma5=c.get("ma5"),
                w52_gap=c.get("w52_gap"),
            )
            c["buy"] = ai_analyzer.is_approved(result)
            c["strength"] = result["strength"]
            c["reason"] = result["reason"]
            if result["reason"] == "분석 실패":
                ai_fail_count += 1
            if c["buy"]:
                approved.append(c)
            else:
                ai_rejected.append({
                    "name": c["name"], "code": c["code"],
                    "strategy": c.get("strategy", ""),
                    "reason": result["reason"],
                })
            time.sleep(2)  # Groq API 속도 제한 방지 (분당 30회)

        # AI가 전부 실패한 경우 → 기술적 조건 통과 종목만으로 진행
        ai_unavailable = candidates and ai_fail_count == len(candidates)
        if ai_unavailable:
            notifier.send(
                "⚠️ <b>AI 분석 서버 일시 불가</b>\n"
                "Groq API 전체 다운 → 기술적 조건 통과 종목으로 진행합니다."
            )
            approved = candidates
            ai_rejected = []
            for c in approved:
                c.setdefault("reason", "AI 분석 불가 (기술적 조건 통과)")

        _last_morning_summary = {
            "stats": stats,
            "candidates": len(candidates),
            "approved": len(approved),
            "ai_rejected": ai_rejected,
        }
        _watchlist = approved

        if approved:
            ai_note = " (AI 미적용)" if ai_unavailable else ""
            lines = [f"🔍 <b>장중매매 워치리스트 {len(approved)}개{ai_note}</b>\n"]
            strategy_map = {"상단매매": "🔴", "돌파매매": "🟡", "하단매매": "🔵", "낙폭반등": "🔶"}
            for c in approved:
                emoji = strategy_map.get(c.get("strategy", ""), "⚪")
                lines.append(
                    f"{emoji} {c['name']}({c['code']}) - {c.get('strategy', '')}\n"
                    f"   MA5:{c.get('ma5', 0):.0f} / 현재:{c.get('current', 0):.0f} / RSI:{c.get('rsi', 0):.0f}"
                )
            notifier.send("\n".join(lines))
        else:
            notifier.send(_format_empty_watchlist_msg("장중"))

        print(f"[스크리닝 완료] 워치리스트 {len(approved)}개")
        return True

    except Exception as e:
        msg = f"오전 스크리닝 오류: {e}"
        print(msg)
        notifier.notify_error(msg)
        return False


def run_market_check() -> None:
    """5분마다 - 진입/청산 조건 체크"""
    if not is_trading_day():
        return

    if is_exit_time():
        _check_exit()

    if crash_bounce.is_entry_window():
        _check_crash_bounce_entry()

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

        # 종가베팅 포지션 현황
        closing_lines = []
        for code, pos in _closing_positions.items():
            try:
                info = kis_api.get_stock_info(code)
                current = float(info.get("stck_prpr", pos["buy_price"]))
                profit_pct = (current - pos["buy_price"]) / pos["buy_price"] * 100
                closing_lines.append(
                    f"  🌙{pos['name']}: {profit_pct:+.1f}% (매수일:{pos.get('buy_date','-')})"
                )
                time.sleep(0.3)
            except Exception:
                closing_lines.append(f"  🌙{pos['name']}: 조회 실패")

        # 워치리스트 종목 중 진입 미충족 종목 정리
        waiting_lines = []
        for s in _watchlist:
            code = s["code"]
            if code not in _positions:
                skip = s.get("_skip_reason", "조건 대기 중")
                waiting_lines.append(
                    f"  ⏳{s['name']}({s.get('strategy','')}): {skip}"
                )

        lines = [
            "📊 <b>오전 11시 상태 보고</b>",
            f"모드: {os.getenv('KIS_MODE', '알 수 없음')}",
            f"장중매매 - 워치리스트: {len(_watchlist)}개 / 보유: {len(_positions)}개",
            f"장중 투자금: {_total_invested_today:,}원 / {MAX_TOTAL_AMOUNT:,}원",
            f"낙폭반등: {sum(1 for p in _positions.values() if p.get('strategy') == '낙폭반등')}개 / "
            f"{_crash_bounce_invested_today:,}원 / {crash_bounce.MAX_AMOUNT:,}원",
            f"종가베팅 - 워치리스트: {len(_closing_watchlist)}개 / 보유: {len(_closing_positions)}개",
            f"종가베팅 투자금: {_closing_invested_today:,}원 / {MAX_CLOSING_AMOUNT:,}원",
        ]
        if pos_lines:
            lines.append("📌 장중 보유 종목:")
            lines.extend(pos_lines)
        if closing_lines:
            lines.append("🌙 종가베팅 보유 종목:")
            lines.extend(closing_lines)
        if waiting_lines:
            lines.append("⏳ 진입 대기 중 (조건 미충족):")
            lines.extend(waiting_lines[:5])  # 최대 5개만 표시

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
        lines = [f"📋 <b>오늘 장마감 보고 ({today})</b>", "매매 없음 (체결 종목 없음)\n"]
        if not _watchlist:
            summary = _last_morning_summary
            stats = summary.get("stats") or screener.get_last_screen_stats()
            if stats.get("pool", 0) > 0:
                lines.append(
                    f"📊 장중 스크리닝: 후보 {stats.get('pool',0)}개 → "
                    f"기술조건 {stats.get('technical_pass',0)}개 → "
                    f"워치리스트 {summary.get('approved', 0)}개"
                )
                if stats.get("technical_pass", 0) == 0:
                    lines.append("   ❌ 종산 기술조건(거래량·신고가·RSI) 충족 종목 없음")
                elif summary.get("ai_rejected"):
                    lines.append(f"   ❌ AI 매수 거절 {len(summary['ai_rejected'])}개")
            else:
                lines.append("📭 워치리스트 0개 - 스크리닝 조건 충족 종목 없음")
        else:
            lines.append(f"📋 워치리스트 {len(_watchlist)}개 있었으나 실시간 진입조건 미충족:")
            for s in _watchlist[:5]:
                skip = s.get("_skip_reason", "진입조건 미충족")
                lines.append(f"  ❌ {s['name']}({s.get('strategy','')}): {skip}")
        notifier.send("\n".join(lines))
        return

    win_trades  = [t for t in _trades_today if t["profit_won"] > 0]
    lose_trades = [t for t in _trades_today if t["profit_won"] <= 0]
    total_profit = sum(t["profit_won"] for t in win_trades)
    total_loss   = sum(t["profit_won"] for t in lose_trades)
    net = total_profit + total_loss

    intraday = [t for t in _trades_today if t.get("strategy") != "종가베팅"]
    closing_bet = [t for t in _trades_today if t.get("strategy") == "종가베팅"]

    lines = [f"📋 <b>오늘 장마감 보고 ({today})</b>\n"]

    if intraday:
        lines.append("━━ 📈 장중매매 ━━")
        for t in intraday:
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

    if closing_bet:
        lines.append("\n━━ 🌙 종가베팅 ━━")
        for t in closing_bet:
            is_profit = t["profit_won"] > 0
            emoji = "📈" if is_profit else "📉"
            sign  = "+" if is_profit else ""
            lines.append(
                f"{emoji} <b>{t['name']} ({t['code']})</b>\n"
                f"   매수가: {t['buy_price']:,}원 | 수량: {t['quantity']}주\n"
                f"   매도가: {t['sell_price']:,}원\n"
                f"   매수사유: {t.get('buy_reason', '-')}\n"
                f"   결과: {sign}{t['profit_pct']}% ({sign}{t['profit_won']:,}원)\n"
                f"   매도사유: {t.get('sell_reason', '-')}"
            )

    # 종가베팅 미청산 포지션 (오늘 매수, 내일 매도 예정)
    if _closing_positions:
        lines.append(f"\n🌙 종가베팅 오버나이트 {len(_closing_positions)}개 보유 (내일 09:00 시초가 매도)")
        for code, pos in _closing_positions.items():
            lines.append(f"   {pos['name']}({code}) {pos['buy_price']:,}원 {pos['quantity']}주")

    # 워치리스트에 있었지만 미진입 종목
    traded_codes = {t["code"] for t in _trades_today}
    missed = [s for s in _watchlist if s["code"] not in traded_codes]
    if missed:
        lines.append("\n📋 워치리스트 미진입 종목:")
        for s in missed[:3]:
            skip = s.get("_skip_reason", "진입조건 미충족")
            lines.append(f"  ❌ {s['name']}({s.get('strategy','')}): {skip}")

    lines.append("")
    lines.append("─────────────────────")
    lines.append(f"총 매매: {len(_trades_today)}건 (장중 {len(intraday)} + 종가베팅 {len(closing_bet)})")
    lines.append(f"  익절 {len(win_trades)}건: +{total_profit:,}원")
    lines.append(f"  손절 {len(lose_trades)}건: {total_loss:,}원")
    net_sign = "+" if net >= 0 else ""
    lines.append(f"\n🏁 오늘 순손익: <b>{net_sign}{net:,}원</b>")

    notifier.send("\n".join(lines))


def run_force_close() -> None:
    """14:50 - 장중매매 잔여 포지션 강제 청산 (종가베팅 포지션은 오버나이트 유지)"""
    if not is_trading_day():
        return
    if not _positions:
        msg = "✅ 14:50 - 장중 포지션 없음, 청산 불필요"
        if _closing_positions:
            msg += f"\n🌙 종가베팅 {len(_closing_positions)}개 오버나이트 유지"
        notifier.send(msg)
        return

    print(f"\n[{datetime.now(KST).strftime('%H:%M:%S')} KST] 강제 청산 시작 ({len(_positions)}개)")
    closing_note = f" | 🌙 종가베팅 {len(_closing_positions)}개 오버나이트 유지" if _closing_positions else ""
    notifier.send(f"⏰ 14시 50분 - 장중 잔여 포지션 {len(_positions)}개 강제 청산{closing_note}")

    for code, pos in list(_positions.items()):
        if pos["name"] in SELL_BLACKLIST:
            notifier.send(f"🚫 {pos['name']} - 매도 금지 종목, 보유 유지")
            continue
        _execute_sell(code, pos, "장 마감 전 강제 청산")
        time.sleep(0.5)


# ── 종가베팅 진입/청산 로직 ────────────────────────────────────────────────────

def _check_closing_bet_entry() -> None:
    """종가베팅 매수 체크 (14:20~14:50, 5분마다)"""
    if not _closing_watchlist:
        return

    global _closing_invested_today
    remaining = MAX_CLOSING_AMOUNT - _closing_invested_today
    if remaining <= 0:
        return

    already_held = set(_closing_positions.keys())

    for stock in _closing_watchlist:
        code = stock["code"]
        name = stock["name"]

        if code in already_held:
            continue

        try:
            current = kis_api.get_current_price(code, fallback=stock.get("current"))
            if current == 0:
                continue

            buy_amount = min(MAX_CLOSING_BUY, remaining)
            quantity = buy_amount // int(current)
            if quantity < 1:
                notifier.send(f"⚠️ {name}: 종가베팅 금액 부족 (현재가 {int(current):,}원)")
                continue

            result = kis_api.buy_stock(code, quantity)
            if result.get("rt_cd") == "0":
                invested = quantity * int(current)
                _closing_invested_today += invested
                _closing_positions[code] = {
                    "name": name,
                    "quantity": quantity,
                    "buy_price": int(current),
                    "strategy": "종가베팅",
                    "buy_reason": stock.get("reason", ""),
                    "buy_date": _today_kst(),
                }
                _save_state()
                already_held.add(code)
                remaining = MAX_CLOSING_AMOUNT - _closing_invested_today
                notifier.notify_buy(name, code, quantity, int(current), f"[종가베팅] {stock.get('reason', '')}")

                if remaining <= 0:
                    break
            else:
                msg = result.get("msg1", "알 수 없는 오류")
                notifier.notify_error(f"{name} 종가베팅 매수 실패: {msg}")

            time.sleep(0.5)

        except Exception as e:
            notifier.notify_error(f"{name} 종가베팅 진입 오류: {e}")


def _execute_closing_sell(code: str, pos: dict, reason: str) -> None:
    """종가베팅 포지션 매도 및 손익 기록"""
    name = pos["name"]
    quantity = pos["quantity"]

    try:
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
                "strategy": "종가베팅",
            })
            del _closing_positions[code]
            _save_state()
            notifier.notify_sell(name, code, quantity, profit_pct, reason)
        else:
            msg = result.get("msg1", "알 수 없는 오류")
            notifier.notify_error(f"{name} 종가베팅 매도 실패: {msg}")

    except Exception as e:
        notifier.notify_error(f"{name} 종가베팅 매도 오류: {e}")


# ── 낙폭반등 진입 로직 ─────────────────────────────────────────────────────────

def _crash_bounce_position_count() -> int:
    return sum(1 for p in _positions.values() if p.get("strategy") == crash_bounce.STRATEGY)


def _check_crash_bounce_entry() -> None:
    """낙폭반등 매수 (09:10~10:30, ENABLE_CRASH_BOUNCE=true)"""
    if not crash_bounce.is_enabled() or not crash_bounce.is_entry_window():
        return

    global _crash_bounce_invested_today

    if _crash_bounce_position_count() >= crash_bounce.MAX_POSITIONS:
        return

    remaining = crash_bounce.MAX_AMOUNT - _crash_bounce_invested_today
    if remaining <= 0:
        return

    already_held = set(_positions.keys())

    try:
        candidates, api_used = crash_bounce.scan_candidates()
        print(f"[낙폭반등] 스캔 {len(candidates)}개 후보 (API {api_used}회)")
    except Exception as e:
        notifier.notify_error(f"낙폭반등 스캔 오류: {e}")
        return

    for stock in candidates:
        code = stock["code"]
        name = stock["name"]

        if code in already_held:
            continue

        try:
            current = kis_api.get_current_price(code, fallback=stock.get("current"))
            if current == 0:
                continue

            buy_amount = min(crash_bounce.MAX_BUY, remaining)
            quantity = buy_amount // int(current)
            if quantity < 1:
                continue

            result = kis_api.buy_stock(code, quantity)
            if result.get("rt_cd") == "0":
                invested = quantity * int(current)
                _crash_bounce_invested_today += invested
                _positions[code] = {
                    "name": name,
                    "quantity": quantity,
                    "buy_price": int(current),
                    "peak_price": int(current),
                    "strategy": crash_bounce.STRATEGY,
                    "buy_reason": stock.get("reason", ""),
                    "exit_ma60": stock.get("ma60", 0),
                    "ma_period": stock.get("ma_period", 60),
                }
                _save_state()
                already_held.add(code)
                remaining = crash_bounce.MAX_AMOUNT - _crash_bounce_invested_today
                notifier.notify_buy(
                    name, code, quantity, int(current),
                    f"[낙폭반등] {stock.get('reason', '')}",
                )
                print(f"[낙폭반등] 매수 {name}({code}) {quantity}주 @ {int(current):,}")

                if remaining <= 0 or _crash_bounce_position_count() >= crash_bounce.MAX_POSITIONS:
                    break
            else:
                msg = result.get("msg1", "알 수 없는 오류")
                notifier.notify_error(f"{name} 낙폭반등 매수 실패: {msg}")

            time.sleep(0.5)

        except Exception as e:
            notifier.notify_error(f"{name} 낙폭반등 진입 오류: {e}")


# ── 장중매매 진입 로직 ─────────────────────────────────────────────────────────

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
            current = kis_api.get_current_price(code, fallback=stock.get("current"))
            if current == 0:
                continue

            # 오전 스크리닝에서 계산된 지표 활용 (재호출 없이 빠른 체크)
            ma5 = stock.get("ma5", 0)
            ma20 = stock.get("ma20", 0)
            high_200 = stock.get("high_200", 0)
            high_20 = stock.get("high_20", 0)
            rsi = stock.get("rsi", 50.0)

            # 스크리닝 때 저장된 52주 신고가 (상단매매 기준)
            w52_high = stock.get("w52_high", 0)

            entry_ok = False
            skip_reason = ""
            if strategy == "상단매매":
                if current < ma5:
                    skip_reason = f"MA5 하회 ({current:,.0f} < {ma5:,.0f})"
                elif w52_high <= 0:
                    skip_reason = "52주 신고가 정보 없음"
                elif (w52_high - current) / w52_high * 100 > screener.W52_GAP_UPPER_MAX:
                    gap = (w52_high - current) / w52_high * 100
                    skip_reason = f"52주신고가 권역 이탈 ({gap:.1f}% 하락, {screener.W52_GAP_UPPER_MAX:g}% 초과)"
                else:
                    entry_ok = True
            elif strategy == "하단매매":
                if current < ma5:
                    skip_reason = f"MA5 하회 ({current:,.0f} < {ma5:,.0f})"
                elif ma20 <= 0 or current >= ma20:
                    skip_reason = f"MA20 위 (눌림목 아님, {current:,.0f} >= {ma20:,.0f})"
                elif rsi > screener.LOWER_RSI_MAX:
                    skip_reason = f"RSI 과매도 아님 ({rsi:.0f} > {screener.LOWER_RSI_MAX:g})"
                else:
                    entry_ok = True
            elif strategy == "돌파매매":
                if high_20 <= 0 or current < high_20 * 0.995:
                    skip_reason = f"20일고가 미돌파 ({current:,.0f} < {high_20*0.995:,.0f})"
                elif current < ma5:
                    skip_reason = f"MA5 하회 ({current:,.0f} < {ma5:,.0f})"
                else:
                    entry_ok = True

            if not entry_ok:
                print(f"[진입 미충족] {name}({strategy}): {skip_reason}")
                # 워치리스트 종목에 skip_reason 기록 (상태 보고용)
                stock["_skip_reason"] = skip_reason
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
            strategy = pos.get("strategy", "")

            # 낙폭반등: 5분봉 MA 저항·전용 손익
            if strategy == crash_bounce.STRATEGY:
                intra = None
                try:
                    intra = kis_api.get_intraday_5min_indicators(code)
                    time.sleep(0.3)
                except Exception as e:
                    print(f"[낙폭반등 청산] {pos['name']} 분봉 조회 실패: {e}")

                should_sell, reason = crash_bounce.evaluate_exit(pos, current, profit_pct, intra)
                if should_sell:
                    _execute_sell(code, pos, reason, current, profit_pct)
                continue

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
    """자정이 지나 날짜가 바뀌면 일별 데이터 초기화 (종가베팅 오버나이트 포지션은 유지)"""
    global _watchlist, _closing_watchlist, _trades_today
    global _total_invested_today, _closing_invested_today, _crash_bounce_invested_today
    _watchlist = []
    _closing_watchlist = []
    _trades_today = []
    _total_invested_today = 0
    _closing_invested_today = 0
    _crash_bounce_invested_today = 0
    # _closing_positions는 초기화 안 함 - 오버나이트 포지션 유지
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
    closing_pos_note = f"\n🌙 종가베팅 오버나이트 {len(_closing_positions)}개 보유 중" if _closing_positions else ""
    crash_note = ""
    if crash_bounce.is_enabled():
        crash_note = (
            f"\n🔶 낙폭반등: {os.getenv('CRASH_BOUNCE_ENTRY_START', '09:10')}~"
            f"{os.getenv('CRASH_BOUNCE_ENTRY_END', '10:30')} / "
            f"한도 {crash_bounce.MAX_AMOUNT:,}원"
        )
    notifier.send(
        f"🤖 자동매매 봇 시작 (장중매매 + 종가베팅) - {now_kst}\n"
        "📌 장중매매: 09:05 스크리닝 → 09:10~14:30 진입 → 14:50 강제청산\n"
        "🌙 종가베팅: 14:00 스크리닝 → 14:20~14:50 매수 → 익일 09:00 시초가 매도\n"
        f"✅ 익절 트레일링 +{TAKE_PROFIT_PCT}% / 손절 -{STOP_LOSS_PCT}% / 15:10 손익보고"
        f"{crash_note}"
        f"{closing_pos_note}"
    )

    print("KST 직접 체크 루프 시작 (schedule 라이브러리 미사용)")
    print(f"현재 KST: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}")

    # ── 봇 재시작 시 즉시 실행 체크 ──────────────────────────────────────────────
    _init_t = datetime.now(KST)
    _init_min = _init_t.hour * 60 + _init_t.minute
    _init_today = _init_t.strftime("%Y-%m-%d")

    if is_trading_day():
        # 09:00~09:10 재시작: 종가베팅 포지션 즉시 매도
        if 9 * 60 <= _init_min <= 9 * 60 + 10 and _closing_positions:
            notifier.send("▶️ 봇 재시작 감지 (09:00 시간대) - 종가베팅 즉시 매도")
            _last_ran["closing_sell"] = _init_today
            run_morning_sell_closing_bet()

        # 09:05~09:30 재시작: 장중매매 스크리닝 즉시 실행
        if 9 * 60 + 5 <= _init_min <= 9 * 60 + 30 and not _watchlist:
            notifier.send("▶️ 봇 재시작 감지 (스크리닝 시간) - 즉시 스크리닝 실행")
            if run_morning_screening():
                _last_ran["screening_ok"] = _init_today

    last_5min_slot = -1       # 장중매매 5분 슬롯
    last_closing_slot = -1    # 종가베팅 5분 슬롯
    last_screening_slot = -1  # 스크리닝 5분 재시도 슬롯

    while True:
        now = datetime.now(KST)
        today = now.strftime("%Y-%m-%d")
        t = now.hour * 60 + now.minute  # 자정 기준 분 단위 (예: 09:10 → 550)

        # 날짜가 바뀌면 일별 상태 초기화
        if _last_ran.get("date") and _last_ran["date"] != today:
            _reset_daily_state()
            last_5min_slot = -1
            last_closing_slot = -1
            last_screening_slot = -1
        _last_ran["date"] = today

        # ── 09:00~09:10 KST - 종가베팅 시초가 매도 ──────────────────────────
        if 9 * 60 <= t <= 9 * 60 + 10 and _last_ran.get("closing_sell") != today:
            _last_ran["closing_sell"] = today
            run_morning_sell_closing_bet()

        # ── 09:05~09:30 KST - 장중매매 워치리스트 스크리닝 (실패 시 5분마다 재시도) ──
        if 9 * 60 + 5 <= t <= 9 * 60 + 30 and _last_ran.get("screening_ok") != today:
            slot = t // 5
            if slot != last_screening_slot:
                last_screening_slot = slot
                if run_morning_screening():
                    _last_ran["screening_ok"] = today

        # ── 09:10~14:45 KST - 5분마다 장중 진입/청산 체크 ───────────────────
        if 9 * 60 + 10 <= t <= 14 * 60 + 45:
            slot = t // 5
            if slot != last_5min_slot:
                last_5min_slot = slot
                run_market_check()

        # ── 11:00~11:10 KST - 상태 보고 ─────────────────────────────────────
        if 11 * 60 <= t <= 11 * 60 + 10 and _last_ran.get("status") != today:
            _last_ran["status"] = today
            run_status_report()

        # ── 14:00~14:15 KST - 종가베팅 스크리닝 ─────────────────────────────
        if 14 * 60 <= t <= 14 * 60 + 15 and _last_ran.get("closing_bet_screening") != today:
            _last_ran["closing_bet_screening"] = today
            run_closing_bet_screening()

        # ── 14:20~14:50 KST - 5분마다 종가베팅 매수 체크 ───────────────────
        if 14 * 60 + 20 <= t <= 14 * 60 + 50:
            slot = t // 5
            if slot != last_closing_slot:
                last_closing_slot = slot
                _check_closing_bet_entry()

        # ── 14:50~15:00 KST - 장중매매 강제 청산 (종가베팅 제외) ────────────
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
