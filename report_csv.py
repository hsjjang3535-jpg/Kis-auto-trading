"""
CSV 보고 및 GitHub 업로드
- 매일 스캔 결과와 거래 내역을 CSV로 저장
- GitHub API로 직접 업로드 (Railway ephemeral 환경 대응)
"""
import csv
import os
import json
import base64
import requests
from datetime import datetime
from io import StringIO

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "hsjjang3535-jpg/Kis-auto-trading")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

REPORTS_DIR = "reports"

def _github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def _get_file_sha(path: str) -> str | None:
    """GitHub에서 파일 SHA 조회 (존재하지 않으면 None)"""
    if not GITHUB_TOKEN:
        return None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}?ref={GITHUB_BRANCH}"
    r = requests.get(url, headers=_github_headers(), timeout=15)
    if r.status_code == 200:
        return r.json().get("sha")
    return None

def _upload_to_github(path: str, content: str, message: str):
    """GitHub Contents API로 파일 업로드/갱신"""
    if not GITHUB_TOKEN:
        print(f"[GitHub] TOKEN 없음, {path} 업로드 스킵")
        return False
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    sha = _get_file_sha(path)
    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        body["sha"] = sha
    r = requests.put(url, headers=_github_headers(), json=body, timeout=30)
    if r.status_code in (200, 201):
        print(f"[GitHub] {path} 업로드 완료")
        return True
    print(f"[GitHub] {path} 업로드 실패: {r.status_code} {r.text[:200]}")
    return False

def _read_from_github(path: str) -> str | None:
    """GitHub에서 파일 내용 읽기"""
    if not GITHUB_TOKEN:
        return None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}?ref={GITHUB_BRANCH}"
    r = requests.get(url, headers=_github_headers(), timeout=15)
    if r.status_code == 200:
        data = r.json()
        return base64.b64decode(data["content"]).decode("utf-8")
    return None

def save_scan_report(date_str: str, stocks: list, market: str = "KR"):
    """
    종목 스캔 결과 저장
    CSV 컬럼: date,market,code,name,trading_value,scan_time
    """
    filename = f"{REPORTS_DIR}/scan_{market}_{date_str}.csv"
    # 기존 내용 읽기
    existing = _read_from_github(filename)
    rows = []
    if existing:
        reader = csv.DictReader(StringIO(existing))
        rows = list(reader)
    # 새로운 스캔 결과 추가
    scan_time = datetime.now().strftime("%H:%M:%S")
    for s in stocks:
        rows.append({
            "date": date_str,
            "market": market,
            "code": s.get("code", ""),
            "name": s.get("name", ""),
            "trading_value": s.get("trading_value", ""),
            "scan_time": scan_time,
        })
    # CSV 작성
    out = StringIO()
    writer = csv.DictWriter(out, fieldnames=["date","market","code","name","trading_value","scan_time"])
    writer.writeheader()
    writer.writerows(rows)
    content = out.getvalue()
    _upload_to_github(
        filename, content,
        f"[{market}] {date_str} 종목 스캔 결과 ({len(stocks)}개)"
    )

def save_trade_report(date_str: str, trade_type: str, stock_code: str, stock_name: str,
                      price: float, qty: int, reason: str, profit_pct: float | None = None,
                      market: str = "KR"):
    """
    거래 내역 저장
    CSV 컬럼: date,market,trade_type,code,name,price,qty,reason,profit_pct,trade_time
    """
    filename = f"{REPORTS_DIR}/trade_{market}_{date_str}.csv"
    existing = _read_from_github(filename)
    rows = []
    if existing:
        reader = csv.DictReader(StringIO(existing))
        rows = list(reader)
    trade_time = datetime.now().strftime("%H:%M:%S")
    row = {
        "date": date_str,
        "market": market,
        "trade_type": trade_type,
        "code": stock_code,
        "name": stock_name,
        "price": f"{price:.2f}" if market == "US" else str(int(price)),
        "qty": str(qty),
        "reason": reason,
        "profit_pct": f"{profit_pct:.2f}" if profit_pct is not None else "",
        "trade_time": trade_time,
    }
    rows.append(row)
    out = StringIO()
    writer = csv.DictWriter(out, fieldnames=["date","market","trade_type","code","name","price","qty","reason","profit_pct","trade_time"])
    writer.writeheader()
    writer.writerows(rows)
    content = out.getvalue()
    _upload_to_github(
        filename, content,
        f"[{market}] {trade_type} {stock_name} ({stock_code}) {qty}주"
    )

def save_balance_report(date_str: str, cash: float, holdings_count: int, market: str = "KR"):
    """
    잔고 요약 저장
    CSV 컬럼: date,market,cash,holdings_count,report_time
    """
    filename = f"{REPORTS_DIR}/balance_{market}_{date_str}.csv"
    existing = _read_from_github(filename)
    rows = []
    if existing:
        reader = csv.DictReader(StringIO(existing))
        rows = list(reader)
    report_time = datetime.now().strftime("%H:%M:%S")
    rows.append({
        "date": date_str,
        "market": market,
        "cash": f"{cash:.2f}" if market == "US" else str(int(cash)),
        "holdings_count": str(holdings_count),
        "report_time": report_time,
    })
    out = StringIO()
    writer = csv.DictWriter(out, fieldnames=["date","market","cash","holdings_count","report_time"])
    writer.writeheader()
    writer.writerows(rows)
    content = out.getvalue()
    _upload_to_github(
        filename, content,
        f"[{market}] {date_str} 잔고 보고 (예수금: {cash})"
    )
