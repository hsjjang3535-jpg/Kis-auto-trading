import json
import re
from typing import Any

from jinja2 import Template

from config import GROQ_API_KEY, GROQ_MODEL, OPENAI_API_KEY, OPENAI_MODEL
from content.templates import POST_TEMPLATE


class ContentGenerator:
    def __init__(self):
        self._provider = "openai" if OPENAI_API_KEY else "groq"

    def _chat(self, system: str, user: str) -> str:
        if OPENAI_API_KEY:
            from openai import OpenAI

            client = OpenAI(api_key=OPENAI_API_KEY)
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.7,
            )
            return response.choices[0].message.content or ""

        if GROQ_API_KEY:
            from groq import Groq

            client = Groq(api_key=GROQ_API_KEY)
            response = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.7,
            )
            return response.choices[0].message.content or ""

        raise ValueError("OPENAI_API_KEY 또는 GROQ_API_KEY가 필요합니다.")

    def _parse_json(self, text: str) -> dict[str, Any]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)
        return json.loads(cleaned)

    def generate_post(self, product: dict[str, Any]) -> dict[str, Any]:
        system = (
            "당신은 한국어 SEO 블로그 작성자입니다. "
            "과장 없이 정보성 리뷰를 작성하고, JSON만 출력하세요."
        )
        user = f"""
상품명: {product.get('product_name')}
가격: {product.get('product_price')}원
키워드: {product.get('keyword')}
로켓배송: {product.get('is_rocket')}
무료배송: {product.get('is_free_shipping')}

아래 JSON 스키마로만 응답:
{{
  "title": "60자 이내 제목",
  "intro": "2~3문장 도입부",
  "body_html": "<p>...</p> 형태 본문 (800~1200자, h3 소제목 2~3개 포함)",
  "pros": ["장점1", "장점2", "장점3"],
  "cons": ["단점1", "단점2"],
  "faq": [["질문1", "답1"], ["질문2", "답2"]]
}}
"""
        raw = self._chat(system, user)
        parsed = self._parse_json(raw)

        html = Template(POST_TEMPLATE).render(
            title=parsed["title"],
            intro=parsed["intro"],
            product_name=product.get("product_name"),
            product_price=f"{product.get('product_price', 0):,}",
            product_image=product.get("product_image"),
            affiliate_url=product.get("affiliate_url"),
            is_rocket=product.get("is_rocket"),
            is_free_shipping=product.get("is_free_shipping"),
            body_html=parsed["body_html"],
            pros=parsed.get("pros", []),
            cons=parsed.get("cons", []),
            faq=parsed.get("faq", []),
        )

        return {
            "title": parsed["title"],
            "content_html": html,
            "meta_json": parsed,
            "provider": self._provider,
        }
