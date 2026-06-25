import os
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


def fetch_news(stock_name: str) -> str:
    """네이버 금융 뉴스 검색"""
    try:
        url = "https://openapi.naver.com/v1/search/news.json"
        headers = {
            "X-Naver-Client-Id": os.getenv("NAVER_CLIENT_ID", ""),
            "X-Naver-Client-Secret": os.getenv("NAVER_CLIENT_SECRET", ""),
        }
        params = {"query": stock_name, "display": 5, "sort": "date"}
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


def analyze(stock_name: str, code: str, change_rate: float) -> dict:
    """
    AI가 종목의 매수 적합성을 판단:
    - 뉴스 재료 강도 (강/중/약/없음)
    - 종가베팅 적합 여부 (True/False)
    - 판단 근거 요약
    """
    news = fetch_news(stock_name)

    prompt = f"""
당신은 한국 주식 단기매매 전문가입니다. 장중매매(당일 매수 후 당일 익절/손절) 관점에서 아래 종목을 분석해주세요.

종목명: {stock_name} ({code})
당일 등락률: {change_rate:+.2f}%
관련 뉴스:
{news}

다음 기준으로 평가하세요:
1. 명확한 상승 재료(뉴스/이슈)가 있어 오늘 추가 상승 가능성이 있는가?
2. 오늘 장중 모멘텀이 살아있는가? (거래량, 뉴스 등 기준)
3. 단기 3% 이상 추가 상승 여력이 있는가?

반드시 아래 JSON 형식으로만 답하세요:
{{"buy": true/false, "strength": "강/중/약/없음", "reason": "한 줄 요약"}}
"""

    import json, re
    for model in _MODELS:
        try:
            response = groq_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            text = response.choices[0].message.content.strip()
            match = re.search(r'\{.*\}', text, re.DOTALL)
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

    return {"buy": False, "strength": "없음", "reason": "분석 실패"}


def analyze_closing_bet(stock_name: str, code: str, change_rate: float) -> dict:
    """종가베팅 AI 분석 - 오버나이트 보유 적합성 판단"""
    news = fetch_news(stock_name)

    prompt = f"""
당신은 한국 주식 단기매매 전문가입니다. 종가베팅(장 마감 직전 매수, 다음날 시초가 매도) 관점에서 아래 종목을 분석해주세요.

종목명: {stock_name} ({code})
당일 등락률: {change_rate:+.2f}%
관련 뉴스:
{news}

다음 기준으로 평가하세요:
1. 명확한 상승 재료(뉴스/이슈)가 있어 오버나이트 보유 시 내일도 추가 상승 가능한가?
2. 리스크(악재, 단기 급등 후 되돌림 가능성)는 없는가?
3. 내일 시초가 갭상승 가능성이 있는가?

반드시 아래 JSON 형식으로만 답하세요:
{{"buy": true/false, "strength": "강/중/약/없음", "reason": "한 줄 요약"}}
"""

    import json, re
    for model in _MODELS:
        try:
            response = groq_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            text = response.choices[0].message.content.strip()
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                return json.loads(match.group())
            break
        except Exception as e:
            err_str = str(e).lower()
            if "503" in err_str or "overcapacity" in err_str or "overloaded" in err_str:
                print(f"[AI] {model} 과부하, 다음 모델 시도...")
                continue
            print(f"[AI 종가베팅 분석 오류] {e}")
            break

    return {"buy": False, "strength": "없음", "reason": "분석 실패"}
