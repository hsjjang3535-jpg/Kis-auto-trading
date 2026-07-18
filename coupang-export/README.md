# Coupang Partners Automation

쿠팡파트너스 올인원 자동화 도구입니다.

Repository: https://github.com/hsjjang3535-jpg/coupang-partnes-automation

- 상품 검색 및 파트너스 링크 수집
- AI 블로그 글 생성
- 워드프레스 자동 포스팅
- 클릭/주문/수익 리포트 엑셀 export

## 빠른 시작

```bash
git clone https://github.com/hsjjang3535-jpg/coupang-partnes-automation.git
cd coupang-partnes-automation
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env 에 API 키 입력
```

## 사용법

```bash
python main.py search
python main.py generate --limit 5
python main.py post --limit 5
python main.py report --days 7
python main.py run --skip-publish
python main.py status
```

## API 키 발급

1. [쿠팡파트너스](https://partners.coupang.com) → 도구 → Open API
2. ACCESS_KEY / SECRET_KEY를 `.env`에 설정

## 주의

- API Rate Limit: 1시간 30회 이상 호출 시 24시간 정지
- AI 100% 자동 글은 SEO 품질 이슈 가능 — 발행 전 검수 권장
