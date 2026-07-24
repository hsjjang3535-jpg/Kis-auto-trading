"""
Fetch Kis-auto-trading Railway variables, merge latest strategy settings,
upsert to Railway, and write local/public backups for restore.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
TG_ENV = Path(r"C:\Users\hsjja\Projects\telegram-gemini-bot\.env")
API = "https://backboard.railway.app/graphql/v2"
KST = ZoneInfo("Asia/Seoul")

SECRET_KEYS = {
    "KIS_APP_KEY",
    "KIS_APP_SECRET",
    "KIS_ACCOUNT_NO",
    "KIS_CANO",
    "KIS_ACNT_PRDT_CD",
    "GROQ_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "API_SECRET",
    "NAVER_CLIENT_ID",
    "NAVER_CLIENT_SECRET",
    "RAILWAY_API_TOKEN",
    "RAILWAY_TRADING_SERVICE_ID",
    "RAILWAY_ENV_ID",
    "RAILWAY_PROJECT_ID",
}

# Settings configured through recent work — merge onto live Railway values.
# Does NOT flip ENABLE_* unless listed (preserve current ON/OFF from Railway).
PATCH = {
    # recovery / ops
    "TZ": "Asia/Seoul",
    "CLOSING_ACCOUNT_SYNC": "true",
    "MAX_BUY_CASH_FAILS": "3",
    # exits
    "STOP_LOSS_PCT": "2.0",
    "QUICK_STOP_LOSS_PCT": "1.5",
    "QUICK_STOP_WINDOW_MIN": "30",
    "TAKE_PROFIT_PCT": "3.0",
    "TRAILING_STOP_PCT": "1.0",
    # screener / breakout B
    "UPPER_MA5_GAP_MAX": "15.0",
    "BREAKOUT_HIGH20_MAX_PCT": "3.0",
    "BREAKOUT_MA5_GAP_MAX": "10.0",
    "BREAKOUT_MOMENTUM_ENABLED": "true",
    "BREAKOUT_MOMENTUM_START": "09:10",
    "BREAKOUT_MOMENTUM_END": "10:30",
    "BREAKOUT_MOMENTUM_BUY_RATIO": "0.5",
    "BREAKOUT_MOMENTUM_HIGH20_MAX_PCT": "8.0",
    "BREAKOUT_MOMENTUM_MA5_GAP_MAX": "20.0",
    "BREAKOUT_MOMENTUM_5MIN_VOL": "1.5",
    # strong V sim (user enabled + relaxed)
    "ENABLE_STRONG_V_SIM": "true",
    "STRONG_V_SCAN_START": "09:00",
    "STRONG_V_ENTRY_START": "09:15",
    "STRONG_V_ENTRY_END": "14:30",
    "STRONG_V_POLL_INTERVAL": "5",
    "STRONG_V_FAST_SCAN_INTERVAL": "2",
    "STRONG_V_FOCUS_INTERVAL": "1",
    "STRONG_V_MAX_BELOW_PREV": "5.0",
    "STRONG_V_MA5_TOLERANCE": "3.0",
    "STRONG_V_MIN_PREV_RATE": "2.0",
    "STRONG_V_MIN_DROP": "3.0",
    "STRONG_V_SIM_AMOUNT": "500000",
    "STRONG_V_MAX_API_CALLS": "20",
    "STRONG_V_TIME_EXIT": "14:20",
    "STRONG_V_TAKE_PROFIT": "3.0",
    "STRONG_V_STOP_LOSS": "2.0",
    "STRONG_V_MA_EXIT": "false",
    # K1+ immediate on breach
    "K1_PLUS_IMMEDIATE_ON_BREACH": "true",
    "K1_PLUS_STOP_LOSS_PCT": "5.0",
    "K1_PLUS_MAX_RISK_TO_LOW_PCT": "8.0",
    # 종가베팅 익일 청산 (시초가 강제매도 → 손절/익절/15:00)
    "CLOSING_STOP_LOSS_PCT": "2.0",
    "CLOSING_TAKE_PROFIT_PCT": "3.0",
    "CLOSING_FORCE_EXIT": "15:00",
}


def gql(token: str, query: str, variables: dict | None = None) -> dict:
    res = requests.post(
        API,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables or {}},
        timeout=60,
    )
    res.raise_for_status()
    data = res.json()
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"], ensure_ascii=False)[:800])
    return data["data"]


def find_project_id(token: str, service_id: str) -> str:
    data = gql(
        token,
        "{ me { projects { edges { node { id name services { edges { node { id name } } } } } } } }",
    )
    for edge in data["me"]["projects"]["edges"]:
        node = edge["node"]
        for s in node["services"]["edges"]:
            if s["node"]["id"] == service_id:
                return node["id"]
    raise RuntimeError(f"project not found for service {service_id}")


def get_variables(token: str, project_id: str, env_id: str, service_id: str) -> dict[str, str]:
    data = gql(
        token,
        """
        query($projectId: String!, $environmentId: String!, $serviceId: String) {
          variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId)
        }
        """,
        {
            "projectId": project_id,
            "environmentId": env_id,
            "serviceId": service_id,
        },
    )
    return dict(data.get("variables") or {})


def upsert_variables(
    token: str,
    project_id: str,
    env_id: str,
    service_id: str,
    variables: dict[str, str],
    *,
    skip_deploys: bool = True,
) -> None:
    gql(
        token,
        """
        mutation($input: VariableCollectionUpsertInput!) {
          variableCollectionUpsert(input: $input)
        }
        """,
        {
            "input": {
                "projectId": project_id,
                "environmentId": env_id,
                "serviceId": service_id,
                "variables": variables,
                "replace": False,
                "skipDeploys": skip_deploys,
            }
        },
    )


def write_env(path: Path, mapping: dict[str, str], header: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = list(header)
    for k in sorted(mapping):
        v = mapping[k]
        # keep multiline-safe single line
        v = str(v).replace("\r", "").replace("\n", "\\n")
        lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    load_dotenv(TG_ENV)
    token = os.getenv("RAILWAY_API_TOKEN", "").strip()
    service_id = os.getenv("RAILWAY_TRADING_SERVICE_ID", "").strip()
    env_id = os.getenv("RAILWAY_ENV_ID", "").strip()
    if not token or not service_id or not env_id:
        print("missing RAILWAY_API_TOKEN / SERVICE_ID / ENV_ID", file=sys.stderr)
        return 1

    # Account tokens may not allow `me`; project id is known for Kis-auto-trading.
    project_id = os.getenv("RAILWAY_PROJECT_ID", "").strip() or "b0cc8dc0-b220-499a-8f90-4e1543726b7b"
    print(f"project={project_id}")
    print(f"service={service_id}")
    print(f"env={env_id}")

    current = get_variables(token, project_id, env_id, service_id)
    print(f"current vars: {len(current)}")

    # Merge patch (overwrite listed keys only)
    merged = dict(current)
    changed: list[str] = []
    added: list[str] = []
    for k, v in PATCH.items():
        if k not in merged:
            added.append(k)
            merged[k] = v
        elif str(merged[k]) != str(v):
            changed.append(f"{k}: {merged[k]!r} -> {v!r}")
            merged[k] = v

    # Upsert only patch keys (safer than full replace)
    to_upsert = {k: str(PATCH[k]) for k in PATCH}
    print(f"upsert {len(to_upsert)} keys (added={len(added)}, changed={len(changed)})")
    for line in added:
        print(f"  + {line}")
    for line in changed:
        print(f"  ~ {line}")

    upsert_variables(token, project_id, env_id, service_id, to_upsert, skip_deploys=True)
    print("Railway upsert OK (skipDeploys=true)")

    # Re-fetch after upsert for accurate backups
    final = get_variables(token, project_id, env_id, service_id)
    stamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    commit_hint = "fc5c061+"

    public = {k: v for k, v in final.items() if k not in SECRET_KEYS}
    write_env(
        ROOT / "railway-public-snapshot.env",
        public,
        [
            "# Public Railway/config snapshot — safe to commit (NO secrets)",
            f"# Generated: {stamp}",
            f"# Code tip: {commit_hint}",
            "# Secrets restore (local only, gitignored):",
            "#   backups/railway-env-LATEST.env → Railway Variables Raw Editor",
            "#",
            "# telegram-gemini-bot(trading) API_SECRET must match this service.",
            "",
        ],
    )
    print(f"wrote railway-public-snapshot.env ({len(public)} keys)")

    backup_dir = ROOT / "backups"
    full_header = [
        f"# Kis-auto-trading Railway FULL backup (SECRETS) — DO NOT COMMIT",
        f"# Generated: {stamp}",
        f"# project={project_id} service={service_id} env={env_id}",
        "# Restore: Railway → Variables → Raw Editor → paste → Save → Redeploy",
        "",
    ]
    write_env(backup_dir / "railway-env-LATEST.env", final, full_header)
    dated = backup_dir / f"railway-env-{datetime.now(KST).strftime('%Y%m%d-%H%M')}.env"
    write_env(dated, final, full_header)
    print(f"wrote {backup_dir / 'railway-env-LATEST.env'} ({len(final)} keys)")
    print(f"wrote {dated.name}")

    # Ensure gitignore covers backups
    gi = ROOT / ".gitignore"
    if gi.exists():
        text = gi.read_text(encoding="utf-8")
        if "backups/" not in text and "railway-env" not in text:
            with gi.open("a", encoding="utf-8") as f:
                f.write("\n# Railway secret backups\nbackups/\n")
            print("appended backups/ to .gitignore")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
