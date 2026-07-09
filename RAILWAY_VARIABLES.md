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
| `KIS_ACCOUNT_NO` | 계좌번호 (하이픈 없이) | 모의: `5012345601` (50으로 시작) |
| `KIS_CANO` | (선택) 계좌 앞 8자리 | `50123456` |
| `KIS_ACNT_PRDT_CD` | (선택) 상품코드 2자리 | `01` |
| `GROQ_API_KEY` | Groq AI (스크리닝 분석) | `gsk_...` |
| `TELEGRAM_BOT_TOKEN` | 텔레그램 봇 토큰 | `(BotFather 발급)` |
| `TELEGRAM_CHAT_ID` | 알림 받을 chat id | `(숫자)` |
| `API_SECRET` | 텔레그램봇 ↔ 매매봇 연동 비밀값 | `(임의 긴 문자열)` |

> **모의투자:** `KIS_APP_KEY`는 **모의투자용** 앱키, `KIS_ACCOUNT_NO`는 **모의 계좌**(보통 `50xxxxxxxx`)여야 합니다.  
> 실계좌번호를 넣으면 `인증 시점의 계좌번호와 요청 계좌번호가 일치하지 않습니다` 오류가 납니다.

> **실전투자:** `KIS_APP_KEY`는 **실전투자용** 앱키, `KIS_ACCOUNT_NO`는 **실계좌**(50으로 시작하지 않음)여야 합니다.  
> 모의 앱키·모의 계좌를 그대로 두고 `KIS_MODE=실전`만 바꾸면 **주문이 되지 않습니다.**

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
| `CLOSING_BET_MAX_PER_SLOT` | `2` | 5분 슬롯당 최대 매수 종목 |
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
| `VOL_RATIO_MIN` | `1.2` | 거래량 1.2배 |
| `W52_GAP_UPPER_MAX` | `10.0` | 상단매매 52주 신고가 % |
| `W52_GAP_LOWER_MAX` | `25.0` | 하단매매 52주 고가 % |
| `UPPER_TAIL_MAX` | `0.35` | 윗꼬리 비율 상한 |
| `CLOSING_BET_MIN_RATE` | `1.5` | 종가베팅 당일 +1.5%↑ |
| `LOWER_RSI_MAX` | `50` | 하단매매 RSI 상한 |
| `ENABLE_INTRADAY_AI` | `false` | 장중 AI (`true`=Groq 2차 필터, `false`=기술조건만) |

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

## 🚀 모의 → 실전 전환 (설정 그대로 유지)

매매 규칙·스크리너·한도 Variables는 **그대로 두고**, 아래 **4개만** 바꿉니다.

| Variable | 변경 내용 |
|----------|-----------|
| `KIS_MODE` | `모의` → **`실전`** |
| `KIS_APP_KEY` | KIS Developers → **실전투자** 앱키로 교체 |
| `KIS_APP_SECRET` | 실전 앱시크릿으로 교체 |
| `KIS_ACCOUNT_NO` | **실계좌** 10자리 (하이픈 없이, 예: `6367728701`) |

### 그대로 유지해도 되는 것 (예시)

- `MAX_TOTAL_AMOUNT`, `MAX_BUY_AMOUNT`, `MAX_CLOSING_AMOUNT`, `MAX_CLOSING_BUY`
- `ENABLE_INTRADAY_AI`, `ENABLE_CRASH_BOUNCE`, `ENABLE_V_REVERSAL`
- `STOP_LOSS_PCT`, `TAKE_PROFIT_PCT`, `DYNAMIC_CAPITAL`, `BUY_RATIO`
- `TELEGRAM_*`, `GROQ_API_KEY`, `API_SECRET`

### 실전 전환 시 달라지는 동작

| 항목 | 모의 | 실전 |
|------|------|------|
| 주문 서버 | VTS (`openapivts...`) | 실전 (`openapi...`) |
| 낙폭반등 | `ENABLE_CRASH_BOUNCE=true`여도 **자동 OFF** | 설정값대로 **실제 동작** |
| API 호출 간격 | 1초 (초당 2건 제한) | 0.05초 |
| 체결 | 가상 | **실제 돈** |

### 전환 전 체크

1. **모의 계좌 잔여 포지션** (데이터솔루션, 종가베팅 오버나이트 등)은 모의 계좌에서 **별도 정리** (실전과 무관)
2. KIS 앱/HTS에서 **실전 계좌 예수금** 확인 — `DYNAMIC_CAPITAL=true`면 예수금 기준으로 한도 자동 조절
3. Railway Variables 4개 변경 후 **Redeploy**
4. 텔레그램 시작 메시지: `📍 모드: 실전` + `✅ 계좌 연결 OK` 확인
5. `/health` → `"kis_mode": "실전"`, `account_warning: null`, `orderable_cash` 확인

> 봇은 `KIS_MODE`가 바뀌면 저장된 모의 포지션 상태를 **자동으로 불러오지 않습니다** (실전/모의 혼선 방지).

---

## ✅ 설정 확인 체크리스트

복구·재배포 후 아래를 확인하세요.

### 모의투자

- [ ] `KIS_APP_KEY` / `SECRET` = **모의투자** 포털에서 발급한 키
- [ ] `KIS_MODE` = `모의`
- [ ] `KIS_ACCOUNT_NO` = `50xxxxxxxx` 형식

### 실전투자

- [ ] `KIS_APP_KEY` / `SECRET` = **실전투자** 포털에서 발급한 키
- [ ] `KIS_MODE` = `실전`
- [ ] `KIS_ACCOUNT_NO` = 실계좌 (50으로 시작하지 않음)

### 공통

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
