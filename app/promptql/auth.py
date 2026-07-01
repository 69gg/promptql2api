"""PromptQL 认证链：hasura-lux cookie → luxJWT → enriched JWT（主 graphql Bearer）。

缓存 enriched JWT，到期前自动刷新（JWT ~24h 有效）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings


def _decode_jwt_exp(token: str) -> float:
    """解析 JWT 的 exp（unix 秒）；失败返回 0。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data: dict[str, Any] = json.loads(base64.urlsafe_b64decode(payload))
        return float(data.get("exp", 0))
    except Exception:  # noqa: BLE001
        return 0.0


@dataclass
class AuthTokens:
    lux_jwt: str
    enriched_jwt: str  # 主 graphql 用的 Bearer
    enriched_exp: float  # enriched_jwt 的 exp（unix 秒）


class AuthManager:
    """管理 PromptQL 认证 token，协程安全地缓存与刷新。"""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._s = settings
        self._client = client
        self._tokens: AuthTokens | None = None
        self._lock = asyncio.Lock()

    async def get_bearer(self) -> str:
        """返回有效的 enriched JWT（即将过期则刷新）。"""
        tok = self._tokens
        now = time.time()
        if tok is None or tok.enriched_exp - now <= self._s.token_refresh_margin:
            async with self._lock:
                tok = self._tokens
                if tok is None or tok.enriched_exp - now <= self._s.token_refresh_margin:
                    self._tokens = await self._refresh()
        return self._tokens.enriched_jwt

    async def _refresh(self) -> AuthTokens:
        lux = await self._fetch_lux_jwt()
        enriched = await self._enrich(lux)
        return AuthTokens(lux_jwt=lux, enriched_jwt=enriched, enriched_exp=_decode_jwt_exp(enriched))

    async def _fetch_lux_jwt(self) -> str:
        r = await self._client.post(
            self._s.auth_token_url,
            headers={"x-hasura-project-id": self._s.project_id},
            cookies=self._s.auth_cookies,
        )
        r.raise_for_status()
        data = r.json()
        token: str = data["token"]
        if not token:
            raise RuntimeError(f"auth token endpoint returned no token: {data}")
        return token

    async def _enrich(self, lux_jwt: str) -> str:
        # EnrichToken 是 playground graphql 的 mutation，请求时无需 Bearer（cookie 即可）。
        query = (
            "mutation EnrichToken($luxJWT: String!, $projectId: uuid!) {"
            "  enrich_token(luxJWT: $luxJWT, projectId: $projectId) { userDirectoryJWT }"
            "}"
        )
        r = await self._client.post(
            self._s.graphql_url,
            json={"query": query, "operationName": "EnrichToken",
                  "variables": {"luxJWT": lux_jwt, "projectId": self._s.project_id}},
            cookies=self._s.auth_cookies,
        )
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise RuntimeError(f"EnrichToken failed: {body['errors']}")
        return body["data"]["enrich_token"]["userDirectoryJWT"]
