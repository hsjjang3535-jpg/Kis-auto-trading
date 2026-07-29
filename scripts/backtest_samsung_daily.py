"""
삼성전자(005930) 일봉 후보 규칙 1~2년 비교 백테스트 (장중 5분봉 없음).

Usage (from repo root):
  python scripts/backtest_samsung_daily.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

TG_ENV = Path(r"C:\Users\hsjja\Projects\telegram-gemini-bot\.env")
load_dotenv(TG_ENV)
load_dotenv(ROOT / ".env")

import kis_api

KST = ZoneInfo("Asia/Seoul")
CODE = "005930"

STOP_LOSS_PCT = 2.0
TAKE_PROFIT_PCT = 3.0
TRAILING_STOP_PCT = 0.6
MAX_HOLD_DAYS = 5


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Trade:
    entry_date: str
    exit_date: str
    entry: float
    exit: float
    pct: float
    reason: str


def fetch_daily_history(code: str, target_trading_days: int = 480) -> list[Bar]:
    """KIS 일봉 — 구간을 나눠 조회해 약 2년치 수집."""
    end_dt = datetime.now(KST)
    seen: set[str] = set()
    bars: list[Bar] = []

    for _ in range(12):
        start_dt = end_dt - timedelta(days=130)
        start = start_dt.strftime("%Y%m%d")
        end = end_dt.strftime("%Y%m%d")
        data = kis_api._market_get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            "FHKST03010100",
            {
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd": code,
                "fid_input_date_1": start,
                "fid_input_date_2": end,
                "fid_period_div_code": "D",
                "fid_org_adj_prc": "0",
            },
        )
        chunk = data.get("output2", []) or []
        for c in chunk:
            d = str(c.get("stck_bsop_date", ""))
            if not d or d in seen:
                continue
            try:
                bars.append(
                    Bar(
                        date=d,
                        open=float(c.get("stck_oprc", 0)),
                        high=float(c.get("stck_hgpr", 0)),
                        low=float(c.get("stck_lwpr", 0)),
                        close=float(c.get("stck_clpr", 0)),
                        volume=float(c.get("acml_vol", 0)),
                    )
                )
                seen.add(d)
            except (TypeError, ValueError):
                continue
        if len(bars) >= target_trading_days:
            break
        if not chunk:
            break
        end_dt = start_dt - timedelta(days=1)
        if end_dt < datetime.now(KST) - timedelta(days=900):
            break

    bars.sort(key=lambda b: b.date)
    return bars


def sma(closes: list[float], i: int, n: int) -> float | None:
    if i < n - 1:
        return None
    return sum(closes[i - n + 1:i + 1]) / n


def rsi(closes: list[float], i: int, period: int = 14) -> float | None:
    if i < period:
        return None
    gains, losses = 0.0, 0.0
    for j in range(i - period + 1, i + 1):
        diff = closes[j] - closes[j - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100 - (100 / (1 + rs))


def high_n(bars: list[Bar], i: int, n: int, exclude_today: bool = True) -> float:
    start = max(0, i - n)
    end = i if exclude_today else i + 1
    if end <= start:
        return bars[i].high
    return max(b.high for b in bars[start:end])


def simulate_exit(bars: list[Bar], entry_i: int, entry_price: float) -> Trade:
    peak = entry_price
    buy = entry_price
    for j in range(entry_i + 1, min(entry_i + 1 + MAX_HOLD_DAYS, len(bars))):
        b = bars[j]
        if b.low <= buy * (1 - STOP_LOSS_PCT / 100):
            exit_p = buy * (1 - STOP_LOSS_PCT / 100)
            pct = (exit_p - buy) / buy * 100
            return Trade(bars[entry_i].date, b.date, buy, exit_p, pct, "손절")
        if b.high > peak:
            peak = b.high
        peak_pct = (peak - buy) / buy * 100
        if peak_pct >= TAKE_PROFIT_PCT:
            trail_level = peak * (1 - TRAILING_STOP_PCT / 100)
            if b.close <= trail_level or b.low <= trail_level:
                exit_p = max(trail_level, b.close)
                pct = (exit_p - buy) / buy * 100
                return Trade(
                    bars[entry_i].date, b.date, buy, exit_p, pct,
                    f"트레일링(고점+{peak_pct:.1f}%)",
                )
    last_i = min(entry_i + MAX_HOLD_DAYS, len(bars) - 1)
    exit_p = bars[last_i].close
    pct = (exit_p - buy) / buy * 100
    return Trade(bars[entry_i].date, bars[last_i].date, buy, exit_p, pct, "시간청산")


def signal_ma60_pullback(bars: list[Bar], closes: list[float], i: int) -> bool:
    ma20 = sma(closes, i, 20)
    ma60 = sma(closes, i, 60)
    r = rsi(closes, i, 14)
    if ma20 is None or ma60 is None or r is None or ma60 <= 0:
        return False
    c = closes[i]
    gap = (c - ma60) / ma60 * 100
    if gap > 2.5 or gap < -1.5:
        return False
    if ma20 < ma60 * 0.985:
        return False
    if not (35 <= r <= 58):
        return False
    o = bars[i].open
    if c <= o:
        return False
    if i < 1 or c <= closes[i - 1]:
        return False
    return True


def signal_breakout(bars: list[Bar], closes: list[float], i: int) -> bool:
    ma20 = sma(closes, i, 20)
    ma60 = sma(closes, i, 60)
    r = rsi(closes, i, 14)
    if ma20 is None or ma60 is None or r is None:
        return False
    c = closes[i]
    if c <= ma20 or ma20 <= ma60:
        return False
    if r < 50:
        return False
    h20 = high_n(bars, i, 20, exclude_today=True)
    if c < h20:
        return False
    vol_avg = sum(bars[k].volume for k in range(i - 5, i)) / 5 if i >= 5 else bars[i].volume
    if vol_avg > 0 and bars[i].volume < vol_avg * 1.1:
        return False
    return True


def signal_mean_reversion(bars: list[Bar], closes: list[float], i: int) -> bool:
    ma20 = sma(closes, i, 20)
    ma60 = sma(closes, i, 60)
    r = rsi(closes, i, 14)
    if ma20 is None or ma60 is None or r is None:
        return False
    c = closes[i]
    if c >= ma20:
        return False
    if r >= 35:
        return False
    if ma60 > 0 and c < ma60 * 0.92:
        return False
    if i < 1 or c <= closes[i - 1]:
        return False
    return True


def run_strategy(name: str, bars: list[Bar], signal_fn) -> list[Trade]:
    closes = [b.close for b in bars]
    trades: list[Trade] = []
    i = 60
    while i < len(bars) - MAX_HOLD_DAYS - 1:
        if signal_fn(bars, closes, i):
            t = simulate_exit(bars, i, bars[i].close)
            trades.append(t)
            i += MAX_HOLD_DAYS + 1
        else:
            i += 1
    return trades


def summarize(name: str, trades: list[Trade]) -> dict:
    if not trades:
        return {
            "name": name,
            "n": 0,
            "win_pct": 0.0,
            "avg_pct": 0.0,
            "total_pct": 0.0,
            "max_dd_pct": 0.0,
            "profit_factor": 0.0,
        }
    wins = [t for t in trades if t.pct > 0]
    losses = [t for t in trades if t.pct <= 0]
    avg = sum(t.pct for t in trades) / len(trades)
    gross_win = sum(t.pct for t in wins)
    gross_loss = abs(sum(t.pct for t in losses))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

    equity = 100.0
    peak_eq = 100.0
    max_dd = 0.0
    for t in trades:
        equity *= (1 + t.pct / 100)
        peak_eq = max(peak_eq, equity)
        dd = (peak_eq - equity) / peak_eq * 100
        max_dd = max(max_dd, dd)

    return {
        "name": name,
        "n": len(trades),
        "win_pct": len(wins) / len(trades) * 100,
        "avg_pct": avg,
        "total_pct": sum(t.pct for t in trades),
        "max_dd_pct": max_dd,
        "profit_factor": pf,
    }


def main() -> int:
    print("삼성전자 일봉 백테스트 (KIS 수정주가)")
    print(f"청산: 손절 -{STOP_LOSS_PCT}% / +{TAKE_PROFIT_PCT}% 후 고점 -{TRAILING_STOP_PCT}% / 최대 {MAX_HOLD_DAYS}일")
    print("진입: 당일 종가 매수 가정 (장중 5분봉 미적용)\n")

    bars = fetch_daily_history(CODE, target_trading_days=480)
    if len(bars) < 120:
        print(f"데이터 부족: {len(bars)}일")
        return 1
    print(f"기간: {bars[0].date} ~ {bars[-1].date} ({len(bars)}거래일)\n")

    strategies = [
        ("A) MA60 눌림+양봉 (현재 시뮬 일봉 부분)", signal_ma60_pullback),
        ("B) 20일 고점 돌파 + 추세", signal_breakout),
        ("C) MA20 아래 과매도 반등", signal_mean_reversion),
    ]

    rows = []
    for name, fn in strategies:
        trades = run_strategy(name, bars, fn)
        rows.append(summarize(name, trades))

    header = f"{'규칙':<36} {'건수':>5} {'승률':>7} {'평균%':>8} {'합계%':>9} {'MDD%':>7} {'PF':>6}"
    print(header)
    print("-" * len(header))
    for r in rows:
        pf = r["profit_factor"]
        pf_s = f"{pf:.2f}" if pf != float("inf") else "inf"
        print(
            f"{r['name']:<36} {r['n']:>5} {r['win_pct']:>6.1f}% "
            f"{r['avg_pct']:>+7.2f}% {r['total_pct']:>+8.1f}% "
            f"{r['max_dd_pct']:>6.1f}% {pf_s:>6}"
        )

    best = max(rows, key=lambda x: (x["n"] >= 10, x["avg_pct"], x["win_pct"]))
    print("\n참고:")
    print("- 일봉만 사용했습니다. 실제 시뮬의 시가대비 -0.3~1.8%·5분봉 반등은 여기엔 없습니다.")
    print("- 합계%는 각 거래 수익률을 단순 합산(복리 아님). MDD는 거래 순서 누적 복리 equity 기준.")
    if best["n"] > 0:
        print(f"- 표본·평균 수익 기준 1차 후보: {best['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
