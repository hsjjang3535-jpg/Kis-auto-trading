import json
import os
import re
import requests
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "gemma2-9b-it",
]

# 완화 모드: buy=false여도 strength가 이 중 하나면 워치리스트 통과
_APPROVE_STRENGTHS = ("강", "중", "약")


def fetch_news(stock_name: str, display: int = 5) -> str:
    """네이버 금융 뉴스 검색"""
    try:
        url = "https://openapi.naver.com/v1/search/news.json"
        headers = {
            "X-Naver-Client-Id": os.getenv("NAVER_CLIENT_ID", ""),
            "X-Naver-Client-Secret": os.getenv("NAVER_CLIENT_SECRET", ""),
        }
        params = {"query": stock_name, "display": display, "sort": "date"}
        res = requests.get(url, headers=headers, params=params, timeout=5)
        if res.status_code == 200:
            items = res.json().get("items", [])
            return "\n".join(
                f"- {item['title'].replace('<b>', '').replace('</b>', '')}: {item['description'][:100]}"
                for item in items
            )
    except Exception:
        pass
    return "뉴스 없음"


def is_approved(result: dict) -> bool:
    """AI 분석 결과가 워치리스트 통과인지 (완화 기준)"""
    if result.get("reason") == "분석 실패":
        return False
    if result.get("buy") is True:
        return True
    return result.get("strength") in _APPROVE_STRENGTHS


def is_closing_approved(result: dict, friday_weekend: bool = False) -> bool:
    """종가베팅 워치리스트 통과 여부 (금요일은 주말 호재 기준 강화)"""
    if result.get("reason") == "분석 실패":
        return False
    if friday_weekend:
        return result.get("buy") is True and result.get("strength") in ("강", "중")
    return is_approved(result)


def _format_technical_context(ctx: dict) -> str:
    lines = []
    if ctx.get("strategy"):
        lines.append(f"- 전략(이미 통과): {ctx['strategy']}")
    if ctx.get("change_rate") is not None:
        lines.append(f"- 당일 등락률: {ctx['change_rate']:+.2f}%")
    if ctx.get("rsi") is not None:
        lines.append(f"- RSI: {ctx['rsi']:.0f}")
    if ctx.get("vol_ratio") is not None:
        lines.append(f"- 거래량 배수: {ctx['vol_ratio']:.1f}x")
    if ctx.get("current") is not None and ctx.get("ma5") is not None:
        lines.append(f"- 현재가/MA5: {ctx['current']:.0f} / {ctx['ma5']:.0f}")
    if ctx.get("w52_gap") is not None:
        lines.append(f"- 52주 신고가 대비: {ctx['w52_gap']:.1f}%")
    return "\n".join(lines) if lines else "- (차트 지표 없음)"


def _call_groq(prompt: str) -> dict | None:
    for model in _MODELS:
        try:
            response = groq_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            text = response.choices[0].message.content.strip()
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                return json.loads(match.group())
            break
        except Exception as e:
            err_str = str(e).lower()
            if "503" in err_str or "overcapacity" in err_str or "overloaded" in err_str:
                print(f"[AI] {model} 과부하, 다음 모델 시도...")
                continue
            print(f"[AI 분석 오류] {e}")
            break
    return None


def analyze(
    stock_name: str,
    code: str,
    change_rate: float,
    *,
    strategy: str = "",
    rsi: float | None = None,
    vol_ratio: float | None = None,
    current: float | None = None,
    ma5: float | None = None,
    w52_gap: float | None = None,
) -> dict:
    """장중매매 AI 분석 (완화 기준)"""
    news = fetch_news(stock_name)
    tech = _format_technical_context({
        "strategy": strategy,
        "change_rate": change_rate,
        "rsi": rsi,
        "vol_ratio": vol_ratio,
        "current": current,
        "ma5": ma5,
        "w52_gap": w52_gap,
    })

    prompt = f"""
당신은 한국 주식 단기매매 전문가입니다. 장중매매(당일 매수 후 당일 익절/손절) 관점에서 아래 종목을 분석해주세요.

종목명: {stock_name} ({code})
기술적 스크리닝 결과 (이미 통과한 종목):
{tech}
관련 뉴스:
{news}

[완화 기준 - 중요]
- 이 종목은 종산 기법 기술조건(RSI, 거래량, 신고가/이평선 등)을 이미 통과했습니다.
- 뉴스 재료가 없거나 약해도, 차트 모멘텀(등락률·거래량)이 살아있으면 buy: true, strength: "중" 이상으로 판단하세요.
- buy: false는 아래 경우에만 사용하세요:
  1) 뉴스에 확실한 악재(유상증자, 조사, 실적 쇼크, 상폐 우려 등)
  2) RSI 75 이상 등 단기 과열 + 되돌림 위험이 명확할 때
  3) 뉴스·차트 모두 매수 근거가 전혀 없을 때

strength 기준: 강=뉴스 재료 있음, 중=재료 약하지만 모멘텀 양호, 약=재료 없으나 기술 통과, 없음=매수 부적합

반드시 아래 JSON 형식으로만 답하세요:
{{"buy": true/false, "strength": "강/중/약/없음", "reason": "한 줄 요약"}}
"""

    result = _call_groq(prompt)
    if result:
        return result
    return {"buy": False, "strength": "없음", "reason": "분석 실패"}


