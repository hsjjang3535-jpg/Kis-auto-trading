import json
import time
from typing import Any
from urllib.parse import urlencode

import requests

from config import API_CALL_DELAY_SEC, COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY, COUPANG_SUB_ID
from coupang.auth import generate_authorization

BASE_URL = "https://api-gateway.coupang.com"
API_PREFIX = "/v2/providers/affiliate_open_api/apis/openapi/v1"


class CoupangAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class CoupangClient:
    def __init__(
        self,
        access_key: str = COUPANG_ACCESS_KEY,
        secret_key: str = COUPANG_SECRET_KEY,
        sub_id: str = COUPANG_SUB_ID,
        delay_sec: float = API_CALL_DELAY_SEC,
    ):
        self.access_key = access_key
        self.secret_key = secret_key
        self.sub_id = sub_id
        self.delay_sec = delay_sec
        self._last_call_at = 0.0

    def _wait_rate_limit(self) -> None:
        elapsed = time.time() - self._last_call_at
        if elapsed < self.delay_sec:
            time.sleep(self.delay_sec - elapsed)
        self._last_call_at = time.time()

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._wait_rate_limit()

        query_params = dict(params or {})
        if self.sub_id and "subId" not in query_params and endpoint != "/deeplink":
            query_params.setdefault("subId", self.sub_id)

        path = f"{API_PREFIX}{endpoint}"
        query = urlencode(query_params, doseq=True) if query_params else ""
        path_with_query = f"{path}?{query}" if query else path

        headers = {
            "Authorization": generate_authorization(
                method, path_with_query, self.access_key, self.secret_key
            ),
            "Content-Type": "application/json;charset=UTF-8",
        }

        url = f"{BASE_URL}{path_with_query}"
        response = requests.request(
            method=method.upper(),
            url=url,
            headers=headers,
            json=json_body,
            timeout=30,
        )

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise CoupangAPIError(
                f"JSON 파싱 실패: {response.text[:200]}",
                status_code=response.status_code,
            ) from exc

        if response.status_code >= 400:
            raise CoupangAPIError(
                payload.get("rMessage") or response.text,
                status_code=response.status_code,
                payload=payload,
            )

        if str(payload.get("rCode", "0")) != "0":
            raise CoupangAPIError(
                payload.get("rMessage") or "쿠팡 API 오류",
                status_code=response.status_code,
                payload=payload,
            )

        return payload

    def search_products(
        self,
        keyword: str,
        limit: int = 10,
        image_size: str = "512x512",
        srp_link_only: bool = False,
    ) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/products/search",
            params={
                "keyword": keyword,
                "limit": min(limit, 10),
                "imageSize": image_size,
                "srpLinkOnly": str(srp_link_only).lower(),
            },
        )
        data = payload.get("data") or {}
        return data.get("productData") or []

    def best_by_category(self, category_id: int, limit: int = 20) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            f"/products/bestcategories/{category_id}",
            params={"limit": limit, "imageSize": "512x512"},
        )
        return payload.get("data") or []

    def create_deeplink(self, coupang_urls: list[str]) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"coupangUrls": coupang_urls}
        if self.sub_id:
            body["subId"] = self.sub_id
        payload = self._request("POST", "/deeplink", json_body=body)
        return payload.get("data") or []

    def fetch_report(
        self,
        report_type: str,
        start_date: str,
        end_date: str,
        page: int = 0,
    ) -> list[dict[str, Any]]:
        endpoints = {
            "clicks": "/reports/clicks",
            "orders": "/reports/orders",
            "cancels": "/reports/cancels",
            "commission": "/reports/commission",
        }
        if report_type not in endpoints:
            raise ValueError(f"지원하지 않는 리포트 타입: {report_type}")

        payload = self._request(
            "GET",
            endpoints[report_type],
            params={"startDate": start_date, "endDate": end_date, "page": page},
        )
        return payload.get("data") or []
