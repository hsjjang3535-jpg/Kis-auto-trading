# 복구 가이드 (Kis-auto-trading)

Railway·로컬 어디서든 **GitHub 태그 기준으로 동일 상태를 복원**할 수 있습니다.

## 1. 코드 복원

```bash
git clone https://github.com/hsjjang3535-jpg/Kis-auto-trading.git
cd Kis-auto-trading
git checkout v2.1-stable
```

특정 커밋으로 복원:

```bash
git checkout 61e4bd4
```

## 2. 환경변수 설정

```bash
cp .env.example .env
# .env 파일을 편집해 실제 키 입력
```

Railway 사용 시: **Dashboard → Variables**에 `.env.example` 항목을 동일하게 설정.
`.env` 파일 자체는 Git에 올리지 않습니다.

## 3. 로컬 실행

```bash
pip install -r requirements.txt
python trader.py
```

## 4. Railway 재배포

- GitHub `main` 브랜치에 push하면 자동 배포
- 또는 Railway Dashboard → Deployments → **Redeploy**

## 5. stable 태그 목록

| 태그 | 설명 |
|------|------|
| `v1.0-stable` | 장중매매만 |
| `v2.0-stable` | 장중 + 종가베팅 |
| `v2.1-stable` | API 수정, 스크리닝 진단, AI 완화 (최신) |

## 6. 문제 발생 시

```bash
git fetch --tags
git checkout v2.1-stable
git push origin HEAD:main --force-with-lease   # Railway를 이 버전으로 되돌릴 때만
```

`--force-with-lease`는 main을 태그 시점으로 되돌릴 때만 사용하세요.
