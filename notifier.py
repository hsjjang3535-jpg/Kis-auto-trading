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


def notify_sell(name: str, code: str, quantity: int, profit_pct: float) -> None:
    emoji = "📈" if profit_pct >= 0 else "📉"
    send(
        f"{emoji} <b>매도 체결</b>\n"
        f"종목: {name} ({code})\n"
        f"수량: {quantity}주 / 수익률: {profit_pct:+.2f}%"
    )


def notify_error(message: str) -> None:
    send(f"⚠️ <b>오류 발생</b>\n{message}")


def notify_screening_result(candidates: list) -> None:
    if not candidates:
        send("🔍 오늘 종가베팅 후보 없음")
        return
    lines = ["🔍 <b>오늘 종가베팅 AI 선정 종목</b>\n"]
    for c in candidates:
        strategy_emoji = "🔴" if c.get("strategy") == "상단매매" else "🔵"
        lines.append(
            f"{strategy_emoji} [{c.get('strategy', '')}] {c['name']}({c['code']}) {c['change_rate']:+.1f}%\n"
            f"  거래량: {c.get('vol_ratio', 0):.1f}x | 재료: {c['strength']} | {c['reason']}"
        )
    send("\n".join(lines))
