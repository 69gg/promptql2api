"""OpenAI /v1/models 兼容接口。"""
from __future__ import annotations

from fastapi import APIRouter

from app.adapters import SUPPORTED_MODELS

router = APIRouter()


@router.get("/v1/models")
async def list_models() -> dict:
    return {"object": "list", "data": SUPPORTED_MODELS}
