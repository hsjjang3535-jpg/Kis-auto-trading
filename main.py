
"""
KIS 자동매매 봇 v2
- 국내주식: 종산TV 3대 기법 (KIS API + 동적 스캔)
- 미국주식: KIS 해외주식 API (NASDAQ/NYSE) 모의투자
- 거래 이력: SQLite + 주간 엑셀 보고
- 종가매매: 장마감 30분 전 후보 스캔 + 다읍날 시초가 리포트
"""

import os
import json
import statistics
import sqlite3
import tempfile
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

import requests
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# ==================== 설정 ====================
KIS_APP_KEY = os.environ.get("KIS_APP_KEY", "")
KIS_APP_SECRET=***os.environ.get("KIS_APP_SECRET", "")
KIS_ACCOUNT_NUMBER = os.environ.get("KIS_ACCOUNT_NUMBER", "50191209-01")
KIS_BASE_URL = os.environ.get("KIS_BASE_URL", "https://openapivts.koreainvestment.com:29443")

TELEGRAM_BOT_TOKEN=***os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MAX_BUDGET = int(os.environ.get("MAX_BUDGET_PER_STOCK", "500000"))
US_MAX_BUDGET_USD = int(os.environ.get("US_MAX_BUDGET_PER_STOCK", "500"))  # USD
FALLBACK_WATCHLIST = os.environ.get("WATCHLIST", "005930,000660,035420,001820,001170,079550").split(",")
US_FALLBACK_WATCHLIST = os.environ.get("US_WATCHLIST", "AAPL,TSLA,NVDA,MSFT,AMZN,GOOGL").split(",")

TIMEZONE = "Asia/Seoul"
US_TIMEZONE = "US/Eastern"
STATE_FILE = "/tmp/trading_state.json"
DB_FILE = "/tmp/trades.db"

ETF_KEYWORDS = ("KODEX", "TIGER", "RISE", "SOL", "PLUS", "HANARO", "KB", "KBI",
                "미래에셏", "삼성인버스", "N2", "ARIRANG", "FOCUS", "HANA", "KOSEF", "TREX",
                "KINDEX", "KBSTAR")

# ==================== 테노그램 ====================
def send_tg(text: str, file_path: str = None):
TELEGRAM_BOT_TOKEN=os.env...N", "")
    chat = (TELEGRAM_CHAT_ID or "").strip()
    if not token or not chat:
        print(f"[TG 미설정] token={'*'*len(token) if token else 'EMPTY'} chat={'*'*len(chat) if chat else 'EMPTY'}")
        return
    try:
        if file_path and os.path.exists(file_path):
            with open(file_path, "rb") as f:
                r = requests.post(
                    f"https://api.telegram.org/bot{token}/sendDocument",
                    data={"chat_id": chat, "caption": text, "parse_mode": "Markdown"},
                    files={"document": f},
                    timeout=30
                )
                print(f"[TG sendDocument] status={r.status_code}, body={r.text[:300]}")
        else:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text, "parse_mode": "Markdown"},
                timeout=15
            )
            print(f"[TG sendMessage] status={r.status_code}, body={r.text[:300]}")
    except Exception as e:
        print(f"TG 전송 실패: {e}")

