# Railway Variables 체크리스트 (Kis-auto-trading)

> **보안:** API 키·토큰·계좌번호는 Git에 저장하지 마세요.  
> Railway Dashboard → Variables → **Raw Editor**에서만 관리하고,  
> 복사본은 **비밀번호 관리자**(1Password, Bitwarden 등)에 보관하세요.

**서비스:** `Kis-auto-trading` (production)  
**백업 태그:** `v4.0-stable` (2026-07-14, commit `b553ed9`)  
**공개 스냅샷:** [railway-public-snapshot.env](./railway-public-snapshot.env)  
**시크릿 로컬 백업:** `backups/railway-env-LATEST.env` (gitignore)  
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

### Railway 재배포 직후 종가베팅 복구 (필요할 때만)

`trading_state.json`이 재배포로 사라진 경우, 계좌 잔고에서 지정 종목만
일반 종가베팅 포지션으로 복원할 수 있습니다.

```env
CLOSING_RECOVERY_POSITIONS=042700|한미반도체|2026-07-15|2026-07-16
```

형식: `종목코드|종목명|매수일|복구만료일` (여러 개는 쉼표 구분).  
만료일 이후에는 자동으로 무시되며, K1·장중 포지션으로 추적 중인 종목은 복구하지 않습니다.

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
| `MAX_CLOSING_BUY` | `500000` | 종가베팅 1회 매수 |
| `CLOSING_BET_MAX_PER_SLOT` | `1` | 5분 슬롯당 최대 매수 종목 |
| `CLOSING_BET_MAX_POSITIONS` | `1` | 종가베팅 동시 보유 종목 수 |
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
| `CRASH_BOUNCE_AFTERNOON_TIME_EXIT` | `14:20` | 13:15 재검색 체결분 시간 청산 |
| `CRASH_BOUNCE_AFTERNOON_VOLUME_RATIO` | `1.2` | 오후 최근 5분봉 거래량 증가 배수 |
| `CRASH_BOUNCE_MIN_DROP` | `3.5` | 시가 대비 -3.5%↑ |
| `MAX_CRASH_BOUNCE_AMOUNT` | `500000` | 총 한도 |
| `MAX_CRASH_BOUNCE_BUY` | `500000` | 1회 매수 |
| `CRASH_BOUNCE_MAX_POSITIONS` | `1` | 동시 보유 |
| `CRASH_BOUNCE_TAKE_PROFIT` | `3.0` | 익절 +3% |
| `CRASH_BOUNCE_STOP_LOSS` | `2.0` | 손절 -2% |
| `CRASH_BOUNCE_MAX_API_CALLS` | `20` | 스캔당 API 상한 |

오전 미체결 시에만 13:15에 최근 30분 새 저점 반등·거래량 증가 조건으로
한 번 더 검색합니다. 매수 한도는 오전과 동일합니다.

### V자반등 오후 재검색

`ENABLE_V_REVERSAL=true`이고 오전 미체결이면 동일하게 13:15에 한 번 재검색합니다.

| Variable | 기본값 | 설명 |
|----------|--------|------|
| `V_REVERSAL_AFTERNOON_VOLUME_RATIO` | `1.2` | 오후 최근 5분봉 거래량 증가 배수 |
| `V_REVERSAL_TIME_EXIT` | `14:20` | 오전·오후 체결분 시간 청산 |
| `MAX_V_REVERSAL_AMOUNT` | `500000` | 오전·오후 공통 총 한도 |

---

## 🟣 상한가 리바운드 — 1단계 알림만 (`ENABLE_UL_REBOUND=true`)

자동 매매 없음. 텔레그램으로 R0~R3 구간·상한가 포착만 알림.

| Variable | 기본값 | 설명 |
|----------|--------|------|
| `ENABLE_UL_REBOUND` | `false` | 알림 ON |
| `UL_REBOUND_MONITOR_START` | `09:00` | 모니터링 시작 |
| `UL_REBOUND_MONITOR_END` | `15:30` | 모니터링 종료 |
| `UL_REBOUND_MIN_TRADING_VALUE` | `50000000000` | 거래대금 500억+ |
| `UL_REBOUND_WINDOW_DAYS` | `7` | 상한가 후 추적 일수 |
| `UL_REBOUND_MIN_HISTORY_DAYS` | `2` | 일봉 최소 일수 (미만=신규상장 제외) |
| `UL_REBOUND_FORCE_SELL_DAY` | `4` | 4거래일차 청산 알림 |
| `UL_REBOUND_SCAN_TOP` | `30` | 거래대금 상위 N |
| `UL_REBOUND_MAX_API_CALLS` | `25` | 스캔당 API 상한 |
| `UL_REBOUND_SIM_ENABLED` | `true` | 가상매매 시뮬 ON |
| `UL_REBOUND_SIM_AMOUNT` | `500000` | 시뮬 1회 매수금 (R2는 1:1 추가) |

> 모의/실전 모두 동작 (실제 주문 없음). **월~목**만 활성. 같은 종목은 K1 우선.

---

## 🔷 K1 종가베팅 (`ENABLE_K1_CLOSING=true`)

**금·월** 실전 종가 매수 (기존 금요일 AI 종가 대체). **4일차** 전량 청산.

