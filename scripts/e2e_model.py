"""验证 model 参数 → llmConfigId 生效：对比不同模型请求的 llm_response.usage.model。

直连 PromptQLClient（不经网关），发同一条消息给不同模型，抓 usage.model 确认切换生效。
用法：uv run python scripts/e2e_model.py
"""
from __future__ import annotations

import asyncio
import time

import httpx

from app.adapters import llm_config_id_for, normalize_model
from app.config import get_settings
from app.promptql.auth import AuthManager
from app.promptql.client import PromptQLClient
from app.promptql.events import parse_thread_event


async def model_usage(client: PromptQLClient, llm_cid: str | None) -> str | None:
    started = await client.start_thread("Reply with exactly one word: PONG", llm_config_id=llm_cid)
    after = started.first_event_id
    deadline = time.time() + 90
    model: str | None = None
    finished = False
    while time.time() < deadline and not finished:
        events = await client.query_events(started.thread_id, after)
        for e in events:
            after = int(e["thread_event_id"])
            for ir in parse_thread_event(e["event_data"]):
                if ir.usage_delta and ir.usage_delta.model:
                    model = ir.usage_delta.model
                if ir.kind == "finish":
                    finished = True
        if not events:
            await asyncio.sleep(1.2)
    return model


async def main() -> None:
    s = get_settings()
    async with httpx.AsyncClient(timeout=180, cookies=s.auth_cookies) as c:
        auth = AuthManager(s, c)
        client = PromptQLClient(s, c, auth)
        for mid in ["claude-sonnet-4-5", "glm-5-2", "gpt-5-5", "claude-opus-4-8"]:
            llm_cid = llm_config_id_for(normalize_model(mid))
            try:
                m = await model_usage(client, llm_cid)
                print(f"{mid:22} llmConfigId={llm_cid}  →  usage.model = {m}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"{mid:22} ERROR: {e}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
