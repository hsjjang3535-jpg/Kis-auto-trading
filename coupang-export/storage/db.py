import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from config import DB_PATH, ensure_data_dir


class Database:
    def __init__(self, db_path: Path = DB_PATH):
        ensure_data_dir()
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT NOT NULL,
                    product_id INTEGER NOT NULL,
                    product_name TEXT NOT NULL,
                    product_price INTEGER,
                    product_image TEXT,
                    affiliate_url TEXT,
                    is_rocket INTEGER DEFAULT 0,
                    is_free_shipping INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'collected',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(keyword, product_id)
                );

                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_row_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    content_html TEXT NOT NULL,
                    meta_json TEXT,
                    wp_post_id INTEGER,
                    status TEXT DEFAULT 'generated',
                    created_at TEXT NOT NULL,
                    posted_at TEXT,
                    FOREIGN KEY(product_row_id) REFERENCES products(id)
                );

                CREATE TABLE IF NOT EXISTS report_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_type TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.utcnow().isoformat()

    def upsert_product(self, keyword: str, item: dict[str, Any]) -> int:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO products (
                    keyword, product_id, product_name, product_price, product_image,
                    affiliate_url, is_rocket, is_free_shipping, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'collected', ?, ?)
                ON CONFLICT(keyword, product_id) DO UPDATE SET
                    product_name=excluded.product_name,
                    product_price=excluded.product_price,
                    product_image=excluded.product_image,
                    affiliate_url=excluded.affiliate_url,
                    is_rocket=excluded.is_rocket,
                    is_free_shipping=excluded.is_free_shipping,
                    updated_at=excluded.updated_at
                """,
                (
                    keyword,
                    item["product_id"],
                    item["product_name"],
                    item.get("product_price"),
                    item.get("product_image"),
                    item.get("affiliate_url"),
                    int(bool(item.get("is_rocket"))),
                    int(bool(item.get("is_free_shipping"))),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM products WHERE keyword=? AND product_id=?",
                (keyword, item["product_id"]),
            ).fetchone()
            return int(row["id"])

    def list_products(self, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        query = "SELECT * FROM products"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def update_product_status(self, product_row_id: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE products SET status=?, updated_at=? WHERE id=?",
                (status, self._now(), product_row_id),
            )

    def save_post(
        self,
        product_row_id: int,
        title: str,
        content_html: str,
        meta_json: dict[str, Any],
        status: str = "generated",
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO posts (product_row_id, title, content_html, meta_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (product_row_id, title, content_html, json.dumps(meta_json, ensure_ascii=False), status, self._now()),
            )
            return int(cur.lastrowid)

    def list_posts(self, status: str = "generated", limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.*, pr.product_name, pr.affiliate_url, pr.keyword
                FROM posts p
                JOIN products pr ON pr.id = p.product_row_id
                WHERE p.status=?
                ORDER BY p.id ASC
                LIMIT ?
                """,
                (status, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_post_published(self, post_id: int, wp_post_id: int) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE posts SET status='published', wp_post_id=?, posted_at=? WHERE id=?
                """,
                (wp_post_id, now, post_id),
            )
            row = conn.execute("SELECT product_row_id FROM posts WHERE id=?", (post_id,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE products SET status='posted', updated_at=? WHERE id=?",
                    (now, row["product_row_id"]),
                )

    def save_report_snapshot(
        self, report_type: str, start_date: str, end_date: str, rows: list[dict[str, Any]]
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO report_snapshots (report_type, start_date, end_date, payload_json, fetched_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (report_type, start_date, end_date, json.dumps(rows, ensure_ascii=False), self._now()),
            )

    def dashboard_summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            products = conn.execute("SELECT status, COUNT(*) AS cnt FROM products GROUP BY status").fetchall()
            posts = conn.execute("SELECT status, COUNT(*) AS cnt FROM posts GROUP BY status").fetchall()
            latest_report = conn.execute(
                "SELECT report_type, start_date, end_date, fetched_at FROM report_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return {
                "products": {row["status"]: row["cnt"] for row in products},
                "posts": {row["status"]: row["cnt"] for row in posts},
                "latest_report": dict(latest_report) if latest_report else None,
            }
