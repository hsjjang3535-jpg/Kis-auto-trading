import os

# KIS API 설정 (모의투자)
KIS_APP_KEY = os.environ.get("KIS_APP_KEY", "")
KIS_APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
KIS_ACCOUNT_NUMBER = os.environ.get("KIS_ACCOUNT_NUMBER", "50191209-01")
KIS_ACCOUNT_PRODUCT_CODE = os.environ.get("KIS_ACCOUNT_PRODUCT_CODE", "01")

# 모의투자 서버
KIS_BASE_URL = "https://openapivts.koreainvestment.com:29443"

# 텔레그램 알림
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# 매매 설정
MAX_BUDGET_PER_STOCK = int(os.environ.get("MAX_BUDGET_PER_STOCK", "500000"))  # 종당 최대 50만원
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", "-2.5"))  # 손절 -2.5%
TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PCT", "2.0"))  # 읍절 +2%

# 관심 종목 목록 (사용자가 바꾼 수 있음)
WATCHLIST = os.environ.get("WATCHLIST", "005930,000660,035420,001820,001170,079550").split(",")

# KRX 시장 시간 (KST)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 0
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30

# 미국장 설정
US_ENABLED = os.environ.get("US_ENABLED", "true").lower() == "true"
US_MAX_BUDGET_PER_STOCK = int(os.environ.get("US_MAX_BUDGET_PER_STOCK", "500"))  # $500
US_STOP_LOSS_PCT = float(os.environ.get("US_STOP_LOSS_PCT", "-2.5"))
US_TAKE_PROFIT_PCT = float(os.environ.get("US_TAKE_PROFIT_PCT", "2.0"))
US_EXCHANGE = os.environ.get("US_EXCHANGE", "NAS")  # NAS (NASDAQ) or NYS (NYSE)
US_WATCHLIST = os.environ.get("US_WATCHLIST", "AAPL,MSFT,GOOGL,AMZN,TSLA,NVDA,META,AMD,CRM,INTC").split(",")

# 바람시간 설정
TIMEZONE = "Asia/Seoul"
