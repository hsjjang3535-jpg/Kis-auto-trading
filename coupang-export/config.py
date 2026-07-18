import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR.parent / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "coupang.db"))

COUPANG_ACCESS_KEY = os.getenv("COUPANG_ACCESS_KEY", "")
COUPANG_SECRET_KEY = os.getenv("COUPANG_SECRET_KEY", "")
COUPANG_SUB_ID = os.getenv("COUPANG_SUB_ID", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

WP_SITE_URL = os.getenv("WP_SITE_URL", "").rstrip("/")
WP_USERNAME = os.getenv("WP_USERNAME", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")
WP_POST_STATUS = os.getenv("WP_POST_STATUS", "draft")

KEYWORDS_FILE = Path(os.getenv("KEYWORDS_FILE", BASE_DIR / "keywords.txt"))
PRODUCTS_PER_KEYWORD = int(os.getenv("PRODUCTS_PER_KEYWORD", "3"))
MIN_PRODUCT_PRICE = int(os.getenv("MIN_PRODUCT_PRICE", "5000"))
API_CALL_DELAY_SEC = float(os.getenv("API_CALL_DELAY_SEC", "1.5"))


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def validate_coupang_keys() -> None:
    if not COUPANG_ACCESS_KEY or not COUPANG_SECRET_KEY:
        raise ValueError(
            "COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY가 필요합니다. "
            "coupang_partners/.env.example 참고 후 .env 파일을 만드세요."
        )


def validate_ai_keys() -> None:
    if not OPENAI_API_KEY and not GROQ_API_KEY:
        raise ValueError("OPENAI_API_KEY 또는 GROQ_API_KEY 중 하나가 필요합니다.")


def validate_wordpress() -> None:
    if not all([WP_SITE_URL, WP_USERNAME, WP_APP_PASSWORD]):
        raise ValueError(
            "워드프레스 포스팅에는 WP_SITE_URL, WP_USERNAME, WP_APP_PASSWORD가 필요합니다."
        )
