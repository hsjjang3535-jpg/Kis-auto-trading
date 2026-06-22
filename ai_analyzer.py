import os
import requests
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))


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
당신은 한국 주식 단기매매 전문가입니다. 종가베팅(장 마감 직전 매수, 다음날 시초가 매도) 관점에서 아래 종목을 분석해주세요.

종목명: {stock_name} ({code})
당일 등락률: {change_rate:+.2f}%
관련 뉴스:
{news}

다음 기준으로 평가하세요:
1. 명확한 상승 재료(뉴스/이슈)가 있는가?
2. 오늘 강한 상승 모멘텀이 있는가?
3. 내일 시초가 갭 상승 가능성이 있는가?

반드시 아래 JSON 형식으로만 답하세요:
{{"buy": true/false, "strength": "강/중/약/없음", "reason": "한 줄 요약"}}
"""

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        text = response.choices[0].message.content.strip()
        import json, re
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"[AI 분석 오류] {e}")

    return {"buy": False, "strength": "없음", "reason": "분석 실패"}
