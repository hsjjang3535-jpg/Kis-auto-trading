import hashlib
import hmac
from time import gmtime, strftime


def generate_authorization(method: str, path_with_query: str, access_key: str, secret_key: str) -> str:
    """쿠팡파트너스 Open API HMAC 서명 생성."""
    path, _, query = path_with_query.partition("?")
    signed_date = strftime("%y%m%d", gmtime()) + "T" + strftime("%H%M%S", gmtime()) + "Z"
    message = signed_date + method.upper() + path + query
    signature = hmac.new(
        secret_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return (
        f"CEA algorithm=HmacSHA256, access-key={access_key}, "
        f"signed-date={signed_date}, signature={signature}"
    )
