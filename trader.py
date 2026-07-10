"""
자동매매 메인 로직 (장중매매 + 종가베팅)

스케줄 (한국시간 KST):
  09:00 - 종가베팅 포지션 시초가 매도 (전일 보유분)
  09:05 - 장중매매 워치리스트 스크리닝
  09:10 ~ 14:45 - 5분마다 장중매매 진입/청산 체크
  09:10 ~ 10:30 - 5분마다 낙폭반등 체크 (ENABLE_CRASH_BOUNCE=true 시)
  09:15 ~ 10:30 - 5분마다 V자반등 체크 (ENABLE_V_REVERSAL=true 시)
  11:00 - 상태 보고 / 오전 워치리스트 0개 시 보충 스크리닝
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
import v_reversal

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
MAX_CLOSING_BUY = int(os.getenv("MAX_CLOSING_BUY", "500000"))        # 종가베팅 1회 매수
CLOSING_BET_MAX_PER_SLOT = int(os.getenv("CLOSING_BET_MAX_PER_SLOT", "1"))  # 5분 슬롯당 최대 매수 종목
CLOSING_BET_MAX_POSITIONS = int(os.getenv("CLOSING_BET_MAX_POSITIONS", "1"))  # 동시 보유 종목 수
# 장중매매 AI (false=기술 통과만 워치리스트, 종가베팅 AI는 별도 유지)
ENABLE_INTRADAY_AI = os.getenv("ENABLE_INTRADAY_AI", "false").lower() == "true"

_STRENGTH_SCORE = {"강": 40, "중": 28, "약": 15, "없음": 0, "-": 10}
# 장중 전략 우선순위 (상단 > 돌파 > 하단 — 스크리너와 동일)
_STRATEGY_SCORE = {"상단매매": 20, "돌파매매": 12, "하단매매": 5}

_STATE_FILE = "trading_state.json"


def _priority_score(stock: dict, max_buy: int) -> float:
    """매수 우선순위 점수 (높을수록 우선)

    AI 강도·거래량·RSI 적정구간·당일 상승률·구매 가능성·장중 전략을 반영.
    잔액이 부족할 때 점수가 높은 종목부터 매수한다.
    """
    strength = str(stock.get("strength") or "-")
    score = float(_STRENGTH_SCORE.get(strength, 10))

    strategy = str(stock.get("strategy") or "")
    score += float(_STRATEGY_SCORE.get(strategy, 0))

    try:
        vol = float(stock.get("vol_ratio") or 0)
    except (TypeError, ValueError):
        vol = 0.0
    # 거래량 폭주 종목 가점 (상한으로 과대평가 방지)
    score += min(vol, 12.0) * 2.0

    try:
        rate = float(stock.get("change_rate") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    # 상승 모멘텀 (15% 이상은 체감 가중치 감소)
    score += min(max(rate, 0.0), 15.0) * 1.5

    try:
        rsi = float(stock.get("rsi") or 50)
    except (TypeError, ValueError):
        rsi = 50.0
    # 과열 직전(50~65) 가점, 과열(≥72) 감점
    if 50 <= rsi <= 65:
        score += 12.0
    elif 45 <= rsi < 50 or 65 < rsi <= 70:
        score += 6.0
    elif rsi >= 72:
        score -= 8.0

    try:
        price = float(stock.get("current") or 0)
    except (TypeError, ValueError):
        price = 0.0
    # 1회 한도로 최소 1주 살 수 있으면 소폭 가점 (예산 활용)
    if price > 0 and price <= max_buy:
        score += 5.0
        if price <= max_buy * 0.5:
            score += 3.0

    return round(score, 2)


def _sort_watchlist_by_priority(stocks: list[dict], max_buy: int) -> list[dict]:
    """우선순위 점수 내림차순 정렬 (동점이면 등락률 높은 순)"""
    ranked = list(stocks)
    for s in ranked:
        s["priority_score"] = _priority_score(s, max_buy)
    ranked.sort(
        key=lambda x: (x.get("priority_score", 0), x.get("change_rate", 0)),
        reverse=True,
    )
    return ranked


def _closing_priority_score(stock: dict) -> float:
    """종가베팅 조건 부합도 (높을수록 우선 매수)

    AI 강도 + 당일 상승률·거래량·RSI·MA5 위 안착도를 종가베팅 기준으로 가중.
    """
    strength = str(stock.get("strength") or "-")
    score = float(_STRENGTH_SCORE.get(strength, 10))

    try:
        vol = float(stock.get("vol_ratio") or 0)
    except (TypeError, ValueError):
        vol = 0.0
    # 거래량 1.5배(기본) 이상일수록 가점
    score += min(max(vol - screener.VOL_RATIO_MIN, 0), 8.0) * 4.0

    try:
        rate = float(stock.get("change_rate") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    # 당일 상승률 여유 (과열 구간은 상한)
    score += min(max(rate - screener.CLOSING_BET_MIN_RATE, 0), 12.0) * 2.5

    try:
        rsi = float(stock.get("rsi") or 50)
    except (TypeError, ValueError):
        rsi = 50.0
    if 50 <= rsi <= 65:
        score += 15.0
    elif 40 <= rsi < 50 or 65 < rsi <= 72:
        score += 8.0
    elif rsi > 75:
        score -= 10.0

    try:
        current = float(stock.get("current") or 0)
        ma5 = float(stock.get("ma5") or 0)
    except (TypeError, ValueError):
        current, ma5 = 0.0, 0.0
    if ma5 > 0 and current >= ma5:
        gap = (current - ma5) / ma5 * 100
        if 0.5 <= gap <= 6.0:
            score += 12.0
        elif gap <= 10.0:
            score += 6.0

    try:
        price = float(stock.get("current") or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price > 0 and price <= MAX_CLOSING_BUY:
        score += 5.0

    return round(score, 2)


def _sort_closing_watchlist(stocks: list[dict]) -> list[dict]:
    """종가베팅 조건 부합도 내림차순 (동점: 등락률·거래량)"""
    ranked = list(stocks)
    for s in ranked:
        s["priority_score"] = _closing_priority_score(s)
    ranked.sort(
        key=lambda x: (
            x.get("priority_score", 0),
            x.get("change_rate", 0),
            x.get("vol_ratio", 0),
        ),
        reverse=True,
    )
    return ranked


def _refresh_closing_watchlist_prices(stocks: list[dict]) -> list[dict]:
    """매수 직전 현재가 반영 후 우선순위 재계산용"""
    refreshed: list[dict] = []
    for s in stocks:
        item = dict(s)
        try:
            item["current"] = kis_api.get_current_price(
                item["code"], fallback=item.get("current"),
            )
        except Exception as e:
            print(f"[종가베팅] {item.get('name')} 현재가 갱신 실패: {e}")
        refreshed.append(item)
    return refreshed


def _sort_intraday_watchlist(stocks: list[dict]) -> list[dict]:
    return _sort_watchlist_by_priority(stocks, MAX_BUY_AMOUNT)

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

# 오늘 V자반등 투자금 (전용 한도)
_v_reversal_invested_today: int = 0

# 종가베팅 워치리스트 (14:00 스크리닝)
_closing_watchlist: list[dict] = []

# 종가베팅 오버나이트 포지션 { 종목코드: {name, quantity, buy_price, buy_date} }
_closing_positions: dict[str, dict] = {}

# 오늘 종가베팅 투자금
_closing_invested_today: int = 0
_closing_low_cash_notified: set[str] = set()   # 금액 부족 알림 (종목당 1회)
_closing_low_cash_skipped: set[str] = set()     # 금액 부족 종목 재시도 스킵
_closing_depleted_notified: bool = False        # 주문가능금액 0 알림 (1회)
_closing_balance_fail_notified: bool = False    # 예수금 조회 실패 알림 (1회)

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
        "kis_mode": os.getenv("KIS_MODE", "모의"),
        "date": _today_kst(),
        "positions": _positions,
        "total_invested_today": _total_invested_today,
        "crash_bounce_invested_today": _crash_bounce_invested_today,
        "v_reversal_invested_today": _v_reversal_invested_today,
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
    global _v_reversal_invested_today
    if not os.path.exists(_STATE_FILE):
        return
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        current_mode = os.getenv("KIS_MODE", "모의")
        saved_mode = state.get("kis_mode")
        if saved_mode and saved_mode != current_mode:
            print(
                f"[상태 초기화] KIS_MODE 변경 ({saved_mode} → {current_mode}) — "
                "모의/실전 포지션 상태를 불러오지 않습니다"
            )
            return

        # 종가베팅 포지션은 날짜와 무관하게 항상 불러옴 (오버나이트 포지션)
        _closing_positions = state.get("closing_positions", {})
        if _closing_positions:
            print(f"[상태 복원] 종가베팅 포지션 {len(_closing_positions)}개 불러옴 (오버나이트)")

        # 장중 포지션은 오늘 날짜인 경우만
        if state.get("date") == _today_kst():
            _positions = state.get("positions", {})
            _total_invested_today = state.get("total_invested_today", 0)
            _crash_bounce_invested_today = state.get("crash_bounce_invested_today", 0)
            _v_reversal_invested_today = state.get("v_reversal_invested_today", 0)
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
    is_friday = datetime.now(KST).weekday() == 4
    print(f"\n[{datetime.now(KST).strftime('%H:%M:%S')} KST] 종가베팅 스크리닝 시작")
    if is_friday:
        notifier.send(
            "⏰ 오후 2시 - 종가베팅 스크리닝 시작\n"
            "📅 <b>금요일 모드</b> — 뉴스 호재가 주말~월요일까지 이어질 종목 중심"
        )
    else:
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
                friday_weekend=is_friday,
            )
            c["buy"] = ai_analyzer.is_closing_approved(result, friday_weekend=is_friday)
            c["strength"] = result["strength"]
            c["reason"] = result["reason"]
            if is_friday:
                c["friday_mode"] = True
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
            if is_friday:
                notifier.send(
                    "⚠️ 금요일 AI 분석 불가 — 주말 호재 검토 없이 종가베팅을 진행하지 않습니다."
                )
                approved = []
                ai_rejected = []
            else:
                approved = candidates
                ai_rejected = []
                for c in approved:
                    c.setdefault("reason", "AI 분석 불가 (기술적 조건 통과)")
                    c.setdefault("strength", "약")

        # 조건 부합도 높은 종목부터 매수 (1종목 보유 시 최우선 1개만 체결)
        approved = _sort_closing_watchlist(approved)

        _last_closing_summary = {
            "stats": screener.get_last_closing_stats(),
            "candidates": len(candidates),
            "approved": len(approved),
            "ai_rejected": ai_rejected,
            "friday_mode": is_friday,
        }
        _closing_watchlist = approved

        if approved:
            mode_note = " (금요일·주말호재)" if is_friday else ""
            lines = [f"🌙 <b>종가베팅 워치리스트 {len(approved)}개</b>{mode_note} (조건 부합도순)\n"]
            for i, c in enumerate(approved, 1):
                lines.append(
                    f"{i}. 🟣 {c['name']}({c['code']}) {c['change_rate']:+.1f}%\n"
                    f"   RSI:{c.get('rsi', 0):.0f} / 거래량{c.get('vol_ratio', 0):.1f}x"
                    f" / 점수{c.get('priority_score', 0):.0f}\n"
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


def run_morning_screening(supplementary: bool = False) -> bool:
    """장중 워치리스트 구성. 성공 True / 실패 False

    supplementary=True: 11:00 보충 스크리닝 (오전 워치리스트 0개일 때만 호출)
    """
    if not is_trading_day():
        return False

    global _watchlist, _last_morning_summary
    label = "보충" if supplementary else "오전"
    print(f"\n[{datetime.now(KST).strftime('%H:%M:%S')} KST] {label} 스크리닝 시작")
    if not supplementary:
        _update_capital()  # 실제 예수금으로 투자 한도 자동 조절
        notifier.send("⏰ 오전 9시 05분 - 장 시작 후 워치리스트 구성 시작")
    else:
        notifier.send(
            "🔄 오전 11시 - <b>보충 스크리닝</b> 시작\n"
            "(09:05 워치리스트 0개 → 장중 후보 재탐색)"
        )

    try:
        candidates = screener.screen_candidates(top_n=30)
        stats = screener.get_last_screen_stats()

        approved = []
        ai_rejected = []
        ai_skipped = not ENABLE_INTRADAY_AI
        ai_unavailable = False

        if ai_skipped:
            approved = list(candidates)
            for c in approved:
                c["buy"] = True
                c["strength"] = "-"
                c["reason"] = "기술적 조건 통과 (장중 AI 생략)"
            print(f"[장중 AI 생략] 기술 통과 {len(approved)}개 → 워치리스트")
        else:
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

        # 잔액 부족 시 모멘텀·전략 점수 높은 종목부터 매수
        approved = _sort_intraday_watchlist(approved)

        _last_morning_summary = {
            "stats": stats,
            "candidates": len(candidates),
            "approved": len(approved),
            "ai_rejected": ai_rejected,
            "ai_skipped": ai_skipped,
            "supplementary": supplementary,
        }
        _watchlist = approved

        if approved:
            if ai_skipped:
                ai_note = " (AI 생략)"
            elif ai_unavailable:
                ai_note = " (AI 미적용)"
            else:
                ai_note = ""
            prefix = "🔄 보충" if supplementary else "🔍"
            lines = [
                f"{prefix} <b>장중매매 워치리스트 {len(approved)}개{ai_note}</b> (우선순위순)\n"
            ]
            strategy_map = {"상단매매": "🔴", "돌파매매": "🟡", "하단매매": "🔵", "낙폭반등": "🔶", "V자반등": "🟢"}
            for i, c in enumerate(approved, 1):
                emoji = strategy_map.get(c.get("strategy", ""), "⚪")
                lines.append(
                    f"{i}. {emoji} {c['name']}({c['code']}) - {c.get('strategy', '')}\n"
                    f"   MA5:{c.get('ma5', 0):.0f} / 현재:{c.get('current', 0):.0f}"
                    f" / RSI:{c.get('rsi', 0):.0f} / 점수{c.get('priority_score', 0):.0f}"
                )
            notifier.send("\n".join(lines))
        else:
            if supplementary:
                notifier.send(
                    "🔄 <b>보충 스크리닝 결과</b>\n"
                    + _format_empty_watchlist_msg("장중").replace(
                        "오늘 장중 워치리스트 없음",
                        "보충 스크리닝 후에도 워치리스트 없음",
                    )
                )
            else:
                notifier.send(_format_empty_watchlist_msg("장중"))

        print(f"[{label} 스크리닝 완료] 워치리스트 {len(approved)}개")
        return True

    except Exception as e:
        msg = f"{label} 스크리닝 오류: {e}"
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

    if v_reversal.is_entry_window():
        _check_v_reversal_entry()

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
            f"V자반등: {sum(1 for p in _positions.values() if p.get('strategy') == 'V자반등')}개 / "
            f"{_v_reversal_invested_today:,}원 / {v_reversal.MAX_AMOUNT:,}원",
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
        lines = [f"📋 <b>오늘 장마감 보고 ({today})</b>\n"]
        if _closing_positions:
            lines.append("당일 청산 완료 매매 없음 (종가베팅 오버나이트 보유)\n")
            lines.append(
                f"🌙 종가베팅 오버나이트 {len(_closing_positions)}개 보유 "
                f"(익일 09:00 시초가 매도)"
            )
            for code, pos in _closing_positions.items():
                invested = pos["buy_price"] * pos["quantity"]
                lines.append(
                    f"   {pos['name']}({code}) {pos['buy_price']:,}원 × "
                    f"{pos['quantity']}주 = {invested:,}원"
                )
                if pos.get("buy_reason"):
                    lines.append(f"   매수사유: {pos['buy_reason']}")
            lines.append("")
        else:
            lines.append("매매 없음 (체결 종목 없음)\n")
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
    global _closing_depleted_notified, _closing_balance_fail_notified
    if len(_closing_positions) >= CLOSING_BET_MAX_POSITIONS:
        return

    budget_remaining = MAX_CLOSING_AMOUNT - _closing_invested_today
    if budget_remaining <= 0:
        return

    cash: int | None = None
    try:
        cash = kis_api.get_orderable_cash()
    except Exception as e:
        print(f"[종가베팅] 예수금 조회 실패: {e}")
        if not _closing_balance_fail_notified:
            _closing_balance_fail_notified = True
            notifier.send(
                f"⚠️ 종가베팅 예수금 조회 실패 — 잔여 한도 기준으로 진행\n"
                f"사유: {e}"
            )
        remaining = budget_remaining
    else:
        remaining = min(budget_remaining, cash)
        if remaining <= 0:
            if not _closing_depleted_notified:
                _closing_depleted_notified = True
                notifier.send(
                    f"⚠️ 종가베팅 매수 불가 — 주문가능금액 {cash:,}원 "
                    f"(한도 {budget_remaining:,}원)"
                )
            return

    already_held = set(_closing_positions.keys())
    bought_this_slot = 0
    candidates = [
        s for s in _closing_watchlist
        if s["code"] not in already_held
        and s["code"] not in _closing_low_cash_skipped
    ]
    if not candidates:
        return

    # 매수 직전 현재가 갱신 후 조건 부합도 재정렬 → 1위부터 시도
    pending = _sort_closing_watchlist(_refresh_closing_watchlist_prices(candidates))
    top = pending[0]
    print(
        f"[종가베팅] 매수 1순위: {top['name']}({top['code']}) "
        f"점수 {top.get('priority_score', 0)} / "
        f"{top.get('change_rate', 0):+.1f}% / 거래량 {top.get('vol_ratio', 0):.1f}x"
    )

    for rank, stock in enumerate(pending, 1):
        if bought_this_slot >= CLOSING_BET_MAX_PER_SLOT:
            break
        if remaining <= 0:
            break

        code = stock["code"]
        name = stock["name"]

        try:
            current = kis_api.get_current_price(code, fallback=stock.get("current"))
            if current == 0:
                continue

            buy_amount = min(MAX_CLOSING_BUY, remaining)
            quantity = buy_amount // int(current)
            if quantity < 1:
                # 잔액으로 살 수 없으면 다음 우선순위 종목으로 넘어감 (알림·재시도 각 1회)
                _closing_low_cash_skipped.add(code)
                print(
                    f"[종가베팅] {name} 금액 부족 "
                    f"(현재가 {int(current):,}원 / 잔여 {remaining:,}원) → 다음 후보"
                )
                if code not in _closing_low_cash_notified:
                    _closing_low_cash_notified.add(code)
                    notifier.send(
                        f"⚠️ {name}: 종가베팅 금액 부족 "
                        f"(현재가 {int(current):,}원) → 다음 우선순위 종목으로 진행"
                    )
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
                if cash is not None:
                    remaining = min(remaining, cash - invested)
                    cash = max(0, cash - invested)
                bought_this_slot += 1
                rank_note = f"우선순위 {rank}위 (점수 {stock.get('priority_score', 0)})"
                notifier.notify_buy(
                    name, code, quantity, int(current),
                    f"[종가베팅] {rank_note} · {stock.get('reason', '')}",
                )
                print(
                    f"[종가베팅] 매수 {name}({code}) {quantity}주 @ {int(current):,} "
                    f"({rank_note})"
                )
                break  # 1종목 보유 — 체결 후 추가 시도 없음
            else:
                msg = result.get("msg1", "알 수 없는 오류")
                notifier.notify_error(f"{name} 종가베팅 매수 실패: {msg}")

            time.sleep(kis_api.trade_interval())

        except Exception as e:
            if kis_api.is_systemic_order_error(e):
                skip_names = [
                    s["name"] for s in pending
                    if s["code"] not in already_held and s["code"] != code
                ]
                notifier.notify_error(
                    f"종가베팅 주문 API 일시 오류 — {name} 실패, "
                    f"나머지 {len(skip_names)}종목 스킵"
                    f"{(' (' + ', '.join(skip_names[:3]) + ')') if skip_names else ''}\n"
                    f"사유: {e}\n"
                    f"(모의 API 한도·서버 오류. 5분 후 재시도)"
                )
                break
            notifier.notify_error(f"{name} 종가베팅 진입 오류: {e}")


def _execute_closing_sell(code: str, pos: dict, reason: str) -> None:
    """종가베팅 포지션 매도 및 손익 기록"""
    name = pos["name"]
    quantity = pos["quantity"]

    try:
        current, profit_pct, price_fallback = _sell_reference_price(code, pos["buy_price"])
        sell_reason = reason
        if price_fallback:
            sell_reason = f"{reason} (현재가 조회 실패, 손익은 매수가 기준)"

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
                "sell_reason": sell_reason,
                "buy_reason": pos.get("buy_reason", ""),
                "strategy": "종가베팅",
            })
            del _closing_positions[code]
            _save_state()
            notifier.notify_sell(name, code, quantity, profit_pct, sell_reason)
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


# ── V자반등 진입 로직 ──────────────────────────────────────────────────────────

def _v_reversal_position_count() -> int:
    return sum(1 for p in _positions.values() if p.get("strategy") == v_reversal.STRATEGY)


def _check_v_reversal_entry() -> None:
    """V자반등 매수 (09:15~10:30, ENABLE_V_REVERSAL=true)"""
    if not v_reversal.is_enabled() or not v_reversal.is_entry_window():
        return

    global _v_reversal_invested_today

    if _v_reversal_position_count() >= v_reversal.MAX_POSITIONS:
        return

    remaining = v_reversal.MAX_AMOUNT - _v_reversal_invested_today
    if remaining <= 0:
        return

    already_held = set(_positions.keys())

    try:
        candidates, api_used = v_reversal.scan_candidates()
        print(f"[V자반등] 스캔 {len(candidates)}개 후보 (API {api_used}회)")
    except Exception as e:
        notifier.notify_error(f"V자반등 스캔 오류: {e}")
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

            buy_amount = min(v_reversal.MAX_BUY, remaining)
            quantity = buy_amount // int(current)
            if quantity < 1:
                continue

            result = kis_api.buy_stock(code, quantity)
            if result.get("rt_cd") == "0":
                invested = quantity * int(current)
                _v_reversal_invested_today += invested
                _positions[code] = {
                    "name": name,
                    "quantity": quantity,
                    "buy_price": int(current),
                    "peak_price": int(current),
                    "strategy": v_reversal.STRATEGY,
                    "buy_reason": stock.get("reason", ""),
                    "exit_ma60": stock.get("ma60", 0),
                    "ma_period": stock.get("ma_period", 60),
                }
                _save_state()
                already_held.add(code)
                remaining = v_reversal.MAX_AMOUNT - _v_reversal_invested_today
                notifier.notify_buy(
                    name, code, quantity, int(current),
                    f"[V자반등] {stock.get('reason', '')}",
                )
                print(f"[V자반등] 매수 {name}({code}) {quantity}주 @ {int(current):,}")

                if remaining <= 0 or _v_reversal_position_count() >= v_reversal.MAX_POSITIONS:
                    break
            else:
                msg = result.get("msg1", "알 수 없는 오류")
                notifier.notify_error(f"{name} V자반등 매수 실패: {msg}")

            time.sleep(0.5)

        except Exception as e:
            notifier.notify_error(f"{name} V자반등 진입 오류: {e}")


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

    # 매수 직전에도 우선순위 재정렬 (점수 높은 종목부터)
    pending = _sort_intraday_watchlist([
        s for s in _watchlist
        if s["code"] not in already_held
    ])

    for stock in pending:
        code = stock["code"]
        name = stock["name"]
        strategy = stock.get("strategy", "")

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
                print(
                    f"[장중] {name} 금액 부족 "
                    f"(현재가 {int(current):,}원 / 잔여 {remaining:,}원) → 다음 후보"
                )
                notifier.send(
                    f"⚠️ {name}: 금액 부족 (현재가 {int(current):,}원) "
                    f"→ 다음 우선순위 종목으로 진행"
                )
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
            if kis_api.is_account_error(e):
                notifier.notify_error(
                    f"⚠️ <b>{name} 매수 중단 — 계좌 설정 오류</b>\n{e}"
                )
                break
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

            # V자반등: 트레일링·MA·14:20 시간청산
            if strategy == v_reversal.STRATEGY:
                peak = pos.get("peak_price", pos["buy_price"])
                if current > peak:
                    _positions[code]["peak_price"] = int(current)
                    _save_state()
                    peak = current

                intra = None
                try:
                    intra = kis_api.get_intraday_5min_indicators(code)
                    time.sleep(0.3)
                except Exception as e:
                    print(f"[V자반등 청산] {pos['name']} 분봉 조회 실패: {e}")

                should_sell, reason = v_reversal.evaluate_exit(pos, current, profit_pct, intra)
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


def _sell_reference_price(code: str, buy_price: float) -> tuple[float, float, bool]:
    """매도 손익 계산용 가격. 조회 실패 시 매수가 fallback (시장가 매도는 계속 진행)."""
    try:
        info = kis_api.get_stock_info(code)
        current = float(info.get("stck_prpr", 0))
        if current > 0:
            profit_pct = (current - buy_price) / buy_price * 100
            return current, profit_pct, False
    except Exception as e:
        print(f"[매도] {code} 현재가 조회 실패, 시장가 매도 진행: {e}")
    return float(buy_price), 0.0, True


def _execute_sell(code: str, pos: dict, reason: str,
                  current: float = 0, profit_pct: float = 0) -> None:
    name = pos["name"]
    quantity = pos["quantity"]
    price_fallback = False

    try:
        if current == 0:
            current, profit_pct, price_fallback = _sell_reference_price(code, pos["buy_price"])

        result = kis_api.sell_stock(code, quantity)
        if result.get("rt_cd") == "0":
            sell_reason = reason
            if price_fallback:
                sell_reason = f"{reason} (현재가 조회 실패, 손익은 매수가 기준)"
            profit_won = int((current - pos["buy_price"]) * quantity)
            _trades_today.append({
                "name": name,
                "code": code,
                "quantity": quantity,
                "buy_price": pos["buy_price"],
                "sell_price": int(current),
                "profit_pct": round(profit_pct, 2),
                "profit_won": profit_won,
                "sell_reason": sell_reason,
                "buy_reason": pos.get("buy_reason", ""),
                "strategy": pos.get("strategy", ""),
            })
            del _positions[code]
            _save_state()
            notifier.notify_sell(name, code, quantity, profit_pct, sell_reason)
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
    global _v_reversal_invested_today
    global _closing_low_cash_notified, _closing_low_cash_skipped
    global _closing_depleted_notified, _closing_balance_fail_notified
    _watchlist = []
    _closing_watchlist = []
    _trades_today = []
    _total_invested_today = 0
    _closing_invested_today = 0
    _crash_bounce_invested_today = 0
    _v_reversal_invested_today = 0
    _closing_low_cash_notified = set()
    _closing_low_cash_skipped = set()
    _closing_depleted_notified = False
    _closing_balance_fail_notified = False
    # _closing_positions는 초기화 안 함 - 오버나이트 포지션 유지
    _save_state()
    print(f"[일별 초기화] {_today_kst()} 새 거래일 시작")


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    print("=== KIS 자동매매 시작 (종산 장중매매) ===")
    _load_state()

    acc_ok, acc_msg, orderable_cash = kis_api.verify_trade_account()
    print(f"[계좌 검증] {acc_msg}")
    if orderable_cash is not None:
        print(f"[예수금] 주문가능금액 {orderable_cash:,}원")

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
    v_note = ""
    if v_reversal.is_enabled():
        v_note = (
            f"\n🟢 V자반등: {os.getenv('V_REVERSAL_ENTRY_START', '09:15')}~"
            f"{os.getenv('V_REVERSAL_ENTRY_END', '10:30')} / "
            f"한도 {v_reversal.MAX_AMOUNT:,}원"
        )
    cash_line = (
        f"💰 주문가능금액: {orderable_cash:,}원\n"
        if orderable_cash is not None
        else "💰 주문가능금액: 조회 실패\n"
    )
    notifier.send(
        f"🤖 자동매매 봇 시작 (장중매매 + 종가베팅) - {now_kst}\n"
        f"📍 모드: {os.getenv('KIS_MODE', '모의')}\n"
        f"{'✅' if acc_ok else '⚠️'} 계좌: {acc_msg}\n"
        f"{cash_line}"
        "📌 장중매매: 09:05 스크리닝 → 09:10~14:30 진입 → 14:50 강제청산\n"
        f"   장중 AI: {'ON (Groq)' if ENABLE_INTRADAY_AI else 'OFF (기술조건만)'}\n"
        "🌙 종가베팅: 14:00 스크리닝 → 14:20~14:50 매수 → 익일 09:00 시초가 매도\n"
        f"✅ 익절 트레일링 +{TAKE_PROFIT_PCT}% / 손절 -{STOP_LOSS_PCT}% / 15:10 손익보고"
        f"{crash_note}"
        f"{v_note}"
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

        # ── 11:00~11:10 KST - 보충 스크리닝 (오전 워치리스트 0개) ─────────────
        if 11 * 60 <= t <= 11 * 60 + 10 and _last_ran.get("supplementary_screening") != today:
            _last_ran["supplementary_screening"] = today
            if not _watchlist:
                run_morning_screening(supplementary=True)

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
