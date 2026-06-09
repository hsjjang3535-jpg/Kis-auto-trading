"""
텔레그램 알림 모듈
- 매수/매도/에러 알림을 텔레그램으로 전송
"""
import requests
import os

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def _send_message(text: str, parse_mode: str = "Markdown"):
    """텔레그램 메시지 전송"""
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[Telegram] TOKEN 또는 CHAT_ID 없음. 메시지: {text[:100]}")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"[Telegram] 전송 실패: {resp.status_code} {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"[Telegram] 전송 오류: {e}")
        return False


def send_info(message: str):
    """일반 정보 메시지"""
    return _send_message(message)


def send_buy_alert(stock_name: str, stock_code: str, price, quantity: int, reason: str):
    """매수 알림"""
    text = (
        f"🟢 *매수 체결*\n"
        f"종목: {stock_name} ({stock_code})\n"
        f"가격: {price:,.0f}원\n"
        f"수량: {quantity}주\n"
        f"사유: {reason}"
    )
    return _send_message(text)


def send_sell_alert(stock_name: str, stock_code: str, price, quantity: int, profit_pct: float, reason: str):
    """매도 알림"""
    emoji = "🔴" if profit_pct < 0 else "🔵"
    text = (
        f"{emoji} *매도 체결*\n"
        f"종목: {stock_name} ({stock_code})\n"
        f"가격: {price:,.0f}원\n"
        f"수량: {quantity}주\n"
        f"수익률: {profit_pct:+.2f}%\n"
        f"사유: {reason}"
    )
    return _send_message(text)


def send_error_alert(message: str):
    """에러 알림"""
    text = f"⚠️ *에러 발생*\n{message}"
    return _send_message(text)


def send_daily_summary(balance: dict, holdings: list, market: str = "KR"):
    """일일 요약 보고"""
    cash = balance.get("cash", 0)
    total_eval = sum(h.get("eval_amount", 0) for h in holdings)

    lines = [
        f"📊 *[{market}] 일일 잔고 보고*",
        f"예수금: {cash:,.0f}{'원' if market == 'KR' else '$'}",
        f"보유 종목: {len(holdings)}개",
        f"평가금액: {total_eval:,.0f}{'원' if market == 'KR' else '$'}",
    ]

    if holdings:
        lines.append("")
        lines.append("*보유 내역:*")
        for h in holdings:
            name = h.get("stock_name", "")
            code = h.get("stock_code", "")
            qty = h.get("quantity", 0)
            profit = h.get("profit_loss_rate", 0)
            emoji = "📈" if profit >= 0 else "📉"
            lines.append(f"  {emoji} {name}({code}) {qty}주 {profit:+.2f}%")

    return _send_message("\n".join(lines))
