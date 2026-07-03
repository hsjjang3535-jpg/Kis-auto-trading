# Railway Variables 체크리스트 (Kis-auto-trading)

> **보안:** API 키·토큰·계좌번호는 Git에 저장하지 마세요.  
> Railway Dashboard → Variables → **Raw Editor**에서만 관리하고,  
> 복사본은 **비밀번호 관리자**(1Password, Bitwarden 등)에 보관하세요.

**서비스:** `Kis-auto-trading` (production)  
**백업 태그:** `v3.0-stable`  
**복구 가이드:** [RESTORE.md](./RESTORE.md)

---

## 🔴 필수 (Secret — 절대 공유·커밋 금지)

| Variable | 설명 | 예시 형식 |
|----------|------|-----------|
| `KIS_APP_KEY` | 한국투자증권 앱키 (**모의투자용**) | `(발급받은 키)` |
| `KIS_APP_SECRET` | 한국투자증권 앱시크릿 | `(발급받은 시크릿)` |
| `KIS_ACCOUNT_NO` | 계좌번호 (하이픈 없이) | `5012345678` |
| `GROQ_API_KEY` | Groq AI (스크리닝 분석) | `gsk_...` |
| `TELEGRAM_BOT_TOKEN` | 텔레그램 봇 토큰 | `(BotFather 발급)` |
| `TELEGRAM_CHAT_ID` | 알림 받을 chat id | `(숫자)` |
| `API_SECRET` | 텔레그램봇 ↔ 매매봇 연동 비밀값 | `(임의 긴 문자열)` |

> `API_SECRET`은 **telegram-gemini-bot (trading)** 의 `API_SECRET`과 **동일**해야 합니다.

---

## 🟡 권장 (공개 가능한 설정값)

| Variable | 현재 권장값 | 설명 |
|----------|-------------|------|
| `KIS_MODE` | `모의` | `모의` / `실전` |
| `TZ` | `Asia/Seoul` | KST 스케줄 (09:05 스크리닝 등) |
| `PORT` | `8080` | HTTP API (Railway 자동 설정 시 생략 가능) |
| `ENABLE_CRASH_BOUNCE` | `true` | 낙폭반등 ON |
| `DYNAMIC_CAPITAL` | `true` | 예수금 기준 자동 한도 조절 |
| `BUY_RATIO` | `0.5` | 1회 매수 = 예수금 50% |

---

## 🟢 자금·매매 규칙 (선택, 기본값 있음)

| Variable | 기본값 | 설명 |
|----------|--------|------|
| `MAX_TOTAL_AMOUNT` | `1000000` | 장중 총 투자 한도 |
| `MAX_BUY_AMOUNT` | `500000` | 장중 1회 매수 한도 |
| `MAX_CLOSING_AMOUNT` | `500000` | 종가베팅 총 한도 |
| `MAX_CLOSING_BUY` | `250000` | 종가베팅 1회 매수 |
| `STOP_LOSS_PCT` | `2.0` | 손절 % |
| `TAKE_PROFIT_PCT` | `3.0` | 트레일링 익절 시작 % |
| `TRAILING_STOP_PCT` | `1.0` | 고점 대비 하락 % |
| `SELL_BLACKLIST` | `한미사이언스` | 매도 금지 종목 (쉼표 구분) |
| `MAX_WATCHLIST` | `15` | 워치리스트 최대 종목 수 |

---

## 🟢 스크리너 완화 1·2순위 (미설정 시 코드 기본값 적용)

| Variable | 기본값 | 설명 |
|----------|--------|------|
| `MIN_CHANGE_RATE` | `-2.0` | -2%까지 허용 (그 아래 제외) |
| `VOL_RATIO_MIN` | `1.5` | 거래량 1.5배 |
| `W52_GAP_UPPER_MAX` | `7.0` | 상단매매 52주 신고가 % |
| `CLOSING_BET_MIN_RATE` | `1.5` | 종가베팅 당일 +1.5%↑ |
| `LOWER_RSI_MAX` | `45` | 하단매매 RSI 상한 |

> Variables에 **넣지 않아도** 위 기본값이 적용됩니다.  
> 예전 값(`CRASH_BOUNCE_MIN_DROP=5.0` 등)을 넣어 두었다면 **삭제하거나** 아래와 맞춰 주세요.

---

## 🟢 낙폭반등 (ENABLE_CRASH_BOUNCE=true 일 때)

| Variable | 기본값 | 설명 |
|----------|--------|------|
| `CRASH_BOUNCE_ENTRY_START` | `09:10` | 진입 시작 |
| `CRASH_BOUNCE_ENTRY_END` | `10:30` | 진입 종료 |
| `CRASH_BOUNCE_TIME_EXIT` | `11:00` | 시간 청산 |
| `CRASH_BOUNCE_MIN_DROP` | `3.5` | 시가 대비 -3.5%↑ |
| `MAX_CRASH_BOUNCE_AMOUNT` | `500000` | 총 한도 |
| `MAX_CRASH_BOUNCE_BUY` | `500000` | 1회 매수 |
| `CRASH_BOUNCE_MAX_POSITIONS` | `1` | 동시 보유 |
| `CRASH_BOUNCE_TAKE_PROFIT` | `3.0` | 익절 +3% |
| `CRASH_BOUNCE_STOP_LOSS` | `2.0` | 손절 -2% |
| `CRASH_BOUNCE_MAX_API_CALLS` | `20` | 스캔당 API 상한 |

---

## 🔵 선택 (없어도 동작)

| Variable | 설명 |
|----------|------|
| `NAVER_CLIENT_ID` | AI 뉴스 검색 (없으면 "뉴스 없음") |
| `NAVER_CLIENT_SECRET` | 위와 쌍 |

---

## ✅ 설정 확인 체크리스트

복구·재배포 후 아래를 확인하세요.

- [ ] `KIS_APP_KEY` / `SECRET` = **모의투자** 포털에서 발급한 키
- [ ] `KIS_MODE` = `모의`
- [ ] `TZ` = `Asia/Seoul`
- [ ] `ENABLE_CRASH_BOUNCE` = `true`
- [ ] `API_SECRET` = 텔레그램 trading 서비스와 **동일**
- [ ] `TELEGRAM_CHAT_ID` = 본인 ID
- [ ] 예전 `CRASH_BOUNCE_*` / `MIN_DROP=5.0` 등 **구버전 값 없음**
- [ ] Deployments = **Active**, `/health` → `{"ok":true}`
- [ ] Telegram 시작 메시지에 `🔶 낙폭반등: 09:10~10:30 / 한도 500,000원` 표시

---

## 💾 Variables 백업 방법 (수동)

1. Railway → `Kis-auto-trading` → **Variables**
2. **Raw Editor** → 전체 복사
3. 비밀번호 관리자에 `kis-auto-trading-railway-vars-YYYY-MM-DD` 로 저장
4. **Git·채팅·스크린샷에 붙여넣지 마세요**

복구 시: Raw Editor에 붙여넣기 → **Redeploy**
