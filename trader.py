"""
자동매매 메인 로직 (장중매매 + 종가베팅)

스케줄 (한국시간 KST):
  09:00 - 종가베팅 포지션 시초가 매도 (전일 보유분)
  09:05 - 장중매매 워치리스트 스크리닝
  09:10 ~ 14:45 - 5분마다 장중매매 진입/청산 체크
  09:10 ~ 10:30 - 5분마다 낙폭반등 체크 (ENABLE_CRASH_BOUNCE=true 시)
  09:15 ~ 10:30 - 5분마다 V자반등 체크 (ENABLE_V_REVERSAL=true 시)
  09:15 ~ 14:30 - 강세V 시뮬 (5분·09~10시 2분·후보/보유 1분)
  11:00 - 상태 보고 / 오전 워치리스트 0개 시 보충 스크리닝
  13:15 - 오전 미체결 낙폭반등·V자반등 오후 필터 1회 재검색
  14:00 - 종가베팅 스크리닝
  14:45 ~ 14:50 - AI 종가베팅 매수 1회 (K1 종가는 14:20~14:50)
  14:50 - 장중매매 잔여 포지션 강제 청산 (종가베팅 제외)
  15:10 - 장마감 손익 보고 (손익금) / 금요일 주간 총손익
"""
import os
import json
import time
from datetime import datetime, timedelta
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
import ul_rebound
import k1_closing
import k2_intraday
import k1_plus
import k2_plus
import strong_v_sim

load_dotenv()

MAX_BUY_AMOUNT = int(os.getenv("MAX_BUY_AMOUNT", "500000"))
MAX_TOTAL_AMOUNT = int(os.getenv("MAX_TOTAL_AMOUNT", "1000000"))
SELL_BLACKLIST = [s.strip() for s in os.getenv("SELL_BLACKLIST", "").split(",") if s.strip()]
CLOSING_HOLD_EXCLUDE = [
    s.strip() for s in os.getenv("CLOSING_HOLD_EXCLUDE", "").split(",") if s.strip()
]
CLOSING_ACCOUNT_SYNC = os.getenv("CLOSING_ACCOUNT_SYNC", "false").lower() == "true"
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "2.0"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "3.0"))
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "1.0"))
# 매수 직후 빠른 손절 (가짜 돌파 조기 청산) — 창 지나면 기존 STOP_LOSS_PCT 적용
QUICK_STOP_LOSS_PCT = float(os.getenv("QUICK_STOP_LOSS_PCT", "1.5"))
QUICK_STOP_WINDOW_MIN = int(os.getenv("QUICK_STOP_WINDOW_MIN", "30"))
# 동적 자금 관리: True면 매일 실제 예수금으로 한도 자동 조절
DYNAMIC_CAPITAL = os.getenv("DYNAMIC_CAPITAL", "true").lower() == "true"
# 1회 매수금액 = 예수금의 이 비율 (기본 50%)
BUY_RATIO = float(os.getenv("BUY_RATIO", "0.5"))
# 종가베팅 자금 한도 (별도 관리)
MAX_CLOSING_AMOUNT = int(os.getenv("MAX_CLOSING_AMOUNT", "500000"))  # 종가베팅 총 한도
MAX_CLOSING_BUY = int(os.getenv("MAX_CLOSING_BUY", "500000"))        # 종가베팅 1회 매수
CLOSING_BET_MAX_PER_SLOT = int(os.getenv("CLOSING_BET_MAX_PER_SLOT", "1"))  # 5분 슬롯당 최대 매수 종목
CLOSING_BET_MAX_POSITIONS = int(os.getenv("CLOSING_BET_MAX_POSITIONS", "1"))  # 동시 보유 종목 수
CLOSING_BET_OVERHEAT_RSI = float(os.getenv("CLOSING_BET_OVERHEAT_RSI", "72"))  # 1순위 RSI 과열 기준
CLOSING_BET_TRY_TOP_N = int(os.getenv("CLOSING_BET_TRY_TOP_N", "2"))  # 1순위 과열 시 2순위까지 시도
# 장중매매 AI (false=기술 통과만 워치리스트, 종가베팅 AI는 별도 유지)
ENABLE_INTRADAY_AI = os.getenv("ENABLE_INTRADAY_AI", "false").lower() == "true"
# 낙폭반등·V자반등 오전 미체결 시 오후 필터 1회 재검색
AFTERNOON_REBOUND_SCAN_MIN = 13 * 60 + 15

_STRENGTH_SCORE = {"강": 40, "중": 28, "약": 15, "없음": 0, "-": 10}
# 장중 전략 우선순위 (상단 > 돌파 > 하단 — 스크리너와 동일)
_STRATEGY_SCORE = {"상단매매": 20, "돌파매매": 12, "하단매매": 5}

_STATE_FILE = "trading_state.json"


def _parse_hhmm_env(name: str, default_h: int, default_m: int) -> int:
    try:
        h, m = os.getenv(name, f"{default_h:02d}:{default_m:02d}").strip().split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return default_h * 60 + default_m


CLOSING_BET_ENTRY_START = _parse_hhmm_env("CLOSING_BET_ENTRY_START", 14, 45)
CLOSING_BET_ENTRY_END = _parse_hhmm_env("CLOSING_BET_ENTRY_END", 14, 50)


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


def _is_closing_overheated(stock: dict) -> bool:
    """1순위 과열 시 2순위로 넘길 때 사용 (RSI 과열)."""
    try:
        rsi = float(stock.get("rsi") or 50)
    except (TypeError, ValueError):
        rsi = 50.0
    return rsi > CLOSING_BET_OVERHEAT_RSI


def _refresh_closing_watchlist_live(stocks: list[dict]) -> list[dict]:
    """매수 직전 현재가·등락률·RSI·거래량 갱신 후 우선순위 재계산용."""
    refreshed: list[dict] = []
    for s in stocks:
        item = dict(s)
        code = item.get("code", "")
        try:
            info = kis_api.get_stock_info(code)
            current = float(info.get("stck_prpr") or 0)
            rate = float(info.get("prdy_ctrt") or item.get("change_rate") or 0)
            if current > 0:
                item["current"] = current
            item["change_rate"] = rate

            ind = kis_api.get_chart_indicators(code)
            time.sleep(0.2)
            if ind:
                item["rsi"] = ind.get("rsi", item.get("rsi", 50))
                item["vol_ratio"] = ind.get("vol_ratio", item.get("vol_ratio", 0))
                item["ma5"] = ind.get("ma5", item.get("ma5", 0))
        except Exception as e:
            print(f"[종가베팅] {item.get('name')} 실시간 갱신 실패: {e}")
        refreshed.append(item)
    return refreshed


def _sort_intraday_watchlist(stocks: list[dict]) -> list[dict]:
    return _sort_watchlist_by_priority(stocks, MAX_BUY_AMOUNT)