def analyze_closing_bet(
    stock_name: str,
    code: str,
    change_rate: float,
    *,
    rsi: float | None = None,
    vol_ratio: float | None = None,
    current: float | None = None,
    ma5: float | None = None,
    friday_weekend: bool = False,
) -> dict:
    """종가베팅 AI 분석 - 오버나이트 보유 적합성 (완화 기준)

    friday_weekend=True(금요일): 뉴스 기반 주말~월요일 호재 지속 가능성 중심 분석.
    """
    news_count = 8 if friday_weekend else 5
    news = fetch_news(stock_name, display=news_count)
    tech = _format_technical_context({
        "strategy": "종가베팅",
        "change_rate": change_rate,
        "rsi": rsi,
        "vol_ratio": vol_ratio,
        "current": current,
        "ma5": ma5,
    })

    if friday_weekend:
        prompt = f"""
당신은 한국 주식 단기매매 전문가입니다. **금요일 종가베팅** 관점에서 아래 종목을 분석해주세요.
보유 기간: 금요일 장 마감 매수 → **주말 2일 + 월요일 시초가** 매도 (총 3일 오버나이트).

종목명: {stock_name} ({code})
기술적 스크리닝 결과 (이미 통과한 종목):
{tech}
관련 뉴스 (최근 {news_count}건):
{news}

[금요일 전용 기준 - 중요]
- 반드시 **뉴스·공시·테마 호재**를 검토하세요. 주말 동안 이어질 수 있는 재료가 핵심입니다.
- buy: true는 아래 중 하나 이상일 때만:
  1) 뉴스에 계약·수주·실적·정책·테마 등 **주말~월요일까지 이어질 호재**가 있음
  2) 당일 강한 수급 + 뉴스가 악재가 아니며, 월요일 갭업 기대가 합리적임
- buy: false는 아래 경우:
  1) 뉴스 없이 차트만 오른 종목 (주말 변동성·갭 하락 위험)
  2) 금요일 차익 실현·단기 급등 후 피로 신호 (RSI 72↑ 등)
  3) 주말 악재 우려 (조사, 유상증자, 실적 쇼크, 대주주 매도 등)
  4) 호재가 이미 소멸·반영 완료된 뉴스

strength 기준:
- 강=뉴스 호재가 주말·월요일까지 이어질 가능성 높음
- 중=호재는 약하지만 수급·테마 지속 기대
- 약=뉴스 근거 부족 (금요일에는 워치리스트 통과 비권장)
- 없음=오버나이트 부적합

reason에는 **어떤 뉴스/호재**를 근거로 판단했는지 한 줄에 포함하세요.

반드시 아래 JSON 형식으로만 답하세요:
{{"buy": true/false, "strength": "강/중/약/없음", "reason": "한 줄 요약"}}
"""
    else:
        prompt = f"""
당신은 한국 주식 단기매매 전문가입니다. 종가베팅(장 마감 직전 매수, 다음날 시초가 매도) 관점에서 아래 종목을 분석해주세요.

종목명: {stock_name} ({code})
기술적 스크리닝 결과 (이미 통과한 종목):
{tech}
관련 뉴스:
{news}

[완화 기준 - 중요]
- 이 종목은 종가베팅 기술조건(당일 +1.5%↑, MA5 위, 거래량 1.5배, RSI 40~75)을 이미 통과했습니다.
- 뉴스 재료가 없어도 당일 수급·모멘텀이 좋으면 buy: true, strength: "중" 이상으로 판단하세요.
- buy: false는 아래 경우에만 사용하세요:
  1) 확실한 악재·공매도 이슈·실적 쇼크
  2) RSI 72 이상 + 단기 급등 후 되돌림 가능성이 매우 높을 때
  3) 내일 갭 하락이 거의 확실한 상황

strength 기준: 강=내일까지 이어질 재료 있음, 중=재료 약하지만 수급 양호, 약=재료 없으나 기술 통과, 없음=오버나이트 부적합

반드시 아래 JSON 형식으로만 답하세요:
{{"buy": true/false, "strength": "강/중/약/없음", "reason": "한 줄 요약"}}
"""

    result = _call_groq(prompt)
    if result:
        return result
    return {"buy": False, "strength": "없음", "reason": "분석 실패"}
