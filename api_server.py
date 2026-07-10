"""
트레이딩 봇 HTTP API 서버
텔레그램 봇에서 호출해 상태 확인 및 명령 실행
"""
import os
import threading
from flask import Flask, jsonify, request, abort

API_SECRET = os.getenv("API_SECRET", "")
flask_app = Flask(__name__)


def _check_auth():
    """API 시크릿 키 인증"""
    if API_SECRET and request.headers.get("X-API-Secret") != API_SECRET:
        abort(403)


@flask_app.route("/health")
def health():
    import kis_api
    import crash_bounce
    import v_reversal
    import ul_rebound
    warn = kis_api.validate_account_for_mode()
    try:
        acct = kis_api.get_account_info()
    except Exception as e:
        acct = {"source": "error", "masked": None, "error": str(e)}
    try:
        orderable_cash = kis_api.get_orderable_cash()
    except Exception:
        orderable_cash = 0
    return jsonify({
        "ok": True,
        "kis_mode": os.getenv("KIS_MODE", "모의"),
        "account": acct,
        "account_warning": warn,
        "orderable_cash": orderable_cash,
        "strategies": {
            "intraday_ai": os.getenv("ENABLE_INTRADAY_AI", "false").lower() == "true",
            "crash_bounce": crash_bounce.is_enabled(),
            "v_reversal": v_reversal.is_enabled(),
            "ul_rebound": ul_rebound.is_enabled(),
        },
    })


@flask_app.route("/status")
def get_status():
    """현재 봇 상태 조회"""
    _check_auth()
    import trader
    total_profit = sum(t["profit_won"] for t in trader._trades_today)
    win = sum(1 for t in trader._trades_today if t["profit_won"] > 0)
    lose = sum(1 for t in trader._trades_today if t["profit_won"] <= 0)
    return jsonify({
        "mode": os.getenv("KIS_MODE", "모의"),
        "watchlist_count": len(trader._watchlist),
        "positions_count": len(trader._positions),
        "invested_today": trader._total_invested_today,
        "max_total": trader.MAX_TOTAL_AMOUNT,
        "trades_count": len(trader._trades_today),
        "win": win,
        "lose": lose,
        "total_profit_won": total_profit,
    })


@flask_app.route("/positions")
def get_positions():
    """현재 보유 포지션 조회"""
    _check_auth()
    import trader, kis_api
    result = []
    for code, pos in trader._positions.items():
        try:
            info = kis_api.get_stock_info(code)
            current = float(info.get("stck_prpr", pos["buy_price"]))
            profit_pct = (current - pos["buy_price"]) / pos["buy_price"] * 100
        except Exception:
            current = pos["buy_price"]
            profit_pct = 0.0
        result.append({
            "code": code,
            "name": pos["name"],
            "quantity": pos["quantity"],
            "buy_price": pos["buy_price"],
            "current_price": int(current),
            "profit_pct": round(profit_pct, 2),
            "strategy": pos["strategy"],
        })
    return jsonify(result)


@flask_app.route("/screening", methods=["POST"])
def do_screening():
    """수동 스크리닝 실행"""
    _check_auth()
    import trader
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    t_min = now.hour * 60 + now.minute

    if not trader.is_trading_day():
        return jsonify({"ok": False, "message": "오늘은 거래일(주말/공휴일)이 아닙니다"})
    if not (9 * 60 <= t_min <= 15 * 60 + 30):
        return jsonify({
            "ok": False,
            "message": f"장 운영 시간이 아닙니다 (현재 KST {now.strftime('%H:%M')})\n스크리닝은 09:00~15:30 사이에 사용하세요."
        })
    th = threading.Thread(target=trader.run_morning_screening, daemon=True)
    th.start()
    return jsonify({"ok": True, "message": "스크리닝 시작됨"})


@flask_app.route("/close-all", methods=["POST"])
def do_close_all():
    """전체 포지션 긴급 청산"""
    _check_auth()
    import trader
    t = threading.Thread(target=trader.run_force_close, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "전체 청산 시작됨"})


def start_api_server():
    port = int(os.getenv("PORT", "8080"))
    print(f"[API 서버] 포트 {port} 시작")
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
