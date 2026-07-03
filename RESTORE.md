# 복구 가이드 (Kis-auto-trading)

Railway·로컬 어디서든 **GitHub stable 태그 기준으로 동일 상태를 복원**할 수 있습니다.

## 1. 코드 복원 (최신 백업)

```bash
git clone https://github.com/hsjjang3535-jpg/Kis-auto-trading.git
cd Kis-auto-trading
git fetch --tags
git checkout v3.0-stable
```

## 2. 환경변수 설정

```bash
cp .env.example .env
# .env 파일을 편집해 실제 키 입력
```

Railway: **Dashboard → Variables**에 `.env.example` 항목 설정.

### Railway 권장 Variables (자동매매)

| 변수 | 권장값 | 비고 |
|------|--------|------|
| `KIS_MODE` | `모의` | 모의투자 |
| `ENABLE_CRASH_BOUNCE` | `true` | 낙폭반등 ON |
| `TZ` | `Asia/Seoul` | KST 스케줄 |

`MIN_CHANGE_RATE`, `VOL_RATIO_MIN`, `W52_GAP_UPPER_MAX`, `CLOSING_BET_MIN_RATE`, `LOWER_RSI_MAX`, `CRASH_BOUNCE_*` 는 **Variables에 넣지 않아도** `.env.example` 기본값 적용.

## 3. 로컬 실행

```bash
pip install -r requirements.txt
python trader.py
```

## 4. Railway 재배포

- GitHub `main` push → 자동 배포
- 또는 Dashboard → **Redeploy**

## 5. stable 태그 (백업)

| 태그 | 설명 |
|------|------|
| **`v3.0-stable`** | **현재 최신** — 낙폭반등, 1·2순위 스크리너 완화, KIS API 재시도 |

> 이전 태그(`v1.0-stable`, `v2.0-stable`, `v2.1-stable`)는 삭제됨. 복원은 `v3.0-stable` 사용.

## 6. v3.0-stable 포함 기능

- 장중매매: 상단 / 돌파 / 하단 (종산법, 1·2순위 완화)
- 종가베팅: 14:00 스크리닝 → 익일 09:00 시초가 매도
- 낙폭반등: 09:10~10:30, 한도 50만, 익절 +3%
- 손절 -2% / 장중 트레일링 익절 +3%

## 7. 문제 발생 시

```bash
git fetch --tags
git checkout v3.0-stable
git push origin HEAD:main --force-with-lease   # Railway를 이 버전으로 되돌릴 때만
```

`--force-with-lease`는 main을 태그 시점으로 되돌릴 때만 사용하세요.
