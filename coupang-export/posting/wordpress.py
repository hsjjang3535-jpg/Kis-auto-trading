from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from config import WP_APP_PASSWORD, WP_POST_STATUS, WP_SITE_URL, WP_USERNAME


class WordPressClient:
    def __init__(
        self,
        site_url: str = WP_SITE_URL,
        username: str = WP_USERNAME,
        app_password: str = WP_APP_PASSWORD,
    ):
        self.site_url = site_url.rstrip("/")
        self.username = username
        self.app_password = app_password.replace(" ", "")

    def create_post(self, title: str, content_html: str, status: str = WP_POST_STATUS) -> dict[str, Any]:
        url = f"{self.site_url}/wp-json/wp/v2/posts"
        response = requests.post(
            url,
            auth=HTTPBasicAuth(self.username, self.app_password),
            json={"title": title, "content": content_html, "status": status},
            timeout=60,
        )
        response.raise_for_status()
        return response.json()