# KRX 휴장일 (평일만 실질 적용 — 토·일은 is_trading_day에서 별도 차단)
# 2026: 거래소 공지·보도 기준 전일 휴장 (임시공휴일 추가 시 수동 갱신)
_KR_HOLIDAYS = {
    # ── 2026 (확정) ───────────────────────────────────────────────────────
    "2026-01-01",  # 신정
    "2026-02-16", "2026-02-17", "2026-02-18",  # 설 연휴
    "2026-03-02",  # 삼일절 대체 (3/1 일)
    "2026-05-01",  # 근로자의 날 (KRX 자체 휴장)
    "2026-05-05",  # 어린이날
    "2026-05-25",  # 부처님오신날 대체 (5/24 일)
    "2026-06-03",  # 전국동시지방선거
    "2026-06-06",  # 현충일 (토)
    "2026-07-17",  # 제헌절
    "2026-08-15",  # 광복절 (토)
    "2026-08-17",  # 광복절 대체
    "2026-09-24", "2026-09-25", "2026-09-26",  # 추석 연휴
    "2026-10-03",  # 개천절 (토)
    "2026-10-05",  # 개천절 대체
    "2026-10-09",  # 한글날
    "2026-12-25",  # 성탄절
    "2026-12-31",  # 연말 휴장
    # ── 2027 (잠정 — 음력·대선·대체공휴일은 연초 KRX 공지로 재확인) ─────
    "2027-01-01",  # 신정
    "2027-02-08", "2027-02-09",  # 설 연휴·대체 (2/6~7 주말)
    "2027-03-01",  # 삼일절
    "2027-03-03",  # 대통령 선거(잠정)
    "2027-05-01",  # 근로자의 날 (토)
    "2027-05-05",  # 어린이날
    "2027-05-13",  # 부처님오신날(잠정)
    "2027-06-06",  # 현충일 (일)
    "2027-07-17",  # 제헌절 (토)
    "2027-07-19",  # 제헌절 대체(잠정)
    "2027-08-16",  # 광복절 대체 (8/15 일)
    "2027-09-14", "2027-09-15", "2027-09-16",  # 추석 연휴(잠정)
    "2027-10-04",  # 개천절 대체 (10/3 일)
    "2027-10-11",  # 한글날 대체 (10/9 토)
    "2027-12-27",  # 성탄절 대체 (12/25 토)
    "2027-12-31",  # 연말 휴장
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

# 장중 금액 부족 알림 (종목당 1회)
_intraday_low_cash_notified: set[str] = set()
# 당일 매도한 장중 종목 — 재진입 금지
_sold_codes_today: set[str] = set()

# 종가베팅 워치리스트 (14:00 스크리닝)
_closing_watchlist: list[dict] = []

# 종가베팅 오버나이트 포지션 { 종목코드: {name, quantity, buy_price, buy_date} }
_closing_positions: dict[str, dict] = {}

# K1 종가베팅 포지션 (4일 보유, 금·월 매수)
_k1_closing_positions: dict[str, dict] = {}

# 오늘 종가베팅 투자금
_closing_invested_today: int = 0
_closing_low_cash_notified: set[str] = set()   # 금액 부족 알림 (종목당 1회)
_closing_low_cash_skipped: set[str] = set()     # 금액 부족 종목 재시도 스킵
_closing_depleted_notified: bool = False        # 주문가능금액 0 알림 (1회)
_closing_balance_fail_notified: bool = False    # 예수금 조회 실패 알림 (1회)

# 오늘 체결된 매도 기록 (장중 + 종가베팅 모두 포함, 손익 보고용)
# { name, code, quantity, buy_price, sell_price, profit_pct, profit_won, reason, strategy }
_trades_today: list[dict] = []

# 일별 손익 장부 (주간 합산용)
# [{date, profit_won, trades, sim_profit_won, sim_trades}]
_daily_pnl_ledger: list[dict] = []

# 오늘 스크리닝 요약 (0개일 때 이유 보고용)
_last_morning_summary: dict = {}
_last_closing_summary: dict = {}


# ── 상태 저장/복원 ─────────────────────────────────────────────────────────────

def _today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _position_buy_cost(pos: dict) -> int:
    try:
        return max(int(pos.get("buy_price") or 0) * int(pos.get("quantity") or 0), 0)
    except (TypeError, ValueError):
        return 0


def _release_intraday_budget(pos: dict) -> None:
    """장중 매도 시 전략별 당일 투자 한도 복구."""
    global _total_invested_today, _crash_bounce_invested_today, _v_reversal_invested_today
    cost = _position_buy_cost(pos)
    if cost <= 0:
        return
    strategy = pos.get("strategy", "")
    if strategy == crash_bounce.STRATEGY:
        _crash_bounce_invested_today = max(0, _crash_bounce_invested_today - cost)
    elif strategy == v_reversal.STRATEGY:
        _v_reversal_invested_today = max(0, _v_reversal_invested_today - cost)
    else:
        _total_invested_today = max(0, _total_invested_today - cost)


def _mark_sold_today(code: str) -> None:
    """당일 매도 종목 재진입 금지 등록."""
    if code:
        _sold_codes_today.add(code)


def _save_state() -> None:
    state = {
        "kis_mode": os.getenv("KIS_MODE", "모의"),
        "date": _today_kst(),
        "positions": _positions,
        "total_invested_today": _total_invested_today,
        "crash_bounce_invested_today": _crash_bounce_invested_today,
        "v_reversal_invested_today": _v_reversal_invested_today,
        "trades_today": _trades_today,
        "sold_codes_today": sorted(_sold_codes_today),
        "intraday_low_cash_notified": sorted(_intraday_low_cash_notified),
        "closing_positions": _closing_positions,       # 오버나이트 유지
        "closing_invested_today": _closing_invested_today,
        "ul_rebound_watchlist": ul_rebound.dump_watchlist(),
        "ul_rebound_sim_trades_today": ul_rebound.dump_sim_trades_today(),
        "k1_closing_watchlist": k1_closing.dump_watchlist(),
        "k1_closing_positions": _k1_closing_positions,
        "k1_closing_sim_trades_today": k1_closing.dump_sim_trades_today(),
        "k2_watchlist": k2_intraday.dump_watchlist(),
        "k2_sim_trades_today": k2_intraday.dump_sim_trades_today(),
        "k1_plus_watchlist": k1_plus.dump_watchlist(),
        "k1_plus_sim_trades_today": k1_plus.dump_sim_trades_today(),
        "k2_plus_watchlist": k2_plus.dump_watchlist(),
        "k2_plus_sim_trades_today": k2_plus.dump_sim_trades_today(),
        "strong_v_sim_open": strong_v_sim.dump_open_position(),
        "strong_v_sim_focus": strong_v_sim.dump_focus_target(),
        "strong_v_sim_trades_today": strong_v_sim.dump_sim_trades_today(),
        "daily_pnl_ledger": _daily_pnl_ledger,
    }
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[상태 저장 오류] {e}")


def _load_state() -> None:
    global _positions, _total_invested_today, _trades_today
    global _closing_positions, _closing_invested_today, _crash_bounce_invested_today
    global _v_reversal_invested_today, _k1_closing_positions
    global _daily_pnl_ledger, _sold_codes_today, _intraday_low_cash_notified
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

        # 손익 장부는 모드 전환과 무관하게 유지하지 않음(실전/모의 혼동 방지)
        # → 동일 모드일 때만 아래에서 복원

        # 종가베팅 포지션은 날짜와 무관하게 항상 불러옴 (오버나이트 포지션)
        _closing_positions = state.get("closing_positions", {})
        if _closing_positions:
            print(f"[상태 복원] 종가베팅 포지션 {len(_closing_positions)}개 불러옴 (오버나이트)")

        ul_rebound.load_watchlist(state.get("ul_rebound_watchlist", {}))
        if ul_rebound.get_watchlist():
            print(
                f"[상태 복원] 상한가 리바운드 추적 "
                f"{len(ul_rebound.get_watchlist())}개 불러옴"
            )

        k1_closing.load_watchlist(state.get("k1_closing_watchlist", {}))
        _k1_closing_positions = state.get("k1_closing_positions", {})
        if _k1_closing_positions:
            print(f"[상태 복원] K1 종가 포지션 {len(_k1_closing_positions)}개 불러옴")

        k2_intraday.load_watchlist(state.get("k2_watchlist", {}))
        if k2_intraday.get_watchlist():
            print(f"[상태 복원] K2 시뮬 추적 {len(k2_intraday.get_watchlist())}개 불러옴")

        k1_plus.load_watchlist(state.get("k1_plus_watchlist", {}))
        if k1_plus.get_watchlist():
            print(f"[상태 복원] K1플러스 시뮬 추적 {len(k1_plus.get_watchlist())}개 불러옴")

        k2_plus.load_watchlist(state.get("k2_plus_watchlist", {}))
        if k2_plus.get_watchlist():
            print(f"[상태 복원] K2플러스 시뮬 추적 {len(k2_plus.get_watchlist())}개 불러옴")

        if state.get("date") == _today_kst():
            ul_rebound.load_sim_trades_today(state.get("ul_rebound_sim_trades_today", []))
            if ul_rebound.get_sim_trades_today():
                print(
                    f"[상태 복원] 상한가 리바운드 시뮬 체결 "
                    f"{len(ul_rebound.get_sim_trades_today())}건 불러옴"
                )
            k1_closing.load_sim_trades_today(state.get("k1_closing_sim_trades_today", []))
            k2_intraday.load_sim_trades_today(state.get("k2_sim_trades_today", []))
            if k2_intraday.get_sim_trades_today():
                print(
                    f"[상태 복원] K2 시뮬 체결 "
                    f"{len(k2_intraday.get_sim_trades_today())}건 불러옴"
                )
            k1_plus.load_sim_trades_today(state.get("k1_plus_sim_trades_today", []))
            if k1_plus.get_sim_trades_today():
                print(
                    f"[상태 복원] K1플러스 시뮬 체결 "
                    f"{len(k1_plus.get_sim_trades_today())}건 불러옴"
                )
            k2_plus.load_sim_trades_today(state.get("k2_plus_sim_trades_today", []))
            if k2_plus.get_sim_trades_today():
                print(
                    f"[상태 복원] K2플러스 시뮬 체결 "
                    f"{len(k2_plus.get_sim_trades_today())}건 불러옴"
                )
            strong_v_sim.load_open_position(state.get("strong_v_sim_open"))
            strong_v_sim.load_focus_target(state.get("strong_v_sim_focus"))
            strong_v_sim.load_sim_trades_today(state.get("strong_v_sim_trades_today", []))
            if strong_v_sim.get_open_position():
                pos = strong_v_sim.get_open_position()
                print(
                    f"[상태 복원] 강세V 시뮬 보유 "
                    f"{pos['name']}({pos['code']}) 불러옴"
                )
            elif strong_v_sim.get_focus_target():
                ft = strong_v_sim.get_focus_target()
                print(
                    f"[상태 복원] 강세V 시뮬 후보 추적 "
                    f"{ft['name']}({ft['code']}) 불러옴"
                )

        ledger = state.get("daily_pnl_ledger", [])
        if isinstance(ledger, list):
            _daily_pnl_ledger = [
                e for e in ledger
                if isinstance(e, dict) and e.get("date") and "profit_won" in e
            ]
            if _daily_pnl_ledger:
                print(f"[상태 복원] 일별 손익 장부 {len(_daily_pnl_ledger)}일 불러옴")

        # 장중 포지션은 오늘 날짜인 경우만
        if state.get("date") == _today_kst():
            _positions = state.get("positions", {})
            _total_invested_today = state.get("total_invested_today", 0)
            _crash_bounce_invested_today = state.get("crash_bounce_invested_today", 0)
            _v_reversal_invested_today = state.get("v_reversal_invested_today", 0)
            _trades_today = state.get("trades_today", [])
            _closing_invested_today = state.get("closing_invested_today", 0)
            sold_raw = state.get("sold_codes_today", [])
            if isinstance(sold_raw, list) and sold_raw:
                _sold_codes_today = {str(c) for c in sold_raw if c}
            else:
                # 구버전 상태: 오늘 장중 매도 기록에서 재진입 금지 목록 복원
                _sold_codes_today = {
                    str(t.get("code"))
                    for t in _trades_today
                    if t.get("code") and t.get("strategy") not in ("종가베팅", k1_closing.STRATEGY)
                }
            low_cash_raw = state.get("intraday_low_cash_notified", [])
            _intraday_low_cash_notified = (
                {str(c) for c in low_cash_raw if c}
                if isinstance(low_cash_raw, list)
                else set()
            )
            if _positions:
                print(f"[상태 복원] 장중 포지션 {len(_positions)}개 불러옴")
            if _sold_codes_today:
                print(f"[상태 복원] 당일 매도 재진입금지 {len(_sold_codes_today)}종목")
            if _trades_today:
                print(f"[상태 복원] 오늘 체결 {len(_trades_today)}건 불러옴")
    except Exception as e:
        print(f"[상태 불러오기 오류] {e}")


def _format_won(amount: int) -> str:
    sign = "+" if amount > 0 else ""
    return f"{sign}{amount:,}원"


def _collect_sim_pnl_today() -> tuple[int, int, list[tuple[str, int, int]]]:
    """시뮬 전략별 오늘 손익. Returns: (합계원, 합계건수, [(라벨, 원, 건), ...])"""
    buckets = [
        ("상한가 리바운드", ul_rebound.get_sim_trades_today()),
        ("K1 종가", k1_closing.get_sim_trades_today()),
        ("K1플러스", k1_plus.get_sim_trades_today()),
        ("K2플러스", k2_plus.get_sim_trades_today()),
        ("K2", k2_intraday.get_sim_trades_today()),
        ("강세V", strong_v_sim.get_sim_trades_today()),
    ]
    details: list[tuple[str, int, int]] = []
    total_won = 0
    total_n = 0
    for label, trades in buckets:
        sells = [
            t for t in trades
            if isinstance(t, dict) and t.get("action", "sell") != "buy"
            and "profit_won" in t
        ]
        # 일부 모듈은 action 없이 sell만 append
        if not sells:
            sells = [t for t in trades if isinstance(t, dict) and "profit_won" in t]
        if not sells:
            continue
        won = sum(int(t.get("profit_won", 0)) for t in sells)
        n = len(sells)
        details.append((label, won, n))
        total_won += won
        total_n += n
    return total_won, total_n, details


def _record_daily_pnl(
    date_str: str,
    profit_won: int,
    trades: int,
    sim_profit_won: int = 0,
    sim_trades: int = 0,
) -> None:
    """오늘 실전·시뮬 손익을 장부에 기록(같은 날이면 갱신)"""
    global _daily_pnl_ledger
    entry = {
        "date": date_str,
        "profit_won": int(profit_won),
        "trades": int(trades),
        "sim_profit_won": int(sim_profit_won),
        "sim_trades": int(sim_trades),
    }
    updated = False
    for i, row in enumerate(_daily_pnl_ledger):
        if row.get("date") == date_str:
            _daily_pnl_ledger[i] = entry
            updated = True
            break
    if not updated:
        _daily_pnl_ledger.append(entry)
    _daily_pnl_ledger = sorted(_daily_pnl_ledger, key=lambda x: x["date"])[-40:]
    _save_state()


def _week_mon_fri_dates(ref: datetime | None = None) -> list[str]:
    """해당 주의 월~금 날짜 문자열 목록"""
    now = ref or datetime.now(KST)
    monday = (now - timedelta(days=now.weekday())).date()
    return [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(5)]


def run_weekly_pnl_report() -> None:
    """금요일 장마감 — 이번 주(월~금) 실전·시뮬 총 손익금"""
    now = datetime.now(KST)
    if now.weekday() != 4:
        return

    week_dates = _week_mon_fri_dates(now)
    by_date = {e["date"]: e for e in _daily_pnl_ledger}
    lines = [
        f"📅 <b>주간 손익 보고</b> ({week_dates[0]} ~ {week_dates[4]})\n",
        "━━ 💰 실전 ━━",
    ]
    live_total = 0
    live_trades = 0
    for d in week_dates:
        row = by_date.get(d)
        if row is None:
            lines.append(f"  {d}: 기록 없음")
            continue
        live_total += int(row.get("profit_won", 0))
        live_trades += int(row.get("trades", 0))
        lines.append(
            f"  {d}: {_format_won(int(row.get('profit_won', 0)))} "
            f"({int(row.get('trades', 0))}건)"
        )
    lines.append(f"💰 <b>이번 주 실전 총 손익금: {_format_won(live_total)}</b>")
    lines.append(f"실전 매매: {live_trades}건\n")

    lines.append("━━ 🧪 시뮬 ━━")
    sim_total = 0
    sim_trades = 0
    for d in week_dates:
        row = by_date.get(d)
        if row is None:
            lines.append(f"  {d}: 기록 없음")
            continue
        sim_total += int(row.get("sim_profit_won", 0))
        sim_trades += int(row.get("sim_trades", 0))
        lines.append(
            f"  {d}: {_format_won(int(row.get('sim_profit_won', 0)))} "
            f"({int(row.get('sim_trades', 0))}건)"
        )
    lines.append(f"🧪 <b>이번 주 시뮬 총 손익금: {_format_won(sim_total)}</b>")
    lines.append(f"시뮬 매매: {sim_trades}건")
    notifier.send("\n".join(lines))
    print(f"[주간손익] 실전 {live_total:,}원 / 시뮬 {sim_total:,}원")


# ── 유틸 ──────────────────────────────────────────────────────────────────────

def is_trading_day(d: datetime | None = None) -> bool:
    today = d or datetime.now(KST)
    if today.weekday() >= 5:
        return False
    if today.strftime("%Y-%m-%d") in _KR_HOLIDAYS:
        print(f"[공휴일] {today.strftime('%Y-%m-%d')} - 매매 건너뜀")
        return False
    return True


def _is_trading_date(day) -> bool:
    """date 객체 기준 거래일 여부."""
    return is_trading_day(datetime.combine(day, datetime.min.time(), tzinfo=KST))


def _previous_trading_date(day) -> "datetime.date":
    """day 직전 거래일 (day 미포함)."""
    from datetime import timedelta
    cursor = day - timedelta(days=1)
    while not _is_trading_date(cursor):
        cursor -= timedelta(days=1)
    return cursor


def _closing_recovery_expires(buy_date: str, grace_days: int = 14) -> str:
    """Railway 복구 env 만료일 (매수일 + grace)."""
    from datetime import timedelta
    try:
        base = datetime.strptime(buy_date, "%Y-%m-%d").date()
    except ValueError:
        base = datetime.now(KST).date()
    return (base + timedelta(days=grace_days)).strftime("%Y-%m-%d")


def _closing_recovery_env_line(code: str, name: str, buy_date: str) -> str:
    expires = _closing_recovery_expires(buy_date)
    return f"{code}|{name}|{buy_date}|{expires}"


def _is_closing_hold_excluded(code: str, name: str) -> bool:
    if name in SELL_BLACKLIST or code in SELL_BLACKLIST:
        return True
    return name in CLOSING_HOLD_EXCLUDE or code in CLOSING_HOLD_EXCLUDE


def _tracked_position_codes() -> set[str]:
    codes = set(_positions.keys())
    codes.update(_closing_positions.keys())
    codes.update(_k1_closing_positions.keys())
    return codes


def _untracked_account_holdings() -> list[dict]:
    """봇이 추적하지 않는 계좌 보유 종목 (종목코드·종목명·수량·평균단가)."""
    try:
        rows = kis_api.get_holdings()
    except Exception as e:
        print(f"[종가동기화] 계좌 보유조회 실패: {e}")
        return []

    tracked = _tracked_position_codes()
    untracked: list[dict] = []
    for row in rows or []:
        code = str(row.get("pdno") or row.get("mksc_shrn_iscd") or "").strip()
        if not code or code in tracked:
            continue
        name = str(row.get("prdt_name") or code).strip()
        if _is_closing_hold_excluded(code, name):
            continue
        try:
            quantity = int(float(row.get("hldg_qty") or 0))
            buy_price = int(float(row.get("pchs_avg_pric") or 0))
        except (TypeError, ValueError):
            continue
        if quantity <= 0:
            continue
        untracked.append({
            "code": code,
            "name": name,
            "quantity": quantity,
            "buy_price": buy_price,
        })
    return untracked


def sync_closing_positions_from_account(notify: bool = True) -> list[str]:
    """
    계좌에 있으나 봇 상태에 없는 종목을 종가베팅 포지션으로 복원.

    CLOSING_ACCOUNT_SYNC=true 일 때만 자동 등록.
    false면 텔레그램으로 복구 env 안내만 보냄.
    """
    untracked = _untracked_account_holdings()
    if not untracked:
        return []

    if not CLOSING_ACCOUNT_SYNC:
        if notify:
            today = datetime.now(KST).date()
            prev_td = _previous_trading_date(today)
            hints = [
                _closing_recovery_env_line(h["code"], h["name"], prev_td.strftime("%Y-%m-%d"))
                for h in untracked
            ]
            names = ", ".join(f"{h['name']}({h['code']})" for h in untracked)
            notifier.send(
                "⚠️ <b>봇 미추적 보유 종목 감지</b>\n"
                f"계좌: {names}\n"
                "재배포 등으로 종가베팅 상태가 사라졌을 수 있습니다.\n\n"
                "Railway Variables에 아래를 추가 후 Redeploy 하세요:\n"
                f"<code>CLOSING_RECOVERY_POSITIONS={','.join(hints)}</code>\n\n"
                "또는 <code>CLOSING_ACCOUNT_SYNC=true</code> 로 자동 복원(블랙리스트·제외 목록 제외)."
            )
        return []

    recovered: list[str] = []
    today = datetime.now(KST).date()
    default_buy = _previous_trading_date(today).strftime("%Y-%m-%d")
    for h in untracked:
        code = h["code"]
        if code in _closing_positions:
            continue
        _closing_positions[code] = {
            "name": h["name"],
            "quantity": h["quantity"],
            "buy_price": h["buy_price"],
            "strategy": "종가베팅",
            "buy_reason": "계좌 동기화 복원",
            "buy_date": default_buy,
        }
        recovered.append(code)
        notifier.send(
            f"♻️ 종가베팅 계좌 동기화 복원\n"
            f"종목: {h['name']} ({code})\n"
            f"수량: {h['quantity']}주 / 평균단가: {h['buy_price']:,}원\n"
            f"매수일(추정): {default_buy} / 다음 09:00 매도 대상"
        )
        print(f"[종가동기화] {h['name']}({code}) {h['quantity']}주 @ {h['buy_price']:,}")

    if recovered:
        _save_state()
    return recovered


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


def _account_holding_qty() -> dict[str, int] | None:
    """계좌 실보유 수량. 조회 실패 시 None (오인식 방지)."""
    try:
        rows = kis_api.get_holdings()
    except Exception as e:
        print(f"[포지션동기화] 보유조회 실패: {e}")
        return None

    qty_map: dict[str, int] = {}
    for row in rows or []:
        code = str(row.get("pdno") or row.get("mksc_shrn_iscd") or "").strip()
        if not code:
            continue
        try:
            qty = int(float(row.get("hldg_qty") or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty > 0:
            qty_map[code] = qty_map.get(code, 0) + qty
    return qty_map


def recover_configured_closing_positions() -> list[str]:
    """
    Railway 재배포로 trading_state.json이 사라졌을 때 지정한 종가베팅만 복원.

    형식:
      CLOSING_RECOVERY_POSITIONS=종목코드|종목명|매수일|복구만료일
      여러 종목은 쉼표로 구분.

    계좌에 실제 잔고가 있을 때만 일반 종가베팅 포지션으로 등록한다.
    K1/장중 포지션으로 이미 추적 중인 종목은 건드리지 않는다.
    """
    raw = os.getenv("CLOSING_RECOVERY_POSITIONS", "").strip()
    if not raw:
        return []

    today = datetime.now(KST).date()
    configured: dict[str, tuple[str, str]] = {}
    for item in raw.split(","):
        parts = [part.strip() for part in item.split("|")]
        if len(parts) != 4:
            print(f"[종가복구] 잘못된 설정 스킵: {item}")
            continue
        code, name, buy_date, expires = parts
        try:
            expires_date = datetime.strptime(expires, "%Y-%m-%d").date()
            datetime.strptime(buy_date, "%Y-%m-%d")
        except ValueError:
            print(f"[종가복구] 날짜 형식 오류 스킵: {item}")
            continue
        if code and today <= expires_date:
            configured[code] = (name or code, buy_date)

    pending = {
        code: meta for code, meta in configured.items()
        if code not in _closing_positions
        and code not in _k1_closing_positions
        and code not in _positions
    }
    if not pending:
        return []

    try:
        rows = kis_api.get_holdings()
    except Exception as e:
        print(f"[종가복구] 계좌 보유조회 실패: {e}")
        return []

    recovered: list[str] = []
    for row in rows or []:
        code = str(row.get("pdno") or row.get("mksc_shrn_iscd") or "").strip()
        if code not in pending:
            continue
        try:
            quantity = int(float(row.get("hldg_qty") or 0))
            buy_price = int(float(row.get("pchs_avg_pric") or 0))
        except (TypeError, ValueError):
            continue
        if quantity <= 0 or buy_price <= 0:
            continue

        configured_name, buy_date = pending[code]
        name = str(row.get("prdt_name") or configured_name).strip()
        _closing_positions[code] = {
            "name": name,
            "quantity": quantity,
            "buy_price": buy_price,
            "strategy": "종가베팅",
            "buy_reason": "Railway 상태 유실 대비 지정 복구",
            "buy_date": buy_date,
        }
        recovered.append(code)
        notifier.send(
            f"♻️ 종가베팅 포지션 복구\n"
            f"종목: {name} ({code})\n"
            f"수량: {quantity}주 / 평균단가: {buy_price:,}원\n"
            f"매수일: {buy_date} / 다음 09:00 매도 대상"
        )
        print(f"[종가복구] {name}({code}) {quantity}주 @ {buy_price:,}")

    if recovered:
        _save_state()
    return recovered


def _record_manual_sell(code: str, pos: dict, strategy: str, store: dict) -> None:
    """계좌에 없으면 봇 포지션을 수동매도로 정리"""
    name = pos.get("name", code)
    quantity = int(pos.get("quantity") or 0)
    buy_price = int(pos.get("buy_price") or 0)
    sell_price = buy_price
    profit_pct = 0.0
    try:
        current = kis_api.get_current_price(code, fallback=buy_price)
        if current > 0 and buy_price > 0:
            sell_price = int(current)
            profit_pct = (current - buy_price) / buy_price * 100
    except Exception:
        pass

    profit_won = int((sell_price - buy_price) * quantity) if quantity else 0
    reason = "수동매도 인식 (계좌 미보유)"
    _trades_today.append({
        "name": name,
        "code": code,
        "quantity": quantity,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "profit_pct": round(profit_pct, 2),
        "profit_won": profit_won,
        "buy_reason": pos.get("buy_reason", ""),
        "sell_reason": reason,
        "strategy": strategy,
    })
    if store is _positions:
        _release_intraday_budget(pos)
        _mark_sold_today(code)
    store.pop(code, None)
    notifier.notify_sell(name, code, quantity, profit_pct, reason)
    print(f"[포지션동기화] {name}({code}) {reason}")


def reconcile_positions_with_account(notify_empty: bool = False) -> list[str]:
    """
    봇 기록 포지션 중 계좌에 없는 종목을 수동매도로 정리.
    장중 / AI종가 / K1종가 모두 대상.
    """
    qty_map = _account_holding_qty()
    if qty_map is None:
        return []

    cleared: list[str] = []
    for code, pos in list(_positions.items()):
        if qty_map.get(code, 0) <= 0:
            _record_manual_sell(code, pos, pos.get("strategy", "장중매매"), _positions)
            cleared.append(code)

    for code, pos in list(_closing_positions.items()):
        if qty_map.get(code, 0) <= 0:
            _record_manual_sell(code, pos, "종가베팅", _closing_positions)
            cleared.append(code)

    for code, pos in list(_k1_closing_positions.items()):
        if qty_map.get(code, 0) <= 0:
            _record_manual_sell(
                code, pos, pos.get("strategy", k1_closing.STRATEGY), _k1_closing_positions,
            )
            cleared.append(code)

    if cleared:
        _save_state()
        print(f"[포지션동기화] 수동매도 인식 {len(cleared)}건: {', '.join(cleared)}")
    elif notify_empty:
        print("[포지션동기화] 봇 포지션 ↔ 계좌 일치 (정리 없음)")
    return cleared


def _is_already_sold_error(msg: str) -> bool:
    text = (msg or "").lower()
    keys = ("잔고", "수량", "매도가능", "보유", "부족", "주문가능수량이")
    return any(k in text for k in keys)


def _due_closing_positions() -> dict[str, dict]:
    """오늘보다 이전에 매수한 일반 종가베팅 포지션만 반환."""
    today = datetime.now(KST).date()
    due: dict[str, dict] = {}
    for code, pos in _closing_positions.items():
        try:
            buy_date = datetime.strptime(pos.get("buy_date", ""), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            # 구버전 상태처럼 매수일이 없으면 익일 매도 대상일 가능성이 높아 포함
            due[code] = pos
            continue
        if buy_date < today:
            due[code] = pos
    return due


def run_morning_sell_closing_bet() -> None:
    """09:00 - 종가베팅 포지션 시초가 매도 (K1 종가 제외)"""
    if not is_trading_day():
        return

    recover_configured_closing_positions()
    sync_closing_positions_from_account(notify=True)
    reconcile_positions_with_account()

    if _k1_closing_positions:
        notifier.send(
            f"🔷 K1 종가 {len(_k1_closing_positions)}개 보유 — "
            f"익일 매도 없음 ({k1_closing.FORCE_SELL_DAY}일차 청산)"
        )

    due_positions = _due_closing_positions()
    if not due_positions:
        return

    count = len(due_positions)
    now = datetime.now(KST)
    is_open_window = now.hour == 9 and now.minute <= 10
    reason = "종가베팅 시초가 매도" if is_open_window else "종가베팅 지연 보충 매도"
    print(f"\n[{now.strftime('%H:%M:%S')} KST] {reason} ({count}개)")
    notifier.send(f"🌅 {reason} - 포지션 {count}개 매도 시작")

    for code, pos in list(due_positions.items()):
        _execute_closing_sell(code, pos, reason)
        time.sleep(0.5)


def run_closing_bet_screening() -> None:
    """14:00 - 종가베팅 워치리스트 구성 (금·월=K1 / 화~목=기존 AI)"""
    if not is_trading_day():
        return

    if k1_closing.is_enabled() and k1_closing.is_k1_closing_day():
        run_k1_closing_screening()
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

    if ul_rebound.is_monitor_window():
        _check_ul_rebound_alerts()

    if k2_intraday.is_monitor_window():
        _check_k2_sim_alerts()

    if k1_plus.is_monitor_window():
        _check_k1_plus_sim_alerts()

    if k2_plus.is_monitor_window():
        _check_k2_plus_sim_alerts()

    _check_k1_closing_exit()

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
            if code in _positions:
                continue
            if code in _sold_codes_today:
                waiting_lines.append(
                    f"  🚫{s['name']}({s.get('strategy','')}): 당일 매도 — 재진입 금지"
                )
            else:
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
        if ul_rebound.is_enabled():
            open_sim = sum(
                1 for e in ul_rebound.get_watchlist().values()
                if e.get("sim", {}).get("status") == "open"
            )
            sim_trades = len(ul_rebound.get_sim_trades_today())
            wd = datetime.now(KST).weekday()
            ul_days = "월~목" if wd <= 3 else "비활성"
            lines.append(
                f"상한가 리바운드 [시뮬] ({ul_days}): 추적 {len(ul_rebound.get_watchlist())}개 / "
                f"보유 {open_sim}개 / 오늘 {sim_trades}건"
            )
        if k1_closing.is_enabled():
            lines.append(
                f"K1 종가 (금·월): 보유 {len(_k1_closing_positions)}개 / "
                f"시뮬 {len(k1_closing.get_sim_trades_today())}건"
            )
        if k2_intraday.is_enabled():
            open_k2 = sum(
                1 for e in k2_intraday.get_watchlist().values()
                if e.get("sim", {}).get("status") == "open"
            )
            lines.append(
                f"K2 [시뮬]: 추적 {len(k2_intraday.get_watchlist())}개 / "
                f"보유 {open_k2}개 / 오늘 {len(k2_intraday.get_sim_trades_today())}건"
            )
        if k1_plus.is_enabled():
            open_p = sum(
                1 for e in k1_plus.get_watchlist().values()
                if e.get("sim", {}).get("status") == "open"
            )
            lines.append(
                f"K1플러스 [시뮬]: 추적 {len(k1_plus.get_watchlist())}개 / "
                f"보유 {open_p}개 / 오늘 {len(k1_plus.get_sim_trades_today())}건"
            )
        if k2_plus.is_enabled():
            open_kp = sum(
                1 for e in k2_plus.get_watchlist().values()
                if e.get("sim", {}).get("status") == "open"
            )
            lines.append(
                f"K2플러스 [시뮬]: 추적 {len(k2_plus.get_watchlist())}개 / "
                f"보유 {open_kp}개 / 오늘 {len(k2_plus.get_sim_trades_today())}건"
            )
        if strong_v_sim.is_enabled():
            open_sv = 1 if strong_v_sim.get_open_position() else 0
            focus_sv = 1 if strong_v_sim.get_focus_target() else 0
            lines.append(
                f"강세V [시뮬]: 보유 {open_sv} / 후보 {focus_sv} / "
                f"오늘 {len(strong_v_sim.get_sim_trades_today())}건 / "
                f"주기 {strong_v_sim.get_poll_interval_min()}분"
            )
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
        sim_net, sim_n, sim_details = _collect_sim_pnl_today()
        lines.append(f"💰 <b>오늘 실전 손익금: {_format_won(0)}</b>")
        lines.append(f"🧪 <b>오늘 시뮬 손익금: {_format_won(sim_net)}</b> ({sim_n}건)")
        for label, won, n in sim_details:
            lines.append(f"   · {label}: {_format_won(won)} ({n}건)")
        lines.append("")
        _record_daily_pnl(today, 0, 0, sim_net, sim_n)
        ul_lines = ul_rebound.format_watchlist_summary()
        if ul_lines:
            lines.extend(ul_lines)
            lines.append("")
        k1_lines = k1_closing.format_summary()
        if k1_lines:
            lines.extend(k1_lines)
            lines.append("")
        k2_lines = k2_intraday.format_summary()
        if k2_lines:
            lines.extend(k2_lines)
            lines.append("")
        plus_lines = k1_plus.format_summary()
        if plus_lines:
            lines.extend(plus_lines)
            lines.append("")
        k2p_lines = k2_plus.format_summary()
        if k2p_lines:
            lines.extend(k2p_lines)
            lines.append("")
        sv_lines = strong_v_sim.format_summary()
        if sv_lines:
            lines.extend(sv_lines)
            lines.append("")
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
        if datetime.now(KST).weekday() == 4:
            run_weekly_pnl_report()
        return

    win_trades  = [t for t in _trades_today if t["profit_won"] > 0]
    lose_trades = [t for t in _trades_today if t["profit_won"] <= 0]
    total_profit = sum(t["profit_won"] for t in win_trades)
    total_loss   = sum(t["profit_won"] for t in lose_trades)
    net = total_profit + total_loss

    intraday = [t for t in _trades_today if t.get("strategy") != "종가베팅"]
    closing_bet = [t for t in _trades_today if t.get("strategy") == "종가베팅"]

    lines = [f"📋 <b>오늘 장마감 보고 ({today})</b>\n"]
    sim_net, sim_n, sim_details = _collect_sim_pnl_today()
    lines.append(f"💰 <b>오늘 실전 손익금: {_format_won(net)}</b>")
    lines.append(f"🧪 <b>오늘 시뮬 손익금: {_format_won(sim_net)}</b> ({sim_n}건)")
    for label, won, n in sim_details:
        lines.append(f"   · {label}: {_format_won(won)} ({n}건)")
    lines.append("")
    _record_daily_pnl(today, net, len(_trades_today), sim_net, sim_n)

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
    lines.append(f"\n🏁 오늘 실전 손익금: <b>{_format_won(net)}</b>")
    lines.append(f"🧪 오늘 시뮬 손익금: <b>{_format_won(sim_net)}</b> ({sim_n}건)")

    ul_lines = ul_rebound.format_watchlist_summary()
    if ul_lines:
        lines.append("")
        lines.extend(ul_lines)

    k1_lines = k1_closing.format_summary()
    if k1_lines:
        lines.append("")
        lines.extend(k1_lines)

    k2_lines = k2_intraday.format_summary()
    if k2_lines:
        lines.append("")
        lines.extend(k2_lines)

    plus_lines = k1_plus.format_summary()
    if plus_lines:
        lines.append("")
        lines.extend(plus_lines)

    k2p_lines = k2_plus.format_summary()
    if k2p_lines:
        lines.append("")
        lines.extend(k2p_lines)

    sv_lines = strong_v_sim.format_summary()
    if sv_lines:
        lines.append("")
        lines.extend(sv_lines)

    notifier.send("\n".join(lines))
    if datetime.now(KST).weekday() == 4:
        run_weekly_pnl_report()


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
    """AI 종가베팅 매수 (기본 14:45~14:50 1회) — 화~목"""
    if k1_closing.is_enabled() and k1_closing.is_k1_closing_day():
        return

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

    live = _refresh_closing_watchlist_live(candidates)
    max_rate = screener.CLOSING_BET_MAX_RATE
    eligible = [
        s for s in live
        if float(s.get("change_rate") or 0) < max_rate
    ]
    if not eligible:
        names = ", ".join(
            f"{s.get('name')}({float(s.get('change_rate') or 0):+.1f}%)"
            for s in live[:3]
        )
        notifier.send(
            f"⚠️ <b>종가베팅 매수 스킵</b>\n"
            f"후보 전원 당일 +{max_rate:g}% 이상 (추격 방지)\n"
            f"{names}"
        )
        print(f"[종가베팅] 매수 스킵 — 전원 +{max_rate:g}% 이상")
        return

    pending = _sort_closing_watchlist(eligible)
    buy_pool = pending[: max(1, CLOSING_BET_TRY_TOP_N)]
    top = buy_pool[0]
    print(
        f"[종가베팅] 매수 1순위: {top['name']}({top['code']}) "
        f"점수 {top.get('priority_score', 0)} / "
        f"{top.get('change_rate', 0):+.1f}% / RSI {top.get('rsi', 0):.0f} / "
        f"거래량 {top.get('vol_ratio', 0):.1f}x"
    )

    for rank, stock in enumerate(buy_pool, 1):
        if _is_closing_overheated(stock):
            print(
                f"[종가베팅] {stock['name']} RSI 과열 스킵 "
                f"(RSI {float(stock.get('rsi') or 0):.0f} > {CLOSING_BET_OVERHEAT_RSI:g})"
            )
            if rank == 1 and len(buy_pool) > 1:
                notifier.send(
                    f"⚠️ 종가베팅 1순위 <b>{stock['name']}</b> RSI 과열 "
                    f"({float(stock.get('rsi') or 0):.0f}) → 2순위 검토"
                )
            continue
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
                buy_date = _today_kst()
                recovery_line = _closing_recovery_env_line(code, name, buy_date)
                notifier.notify_buy(
                    name, code, quantity, int(current),
                    f"[종가베팅] {rank_note} · {stock.get('reason', '')}\n"
                    f"♻️ 재배포 복구용: <code>{recovery_line}</code>",
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
            if _is_already_sold_error(msg):
                _record_manual_sell(code, pos, "종가베팅", _closing_positions)
                _save_state()
            else:
                notifier.notify_error(f"{name} 종가베팅 매도 실패: {msg}")

    except Exception as e:
        notifier.notify_error(f"{name} 종가베팅 매도 오류: {e}")


# ── 낙폭반등 진입 로직 ─────────────────────────────────────────────────────────

def _crash_bounce_position_count() -> int:
    return sum(1 for p in _positions.values() if p.get("strategy") == crash_bounce.STRATEGY)


def _check_crash_bounce_entry(afternoon: bool = False) -> None:
    """낙폭반등 매수 (오전 진입 또는 13:15 오후 필터 재검색)"""
    if not crash_bounce.is_enabled():
        return
    if not afternoon and not crash_bounce.is_entry_window():
        return

    global _crash_bounce_invested_today

    if _crash_bounce_position_count() >= crash_bounce.MAX_POSITIONS:
        return

    remaining = crash_bounce.MAX_AMOUNT - _crash_bounce_invested_today
    if remaining <= 0:
        return

    already_held = set(_positions.keys())

    try:
        candidates, api_used = crash_bounce.scan_candidates(afternoon=afternoon)
        session_label = "오후 재검색" if afternoon else "오전"
        print(f"[낙폭반등/{session_label}] 스캔 {len(candidates)}개 후보 (API {api_used}회)")
    except Exception as e:
        notifier.notify_error(f"낙폭반등 스캔 오류: {e}")
        return

    for stock in candidates:
        code = stock["code"]
        name = stock["name"]

        if code in already_held:
            continue
        if code in _sold_codes_today:
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
                    "buy_time": datetime.now(KST).isoformat(),
                    "exit_ma60": stock.get("ma60", 0),
                    "ma_period": stock.get("ma_period", 60),
                    "entry_session": "afternoon" if afternoon else "morning",
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


def _check_v_reversal_entry(afternoon: bool = False) -> None:
    """V자반등 매수 (오전 진입 또는 13:15 오후 필터 재검색)"""
    if not v_reversal.is_enabled():
        return
    if not afternoon and not v_reversal.is_entry_window():
        return

    global _v_reversal_invested_today

    if _v_reversal_position_count() >= v_reversal.MAX_POSITIONS:
        return

    remaining = v_reversal.MAX_AMOUNT - _v_reversal_invested_today
    if remaining <= 0:
        return

    already_held = set(_positions.keys())

    try:
        candidates, api_used = v_reversal.scan_candidates(afternoon=afternoon)
        session_label = "오후 재검색" if afternoon else "오전"
        print(f"[V자반등/{session_label}] 스캔 {len(candidates)}개 후보 (API {api_used}회)")
    except Exception as e:
        notifier.notify_error(f"V자반등 스캔 오류: {e}")
        return

    for stock in candidates:
        code = stock["code"]
        name = stock["name"]

        if code in already_held:
            continue
        if code in _sold_codes_today:
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
                    "buy_time": datetime.now(KST).isoformat(),
                    "exit_ma60": stock.get("ma60", 0),
                    "ma_period": stock.get("ma_period", 60),
                    "entry_session": "afternoon" if afternoon else "morning",
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


def _check_strong_v_sim() -> None:
    """5분마다 — 강세주 급락 V 시뮬 진입·청산"""
    if not strong_v_sim.is_enabled() or not strong_v_sim.is_monitor_window():
        return
    try:
        events, api_used = strong_v_sim.run_check()
        if events:
            for ev in events:
                if ev.get("action") == "buy":
                    notifier.notify_strong_v_sim_buy(
                        ev["name"], ev["code"], ev["quantity"],
                        ev["price"], ev["reason"],
                    )
                elif ev.get("action") == "sell":
                    notifier.notify_strong_v_sim_sell(
                        ev["name"], ev["code"], ev["quantity"],
                        ev["buy_price"], ev["sell_price"],
                        ev["profit_pct"], ev["profit_won"],
                        ev["sell_reason"],
                    )
            _save_state()
            print(f"[강세V시뮬] 이벤트 {len(events)}건 (API {api_used}회)")
    except Exception as e:
        print(f"[강세V시뮬] 구간 체크 오류: {e}")
        notifier.notify_error(f"강세V 시뮬 체크 오류: {e}")


def run_afternoon_rebound_scan() -> None:
    """13:15 — 오전 미체결 전략만 오후 전용 필터로 한 번 재검색."""
    if not is_trading_day():
        return

    eligible: list[str] = []
    if (
        crash_bounce.is_enabled()
        and _crash_bounce_invested_today == 0
        and _crash_bounce_position_count() == 0
    ):
        eligible.append(crash_bounce.STRATEGY)
    if (
        v_reversal.is_enabled()
        and _v_reversal_invested_today == 0
        and _v_reversal_position_count() == 0
    ):
        eligible.append(v_reversal.STRATEGY)

    if not eligible:
        return

    notifier.send(
        "🔄 13:15 오후 반등 1회 재검색\n"
        f"대상: {', '.join(eligible)}\n"
        "조건: 오전 미체결 · 최근 30분 새 저점 반등 · 거래량 증가\n"
        "매수 한도: 오전과 동일"
    )

    results: list[str] = []
    if crash_bounce.STRATEGY in eligible:
        before = _crash_bounce_invested_today
        _check_crash_bounce_entry(afternoon=True)
        results.append(
            f"낙폭반등: {'매수 체결' if _crash_bounce_invested_today > before else '조건 종목 없음'}"
        )

    if v_reversal.STRATEGY in eligible:
        before = _v_reversal_invested_today
        _check_v_reversal_entry(afternoon=True)
        results.append(
            f"V자반등: {'매수 체결' if _v_reversal_invested_today > before else '조건 종목 없음'}"
        )

    notifier.send("🔎 오후 반등 재검색 완료\n" + "\n".join(results))


# ── 상한가 리바운드 알림 (자동 매매 없음) ─────────────────────────────────────

def _send_ul_rebound_alerts(alerts: list[dict]) -> None:
    for alert in alerts:
        entry = alert["entry"]
        sim = alert.get("sim")

        notifier.notify_ul_rebound(
            alert["type"],
            entry["name"],
            entry["code"],
            alert.get("current", 0),
            entry,
            alert.get("message", ""),
            sim=sim,
        )

        if not sim:
            continue

        action = sim.get("action")
        if action == "buy":
            notifier.notify_ul_rebound_sim_buy(
                sim["name"], sim["code"], sim["quantity"],
                sim["price"], sim["reason"],
            )
        elif action == "add":
            notifier.notify_ul_rebound_sim_add(
                sim["name"], sim["code"], sim["quantity"],
                sim["price"], sim["total_quantity"],
                sim["avg_price"], sim["reason"],
            )
        elif action == "sell":
            notifier.notify_ul_rebound_sim_sell(
                sim["name"], sim["code"], sim["quantity"],
                sim["buy_price"], sim["sell_price"],
                sim["profit_pct"], sim["profit_won"],
                sim["sell_reason"],
            )


def run_ul_rebound_morning_scan() -> None:
    """09:05 — 상한가 후보 스캔 (월~목 알림·시뮬)"""
    if not ul_rebound.is_enabled() or not ul_rebound.is_weekday_active() or not is_trading_day():
        return

    try:
        new_alerts, api_used = ul_rebound.scan_new_candidates()
        print(f"[상한가리바운드] 오전 스캔 신규 {len(new_alerts)}개 (API {api_used}회)")
        if new_alerts:
            _send_ul_rebound_alerts(new_alerts)
            _save_state()
    except Exception as e:
        print(f"[상한가리바운드] 오전 스캔 오류: {e}")
        notifier.notify_error(f"상한가 리바운드 스캔 오류: {e}")


def _check_ul_rebound_alerts() -> None:
    """5분마다 — R0~R3 구간 알림 체크 (월~목)"""
    if not ul_rebound.is_enabled() or not ul_rebound.is_weekday_active():
        return

    try:
        level_alerts, removed, api_used = ul_rebound.check_level_alerts()
        if level_alerts:
            _send_ul_rebound_alerts(level_alerts)
            _save_state()
            print(
                f"[상한가리바운드] 구간 알림 {len(level_alerts)}건 "
                f"(제거 {len(removed)}개, API {api_used}회)"
            )
    except Exception as e:
        print(f"[상한가리바운드] 구간 체크 오류: {e}")


def _send_k2_sim_alerts(alerts: list[dict]) -> None:
    for alert in alerts:
        entry = alert["entry"]
        sim = alert.get("sim")
        notifier.notify_k2_alert(
            alert["type"],
            entry["name"],
            entry["code"],
            alert.get("current", 0),
            entry,
            alert.get("message", ""),
        )
        if not sim:
            continue
        if sim.get("action") == "buy":
            notifier.notify_k2_sim_buy(
                sim["name"], sim["code"], sim["quantity"],
                sim["price"], sim.get("k2", 0), sim["reason"],
            )
        elif sim.get("action") == "sell":
            notifier.notify_k2_sim_sell(
                sim["name"], sim["code"], sim["quantity"],
                sim["buy_price"], sim["sell_price"],
                sim["profit_pct"], sim["profit_won"],
                sim["sell_reason"],
            )


def run_k2_morning_scan() -> None:
    """09:05 — K2 시뮬 후보 스캔"""
    if not k2_intraday.is_enabled() or not is_trading_day():
        return
    try:
        new_alerts, api_used = k2_intraday.scan_new_candidates()
        print(f"[K2시뮬] 오전 스캔 신규 {len(new_alerts)}개 (API {api_used}회)")
        if new_alerts:
            _send_k2_sim_alerts(new_alerts)
            _save_state()
    except Exception as e:
        print(f"[K2시뮬] 오전 스캔 오류: {e}")
        notifier.notify_error(f"K2 시뮬 스캔 오류: {e}")


def _check_k2_sim_alerts() -> None:
    """5분마다 — K2 이탈·시뮬 청산"""
    if not k2_intraday.is_enabled() or not k2_intraday.is_monitor_window():
        return
    try:
        alerts, removed, api_used = k2_intraday.check_alerts()
        if alerts:
            _send_k2_sim_alerts(alerts)
            _save_state()
            print(
                f"[K2시뮬] 알림 {len(alerts)}건 "
                f"(제거 {len(removed)}개, API {api_used}회)"
            )
    except Exception as e:
        print(f"[K2시뮬] 구간 체크 오류: {e}")


def _send_k1_plus_alerts(alerts: list[dict]) -> None:
    for alert in alerts:
        entry = alert["entry"]
        sim = alert.get("sim")
        notifier.notify_k1_plus_alert(
            alert["type"],
            entry["name"],
            entry["code"],
            alert.get("current", 0),
            entry,
            alert.get("message", ""),
        )
        if not sim:
            continue
        if sim.get("action") == "buy":
            notifier.notify_k1_plus_sim_buy(
                sim["name"], sim["code"], sim["quantity"],
                sim["price"], sim.get("k1", 0), sim["reason"],
            )
        elif sim.get("action") == "sell":
            notifier.notify_k1_plus_sim_sell(
                sim["name"], sim["code"], sim["quantity"],
                sim["buy_price"], sim["sell_price"],
                sim["profit_pct"], sim["profit_won"],
                sim["sell_reason"],
            )


def run_k1_plus_morning_scan() -> None:
    """09:05 — K1플러스 세력봉 후보 스캔"""
    if not k1_plus.is_enabled() or not is_trading_day():
        return
    try:
        new_alerts, api_used = k1_plus.scan_new_candidates()
        print(f"[K1+시뮬] 오전 스캔 신규 {len(new_alerts)}개 (API {api_used}회)")
        if new_alerts:
            _send_k1_plus_alerts(new_alerts)
            _save_state()
    except Exception as e:
        print(f"[K1+시뮬] 스캔 오류: {e}")
        notifier.notify_error(f"K1플러스 시뮬 스캔 오류: {e}")


def _check_k1_plus_sim_alerts() -> None:
    if not k1_plus.is_enabled() or not k1_plus.is_monitor_window():
        return
    try:
        alerts, removed, api_used = k1_plus.check_alerts()
        if alerts:
            _send_k1_plus_alerts(alerts)
            _save_state()
            print(
                f"[K1+시뮬] 알림 {len(alerts)}건 "
                f"(제거 {len(removed)}개, API {api_used}회)"
            )
    except Exception as e:
        print(f"[K1+시뮬] 체크 오류: {e}")


def _send_k2_plus_alerts(alerts: list[dict]) -> None:
    for alert in alerts:
        entry = alert["entry"]
        sim = alert.get("sim")
        notifier.notify_k2_plus_alert(
            alert["type"],
            entry["name"],
            entry["code"],
            alert.get("current", 0),
            entry,
            alert.get("message", ""),
        )
        if not sim:
            continue
        if sim.get("action") == "buy":
            notifier.notify_k2_plus_sim_buy(
                sim["name"], sim["code"], sim["quantity"],
                sim["price"], sim.get("k2", 0), sim["reason"],
            )
        elif sim.get("action") == "sell":
            notifier.notify_k2_plus_sim_sell(
                sim["name"], sim["code"], sim["quantity"],
                sim["buy_price"], sim["sell_price"],
                sim["profit_pct"], sim["profit_won"],
                sim["sell_reason"],
            )


def run_k2_plus_morning_scan() -> None:
    """09:05 — K2플러스 세력봉 후보 스캔"""
    if not k2_plus.is_enabled() or not is_trading_day():
        return
    try:
        new_alerts, api_used = k2_plus.scan_new_candidates()
        print(f"[K2+시뮬] 오전 스캔 신규 {len(new_alerts)}개 (API {api_used}회)")
        if new_alerts:
            _send_k2_plus_alerts(new_alerts)
            _save_state()
    except Exception as e:
        print(f"[K2+시뮬] 스캔 오류: {e}")
        notifier.notify_error(f"K2플러스 시뮬 스캔 오류: {e}")


def _check_k2_plus_sim_alerts() -> None:
    if not k2_plus.is_enabled() or not k2_plus.is_monitor_window():
        return
    try:
        alerts, removed, api_used = k2_plus.check_alerts()
        if alerts:
            _send_k2_plus_alerts(alerts)
            _save_state()
            print(
                f"[K2+시뮬] 알림 {len(alerts)}건 "
                f"(제거 {len(removed)}개, API {api_used}회)"
            )
    except Exception as e:
        print(f"[K2+시뮬] 체크 오류: {e}")


# ── K1 종가베팅 (금·월 실전 + 시뮬) ───────────────────────────────────────────

def run_k1_closing_screening() -> None:
    """14:00 — K1 종가 후보 스크리닝 (금·월)"""
    if not is_trading_day():
        return

    wd = datetime.now(KST).weekday()
    day_label = "금요일" if wd == 4 else "월요일"
    print(f"\n[{datetime.now(KST).strftime('%H:%M:%S')} KST] K1 종가 스크리닝 ({day_label})")
    notifier.send(
        f"⏰ 오후 2시 - <b>K1 종가 스크리닝</b> ({day_label})\n"
        f"피보 K1(0.236) 이탈 · 상한가 후 2일 이내"
    )

    global _closing_watchlist
    try:
        candidates, api_used = k1_closing.scan_closing_candidates()
        print(f"[K1종가] 스크리닝 {len(candidates)}개 (API {api_used}회)")
        _closing_watchlist = candidates  # 진입 체크용 임시 워치리스트

        if candidates:
            lines = [f"🔷 <b>K1 종가 후보 {len(candidates)}개</b> ({day_label})\n"]
            for i, c in enumerate(candidates, 1):
                lines.append(
                    f"{i}. {c['name']}({c['code']}) [{c['pattern']}]\n"
                    f"   K1 {c['k1']:,} / K2 {c['k2']:,} / 현재 {c['current']:,}\n"
                    f"   {c['reason']}"
                )
            notifier.send("\n".join(lines))
        else:
            notifier.send(f"🔷 K1 종가 후보 없음 ({day_label})")
        _save_state()
    except Exception as e:
        notifier.notify_error(f"K1 종가 스크리닝 오류: {e}")


def _check_k1_closing_entry() -> None:
    """14:20~14:50 — K1 종가 실전 매수 + 시뮬 기록"""
    if not k1_closing.is_closing_entry_window():
        return

    global _closing_invested_today, _k1_closing_positions
    global _closing_depleted_notified, _closing_balance_fail_notified

    watchlist = list(k1_closing.get_watchlist().values())
    if not watchlist:
        return

    if len(_k1_closing_positions) >= CLOSING_BET_MAX_POSITIONS:
        return

    budget_remaining = MAX_CLOSING_AMOUNT - _closing_invested_today
    if budget_remaining <= 0:
        return

    cash: int | None = None
    try:
        cash = kis_api.get_orderable_cash()
    except Exception as e:
        print(f"[K1종가] 예수금 조회 실패: {e}")
        remaining = budget_remaining
    else:
        remaining = min(budget_remaining, cash)
        if remaining <= 0:
            return

    pending = sorted(watchlist, key=lambda x: x.get("priority_score", 0), reverse=True)
    for stock in pending:
        code = stock["code"]
        name = stock["name"]
        if code in _k1_closing_positions:
            continue

        try:
            current = int(kis_api.get_current_price(code, fallback=stock.get("current")))
            if current <= 0:
                continue

            k1_level = stock.get("k1", 0)
            if k1_level <= 0 or current > k1_level * (1 + k1_closing.LEVEL_TOLERANCE_PCT / 100):
                continue

            sim_evt = k1_closing.record_sim_buy(stock, current)
            if sim_evt:
                notifier.notify_k1_sim_buy(
                    sim_evt["name"], sim_evt["code"], sim_evt["quantity"],
                    sim_evt["price"], sim_evt["pattern"], sim_evt["reason"],
                )

            buy_amount = min(MAX_CLOSING_BUY, remaining)
            quantity = buy_amount // current
            if quantity < 1:
                continue

            result = kis_api.buy_stock(code, quantity)
            if result.get("rt_cd") == "0":
                invested = quantity * current
                _closing_invested_today += invested
                _k1_closing_positions[code] = {
                    "name": name,
                    "quantity": quantity,
                    "buy_price": current,
                    "strategy": k1_closing.STRATEGY,
                    "buy_reason": stock.get("reason", ""),
                    "buy_date": _today_kst(),
                    "pattern": stock.get("pattern", ""),
                    "k1": stock.get("k1", 0),
                }
                _save_state()
                remaining -= invested
                notifier.notify_buy(
                    name, code, quantity, current,
                    f"[K1종가] {stock.get('pattern', '')} · {stock.get('reason', '')}",
                )
                print(f"[K1종가] 매수 {name}({code}) {quantity}주 @ {current:,}")
                break
            else:
                notifier.notify_error(f"{name} K1 종가 매수 실패: {result.get('msg1', '')}")

            time.sleep(kis_api.trade_interval())
        except Exception as e:
            notifier.notify_error(f"{name} K1 종가 진입 오류: {e}")


def _check_k1_closing_exit() -> None:
    """K1 포지션 4일차 강제 청산"""
    reconcile_positions_with_account()
    if not _k1_closing_positions:
        return

    for code, pos in list(_k1_closing_positions.items()):
        should, reason = k1_closing.evaluate_force_sell_day(pos)
        if not should:
            continue
        try:
            current, profit_pct, _ = _sell_reference_price(code, pos["buy_price"])
            quantity = pos["quantity"]
            name = pos["name"]
            result = kis_api.sell_stock(code, quantity)
            if result.get("rt_cd") == "0":
                profit_won = int((current - pos["buy_price"]) * quantity)
                _trades_today.append({
                    "name": name, "code": code, "quantity": quantity,
                    "buy_price": pos["buy_price"], "sell_price": int(current),
                    "profit_pct": round(profit_pct, 2), "profit_won": profit_won,
                    "buy_reason": pos.get("buy_reason", ""),
                    "sell_reason": reason, "strategy": k1_closing.STRATEGY,
                })
                del _k1_closing_positions[code]
                _save_state()
                notifier.notify_sell(name, code, quantity, profit_pct, reason)
                print(f"[K1종가] 매도 {name}({code}) {reason}")
            else:
                msg = result.get("msg1", "")
                if _is_already_sold_error(msg):
                    _record_manual_sell(
                        code, pos, pos.get("strategy", k1_closing.STRATEGY),
                        _k1_closing_positions,
                    )
                    _save_state()
                else:
                    notifier.notify_error(f"{name} K1 청산 실패: {msg}")
        except Exception as e:
            notifier.notify_error(f"{name} K1 청산 오류: {e}")
        time.sleep(0.5)


# ── 장중매매 진입 로직 ─────────────────────────────────────────────────────────

def _confirm_5min_entry(code: str) -> tuple[bool, str]:
    """매수 직전 5분봉 확인: 최근 완료 봉이 양봉·상승·거래량 동반인지 검사.

    워치리스트 전체가 아니라 일·기술적 조건을 통과한 소수 종목에만 호출한다.
    """
    if not screener.ENTRY_5MIN_CONFIRM:
        return True, ""

    try:
        # 최근 40분이면 5분봉 확인에 충분 — API 호출 수 절약
        minutes = kis_api.get_intraday_minute_bars(code, max_minutes=40)
        bars = kis_api.aggregate_5min_bars(minutes)
        time.sleep(0.3)
    except Exception as e:
        return False, f"5분봉 조회 실패 ({e})"

    if len(bars) < 3:
        return False, "5분봉 부족"

    # 마지막 봉은 진행 중일 수 있어 직전 완료 봉을 신호로 사용
    signal = bars[-2]
    previous = bars[-3]
    if signal["close"] <= signal["open"]:
        return False, "5분봉 음봉"
    if signal["close"] <= previous["close"]:
        return False, "5분봉 하락 전환"

    prior = bars[:-2][-4:] if len(bars) >= 6 else bars[:-2]
    if not prior:
        return False, "5분봉 거래량 비교 불가"
    avg_vol = sum(b.get("volume", 0) for b in prior) / len(prior)
    vol_ratio = (signal.get("volume", 0) / avg_vol) if avg_vol > 0 else 0.0
    if vol_ratio < screener.ENTRY_5MIN_VOLUME_RATIO:
        return False, (
            f"5분봉 거래량 부족 ({vol_ratio:.1f}x "
            f"< {screener.ENTRY_5MIN_VOLUME_RATIO:g}x)"
        )
    return True, f"5분봉 양봉·거래량 {vol_ratio:.1f}x"


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
        if s["code"] not in already_held and s["code"] not in _sold_codes_today
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
                elif ma5 > 0 and (current - ma5) / ma5 * 100 > screener.UPPER_MA5_GAP_MAX:
                    ma5_gap = (current - ma5) / ma5 * 100
                    skip_reason = (
                        f"MA5 과열이격 ({ma5_gap:.1f}% > {screener.UPPER_MA5_GAP_MAX:g}%)"
                    )
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

            candle_ok, candle_msg = _confirm_5min_entry(code)
            if not candle_ok:
                print(f"[진입 미충족] {name}({strategy}): {candle_msg}")
                stock["_skip_reason"] = candle_msg
                continue

            # 매수 실행
            buy_amount = min(MAX_BUY_AMOUNT, remaining)
            quantity = buy_amount // int(current)
            if quantity < 1:
                print(
                    f"[장중] {name} 금액 부족 "
                    f"(현재가 {int(current):,}원 / 잔여 {remaining:,}원) → 다음 후보"
                )
                if code not in _intraday_low_cash_notified:
                    _intraday_low_cash_notified.add(code)
                    _save_state()
                    notifier.send(
                        f"⚠️ {name}: 금액 부족 (현재가 {int(current):,}원) "
                        f"→ 다음 우선순위 종목으로 진행"
                    )
                continue

            result = kis_api.buy_stock(code, quantity)
            if result.get("rt_cd") == "0":
                invested = quantity * int(current)
                _total_invested_today += invested
                buy_reason = stock.get("reason", "")
                if candle_msg:
                    buy_reason = (
                        f"{buy_reason} · {candle_msg}".strip(" ·")
                        if buy_reason
                        else candle_msg
                    )
                _positions[code] = {
                    "name": name,
                    "quantity": quantity,
                    "buy_price": int(current),
                    "peak_price": int(current),  # 트레일링 스탑용 고점 추적
                    "strategy": strategy,
                    "buy_reason": buy_reason,
                    "buy_time": datetime.now(KST).isoformat(),
                }
                _save_state()
                already_held.add(code)
                remaining = MAX_TOTAL_AMOUNT - _total_invested_today
                notifier.notify_buy(name, code, quantity, int(current), buy_reason)

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

def _minutes_since_buy(pos: dict) -> float | None:
    """매수 후 경과 분. buy_time 없으면 None (빠른손절 스킵)."""
    raw = pos.get("buy_time")
    if not raw:
        return None
    try:
        bought = datetime.fromisoformat(str(raw))
        if bought.tzinfo is None:
            bought = bought.replace(tzinfo=KST)
        return (datetime.now(KST) - bought).total_seconds() / 60.0
    except (TypeError, ValueError):
        return None


def _quick_stop_hit(pos: dict, profit_pct: float) -> tuple[bool, str]:
    """매수 직후 창 안이면 더 타이트한 손절 적용."""
    if QUICK_STOP_LOSS_PCT <= 0 or QUICK_STOP_WINDOW_MIN <= 0:
        return False, ""
    elapsed = _minutes_since_buy(pos)
    if elapsed is None or elapsed > QUICK_STOP_WINDOW_MIN:
        return False, ""
    if profit_pct <= -QUICK_STOP_LOSS_PCT:
        return True, (
            f"빠른손절 ({profit_pct:.1f}%, 매수 후 {elapsed:.0f}분/"
            f"{QUICK_STOP_WINDOW_MIN}분·−{QUICK_STOP_LOSS_PCT:g}%)"
        )
    return False, ""


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

            quick, quick_reason = _quick_stop_hit(pos, profit_pct)
            if quick:
                _execute_sell(code, pos, quick_reason, current, profit_pct)
                continue

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
            _release_intraday_budget(pos)
            _mark_sold_today(code)
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
    global _sold_codes_today, _intraday_low_cash_notified
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
    _sold_codes_today = set()
    _intraday_low_cash_notified = set()
    # _closing_positions는 초기화 안 함 - 오버나이트 포지션 유지
    ul_rebound.reset_daily_sim_trades()
    k1_closing.reset_daily_sim_trades()
    k2_intraday.reset_daily_sim_trades()
    k1_plus.reset_daily_sim_trades()
    k2_plus.reset_daily_sim_trades()
    strong_v_sim.reset_daily_sim_trades()
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

    # 재배포로 상태가 사라진 지정 종가베팅 복원 후 수동매도 상태 동기화
    recover_configured_closing_positions()
    sync_closing_positions_from_account(notify=False)
    reconcile_positions_with_account(notify_empty=True)

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
            f"한도 {crash_bounce.MAX_AMOUNT:,}원 / 오전 미체결 시 13:15 재검색"
        )
    v_note = ""
    if v_reversal.is_enabled():
        v_note = (
            f"\n🟢 V자반등: {os.getenv('V_REVERSAL_ENTRY_START', '09:15')}~"
            f"{os.getenv('V_REVERSAL_ENTRY_END', '10:30')} / "
            f"한도 {v_reversal.MAX_AMOUNT:,}원 / 오전 미체결 시 13:15 재검색"
        )
    ul_note = ""
    if ul_rebound.is_enabled():
        ul_note = (
            f"\n🟣 상한가 리바운드: [시뮬] 월~목 / 추적 {len(ul_rebound.get_watchlist())}개"
        )
    k1_note = ""
    if k1_closing.is_enabled():
        k1_note = (
            f"\n🔷 K1 종가: 금·월 실전 / 보유 {len(_k1_closing_positions)}개 / "
            f"4일차 청산"
        )
    k2_note = ""
    if k2_intraday.is_enabled():
        k2_note = (
            f"\n🔶 K2: [시뮬만] / 추적 {len(k2_intraday.get_watchlist())}개 / "
            f"상한가일=D1~D{k2_intraday.MAX_DAYS_FROM_UL}"
        )
    plus_note = ""
    if k1_plus.is_enabled():
        mode = "K1훼손 즉시" if k1_plus.IMMEDIATE_ON_BREACH else "종가창만"
        plus_note = (
            f"\n💠 K1플러스: [시뮬만] / 추적 {len(k1_plus.get_watchlist())}개 / "
            f"{mode} / 세력봉 당일 양봉"
        )
    k2p_note = ""
    if k2_plus.is_enabled():
        k2p_note = (
            f"\n🔷 K2플러스: [시뮬만] / 추적 {len(k2_plus.get_watchlist())}개 / "
            f"세력봉 D1~D{k2_plus.MAX_DAYS_FROM_POWER} K2훼손"
        )
    sv_note = ""
    if strong_v_sim.is_enabled():
        sv_note = (
            f"\n🟢 강세V: [시뮬만] / "
            f"스캔 {os.getenv('STRONG_V_SCAN_START', '09:00')}~"
            f"{os.getenv('STRONG_V_ENTRY_END', '14:30')} / "
            f"5분(09~10시 2분·후보/보유 1분) / "
            f"전일종가 -{strong_v_sim.MAX_BELOW_PREV_PCT}% · MA5 ±{strong_v_sim.MA5_BELOW_TOLERANCE_PCT}%"
        )
    k1_pos_note = ""
    if _k1_closing_positions:
        k1_pos_note = f"\n🔷 K1 종가 보유 {len(_k1_closing_positions)}개 (4일 보유)"
    cash_line = (
        f"💰 주문가능금액: {orderable_cash:,}원\n"
        if orderable_cash is not None
        else "💰 주문가능금액: 조회 실패\n"
    )
    trading_today = is_trading_day()
    if not trading_today:
        notifier.send(
            f"🤖 자동매매 봇 시작 - {now_kst}\n"
            f"📍 모드: {os.getenv('KIS_MODE', '모의')}\n"
            f"{'✅' if acc_ok else '⚠️'} 계좌: {acc_msg}\n"
            f"{cash_line}"
            "📅 <b>오늘은 휴장일</b> — 매매·스크리닝·재검색 스케줄을 실행하지 않습니다.\n"
            "다음 거래일에 정상 동작합니다."
            f"{closing_pos_note}"
            f"{k1_pos_note}"
        )
    else:
        notifier.send(
            f"🤖 자동매매 봇 시작 (장중매매 + 종가베팅) - {now_kst}\n"
            f"📍 모드: {os.getenv('KIS_MODE', '모의')}\n"
            f"{'✅' if acc_ok else '⚠️'} 계좌: {acc_msg}\n"
            f"{cash_line}"
            "📌 장중매매: 09:05 스크리닝 → 09:10~14:30 진입 → 14:50 강제청산\n"
            f"   장중 AI: {'ON (Groq)' if ENABLE_INTRADAY_AI else 'OFF (기술조건만)'}\n"
            "🌙 종가: 14:00 스크리닝 → 14:45 AI매수(익일09:00매도) / 금 K1종가\n"
            f"✅ 익절 트레일링 +{TAKE_PROFIT_PCT}% / "
            f"손절 −{STOP_LOSS_PCT}% (매수 {QUICK_STOP_WINDOW_MIN}분 내 −{QUICK_STOP_LOSS_PCT}%) / "
            f"15:10 손익보고"
            f"{crash_note}"
            f"{v_note}"
            f"{ul_note}"
            f"{k1_note}"
            f"{k2_note}"
            f"{plus_note}"
            f"{k2p_note}"
            f"{sv_note}"
            f"{closing_pos_note}"
            f"{k1_pos_note}"
        )

    print("KST 직접 체크 루프 시작 (schedule 라이브러리 미사용)")
    print(f"현재 KST: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}")

    # ── 봇 재시작 시 즉시 실행 체크 ──────────────────────────────────────────────
    _init_t = datetime.now(KST)
    _init_min = _init_t.hour * 60 + _init_t.minute
    _init_today = _init_t.strftime("%Y-%m-%d")

    if is_trading_day():
        # 09:00 이후 재시작: 놓친 전일 종가베팅이 있으면 장중 보충 매도
        if 9 * 60 <= _init_min <= 15 * 60 + 20:
            if _due_closing_positions():
                notifier.send("▶️ 봇 재시작 감지 - 전일 종가베팅 보충 매도")
                _last_ran["closing_sell"] = _init_today
                run_morning_sell_closing_bet()
            elif _untracked_account_holdings():
                sync_closing_positions_from_account(notify=True)

        # 09:05~09:30 재시작: 장중매매 스크리닝 즉시 실행
        if 9 * 60 + 5 <= _init_min <= 9 * 60 + 30 and not _watchlist:
            notifier.send("▶️ 봇 재시작 감지 (스크리닝 시간) - 즉시 스크리닝 실행")
            if run_morning_screening():
                _last_ran["screening_ok"] = _init_today
                # 우선순위 A: K1+ > K2+ > K2 > UL (높은 쪽 먼저 선점)
                run_k1_plus_morning_scan()
                run_k2_plus_morning_scan()
                run_k2_morning_scan()
                run_ul_rebound_morning_scan()

    last_5min_slot = -1       # 장중매매 5분 슬롯
    last_closing_slot = -1    # 종가베팅 5분 슬롯
    last_screening_slot = -1  # 스크리닝 5분 재시도 슬롯
    last_strong_v_min = -1    # 강세V 시뮬 가변 주기

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
            last_strong_v_min = -1
        _last_ran["date"] = today

        # 휴장일·주말: API 서버만 유지, 매매 스케줄은 전부 스킵
        if not is_trading_day():
            time.sleep(30)
            continue

        # ── 09:00~15:20 KST - 전일 종가베팅 매도 (재시작 시 보충 포함) ──────
        if 9 * 60 <= t <= 15 * 60 + 20 and _last_ran.get("closing_sell") != today:
            _last_ran["closing_sell"] = today
            run_morning_sell_closing_bet()

        # ── 09:05~09:30 KST - 장중매매 워치리스트 스크리닝 (실패 시 5분마다 재시도) ──
        if 9 * 60 + 5 <= t <= 9 * 60 + 30 and _last_ran.get("screening_ok") != today:
            slot = t // 5
            if slot != last_screening_slot:
                last_screening_slot = slot
                if run_morning_screening():
                    _last_ran["screening_ok"] = today
                    # 우선순위 A: K1+ > K2+ > K2 > UL (높은 쪽 먼저 선점)
                    run_k1_plus_morning_scan()
                    run_k2_plus_morning_scan()
                    run_k2_morning_scan()
                    run_ul_rebound_morning_scan()

        # ── 09:10~14:45 KST - 5분마다 장중 진입/청산 체크 ───────────────────
        if 9 * 60 + 10 <= t <= 14 * 60 + 45:
            slot = t // 5
            if slot != last_5min_slot:
                last_5min_slot = slot
                run_market_check()

        # ── 09:00~14:50 KST - 강세V 시뮬 (5분 / 09~10시 2분 / 후보·보유 1분) ──
        if (
            strong_v_sim.is_enabled()
            and strong_v_sim.is_monitor_window()
            and 9 * 60 <= t <= 14 * 60 + 50
        ):
            interval = strong_v_sim.get_poll_interval_min()
            if last_strong_v_min < 0 or t - last_strong_v_min >= interval:
                last_strong_v_min = t
                _check_strong_v_sim()

        # ── 11:00~11:10 KST - 보충 스크리닝 (오전 워치리스트 0개) ─────────────
        if 11 * 60 <= t <= 11 * 60 + 10 and _last_ran.get("supplementary_screening") != today:
            _last_ran["supplementary_screening"] = today
            if not _watchlist:
                run_morning_screening(supplementary=True)

        # ── 11:00~11:10 KST - 상태 보고 ─────────────────────────────────────
        if 11 * 60 <= t <= 11 * 60 + 10 and _last_ran.get("status") != today:
            _last_ran["status"] = today
            run_status_report()

        # ── 13:15~13:20 KST - 오전 미체결 반등 전략 오후 필터 1회 재검색 ─────
        if (
            AFTERNOON_REBOUND_SCAN_MIN <= t <= AFTERNOON_REBOUND_SCAN_MIN + 5
            and _last_ran.get("afternoon_rebound_scan") != today
        ):
            _last_ran["afternoon_rebound_scan"] = today
            run_afternoon_rebound_scan()

        # ── 14:00~14:15 KST - 종가베팅 스크리닝 ─────────────────────────────
        if 14 * 60 <= t <= 14 * 60 + 15 and _last_ran.get("closing_bet_screening") != today:
            _last_ran["closing_bet_screening"] = today
            run_closing_bet_screening()

        # ── 14:45~14:50 KST - AI 종가베팅 매수 1회 ───────────────────────────
        if (
            CLOSING_BET_ENTRY_START <= t <= CLOSING_BET_ENTRY_END
            and _last_ran.get("closing_bet_entry") != today
            and not (k1_closing.is_enabled() and k1_closing.is_k1_closing_day())
        ):
            _last_ran["closing_bet_entry"] = today
            _check_closing_bet_entry()

        # ── 14:20~14:50 KST - K1 종가베팅 매수 (5분마다) ─────────────────────
        if 14 * 60 + 20 <= t <= 14 * 60 + 50:
            slot = t // 5
            if slot != last_closing_slot:
                last_closing_slot = slot
                if k1_closing.is_closing_entry_window():
                    _check_k1_closing_entry()

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
