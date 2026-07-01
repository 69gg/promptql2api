"""共享 FastAPI 依赖：注入 PromptQLClient + API key 校验。"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings
from app.promptql.client import PromptQLClient

_bearer = HTTPBearer(auto_error=False)


def get_client(request: Request) -> PromptQLClient:
    return request.app.state.client  # type: ignore[no-any-return]


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def verify_api_key(request: Request,
                   cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    settings: Settings = request.app.state.settings
    if not settings.gateway_api_key:
        return
    if cred is None or cred.credentials != settings.gateway_api_key:
        raise HTTPException(status_code=401, detail="invalid api key")
