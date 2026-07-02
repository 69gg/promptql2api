"""管理后台端点（/admin/*）。

提供账号池的查看、上传、删除与重载功能，独立使用 ``[admin].auth_key`` 鉴权。
鉴权方式（二选一）：

- Header ``Authorization: Bearer <admin_auth_key>``
- Query param ``?auth_key=<admin_auth_key>``

``admin_auth_key`` 留空时，所有 /admin/* 端点返回 404（关闭状态，不暴露端点存在）。
"""
from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.account import Account, AccountPool
from app.config import Settings
from app.promptql.auth import AuthManager
from app.promptql.client import PromptQLClient

_bearer = HTTPBearer(auto_error=False)
router = APIRouter(prefix="/admin")


def _get_admin_key(request: Request) -> str:
    """从 app.state 读取 admin auth key。"""
    settings: Settings = request.app.state.settings
    return getattr(settings, "admin_auth_key", "")


def verify_admin_key(
    request: Request,
    auth_key: str | None = Query(None, description="管理后台鉴权 key（query 方式）"),
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """校验 admin auth key；未配置时隐藏端点，错误时返回 401。"""
    key = _get_admin_key(request)
    if not key:
        raise HTTPException(status_code=404, detail="admin endpoints disabled")
    provided = ""
    if cred is not None and cred.scheme.lower() == "bearer":
        provided = cred.credentials
    if not provided and auth_key:
        provided = auth_key
    if provided != key:
        raise HTTPException(status_code=401, detail="invalid admin auth key")


def _pool(request: Request) -> AccountPool:
    return request.app.state.pool  # type: ignore[no-any-return]


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client  # type: ignore[no-any-return]


def _clients(request: Request) -> dict[str, PromptQLClient]:
    return request.app.state.clients  # type: ignore[no-any-return]


def _sync_client(
    clients: dict[str, PromptQLClient],
    acc: Account,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> None:
    """为新增/更新的账号构造 PromptQLClient 并注入 clients 字典。"""
    auth = AuthManager(acc, settings, http_client)
    clients[acc.name] = PromptQLClient(acc, settings, http_client, auth)


def _rebuild_clients(
    pool: AccountPool,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> dict[str, PromptQLClient]:
    """根据当前 pool 重建全部 PromptQLClient。"""
    clients: dict[str, PromptQLClient] = {}
    for acc in pool.all():
        _sync_client(clients, acc, settings, http_client)
    return clients


@router.get("/accounts")
async def list_accounts(
    request: Request,
    _: None = Depends(verify_admin_key),
) -> dict[str, Any]:
    """列出账号摘要，不暴露 hasura_lux/project_id 等敏感字段。"""
    pool = _pool(request)
    data = [
        {
            "name": a.name,
            "source_email": a.source_email,
            "project_name": a.project_name,
            "created_at": a.created_at,
            "disabled": a.disabled,
        }
        for a in pool.all()
    ]
    return {"object": "list", "data": data}


@router.get("/accounts/{name}")
async def get_account(
    request: Request,
    name: str,
    _: None = Depends(verify_admin_key),
) -> Account:
    """获取单个账号完整信息。"""
    pool = _pool(request)
    for acc in pool.all():
        if acc.name == name:
            return acc
    raise HTTPException(status_code=404, detail=f"account not found: {name}")


@router.post("/accounts")
async def create_or_update_account(
    request: Request,
    account: Account,
    _: None = Depends(verify_admin_key),
) -> Account:
    """上传/新增账号，持久化到 account/<name>.json 并同步内存账号池与 client 缓存。"""
    pool = _pool(request)
    settings = _settings(request)
    http_client = _http_client(request)
    clients = _clients(request)

    pool.add_or_update(account)
    _sync_client(clients, account, settings, http_client)
    return account


@router.delete("/accounts/{name}")
async def delete_account(
    request: Request,
    name: str,
    _: None = Depends(verify_admin_key),
) -> dict[str, Any]:
    """删除指定账号。"""
    pool = _pool(request)
    clients = _clients(request)

    if not pool.remove(name):
        raise HTTPException(status_code=404, detail=f"account not found: {name}")
    clients.pop(name, None)
    return {"deleted": True, "name": name}


@router.post("/reload")
async def reload_accounts(
    request: Request,
    _: None = Depends(verify_admin_key),
) -> dict[str, Any]:
    """重新从磁盘加载账号池，并重建 client 缓存。"""
    pool = _pool(request)
    settings = _settings(request)
    http_client = _http_client(request)

    pool.reload()
    request.app.state.clients = _rebuild_clients(pool, settings, http_client)
    return {"reloaded": True, "count": len(pool.all())}
