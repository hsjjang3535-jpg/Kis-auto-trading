# 복구 가이드 (Kis-auto-trading)

초기화·오배포 후 **코드 + Variables**를 이 가이드로 되돌립니다.

## 백업 구성 (2026-07-23 갱신)

| 종류 | 위치 | 내용 |
|------|------|------|
| 코드 | Git `main` (최신) / 태그 `v4.0-stable` | 돌파 안전·모멘텀, 강세V 시뮬, 빠른손절 등 |
| 공개 설정 | [`railway-public-snapshot.env`](./railway-public-snapshot.env) | 시크릿 없는 Variables 스냅샷 (커밋됨) |
| 시크릿 | `backups/railway-env-LATEST.env` | 로컬 전용 (gitignore). KIS/텔레그램/Groq 등 |
| 동기화 스크립트 | [`scripts/sync_railway_vars.py`](./scripts/sync_railway_vars.py) | Railway 변수 upsert + 백업 생성 |
| 체크리스트 | [`RAILWAY_VARIABLES.md`](./RAILWAY_VARIABLES.md) | 변수 설명 |

> Railway Dashboard에 “설정 파일 업로드” 기능은 없습니다.  
> **Variables Raw Editor에 붙여 넣는 방식**이 복구입니다.

로컬에서 다시 Railway에 맞추려면 (시크릿은 덮어쓰지 않고 PATCH만 upsert):

```bash
python scripts/sync_railway_vars.py
```

---

## 0. Railway Volume (주간손익·포지션 유지) — 필수 권장

재배포 시 컨테이너 디스크가 초기화되어 `trading_state.json` / 손익 장부가 사라집니다.  
주간 손익이 “기록 없음”으로 나오는 주된 원인입니다.

1. Railway → **Kis-auto-trading** → **Volumes** → **Add Volume**
2. Mount path: `/data`
3. Variables에 `DATA_DIR=/data` 추가
4. Redeploy

이후 상태는 `/data/trading_state.json`, 손익은 `/data/daily_pnl_ledger.json`에 남습니다.

---

## 1. 코드 복원

```bash
git fetch --tags
git checkout v4.0-stable
# Railway가 main을 배포한다면:
git checkout main
git reset --hard v4.0-stable
git push origin main --force-with-lease   # 복원할 때만
```

또는 GitHub에서 태그 `v4.0-stable` → 해당 커밋으로 Redeploy.

---

## 2. Variables 복원 (권장 순서)

### A) 시크릿 포함 전체 복구 (로컬 PC에 백업 있는 경우)

1. `kis-trading-bot/backups/railway-env-LATEST.env` 연다  
2. Railway → **Kis-auto-trading** → Variables → **Raw Editor**  
3. 내용 전체 붙여넣기 → Save → **Redeploy**

### B) 시크릿 백업이 없을 때

1. `railway-public-snapshot.env` 내용을 Raw Editor에 붙인다  
2. 아래 시크릿만 Dashboard에서 다시 입력한다  
   - `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO`  
   - `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`  
   - `API_SECRET` (telegram-gemini-bot trading과 **동일**)  
   - (선택) `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`  
3. `KIS_MODE` / 계좌·앱키가 **모의↔실전 짝**인지 확인  
4. Redeploy

---

## 3. 운영 시 켜 두는 플래그 (참고)

스냅샷 기본값은 안전을 위해 전략 OFF(`false`)입니다.  
실제 운영 중인 ON/OFF는 **시크릿 백업 파일** 또는 Railway 현재 값이 정본입니다.

우선순위: **K1 실전 > K1+ 시뮬 > K2+ 시뮬 > K2 시뮬 > UL 시뮬**

---

## 4. 로컬 실행 복구

```bash
# 시크릿 백업이 있으면
copy backups\railway-env-LATEST.env .env

pip install -r requirements.txt
python trader.py
```

---

## 5. stable 태그 이력

| 태그 | 설명 |
|------|------|
| **`v4.0-stable`** | **현재** — K1+/K2+/K2 시뮬, 우선순위 A, 손익 실전/시뮬 분리 |
| `v3.0-stable` | 이전 — 낙폭반등·스크리너 완화 등 |

---

## 6. 주의

- `backups/*.env`는 **커밋·공유 금지** (시크릿)  
- main force-push는 복원 목적일 때만  
- 실전 전환 시 키 4종(`KIS_MODE`, APP_KEY, APP_SECRET, ACCOUNT_NO)만 바꾸면 됨 — 공개 스냅샷 참고
