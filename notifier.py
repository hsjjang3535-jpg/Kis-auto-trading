import os
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send(message: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[텔레그램 미설정] {message}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"[텔레그램 전송 오류] {e}")


def notify_buy(name: str, code: str, quantity: int, price: int, reason: str) -> None:
    send(
        f"🟢 <b>매수 체결</b>\n"
        f"종목: {name} ({code})\n"
        f"수량: {quantity}주 / 가격: {price:,}원\n"
        f"AI 판단: {reason}"
    )


def notify_sell(name: str, code: str, quantity: int, profit_pct: float, reason: str = "") -> None:
    emoji = "📈" if profit_pct >= 0 else "📉"
    send(
        f"{emoji} <b>매도 체결</b>\n"
        f"종목: {name} ({code})\n"
        f"수량: {quantity}주 / 수익률: {profit_pct:+.2f}%\n"
        f"사유: {reason}"
    )


def notify_error(message: str) -> None:
    send(f"⚠️ <b>오류 발생</b>\n{message}")


_ALERT_LABELS = {
    "NEW_UL": "신규 상한가",
    "R1_BUY": "R1 매수구간",
    "R0_SELL": "R0 익절구간",
    "R2_ADD": "R2 추가매수",
    "R3_STOP": "R3 손절",
    "DAY4_FORCE": "4일차 청산",
    "EXPIRED": "추적 종료",
}


def notify_ul_rebound(
    alert_type: str,
    name: str,
    code: str,
    current: int,
    entry: dict,
    message: str,
    sim: dict | None = None,
) -> None:
    label = _ALERT_LABELS.get(alert_type, alert_type)
    lines = [
        f"🟣 <b>상한가 리바운드 [{label}]</b>",
        f"종목: {name} ({code})",
        f"현재가: {current:,}원" if current > 0 else "",
        f"상한가일: {entry.get('ul_date', '-')}",
        f"R0 {entry.get('r0', 0):,} | R1 {entry.get('r1', 0):,} | "
        f"R2 {entry.get('r2', 0):,} | R3 {entry.get('r3', 0):,}",
        f"💡 {message}",
    ]
    if sim:
        lines.append("📋 가상매매 이벤트 발생 (아래 체결 알림 참고)")
    else:
        lines.append("⚠️ 알림만 — 실제 주문 없음")
    send("\n".join(line for line in lines if line))


def notify_ul_rebound_sim_buy(
    name: str, code: str, quantity: int, price: int, reason: str,
) -> None:
    invested = quantity * price
    send(
        f"🟣🟢 <b>[시뮬] 매수 체결</b>\n"
        f"종목: {name} ({code})\n"
        f"수량: {quantity}주 / 가격: {price:,}원\n"
        f"투입금: {invested:,}원\n"
        f"사유: {reason}\n"
        f"⚠️ 가상매매 — 실제 주문 없음"
    )


def notify_ul_rebound_sim_add(
    name: str, code: str, quantity: int, price: int,
    total_quantity: int, avg_price: int, reason: str,
) -> None:
    send(
        f"🟣➕ <b>[시뮬] 추가매수</b>\n"
        f"종목: {name} ({code})\n"
        f"추가: {quantity}주 @ {price:,}원\n"
        f"총 {total_quantity}주 / 평단 {avg_price:,}원\n"
        f"사유: {reason}\n"
        f"⚠️ 가상매매 — 실제 주문 없음"
    )


def notify_ul_rebound_sim_sell(
    name: str, code: str, quantity: int,
    buy_price: int, sell_price: int,
    profit_pct: float, profit_won: int, reason: str,
) -> None:
    emoji = "📈" if profit_won >= 0 else "📉"
    sign = "+" if profit_won >= 0 else ""
    send(
        f"🟣{emoji} <b>[시뮬] 매도 체결</b>\n"
        f"종목: {name} ({code})\n"
        f"수량: {quantity}주\n"
        f"매수가: {buy_price:,}원 → 매도가: {sell_price:,}원\n"
        f"수익률: {sign}{profit_pct:.2f}% ({sign}{profit_won:,}원)\n"
        f"사유: {reason}\n"
        f"⚠️ 가상매매 — 실제 주문 없음"
    )


def notify_k1_sim_buy(
    name: str, code: str, quantity: int, price: int,
    pattern: str, reason: str,
) -> None:
    send(
        f"🔷🟢 <b>[K1 시뮬] 매수</b>\n"
        f"종목: {name} ({code})\n"
        f"패턴: {pattern}\n"
        f"수량: {quantity}주 / 가격: {price:,}원\n"
        f"사유: {reason}\n"
        f"📋 실전 매수와 함께 기록 (4일차 청산)"
    )


def notify_screening_result(candidates: list) -> None:
    if not candidates:
        send("🔍 오늘 종가베팅 후보 없음")
        return
    lines = ["🔍 <b>오늘 종가베팅 AI 선정 종목</b>\n"]
    for c in candidates:
        strategy_map = {"상단매매": "🔴", "돌파매매": "🟡", "하단매매": "🔵", "낙폭반등": "🔶", "V자반등": "🟢"}
        strategy_emoji = strategy_map.get(c.get("strategy", ""), "⚪")
        lines.append(
            f"{strategy_emoji} [{c.get('strategy', '')}] {c['name']}({c['code']}) {c['change_rate']:+.1f}%\n"
            f"  거래량: {c.get('vol_ratio', 0):.1f}x | 재료: {c['strength']} | {c['reason']}"
        )
    send("\n".join(lines))
