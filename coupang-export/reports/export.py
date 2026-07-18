from datetime import date, timedelta
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from config import DATA_DIR, ensure_data_dir
from storage.db import Database


def export_reports_to_excel(output_path: Path | None = None) -> Path:
    ensure_data_dir()
    output_path = output_path or DATA_DIR / "reports_export.xlsx"

    db = Database()
    with db._connect() as conn:
        snapshots = conn.execute(
            "SELECT report_type, start_date, end_date, payload_json, fetched_at FROM report_snapshots ORDER BY id"
        ).fetchall()

    wb = Workbook()
    summary = wb.active
    summary.title = "summary"
    summary.append(["report_type", "start_date", "end_date", "rows", "fetched_at"])

    for snap in snapshots:
        rows = __import__("json").loads(snap["payload_json"])
        sheet_name = f"{snap['report_type']}_{snap['start_date']}"[:31]
        ws = wb.create_sheet(title=sheet_name)
        if rows:
            headers = list(rows[0].keys())
            ws.append(headers)
            for row in rows:
                ws.append([row.get(h) for h in headers])
        summary.append([snap["report_type"], snap["start_date"], snap["end_date"], len(rows), snap["fetched_at"]])

    wb.save(output_path)
    return output_path


def fetch_and_store_reports(
    client: Any,
    days: int = 7,
    report_types: list[str] | None = None,
) -> dict[str, int]:
    report_types = report_types or ["clicks", "orders", "commission"]
    end = date.today()
    start = end - timedelta(days=days)
    start_s = start.strftime("%Y%m%d")
    end_s = end.strftime("%Y%m%d")

    db = Database()
    counts: dict[str, int] = {}
    for report_type in report_types:
        rows = client.fetch_report(report_type, start_s, end_s, page=0)
        db.save_report_snapshot(report_type, start_s, end_s, rows)
        counts[report_type] = len(rows)
    return counts
