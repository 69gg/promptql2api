"""FastAPI 入口：组装 PromptQL 客户端 + 挂载 OpenAI/Anthropic 兼容路由。"""
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.adapters.anthropic_messages import router as anthropic_router
from app.adapters.openai_chat import router as openai_chat_router
from app.adapters.openai_models import router as openai_models_router
from app.adapters.openai_responses import router as openai_responses_router
from app.config import get_settings
from app.promptql.auth import AuthManager
from app.promptql.client import PromptQLClient


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    http_client = httpx.AsyncClient(timeout=settings.request_timeout)
    auth = AuthManager(settings, http_client)
    client = PromptQLClient(settings, http_client, auth)
    app.state.settings = settings
    app.state.http_client = http_client
    app.state.client = client
    try:
        yield
    finally:
        await http_client.aclose()


app = FastAPI(title="promptql2api", version="0.1.0", lifespan=lifespan)

app.include_router(openai_models_router)
app.include_router(openai_chat_router)
app.include_router(openai_responses_router)
app.include_router(anthropic_router)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}
