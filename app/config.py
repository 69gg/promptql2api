"""网关配置：从 config.toml 加载（pydantic.BaseModel + tomllib，无 pydantic-settings）。

config.toml 只放「与账号无关」的配置（网关/行为/端点/注册机）；
每个 PromptQL 账号凭据存 ``account/<name>.json``，由 :mod:`app.account` 管理。
"""
from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel


class Settings(BaseModel):
    """网关行为与端点配置（不含账号凭据）。"""

    # 网关监听
    host: str = "0.0.0.0"
    port: int = 8088
    gateway_api_key: str = ""  # 客户端访问网关用的 key；空则不校验

    # PromptQL 端点（一般无需改）
    auth_token_url: str = "https://auth.pro.ql.app/ddn/promptql/token"
    graphql_url: str = "https://data.prompt.ql.app/promptql/playground-v2-hge/v1/graphql"

    # 行为参数
    timezone: str = "Asia/Shanghai"
    agent_response_config: str = ""  # 空=null(触发 agent)；"force_skip"=不触发
    poll_interval: float = 1.2  # 轮询 QueryThreadEvents 间隔（秒）
    request_timeout: float = 120.0
    token_refresh_margin: int = 300  # JWT 到期前多少秒刷新

    # 账号凭据目录（相对路径以工作目录解析；account/<name>.json，gitignored）
    account_dir: str = "account"


def _flatten_toml(data: dict) -> dict:
    """把 [gateway]/[promptql]/[registry] 三个 section 的字段平铺到一层。

    其余 section（[email]/[turnstile] 等仅注册机用）忽略。
    toml 里的简短键名映射到 Settings 字段（``api_key`` → ``gateway_api_key``）。
    """
    flat: dict = {}
    for section in ("gateway", "promptql", "registry"):
        flat.update(data.get(section, {}))
    # 别名映射：toml 用更短的 api_key，Settings 用语义更清晰的 gateway_api_key
    if "api_key" in flat and "gateway_api_key" not in flat:
        flat["gateway_api_key"] = flat.pop("api_key")
    return flat


@lru_cache(maxsize=8)
def get_settings(path: str | None = None) -> Settings:
    """加载 config.toml 构造 Settings。

    ``path`` 默认 ``$PROMPTQL2API_CONFIG`` 或 ``config.toml``。
    被 :func:`clear_settings_cache` 用于测试重读。
    """
    p = path or os.getenv("PROMPTQL2API_CONFIG", "config.toml")
    fpath = Path(p)
    if not fpath.is_file():
        # 配置文件缺失时回退全默认值（账号仍需 account/*.json）
        return Settings()
    with fpath.open("rb") as f:
        data = tomllib.load(f)
    return Settings(**_flatten_toml(data))


def clear_settings_cache() -> None:
    """清空 get_settings 的 lru_cache，供测试重读配置。"""
    get_settings.cache_clear()