| Variable | 기본값 | 설명 |
|----------|--------|------|
| `ENABLE_K1_CLOSING` | `false` | K1 종가 ON |
| `K1_CLOSING_ENTRY_START` | `14:20` | 매수 시작 |
| `K1_CLOSING_ENTRY_END` | `14:50` | 매수 종료 |
| `K1_MIN_TRADING_VALUE` | `50000000000` | 상한가 500억+ |
| `K1_MAX_BUY_DAYS_AFTER_UL` | `2` | 상한가 후 2일까지만 |
| `K1_FORCE_SELL_DAY` | `4` | 4일차 청산 |
| `K1_SIM_ENABLED` | `true` | 시뮬 기록 (실전과 함께) |

### 요일별 스케줄

| 요일 | 종가 전략 |
|------|-----------|
| 월 | **K1 실전** + 상한가 리바운딩 시뮬 (K1 우선) |
| 화~목 | AI 종가베팅 + 상한가 리바운딩 시뮬 |
| 금 | **K1 실전** (주말 보유) |

---

## 🔶 K2 장중 — 시뮬만 (`ENABLE_K2_SIM=true`)

실제 주문 없음. 피보 **0.5(K2)** 훼손 시 가상 매수·청산 보고.

| Variable | 기본값 | 설명 |
|----------|--------|------|
| `ENABLE_K2_SIM` | `false` | K2 시뮬 ON |
| `K2_MAX_DAYS_FROM_HIGH` | `4` | 상한가일=D1 포함 4일까지 매수 |
| `K2_FORCE_SELL_DAY` | `4` | 매수 후 4일차 강제청산 (가정) |
| `K2_SIM_AMOUNT` | `500000` | 가상 1회 매수금 |
| `K2_MAX_API_CALLS` | `20` | 스캔당 API 상한 |
| `K2_MAX_WATCH` | `5` | 최대 추적 종목 |

우선순위: **K1 > K1플러스 > K2플러스 > K2 > 상한가 리바운딩**. 장중은 현재가만 조회(분봉 반복 없음).

---

## 💠 K1플러스 — 시뮬만 (`ENABLE_K1_PLUS_SIM=true`)

세력봉(거래대금 500억+, 상한가 제외) 당일 K1 훼손 + 양봉 종가대 가상 매수. 실제 주문 없음.

| Variable | 기본값 | 설명 |
|----------|--------|------|
| `ENABLE_K1_PLUS_SIM` | `false` | K1플러스 시뮬 ON |
| `K1_PLUS_MIN_TRADING_VALUE` | `50000000000` | 세력봉 최소 거래대금 (500억) |
| `K1_PLUS_MIN_DAY_RATE` | `5.0` | 당일 등락률 하한 (%) |
| `K1_PLUS_FORCE_SELL_DAY` | `4` | 매수 후 4일차 강제청산 |
| `K1_PLUS_SIM_AMOUNT` | `500000` | 가상 1회 매수금 |
| `K1_PLUS_REQUIRE_NEW_HIGH` | `true` | 신고가 필터 |
| `K1_PLUS_MAX_WATCH` | `5` | 최대 추적 종목 |

우선순위: **K1 실전 > K1플러스 > K2플러스 > K2 > 상한가 리바운딩**.

---

## 🔷 K2플러스 — 시뮬만 (`ENABLE_K2_PLUS_SIM=true`)

세력봉(거래대금 500억+, 상한가 제외) 기준 피보 K2(0.5) 훼손 시 장중 가상 매수. 세력봉일=D1 포함 4일까지. 실제 주문 없음.

| Variable | 기본값 | 설명 |
|----------|--------|------|
| `ENABLE_K2_PLUS_SIM` | `false` | K2플러스 시뮬 ON |
| `K2_PLUS_MIN_TRADING_VALUE` | `50000000000` | 세력봉 최소 거래대금 (500억) |
| `K2_PLUS_MIN_DAY_RATE` | `5.0` | 당일 등락률 하한 (%) |
| `K2_PLUS_MAX_DAYS_FROM_HIGH` | `4` | 세력봉일=D1 포함 4일까지 매수 |
| `K2_PLUS_FORCE_SELL_DAY` | `4` | 매수 후 4일차 강제청산 (가정) |
| `K2_PLUS_SIM_AMOUNT` | `500000` | 가상 1회 매수금 |
| `K2_PLUS_REQUIRE_NEW_HIGH` | `true` | 신고가 필터 |
| `K2_PLUS_MAX_WATCH` | `5` | 최대 추적 종목 |

우선순위: **K1 실전 > K1플러스 > K2플러스 > K2 > 상한가 리바운딩**.

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
- `ENABLE_INTRADAY_AI`, `ENABLE_CRASH_BOUNCE`, `ENABLE_V_REVERSAL`, `ENABLE_UL_REBOUND`, `ENABLE_K2_SIM`, `ENABLE_K1_PLUS_SIM`, `ENABLE_K2_PLUS_SIM`
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

### 계좌번호가 안 바뀔 때

1. **Redeploy** — Variables 저장만으로는 실행 중 컨테이너에 반영 안 될 수 있음 → **Deployments → Redeploy**
2. **`KIS_CANO` / `KIS_ACNT_PRDT_CD`** — Railway에 이 변수가 있으면 예전에는 `KIS_ACCOUNT_NO`보다 **우선** 적용됨. 없으면 삭제, 있으면 `KIS_ACCOUNT_NO`와 **동일한 계좌**로 맞출 것
3. **10자리 확인** — `6367728701` 형식 (8+2). 8자리만 넣으면 뒤 `01`이 자동 붙음
4. **`/health` 확인** — `account.masked`, `account.source`가 바뀐 계좌인지 확인

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
