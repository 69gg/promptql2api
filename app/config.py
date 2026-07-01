"""网关配置。"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # PromptQL 凭据
    hasura_lux: str = Field(..., alias="HASURA_LUX", description="auth.pro.ql.app 的 hasura-lux cookie 值")
    project_id: str = Field(..., alias="PROJECT_ID", description="项目 uuid（短横线格式）")
    project_name: str = Field("", alias="PROJECT_NAME", description="项目名 p-<id>")
    timezone: str = Field("Asia/Shanghai", alias="TIMEZONE")

    # 网关
    host: str = Field("0.0.0.0", alias="HOST")
    port: int = Field(8088, alias="PORT")
    gateway_api_key: str = Field("", alias="GATEWAY_API_KEY", description="客户端访问网关用的 key；空则不校验")

    # PromptQL 端点（一般无需改）
    auth_token_url: str = Field("https://auth.pro.ql.app/ddn/promptql/token", alias="AUTH_TOKEN_URL")
    graphql_url: str = Field(
        "https://data.prompt.ql.app/promptql/playground-v2-hge/v1/graphql", alias="GRAPHQL_URL"
    )

    # 行为
    agent_response_config: str = Field(
        "", alias="AGENT_RESPONSE_CONFIG",
        description="传给 start_thread/send_thread_message 的 agentResponseConfig；空=null(触发 agent)",
    )
    poll_interval: float = Field(1.2, alias="POLL_INTERVAL", description="轮询 QueryThreadEvents 间隔(秒)")
    request_timeout: float = Field(120.0, alias="REQUEST_TIMEOUT")
    token_refresh_margin: int = Field(300, alias="TOKEN_REFRESH_MARGIN", description="JWT 到期前多少秒刷新")

    @property
    def auth_cookies(self) -> dict[str, str]:
        return {"hasura-lux": self.hasura_lux}


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
