from pathlib import Path
from typing import Any

from config import (
    KEYWORDS_FILE,
    MIN_PRODUCT_PRICE,
    PRODUCTS_PER_KEYWORD,
    validate_ai_keys,
    validate_coupang_keys,
    validate_wordpress,
)
from content.generator import ContentGenerator
from coupang.client import CoupangClient
from posting.wordpress import WordPressClient
from reports.export import export_reports_to_excel, fetch_and_store_reports
from storage.db import Database


def load_keywords(path: Path = KEYWORDS_FILE) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"키워드 파일이 없습니다: {path}")
    keywords = []
    for line in path.read_text(encoding="utf-8").splitlines():
        word = line.strip()
        if word and not word.startswith("#"):
            keywords.append(word)
    return keywords


def _normalize_product(keyword: str, item: dict[str, Any]) -> dict[str, Any] | None:
    product_id = item.get("productId") or item.get("product_id")
    product_name = item.get("productName") or item.get("product_name")
    if not product_id or not product_name:
        return None

    price = item.get("productPrice") or item.get("product_price") or 0
    if price < MIN_PRODUCT_PRICE:
        return None

    return {
        "keyword": keyword,
        "product_id": int(product_id),
        "product_name": product_name,
        "product_price": int(price),
        "product_image": item.get("productImage") or item.get("product_image"),
        "affiliate_url": item.get("productUrl") or item.get("affiliate_url"),
        "is_rocket": item.get("isRocket") or item.get("is_rocket") or False,
        "is_free_shipping": item.get("isFreeShipping") or item.get("is_free_shipping") or False,
    }


def collect_products(keywords: list[str] | None = None, limit_per_keyword: int = PRODUCTS_PER_KEYWORD) -> int:
    validate_coupang_keys()
    keywords = keywords or load_keywords()
    client = CoupangClient()
    db = Database()
    saved = 0

    for keyword in keywords:
        items = client.search_products(keyword, limit=limit_per_keyword)
        for item in items:
            normalized = _normalize_product(keyword, item)
            if not normalized:
                continue
            db.upsert_product(keyword, normalized)
            saved += 1
    return saved


def generate_posts(limit: int = 10) -> int:
    validate_ai_keys()
    db = Database()
    generator = ContentGenerator()
    products = db.list_products(status="collected", limit=limit)
    created = 0

    for product in products:
        payload = {
            "keyword": product["keyword"],
            "product_name": product["product_name"],
            "product_price": product["product_price"],
            "product_image": product["product_image"],
            "affiliate_url": product["affiliate_url"],
            "is_rocket": product["is_rocket"],
            "is_free_shipping": product["is_free_shipping"],
        }
        result = generator.generate_post(payload)
        db.save_post(
            product_row_id=product["id"],
            title=result["title"],
            content_html=result["content_html"],
            meta_json=result["meta_json"],
        )
        db.update_product_status(product["id"], "generated")
        created += 1
    return created


def publish_posts(limit: int = 10) -> int:
    validate_wordpress()
    db = Database()
    wp = WordPressClient()
    posts = db.list_posts(status="generated", limit=limit)
    published = 0

    for post in posts:
        response = wp.create_post(post["title"], post["content_html"])
        db.mark_post_published(post["id"], int(response["id"]))
        published += 1
    return published


def sync_reports(days: int = 7) -> dict[str, int]:
    validate_coupang_keys()
    client = CoupangClient()
    return fetch_and_store_reports(client, days=days)


def run_full_pipeline(
    keywords: list[str] | None = None,
    collect_limit: int = PRODUCTS_PER_KEYWORD,
    generate_limit: int = 10,
    publish_limit: int = 10,
    report_days: int = 7,
    skip_publish: bool = False,
) -> dict[str, Any]:
    result = {
        "collected": collect_products(keywords, collect_limit),
        "generated": generate_posts(generate_limit),
        "published": 0,
        "reports": {},
        "excel": None,
    }

    if not skip_publish:
        try:
            result["published"] = publish_posts(publish_limit)
        except ValueError:
            result["published"] = 0

    result["reports"] = sync_reports(report_days)
    result["excel"] = str(export_reports_to_excel())
    return result