# ==================== 거래 이력 DB (SQLite) ====================
class TradeDB:
    def __init__(self, db_path: str = DB_FILE):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_tables()

    def _init_tables(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                market TEXT NOT NULL,  -- KOREA or US
                code TEXT NOT NULL,
                name TEXT,
                action TEXT NOT NULL,  -- BUY or SELL
                qty INTEGER NOT NULL,
                price REAL NOT NULL,
                reason TEXT,
                technique TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS close_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_date TEXT NOT NULL,
                market TEXT NOT NULL DEFAULT 'KR',
                code TEXT NOT NULL,
                name TEXT,
                close_price REAL,
                change_rate REAL,
                trading_value INTEGER,
                reason TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.commit()

    def log(self, market: str, code: str, name: str, action: str, qty: int, price: float, reason: str, technique: str = ""):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today = datetime.now().strftime("%Y%m%d")
        self.conn.execute(
            "INSERT INTO trades (trade_date, market, code, name, action, qty, price, reason, technique, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (today, market, code, name, action, qty, price, reason, technique, now)
        )
        self.conn.commit()

    def save_candidate(self, scan_date: str, code: str, name: str, close_price: float, change_rate: float, trading_value: int, reason: str, market: str = "KR"):
        self.conn.execute(
            "INSERT INTO close_candidates (scan_date, market, code, name, close_price, change_rate, trading_value, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (scan_date, market, code, name, close_price, change_rate, trading_value, reason)
        )
        self.conn.commit()

    def get_candidates(self, scan_date: str, market: str = "KR") -> List[Dict]:
        cur = self.conn.execute(
            "SELECT * FROM close_candidates WHERE scan_date = ? AND market = ? ORDER BY trading_value DESC",
            (scan_date, market)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_weekly_trades(self, start_date: str, end_date: str) -> List[Dict]:
        cur = self.conn.execute(
            "SELECT * FROM trades WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date, created_at",
            (start_date, end_date)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

trade_db = TradeDB()

# ==================== CSV 주간 보고 생성 ====================
def generate_weekly_report():
    """이번 주 월~금 거래 이력을 CSV로 생성하고 텔레그램 전송 (pandas 미사용)"""
    import csv

    now = datetime.now()
    monday = now - timedelta(days=now.weekday())
    friday = monday + timedelta(days=4)
    start = monday.strftime("%Y%m%d")
    end = friday.strftime("%Y%m%d")

    rows = trade_db.get_weekly_trades(start, end)
    if not rows:
        send_tg(f"📊 *{start}~{end} 주간 보고*\n이번 주 거래 이력이 없습니다.")
        return

    # 종목별 매수/매도 집계
    code_groups = {}
    for r in rows:
        code = r["code"]
        if code not in code_groups:
            code_groups[code] = {
                "name": r.get("name", code),
                "market": r.get("market", "KR"),
                "techniques": set(),
                "buys": [],
                "sells": []
            }
        code_groups[code]["techniques"].add(r.get("technique", ""))
        if r["action"] == "BUY":
            code_groups[code]["buys"].append(r)
        else:
            code_groups[code]["sells"].append(r)

    summary = []
    total_pl = 0.0
    win_cnt = 0
    loss_cnt = 0
    for code, info in code_groups.items():
        buy_qty = sum(b["qty"] for b in info["buys"])
        sell_qty = sum(s["qty"] for s in info["sells"])
        avg_buy = round(statistics.mean([b["price"] for b in info["buys"]]) if info["buys"] else 0, 2)
        avg_sell = round(statistics.mean([s["price"] for s in info["sells"]]) if info["sells"] else 0, 2)
        if avg_buy > 0 and avg_sell > 0:
            pl_pct = round((avg_sell - avg_buy) / avg_buy * 100, 2)
        else:
            pl_pct = 0.0
        total_pl += pl_pct
        if pl_pct > 0:
            win_cnt += 1
        elif pl_pct < 0:
            loss_cnt += 1
        summary.append({
            "code": code,
            "name": info["name"],
            "market": info["market"],
            "total_buy_qty": buy_qty,
            "total_sell_qty": sell_qty,
            "avg_buy": avg_buy,
            "avg_sell": avg_sell,
            "pl_pct": pl_pct,
            "technique": ", ".join(t for t in info["techniques"] if t)
        })

    total_cnt = len(summary)
    avg_total_pl = round(total_pl / total_cnt, 2) if total_cnt else 0.0

    # CSV 파일 작성
    tmp_path = f"/tmp/weekly_report_{start}_{end}.csv"
    with open(tmp_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        # 1) 주간 요약 시트
        writer.writerow(["주간", f"{start}~{end}"])
        writer.writerow(["총 매도 종목", total_cnt])
        writer.writerow(["이김", win_cnt])
        writer.writerow(["져밭", loss_cnt])
        writer.writerow(["총 수익률(평균)", f"{avg_total_pl:.2f}%"])
        writer.writerow([])
        # 2) 종목별 요약
        writer.writerow(["종목별 요약"])
        writer.writerow(["code", "name", "market", "total_buy_qty", "total_sell_qty", "avg_buy", "avg_sell", "pl_pct", "technique"])
        for s in summary:
            writer.writerow([
                s["code"], s["name"], s["market"],
                s["total_buy_qty"], s["total_sell_qty"],
                s["avg_buy"], s["avg_sell"], s["pl_pct"], s["technique"]
            ])
        writer.writerow([])
        # 3) 상세 거래 이력
        writer.writerow(["상세 거래 이력"])
        headers = list(rows[0].keys()) if rows else []
        writer.writerow(headers)
        for r in rows:
            writer.writerow([r.get(h, "") for h in headers])

    msg = (
        f"📊 *{start}~{end} 주간 거래 보고*\n"
        f"총 매도 종목: {total_cnt}\n"
        f"이김: {win_cnt} / 져밭: {loss_cnt}\n"
        f"총 수익률(평균): {avg_total_pl:.2f}%"
    )
    send_tg(msg, file_path=tmp_path)
    print(f"[weekly_report] CSV 생성 완료: {tmp_path}")

# ==================== KIS API (국내) ====================
class KISClient:
    def __init__(self):
        self.token = None
        self.expires = None

    def _get_token(self):
        if self.token and self.expires and datetime.now() < self.expires:
            return self.token
        url = f"{KIS_BASE_URL}/oauth2/token"
KIS_APP_SECRET=os.env...T", "")
        r = requests.post(url, headers={"content-type": "application/json"}, data=json.dumps(body), timeout=15)
        d = r.json()
        if "access_token" not in d:
            raise Exception(f"토큰 실패: {d}")
        self.token = d["access_token"]
        sec = d.get("expires_in", 86400)
        self.expires = datetime.now() + timedelta(seconds=sec - 600)
        return self.token

    def _headers(self, tr_id):
        return {
            "content-type": "application/json",
            "authorization": f"Bearer {self._get_token()}",
            "appkey": KIS_APP_KEY,
KIS_APP_SECRET=os.env...T", "")
            "tr_id": tr_id,
        }

    def get_price(self, code: str):
        url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
        r = requests.get(url, headers=self._headers("FHKST01010100"), params=params, timeout=15)
        d = r.json()
        o = d.get("output", {})
        return {
            "name": o.get("hts_kor_isnm", code),
            "price": int(o.get("stck_prpr", 0)),
            "prev": int(o.get("stck_prdy_clpr", 0)),
            "open": int(o.get("stck_oprc", 0)),
            "change": float(o.get("prdy_ctrt", 0)),
            "value": int(o.get("acml_tr_pbmn", 0)),
        }

    def get_minute(self, code: str):
        url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_HOUR_1": "5", "FID_PW_DATA_INCU_YN": "N"}
        r = requests.get(url, headers=self._headers("FHKST03010200"), params=params, timeout=15)
        return r.json().get("output2", [])

    def get_daily(self, code: str):
        url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1"}
        r = requests.get(url, headers=self._headers("FHKST01010400"), params=params, timeout=15)
        return r.json().get("output2", [])[:60]

    def get_balance(self):
        cano = KIS_ACCOUNT_NUMBER.split("-")[0]
        prdt = KIS_ACCOUNT_NUMBER.split("-")[1] if "-" in KIS_ACCOUNT_NUMBER else "01"
        url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
        params = {
            "CANO": cano, "ACNT_PRDT_CD": prdt, "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "01",
            "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCC_ASTM_TCD": "", "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        }
        r = requests.get(url, headers=self._headers("VTTC8434R"), params=params, timeout=15)
        d = r.json()
        holdings = []
        for item in d.get("output1", []):
            if item.get("pdno"):
                holdings.append({
                    "code": item["pdno"],
                    "name": item.get("prdt_name", ""),
                    "qty": int(item.get("hldg_qty", 0)),
                    "avg": int(item.get("pchs_avg_pric", 0)),
                    "cur": int(item.get("prpr", 0)),
                    "pl": float(item.get("evlu_pfls_rt", 0)),
                })
        cash = int(d.get("output2", [{}])[0].get("dnca_tot_amt", 0))
        return cash, holdings

    def buy(self, code: str, qty: int):
        cano = KIS_ACCOUNT_NUMBER.split("-")[0]
        prdt = KIS_ACCOUNT_NUMBER.split("-")[1] if "-" in KIS_ACCOUNT_NUMBER else "01"
        url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
        body = {"CANO": cano, "ACNT_PRDT_CD": prdt, "PDNO": code, "ORD_DVSN": "01", "ORD_QTY": str(qty), "ORD_UNPR": "0"}
        return requests.post(url, headers=self._headers("VTTC0802U"), data=json.dumps(body), timeout=15).json()

    def sell(self, code: str, qty: int):
        cano = KIS_ACCOUNT_NUMBER.split("-")[0]
        prdt = KIS_ACCOUNT_NUMBER.split("-")[1] if "-" in KIS_ACCOUNT_NUMBER else "01"
        url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
        body = {"CANO": cano, "ACNT_PRDT_CD": prdt, "PDNO": code, "ORD_DVSN": "01", "ORD_QTY": str(qty), "ORD_UNPR": "0"}
        return requests.post(url, headers=self._headers("VTTC0801U"), data=json.dumps(body), timeout=15).json()

# ==================== KIS API (미국 / 해외주식) ====================
class KISOverseasClient:
    """
KIS 해외주식 API (모의투자)
각 메서드의 TR_ID는 실제 KIS 문서와 대조 후 적용 필요.
아래는 일반적인 해외주식 API 구조를 사용한 플렉스를 바탕오를 잎은 것입니다.
"""
    def __init__(self, client: KISClient):
        self.client = client  # 토큰 재사용

    def _headers(self, tr_id):
        return {
            "content-type": "application/json",
            "authorization": f"Bearer {self.client._get_token()}",
            "appkey": KIS_APP_KEY,
KIS_APP_SECRET=os.env...T", "")
            "tr_id": tr_id,
        }

    def _market_code(self, ticker: str) -> str:
        # 미국 종목 거래소 추정 (간단화: NASD vs NYSE)
        # 실제로는 KIS API에서 제공하지 않으면 외부 데이터 사용 권장
        nasdaq_list = set(os.environ.get("NASDAQ_LIST", "AAPL,MSFT,AMZN,GOOGL,META,TSLA,NVDA,NFLX,ADBE,INTC,CSCO,PYPL,CMCSA,PEP,COST,AVGO,TXN,QCOM,AMD,AMGN").split(","))
        return "NAS" if ticker in nasdaq_list else "NYS"

    def get_price(self, ticker: str):
        mk = self._market_code(ticker)
        # 해외 현재가: FHKST03010100
        url = f"{KIS_BASE_URL}/uapi/overseas-price/v1/quotations/price"
        params = {"FID_COND_MRKT_DIV_CODE": mk, "FID_INPUT_ISCD": ticker}
        r = requests.get(url, headers=self._headers("FHKST03010100"), params=params, timeout=15)
        d = r.json()
        o = d.get("output", {})
        try:
            price = float(o.get("ovrs_nmix_prpr", 0))
            prev = float(o.get("ovrs_nmix_prdy_clpr", 0))
            change = float(o.get("ovrs_nmix_prdy_ctrt", 0))
            value = int(o.get("acml_tr_pbmn", 0))
        except Exception:
            price = prev = change = value = 0
        return {
            "name": ticker,
            "price": price,
            "prev": prev,
            "change": change,
            "value": value,
            "market": mk,
        }

    def get_balance(self):
        cano = KIS_ACCOUNT_NUMBER.split("-")[0]
        prdt = KIS_ACCOUNT_NUMBER.split("-")[1] if "-" in KIS_ACCOUNT_NUMBER else "01"
        url = f"{KIS_BASE_URL}/uapi/overseas-stock/v1/trading/inquire-balance"
        params = {
            "CANO": cano, "ACNT_PRDT_CD": prdt,
            "OVRS_EXCG_CD": "NASD",  # 미국 종합 조회시 NASD를 사용하는 경우도 있음; 실제 KIS 문서 참조
            "TR_CRCY_CODE": "USD",
            "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
        }
        r = requests.get(url, headers=self._headers("VTTS9112R"), params=params, timeout=15)
        d = r.json()
        holdings = []
        for item in d.get("output1", []):
            if item.get("ovrs_pdno"):
                holdings.append({
                    "code": item["ovrs_pdno"],
                    "name": item.get("ovrs_item_name", item["ovrs_pdno"]),
                    "qty": int(item.get("ovrs_cblc_qty", 0)),
                    "avg": float(item.get("pchs_avg_pric", 0)),
                    "cur": float(item.get("now_pric2", 0)),
                    "pl": float(item.get("evlu_pfls_rt", 0)),
                })
        cash = float(d.get("output2", [{}])[0].get("tot_dncl_amt", 0))
        return cash, holdings

    def buy(self, ticker: str, qty: int, market: str = "NAS"):
        cano = KIS_ACCOUNT_NUMBER.split("-")[0]
        prdt = KIS_ACCOUNT_NUMBER.split("-")[1] if "-" in KIS_ACCOUNT_NUMBER else "01"
        ovrs_excg = "NASD" if market in ("NAS", "NASDAQ") else "NYSE"
        url = f"{KIS_BASE_URL}/uapi/overseas-stock/v1/trading/order"
        body = {
            "CANO": cano, "ACNT_PRDT_CD": prdt,
            "OVRS_EXCG_CD": ovrs_excg,
            "PDNO": ticker,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": "0",
            "CTNS_TCNCL_DVSN_CD": "01",
            "SLL_BUY_DVSN_CD": "02",  # 매수
            "ORD_SVR_DVSN_CD": "0",
            "TR_CRCY_CODE": "USD",
        }
        return requests.post(url, headers=self._headers("VTTS0311U"), data=json.dumps(body), timeout=15).json()

    def sell(self, ticker: str, qty: int, market: str = "NAS"):
        cano = KIS_ACCOUNT_NUMBER.split("-")[0]
        prdt = KIS_ACCOUNT_NUMBER.split("-")[1] if "-" in KIS_ACCOUNT_NUMBER else "01"
        ovrs_excg = "NASD" if market in ("NAS", "NASDAQ") else "NYSE"
        url = f"{KIS_BASE_URL}/uapi/overseas-stock/v1/trading/order"
        body = {
            "CANO": cano, "ACNT_PRDT_CD": prdt,
            "OVRS_EXCG_CD": ovrs_excg,
            "PDNO": ticker,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": "0",
            "CTNS_TCNCL_DVSN_CD": "01",
            "SLL_BUY_DVSN_CD": "01",  # 매도
            "ORD_SVR_DVSN_CD": "0",
            "TR_CRCY_CODE": "USD",
        }
        return requests.post(url, headers=self._headers("VTTS0312U"), data=json.dumps(body), timeout=15).json()

# ==================== 동적 종목 스캔 ====================
def fetch_hot_stocks_from_kis(limit=20, min_value=30_000_000_000):
    try:
        url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/ranking/volume-ranks"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_RANK_SORT_CLS_CODE": "2",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "0",
            "FID_TRGT_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "0",
            "FID_TRGT_EXLS_CLS_CODE": "0",
            "FID_INPUT_PRICE_1": "0",
            "FID_INPUT_PRICE_2": "0",
            "FID_VOL_CNT": "0",
        }
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {client._get_token()}",
            "appkey": KIS_APP_KEY,
KIS_APP_SECRET=os.env...T", "")
            "tr_id": "FHPST01710000",
        }
        r = requests.get(url, headers=headers, params=params, timeout=15)
        d = r.json()
        if d.get("rt_cd") != "0":
            print(f"KIS 상위조회 실패: {d}")
            return []
        stocks = []
        for item in d.get("output", []):
            code = item.get("mksc_shrn_iscd", "")
            name = item.get("hts_kor_isnm", "")
            if not code or len(code) != 6:
                continue
            if any(name.startswith(k) for k in ETF_KEYWORDS):
                continue
            try:
                change_rate = float(item.get("prdy_ctrt", 0))
            except:
                continue
            try:
                value = int(item.get("acml_tr_pbmn", 0))
            except:
                continue
            try:
                vol_rate = float(item.get("prdy_vrss_vol_rate", 0))
            except:
                vol_rate = 0.0
            # 거래대금 기준: 1.5배 급증 시 반드시 포함, 그 외는 min_value 적용
            if vol_rate >= 150:
                threshold = min_value // 3  # 급증 시 1/3 기준으로 하향
                if threshold < 10_000_000_000:
                    threshold = 10_000_000_000
            else:
                threshold = min_value
            if value < threshold:
                continue
            stocks.append({"code": code, "name": name, "change_rate": change_rate, "trading_value": value, "vol_rate": vol_rate})
        # 거래대금 급증율 높은 순으로 정렬
        stocks.sort(key=lambda x: x.get("vol_rate", 0), reverse=True)
        return stocks[:limit]
    except Exception as e:
        print(f"KIS 상위조회 예외: {e}")
        return []

# ==================== 미국 종목 스캔 (Yahoo Finance 편용) ====================
def fetch_us_hot_stocks(limit=20):
    """
Yahoo Finance 기반 최근 거래대금 상위 스크린더 (무료)
KIS 해외 거래대깃 순위 API가 있다면 거기로 갱신 가능
"""
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?formatted=true&start=0&count=50&scrIds=most_actives"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        d = r.json()
        items = d.get("finance", {}).get("result", [])[0].get("quotes", [])
        stocks = []
        for q in items:
            ticker = q.get("symbol", "")
            name = q.get("shortName", ticker)
            price = q.get("regularMarketPrice", {}).get("raw", 0)
            change = q.get("regularMarketChangePercent", {}).get("raw", 0)
            value = q.get("regularMarketVolume", {}).get("raw", 0) * price
            if not ticker or price <= 0:
                continue
            # ETF 제외는 추후 필터 강화
            if value < 1_000_000_000:  # 10억 달러 이하 제외
                continue
            stocks.append({"code": ticker, "name": name, "change_rate": change, "trading_value": value, "vol_rate": 0})
        return stocks[:limit]
    except Exception as e:
        print(f"Yahoo Finance US 스크런 실패: {e}")
        return []

# ==================== Yahoo Finance 일별시세 (백테스트 용) ====================
def fetch_yahoo_daily(code: str, range_days: int = 10):
    """
Yahoo Finance API로 국내 주식 일별시세를 조회한다.
반화: {"2026.06.02": {"open": 10000, "close": 10200}, ...}
    """
    try:
        ticker = f"{code}.KS"
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range={range_days}d"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        d = r.json()
        result = d.get("chart", {}).get("result", [[]])[0]
        timestamps = result.get("timestamp", [])
        quote = result.get("indicators", {}).get("quote", [{}])[0]
        opens = quote.get("open", [])
        closes = quote.get("close", [])
        data = {}
        for i, ts in enumerate(timestamps):
            if closes[i] is not None:
                date = datetime.fromtimestamp(ts).strftime("%Y.%m.%d")
                data[date] = {
                    "open": round(opens[i], 2) if opens[i] else 0,
                    "close": round(closes[i], 2) if closes[i] else 0,
                }
        return data
    except Exception as e:
        print(f"Yahoo Finance 조회 오류 [{code}]: {e}")
        return {}

# ==================== 전략 ====================
client = KISClient()
us_client = KISOverseasClient(client)

def sma(vals, n):
    if len(vals) < n:
        return None
    return statistics.mean(vals[-n:])

def analyze(code: str, is_us: bool = False):
    if is_us:
        price = us_client.get_price(code)
        # 미국주식은 KIS 분능 데이터가 제한적이라 Yahoo Finance 편이백 사용
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}?interval=5m&range=5d"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            d = r.json()
            result = d.get("chart", {}).get("result", [[]])[0]
            timestamps = result.get("timestamp", [])
            closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
            volumes = result.get("indicators", {}).get("quote", [{}])[0].get("volume", [])
            minute = [{"stck_prpr": c, "cntg_vol": v} for c, v in zip(closes, volumes) if c is not None]
        except Exception:
            minute = []
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}?interval=1d&range=60d"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            d = r.json()
            result = d.get("chart", {}).get("result", [[]])[0]
            daily_closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
            daily_volumes = result.get("indicators", {}).get("quote", [{}])[0].get("volume", [])
            daily_dates = result.get("timestamp", [])
            daily = [{"stck_clpr": c, "acml_tr_pbmn": v * c, "stck_bsop_date": str(datetime.fromtimestamp(ts).strftime("%Y%m%d"))}
                     for c, v, ts in zip(daily_closes, daily_volumes, daily_dates) if c is not None]
        except Exception:
            daily = []
    else:
        minute = client.get_minute(code)
        daily = client.get_daily(code)
        price = client.get_price(code)

    ma = {}
    if minute and len(minute) >= 5:
        c = [float(x.get("stck_prpr", 0)) for x in minute]
        v = [int(x.get("cntg_vol", 0)) for x in minute]
        ma5 = sma(c, 5)
        ma20 = sma(c, 20)
        mv5 = sma(v, 5)
        recent_low = min(c[-10:])
        recent_high = max(c[-10:])
        ma = {
            "price": c[-1],
            "ma5": ma5, "ma20": ma20,
            "above_ma5": c[-1] > ma5 if ma5 else False,
            "above_ma20": c[-1] > ma20 if ma20 else False,
            "golden": ma5 > ma20 if ma5 and ma20 else False,
            "vol_spike": v[-1] > mv5 * 1.5 if mv5 else False,
            "pullback": (c[-1] - recent_high) / recent_high * 100 if recent_high else 0,
            "bounce": (c[-1] - recent_low) / recent_low * 100 if recent_low else 0,
        }

    da = {}
    if daily and len(daily) >= 5:
        daily.sort(key=lambda x: x.get("stck_bsop_date", ""), reverse=True)
        cl = [float(x.get("stck_clpr", 0)) for x in daily]
        tv = [int(x.get("acml_tr_pbmn", 0)) for x in daily]
        da = {
            "is_pos": cl[0] > cl[1] if len(cl) > 1 else False,
            "value": tv[0],
            "above_ma5": cl[0] > sma(cl, 5) if len(cl) >= 5 else False,
        }
    return price, ma, da

def eval_buy(price, ma, da, is_us: bool = False, vol_rate: float = 0.0):
    """
    종산TV 3대 기법을 각각 독립적으로 판정 (OR 로직)
    - 기법1: 종가베팅 (양봉 + 거래대금)
    - 기법2: 이평선 (5선/20선 정배열 + 골든크로스) → 상승/하락 괴교없이 판정
    - 기법3: 눈름목/돌파 (눈름 또는 돌파 + 양봉 조건 왜화)
    """
    p = price["price"]
    if p <= 0:
        return False, 0, "", "가격 데이터 없음"

    # 공통 필터: 과도성장
    gap = (p - price.get("prev", p)) / price.get("prev", p) * 100 if price.get("prev") else 0
    if gap > 15:
        return False, 0, "", f"과도성장{gap:.0f}%"

    budget = US_MAX_BUDGET_USD if is_us else MAX_BUDGET
    qty = max(1, int(budget * 0.95 / p))

    # 기법 1: 종가베팅 (양봉 + 거래대금) → 양봉 종목만 적용
    if da.get("is_pos"):
        if is_us:
            return True, qty, "양봉+활발", "종가베팅"
        elif da.get("value", 0) >= 30_000_000_000:  # 300억 원으로 하향
            return True, qty, "양봉+거래대", "종가베팅"

    # 기법 2: 이평선 (5선 위 + 20선 위 + 골든크로스) → 상승/하락 괴교없이 판정
    if ma.get("above_ma5") and ma.get("above_ma20") and ma.get("golden"):
        reasons = ["이평선정배열"]
        tech = "이평선"
        if -8 < ma.get("pullback", 0) < -2:
            reasons.append(f"눈름{ma['pullback']:.1f}%")
            tech += "+눈름목"
        elif ma.get("bounce", 0) > 1:
            reasons.append(f"돌파{ma['bounce']:.1f}%")
            tech += "+돌파"
        return True, qty, ";".join(reasons), tech

    # 기법 3: 눈름목 단독 (눈름 + 양봉 조건 완화)
    if -8 < ma.get("pullback", 0) < -2 and da.get("is_pos"):
        return True, qty, f"눈름{ma['pullback']:.1f}%", "눈름목"

    # 기법 4: 돌파 단독 (돌파 + 양봉 조건 완화)
    if ma.get("bounce", 0) > 1 and da.get("is_pos"):
        return True, qty, f"돌파{ma['bounce']:.1f}%", "돌파"

    # 실패 이유 요약
    fail_reasons = []
    if not da.get("is_pos"):
        fail_reasons.append("은봉")
    if not (ma.get("above_ma5") and ma.get("above_ma20")):
        fail_reasons.append("이평선배열X")
    if not ma.get("golden"):
        fail_reasons.append("골든크로스X")
    if not (-8 < ma.get("pullback", 0) < -2):
        fail_reasons.append(f"눈름{ma.get('pullback',0):.1f}%")
    if not (ma.get("bounce", 0) > 1):
        fail_reasons.append(f"돌파{ma.get('bounce',0):.1f}%")
    if not (da.get("value", 0) >= 30_000_000_000):
        fail_reasons.append(f"거래대{da.get('value',0)//1_000_000_000:.0f}억")

    return False, 0, "", ", ".join(fail_reasons)

def eval_sell(pos, price, ma, is_us: bool = False):
    avg = pos.get("avg", 0)
    cur = price.get("price", 0)
    qty = pos.get("qty", 0)
    if avg <= 0 or qty <= 0:
        return False, 0, ""
    pl = (cur - avg) / avg * 100
    if pl >= 3.0:
        return True, qty, f"수익+{pl:.1f}%"
    if pl <= -2.5:
        return True, qty, f"손절{pl:.1f}%"
    if not ma.get("above_ma5", True):
        return True, qty, "5선이탈"
    return False, 0, ""

# ==================== 종가매매 후보 스캔 (오후 3시) ====================
def scan_close_candidates():
    """
매일 오후 3시: 종가매매 후보 종목을 스캔하여 DB에 저장하고 템레그랼 전송
    """
    now = datetime.now(pytz.timezone(TIMEZONE))
    today = now.strftime("%Y%m%d")
    print(f"\n=== 종가매매 후보 스캔 시작 {now.strftime('%Y-%m-%d %H:%M:%S')} ===")

    hot = fetch_hot_stocks_from_kis(limit=30, min_value=50_000_000_000)
    if not hot:
        hot = [{"code": c, "name": c, "change_rate": 0, "trading_value": 0} for c in FALLBACK_WATCHLIST]
        print("스얀 실패, 폴백 WATCHLIST 사용")

    candidates = []
    for item in hot:
        code = item["code"]
        try:
            price, ma, da = analyze(code, is_us=False)
            name = price.get("name", item.get("name", code))
            fl, qty, r, tech = eval_buy(price, ma, da)
            if fl:
                candidates.append({
                    "code": code,
                    "name": name,
                    "close_price": price["price"],
                    "change_rate": price["change"],
                    "trading_value": da.get("value", 0),
                    "reason": r,
                    "technique": tech,
                })
                vol_tag = f" 거래대금 {vol_rate:.0f}% 급증" if vol_rate >= 150 else ""
                trade_db.save_candidate(
                    scan_date=today,
                    market="KR",
                    code=code,
                    name=name,
                    close_price=price["price"],
                    change_rate=price["change"],
                    trading_value=da.get("value", 0),
                    reason=r + vol_tag,
                )
        except Exception as e:
            print(f"    -> 오류 [{code}]: {e}")
            continue

    if not candidates:
        send_tg(f"🔍 *{today} 종가매매 후보*\n조건을 만족하는 종목이 없습니다.")
        print("후보 없음")
        return

    lines = []
    for c in candidates[:10]:
        vol_tag = f" | 거래대금 {c['vol_rate']:.0f}% 급증" if c.get('vol_rate', 0) >= 150 else ""
        lines.append(f"*{c['name']}* ({c['code']})\n  종가: {c['close_price']:,}원 | 등락률: {c['change_rate']:.1f}% | 사유: {c['reason']}{vol_tag}")

    msg = f"🔍 *{today} 종가매매 후보 종목* (총 {len(candidates)}개)\n\n" + "\n\n".join(lines)
    send_tg(msg)
    print(f"후보 {len(candidates)}개 전송 완료")

# ==================== 다읍날 시초가 리포트 (오전 9:05) ====================
def next_day_open_report():
    """다읍날 오전 9:05: 전일 종가매매 후보 종목의 종가 vs 오늘 시초가 비교
    """
    now = datetime.now(pytz.timezone(TIMEZONE))
    today = now.strftime("%Y%m%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
    # 학요일 대응 (전일이 목요일이면 금요일을 찾음)
    wday = now.weekday()
    if wday == 0:  # 월요일
        yesterday = (now - timedelta(days=3)).strftime("%Y%m%d")
    elif wday == 5:  # 토요일
        return  # 시장 아님
    elif wday == 6:  # 일요일
        return  # 시장 아님

    print(f"\n=== 시초가 리포트 시작 {now.strftime('%Y-%m-%d %H:%M:%S')} ===")
    candidates = trade_db.get_candidates(yesterday, market="KR")
    if not candidates:
        send_tg(f"📊 *{today} 시초가 리포트*\n전일({yesterday}) 후보 종목이 없습니다.")
        print("전일 후보 없음")
        return

    lines = []
    win = 0
    loss = 0
    for c in candidates:
        code = c["code"]
        name = c["name"]
        close_price = c["close_price"]
        try:
            price = client.get_price(code)
            open_price = price.get("open", 0)
            if open_price and close_price:
                pl_pct = (open_price - close_price) / close_price * 100
                emoji = "🔺" if pl_pct >= 0 else "🔴"
                if pl_pct >= 0:
                    win += 1
                else:
                    loss += 1
                lines.append(f"{emoji} *{name}* ({code})\n  전일종가: {close_price:,}원 → 오늘시가: {open_price:,}원 | 수익률: {pl_pct:+.2f}%")
            else:
                lines.append(f"⚠ *{name}* ({code})\n  시초가 데이터 없음")
        except Exception as e:
            lines.append(f"⚠ *{name}* ({code})\n  조회 오류: {e}")
            continue

    summary = f"총 {len(candidates)}개 중 이김: {win} / 져밭: {loss}\n\n"
    msg = f"📊 *{today} 시초가 리포트* (전일 {yesterday} 후보 기준)\n\n" + summary + "\n".join(lines)
    send_tg(msg)
    print(f"리포트 전송 완료: 이김 {win}, 져밭 {loss}")

# ==================== 미국장 종가매매 후보 스캔 (오후 3:30 EST) ====================
def scan_us_close_candidates():
    """
미국 동부 오후 3:30: 미국 종가매매 후보 종목을 스캔하여 DB에 저장하고 텔레그램 전송
    """
    now = datetime.now(pytz.timezone(US_TIMEZONE))
    today = now.strftime("%Y%m%d")
    print(f"\n=== 미국 종가매매 후보 스캔 시작 {now.strftime('%Y-%m-%d %H:%M:%S')} EST ===")

    hot = fetch_us_hot_stocks(limit=30)
    if not hot:
        hot = [{"code": c, "name": c, "change_rate": 0, "trading_value": 0} for c in US_FALLBACK_WATCHLIST]
        print("미국 스캔 실패, 폴백 WATCHLIST 사용")

    candidates = []
    for item in hot:
        code = item["code"]
        try:
            price, ma, da = analyze(code, is_us=True)
            name = price.get("name", item.get("name", code))
            fl, qty, r, tech = eval_buy(price, ma, da, is_us=True)
            if fl:
                candidates.append({
                    "code": code,
                    "name": name,
                    "close_price": price["price"],
                    "change_rate": price["change"],
                    "trading_value": da.get("value", 0),
                    "reason": r,
                    "technique": tech,
                })
                trade_db.save_candidate(
                    scan_date=today,
                    market="US",
                    code=code,
                    name=name,
                    close_price=price["price"],
                    change_rate=price["change"],
                    trading_value=da.get("value", 0),
                    reason=r,
                )
        except Exception as e:
            print(f"    -> 오류 [{code}]: {e}")
            continue

    if not candidates:
        send_tg(f"🔍 *{today} 미국 종가매매 후보*\n조건을 만족하는 종목이 없습니다.")
        print("후보 없음")
        return

    lines = []
    for c in candidates[:10]:
        lines.append(f"*{c['name']}* ({c['code']})\n  종가: ${c['close_price']:.2f} | 등락률: {c['change_rate']:.1f}% | 사유: {c['reason']}")

    msg = f"🔍 *{today} 미국 종가매매 후보 종목* (총 {len(candidates)}개)\n\n" + "\n\n".join(lines)
    send_tg(msg)
    print(f"미국 후보 {len(candidates)}개 전송 완료")

# ==================== 미국장 다음날 시초가 리포트 (오전 9:35 EST) ====================
def next_day_us_open_report():
    """
미국 동부 오전 9:35: 전일 미국 종가매매 후보 종목의 종가 vs 오늘 시초가 비교
    """
    now = datetime.now(pytz.timezone(US_TIMEZONE))
    today = now.strftime("%Y%m%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
    # 미국 휴일 대응 (월요일이면 금요일로)
    wday = now.weekday()
    if wday == 0:  # 월요일
        yesterday = (now - timedelta(days=3)).strftime("%Y%m%d")
    elif wday == 5:  # 토요일
        return
    elif wday == 6:  # 일요일
        return

    print(f"\n=== 미국 시초가 리포트 시작 {now.strftime('%Y-%m-%d %H:%M:%S')} EST ===")
    candidates = trade_db.get_candidates(yesterday, market="US")
    if not candidates:
        send_tg(f"📊 *{today} 미국 시초가 리포트*\n전일({yesterday}) 미국 후보 종목이 없습니다.")
        print("전일 미국 후보 없음")
        return

    lines = []
    win = 0
    loss = 0
    for c in candidates:
        code = c["code"]
        name = c["name"]
        close_price = c["close_price"]
        try:
            price = us_client.get_price(code)
            open_price = price.get("price", 0)  # 미국은 시가를 변수로 담기 어려워. 현재가로 대체하여 추정
            # Yahoo Finance로 시가 조회
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}?interval=1d&range=5d"
                r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                d = r.json()
                result = d.get("chart", {}).get("result", [[]])[0]
                timestamps = result.get("timestamp", [])
                opens = result.get("indicators", {}).get("quote", [{}])[0].get("open", [])
                closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
                for i, ts in enumerate(timestamps):
                    ts_date = datetime.fromtimestamp(ts).strftime("%Y%m%d")
                    if ts_date == today and opens[i] is not None:
                        open_price = opens[i]
                        break
            except Exception:
                pass

            if open_price and close_price:
                pl_pct = (open_price - close_price) / close_price * 100
                emoji = "🔺" if pl_pct >= 0 else "🔴"
                if pl_pct >= 0:
                    win += 1
                else:
                    loss += 1
                lines.append(f"{emoji} *{name}* ({code})\n  전일종가: ${close_price:.2f} → 오늘시가: ${open_price:.2f} | 수익률: {pl_pct:+.2f}%")
            else:
                lines.append(f"⚠ *{name}* ({code})\n  시초가 데이터 없음")
        except Exception as e:
            lines.append(f"⚠ *{name}* ({code})\n  조회 오류: {e}")
            continue

    summary = f"총 {len(candidates)}개 중 이김: {win} / 져밭: {loss}\n\n"
    msg = f"📊 *{today} 미국 시초가 리포트* (전일 {yesterday} 후보 기준)\n\n" + summary + "\n".join(lines)
    send_tg(msg)
    print(f"미국 리포트 전송 완료: 이김 {win}, 져밭 {loss}")

# ==================== 6월 2일 백테스트 (일회성) ====================
def backtest_june_2():
    """
6월 2일 종가 vs 다읍 거래일 시초가 백테스트
Yahoo Finance API를 통해 일별시세 조회. 6/3 휴장 시 6/4로 fallback.
    """
    names = {
        "005930": "삼성전자",
        "000660": "SK하이닉스",
        "035420": "NAVER",
        "001820": "우리은행",
        "001170": "SK네트워크스",
        "079550": "삼화콘덴서",
        "035720": "카카오",
        "051910": "LG화학",
        "028050": "삼원제약",
        "010140": "삼월전자",
    }
    target_codes = list(set(FALLBACK_WATCHLIST + ["001820", "001170", "079550", "005930", "000660", "035420", "035720", "051910", "028050", "010140"]))
    june2 = "2026.06.02"
    june3 = "2026.06.03"
    june4 = "2026.06.04"

    results = []
    for code in target_codes:
        try:
            daily = fetch_yahoo_daily(code, range_days=10)
            june2_data = daily.get(june2)
            june3_data = daily.get(june3)
            june4_data = daily.get(june4)

            if not june2_data:
                continue

            close_price = june2_data["close"]
            # 6/3 데이터가 있으면 6/3 시가, 없으면 6/4 시가로 fallback
            if june3_data:
                open_price = june3_data["open"]
                label = "6월 3일 시가"
            elif june4_data:
                open_price = june4_data["open"]
                label = "6월 4일 시가(6/3 휴장)"
            else:
                continue

            pl_pct = (open_price - close_price) / close_price * 100
            results.append({
                "code": code,
                "name": names.get(code, code),
                "close": int(close_price),
                "open": int(open_price),
                "pl_pct": pl_pct,
                "label": label,
            })
        except Exception as e:
            print(f"백테스트 오류 [{code}]: {e}")
            continue

    if not results:
        send_tg("🔍 6월 2일 백테스트: Yahoo Finance 데이터 없음")
        return

    lines = []
    win = sum(1 for r in results if r["pl_pct"] >= 0)
    loss = sum(1 for r in results if r["pl_pct"] < 0)
    avg_pl = sum(r["pl_pct"] for r in results) / len(results)

    for r in sorted(results, key=lambda x: x["pl_pct"], reverse=True):
        emoji = "🔺" if r["pl_pct"] >= 0 else "🔴"
        lines.append(f"{emoji} *{r['name']}*({r['code']}): 종가 {r['close']:,}원 → {r['label']} {r['open']:,}원 | {r['pl_pct']:+.2f}%")

    msg = (
        f"📊 *6월 2일 종가 → 다읍날 시초가 백테스트*\n\n"
        f"총 종목: {len(results)} | 이김: {win} | 져밭: {loss}\n"
        f"평균 수익률: {avg_pl:+.2f}%\n\n"
        + "\n".join(lines)
    )
    send_tg(msg)
    print(f"6월 2일 백테스트 완료: {len(results)}개 종목")

# ==================== 실행 루프 ====================
def load_state(market: str = "kr"):
    path = f"/tmp/trading_state_{market}.json"
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

def save_state(s, market: str = "kr"):
    path = f"/tmp/trading_state_{market}.json"
    with open(path, "w") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

def is_market_open_kr(now=None):
    if now is None:
        now = datetime.now(pytz.timezone(TIMEZONE))
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 900 <= hm <= 1530

def is_market_open_us(now=None):
    if now is None:
        now = datetime.now(pytz.timezone(US_TIMEZONE))
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 930 <= hm <= 1600

def cycle_kr():
    now = datetime.now(pytz.timezone(TIMEZONE))
    if not is_market_open_kr(now):
        print(f"[{now.strftime('%H:%M')}] 국내 시장 아님. 스킵.")
        return
    print(f"\n=== 국내 사이플 시작 {now.strftime('%Y-%m-%d %H:%M:%S')} ===")
    state = load_state("kr")
    today = now.strftime("%Y%m%d")
    if state.get("date") != today:
        state = {"date": today, "bought": {}, "hot_stocks": [], "scan_done": False}
    if not state.get("scan_done"):
        print("오늘의 핫 종목 스캔 중...")
        hot = fetch_hot_stocks_from_kis(limit=20, min_value=30_000_000_000)
        if not hot:
            print("한투 KIS 스캔 실패, 네이버 폴방 시도...")
            hot = [{"code": c, "name": c, "change_rate": 0, "trading_value": 0} for c in FALLBACK_WATCHLIST]
        if not hot:
            hot = [{"code": c, "name": c, "change_rate": 0, "trading_value": 0} for c in FALLBACK_WATCHLIST]
            print("모든 스캔 실패, 폴백 WATCHLIST 사용")
        state["hot_stocks"] = hot
        state["scan_done"] = True
        print(f"오늘 감시 종목: {len(hot)}개")
    watchlist = state["hot_stocks"]
    try:
        cash, holdings = client.get_balance()
        print(f"예수금: {cash:,} | 보유: {len(holdings)}개")
        held = {h["code"]: h for h in holdings}
        for item in watchlist:
            code = item["code"]
            vol_rate = item.get("vol_rate", 0.0)
            try:
                price, ma, da = analyze(code, is_us=False)
                name = price.get("name", item.get("name", code))
                print(f"  [{name}] {price['price']:,}원 | 양봉:{da.get('is_pos')} | 거래대:{da.get('value',0)//1_000_000_000:.0f}억")
                if code in held:
                    fl, q, r = eval_sell(held[code], price, ma)
                    if fl:
                        print(f"    -> 매도! {r}")
                        resp = client.sell(code, q)
                        print(f"    -> {resp}")
                        send_tg(f"*[매도]* {name}({code}) {q}주 @ {price['price']:,}원\n사유: {r}")
                        trade_db.log("KOREA", code, name, "SELL", q, price["price"], r, "종가베팅+이평선")
                        state["bought"].pop(code, None)
                    continue
                if not state["bought"].get(code):
                    fl, q, r, tech = eval_buy(price, ma, da, vol_rate=vol_rate)
                    if fl:
                        print(f"    -> 매수! {r} | {q}주")
                        resp = client.buy(code, q)
                        print(f"    -> {resp}")
                        send_tg(f"*[매수]* {name}({code}) {q}주 @ {price['price']:,}원\n사유: {r}")
                        trade_db.log("KOREA", code, name, "BUY", q, price["price"], r, tech)
                        state["bought"][code] = True
            except Exception as e:
                print(f"    -> 오류 [{code}]: {e}")
                send_tg(f"[오류] {code}: {e}")
        save_state(state, "kr")
        print("=== 국내 사이플 완료 ===\n")
    except Exception as e:
        print(f"주요 오류: {e}")
        send_tg(f"[최종오류] {e}")

def cycle_us():
    now = datetime.now(pytz.timezone(US_TIMEZONE))
    if not is_market_open_us(now):
        print(f"[{now.strftime('%H:%M')}] 미국 시장 아님. 스킵.")
        return
    print(f"\n=== 미국 사이플 시작 {now.strftime('%Y-%m-%d %H:%M:%S')} ===")
    state = load_state("us")
    today = now.strftime("%Y%m%d")
    if state.get("date") != today:
        state = {"date": today, "bought": {}, "hot_stocks": [], "scan_done": False}
    if not state.get("scan_done"):
        print("오늘의 미국 핫 종목 스캔 중...")
        hot = fetch_us_hot_stocks(limit=20)
        if not hot:
            hot = [{"code": c, "name": c, "change_rate": 0, "trading_value": 0} for c in US_FALLBACK_WATCHLIST]
            print("미국 스캔 실패, 폴백 WATCHLIST 사용")
        state["hot_stocks"] = hot
        state["scan_done"] = True
        print(f"오늘 미국 감시 종목: {len(hot)}개")
    watchlist = state["hot_stocks"]
    try:
        cash, holdings = us_client.get_balance()
        print(f"미국 예수금: {cash:.2f} USD | 보유: {len(holdings)}개")
        held = {h["code"]: h for h in holdings}
        for item in watchlist:
            code = item["code"]
            try:
                price, ma, da = analyze(code, is_us=True)
                name = price.get("name", item.get("name", code))
                print(f"  [{name}] ${price['price']:.2f} | 양봉:{da.get('is_pos')} | 거래대:{da.get('value',0):,.0f}")
                if code in held:
                    fl, q, r = eval_sell(held[code], price, ma, is_us=True)
                    if fl:
                        print(f"    -> 미국 매도! {r}")
                        resp = us_client.sell(code, q, market=price.get("market", "NAS"))
                        print(f"    -> {resp}")
                        send_tg(f"*[미국 매도]* {name}({code}) {q}주 @ ${price['price']:.2f}\n사유: {r}")
                        trade_db.log("US", code, name, "SELL", q, price["price"], r, "종가베팅+이평선")
                        state["bought"].pop(code, None)
                    continue
                if not state["bought"].get(code):
                    fl, q, r, tech = eval_buy(price, ma, da, is_us=True)
                    if fl:
                        print(f"    -> 미국 매수! {r} | {q}주")
                        resp = us_client.buy(code, q, market=price.get("market", "NAS"))
                        print(f"    -> {resp}")
                        send_tg(f"*[미국 매수]* {name}({code}) {q}주 @ ${price['price']:.2f}\n사유: {r}")
                        trade_db.log("US", code, name, "BUY", q, price["price"], r, tech)
                        state["bought"][code] = True
            except Exception as e:
                print(f"    -> 미국 오류 [{code}]: {e}")
                send_tg(f"[미국 오류] {code}: {e}")
        save_state(state, "us")
        print("=== 미국 사이플 완료 ===\n")
    except Exception as e:
        print(f"미국 주요 오류: {e}")
        send_tg(f"[미국 최종오류] {e}")

# ==================== FastAPI + 스케줄러 ====================
app = FastAPI()
scheduler = BackgroundScheduler(timezone=pytz.timezone(TIMEZONE))

# 국내: 월~금 9:00~15:30 사이 5분 매다
scheduler.add_job(cycle_kr, CronTrigger(minute="*/5", hour="9-15", day_of_week="mon-fri", timezone=TIMEZONE), id="kr_cyc", replace_existing=True)
# 미국: 미 동부 시간 09:30~16:00 (한국 시간으로 변환해야 하지만 pytz timezone 사용)
# 미국 동부 시간은 한국 격격일 수 있어서 timezone=US_TIMEZONE로 설정
scheduler.add_job(cycle_us, CronTrigger(minute="*/5", hour="9-15", day_of_week="mon-fri", timezone=US_TIMEZONE), id="us_cyc", replace_existing=True)
# 주간 보고: 금요일 18:00 KST
scheduler.add_job(generate_weekly_report, CronTrigger(hour="18", minute="0", day_of_week="fri", timezone=TIMEZONE), id="weekly_rpt", replace_existing=True)
# 종가매매 후보 스캔: 월~금 15:00 KST (장마감 30분 전)
scheduler.add_job(scan_close_candidates, CronTrigger(hour="15", minute="0", day_of_week="mon-fri", timezone=TIMEZONE), id="close_scan", replace_existing=True)
# 다음날 시초가 리포트: 월~금 09:05 KST
scheduler.add_job(next_day_open_report, CronTrigger(hour="9", minute="5", day_of_week="mon-fri", timezone=TIMEZONE), id="open_rpt", replace_existing=True)
# 미국 종가매매 후보 스캔: 월~금 15:30 EST (미국 장마감 30분 전)
scheduler.add_job(scan_us_close_candidates, CronTrigger(hour="15", minute="30", day_of_week="mon-fri", timezone=US_TIMEZONE), id="us_close_scan", replace_existing=True)
# 미국 다음날 시초가 리포트: 월~금 09:35 EST
scheduler.add_job(next_day_us_open_report, CronTrigger(hour="9", minute="35", day_of_week="mon-fri", timezone=US_TIMEZONE), id="us_open_rpt", replace_existing=True)

scheduler.start()

@app.get("/")
def root():
    return {"status": "bot running (KR+US auto mode)", "time": datetime.now(pytz.timezone(TIMEZONE)).isoformat()}

@app.get("/health")
def health():
    kr = is_market_open_kr()
    us = is_market_open_us()
    return {"ok": True, "kr_market": kr, "us_market": us}

@app.get("/today")
def today_stocks():
    kr_state = load_state("kr")
    us_state = load_state("us")
    return {"kr": kr_state.get("hot_stocks", []), "us": us_state.get("hot_stocks", [])}

@app.get("/trades")
def list_trades():
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                market TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                action TEXT NOT NULL,
                qty INTEGER NOT NULL,
                price REAL NOT NULL,
                reason TEXT,
                technique TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 50")
        rows = cur.fetchall()
        if cur.description:
            cols = [d[0] for d in cur.description]
            return {"trades": [dict(zip(cols, r)) for r in rows]}
        return {"trades": []}
    except Exception as e:
        return {"error": str(e)}
    finally:
        if 'conn' in locals():
            conn.close()

@app.get("/candidates")
def list_candidates(date: str = None, market: str = "KR"):
    if not date:
        if market == "US":
            date = datetime.now(pytz.timezone(US_TIMEZONE)).strftime("%Y%m%d")
        else:
            date = datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y%m%d")
    rows = trade_db.get_candidates(date, market=market)
    return {"date": date, "market": market, "candidates": rows}

@app.post("/report")
def trigger_report():
    generate_weekly_report()
    return {"ok": True}

@app.post("/backtest-june2")
def trigger_backtest_june2():
    backtest_june_2()
    return {"ok": True}

@app.post("/us/scan-close")
def trigger_us_close_scan():
    scan_us_close_candidates()
    return {"ok": True}

@app.post("/us/open-report")
def trigger_us_open_report():
    next_day_us_open_report()
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
