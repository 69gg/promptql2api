"""协议式 HTTP 客户端：``curl-cffi``（impersonate Chrome）+ 429 退避 + 随机 UA。

参考 ``/data0/CrazyRegistrants/lib/http_client.py``：用浏览器指纹绕过 Cloudflare 的
UA/IP 维度限流。所有方法返回 ``curl_cffi`` 的 ``Response`` 对象，调用方自行取
``status_code`` / ``json()`` / ``cookies``。
"""
from __future__ import annotations

import random
import time
from typing import Any

from curl_cffi import requests as cffi_requests

# 常见桌面 Chrome UA（每次请求随机一个，分散指纹）
_UA_POOL: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)


class HttpClient:
    """带浏览器指纹的 HTTP 客户端（不保持会话；每次独立请求）。"""

    def __init__(
        self, proxy: str | None = None, impersonate: str = "chrome136"
    ) -> None:
        self._proxies = {"http": proxy, "https": proxy} if proxy else None
        self._impersonate = impersonate

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any | None = None,
        retries: int = 3,
        timeout: int = 30,
    ) -> cffi_requests.Response:
        """发请求，429 指数退避，网络异常重试。"""
        last_exc: Exception | None = None
        resp: cffi_requests.Response | None = None
        for attempt in range(retries):
            merged = {"user-agent": random.choice(_UA_POOL)}
            if headers:
                merged.update(headers)
            try:
                resp = cffi_requests.request(
                    method,
                    url,
                    headers=merged,
                    json=json,
                    proxies=self._proxies,
                    impersonate=self._impersonate,
                    timeout=timeout,
                )
                if resp.status_code == 429:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                return resp
            except Exception as exc:  # noqa: BLE001 - 网络抖动统一重试
                last_exc = exc
                time.sleep(1.0 * (attempt + 1))
        if resp is not None:
            return resp
        raise last_exc or RuntimeError(f"request failed: {method} {url}")

    def post_json(
        self,
        url: str,
        body: Any,
        *,
        headers: dict[str, str] | None = None,
        retries: int = 3,
    ) -> cffi_requests.Response:
        return self._request("POST", url, headers=headers, json=body, retries=retries)

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        retries: int = 3,
    ) -> cffi_requests.Response:
        return self._request("GET", url, headers=headers, retries=retries)
