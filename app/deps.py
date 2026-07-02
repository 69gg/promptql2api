"""共享 FastAPI 依赖：注入 PromptQLClient + API key 校验。

每次请求 round-robin 取一个账号的 client（见 :class:`app.account.AccountPool`），
并用 :class:`_RetryingClient` 包一层：捕获认证失败（401/403 或 EnrichToken/auth token
相关 RuntimeError）→ 标记该账号 disabled → 抛 503 给客户端，下一次请求自动换号。
"""
from __future__ import annotations

from typing import Any, AsyncIterator

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.account import Account, AccountPool
from app.config import Settings
from app.promptql.client import PromptQLClient
from app.promptql.events import IREvent

_bearer = HTTPBearer(auto_error=False)

# 认证失败时的错误消息特征
_AUTH_ERR_HINTS = ("EnrichToken", "auth token")


def _is_auth_failure(exc: BaseException) -> bool:
    """判断异常是否为 PromptQL 账号认证失败（需换号）。"""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
        return True
    if isinstance(exc, RuntimeError):
        msg = str(exc)
        return any(h in msg for h in _AUTH_ERR_HINTS)
    return False


class _RetryingClient:
    """duck-type PromptQLClient：包装 stream_thread，认证失败时标记账号并抛 503。

    v1 选型：流式已 yield 部分内容后重试会重复输出，故不自动重试同请求；
    抛 503 让客户端重试即可在下一次请求换号。
    """

    def __init__(self, pool: AccountPool, account: Account, underlying: PromptQLClient) -> None:
        self._pool = pool
        self._account = account
        self._underlying = underlying

    async def stream_thread(self, *args: Any, **kwargs: Any) -> AsyncIterator[IREvent]:
        """透传底层 client.stream_thread；认证失败则标记账号并抛 503。"""
        try:
            async for ev in self._underlying.stream_thread(*args, **kwargs):
                yield ev
        except Exception as e:  # noqa: BLE001
            if _is_auth_failure(e):
                self._pool.mark_disabled(self._account)
                raise HTTPException(
                    status_code=503,
                    detail="promptql account auth failed; retry request to pick another account",
                ) from e
            raise


def get_client(request: Request) -> _RetryingClient:
    """round-robin 取一个账号，返回其 _RetryingClient 包装。"""
    st = request.app.state
    pool: AccountPool = st.pool
    clients: dict[str, PromptQLClient] = st.clients
    acc = pool.next()
    return _RetryingClient(pool, acc, clients[acc.name])


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def verify_api_key(request: Request,
                   cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    settings: Settings = request.app.state.settings
    if not settings.gateway_api_key:
        return
    if cred is None or cred.credentials != settings.gateway_api_key:
        raise HTTPException(status_code=401, detail="invalid api key")
