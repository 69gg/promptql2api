"""PromptQL GraphQL 客户端：建 thread、发消息、轮询事件流。

对外提供异步生成器 stream_thread() —— 一次性「创建 thread + 发首条消息 + 轮询回复」，
逐个产出归一后的 IREvent，adapter 直接消费。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from app.account import Account
from app.config import Settings
from app.promptql.auth import AuthManager
from app.promptql.events import IREvent, parse_thread_event

_START_THREAD = (
    "mutation StartThread($projectId: String!, $message: String!, $timezone: String!,"
    "  $roomless: Boolean, $agentResponseConfig: String, $llmConfigId: String) {"
    "  start_thread(projectId: $projectId, message: $message, timezone: $timezone,"
    "    roomless: $roomless, agentResponseConfig: $agentResponseConfig, llmConfigId: $llmConfigId) {"
    "    thread_id message_id title created_at"
    "    thread_events { thread_event_id event_data created_at }"
    "  }"
    "}"
)

_SEND_MESSAGE = (
    "mutation SendThreadMessage($threadId: String!, $message: String!, $timezone: String!,"
    "  $agentResponseConfig: String) {"
    "  send_thread_message(threadId: $threadId, message: $message, timezone: $timezone,"
    "    agentResponseConfig: $agentResponseConfig)"
    "  { thread_event_id event_data created_at }"
    "}"
)

_QUERY_EVENTS = (
    "query QueryThreadEvents($thread_id: uuid!, $after_event_id: bigint!) {"
    "  thread_events(where: {thread_id: {_eq: $thread_id}, thread_event_id: {_gt: $after_event_id}},"
    "    order_by: {thread_event_id: asc}) { thread_event_id event_data created_at }"
    "}"
)


@dataclass
class StartResult:
    thread_id: str
    first_event_id: int  # UserMessage 的 event id（轮询从 > 它开始）


class GraphQLError(RuntimeError):
    pass


class PromptQLClient:
    def __init__(self, account: Account, settings: Settings, client: httpx.AsyncClient,
                 auth: AuthManager) -> None:
        self._account = account
        self._s = settings
        self._client = client
        self._auth = auth

    async def _gql(self, query: str, variables: dict[str, Any], op: str) -> dict[str, Any]:
        bearer = await self._auth.get_bearer()
        r = await self._client.post(
            self._s.graphql_url,
            json={"query": query, "operationName": op, "variables": variables},
            headers={"authorization": f"Bearer {bearer}"},
        )
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise GraphQLError(f"{op} failed: {body['errors']}")
        return body["data"]

    async def start_thread(self, message: str, llm_config_id: str | None = None) -> StartResult:
        data = await self._gql(_START_THREAD, {
            "projectId": self._account.project_id,
            "message": message,
            "timezone": self._s.timezone,
            "roomless": True,
            "agentResponseConfig": self._s.agent_response_config or None,
            "llmConfigId": llm_config_id,
        }, "StartThread")
        st = data["start_thread"]
        events = st.get("thread_events") or []
        first_id = int(events[-1]["thread_event_id"]) if events else 0
        return StartResult(thread_id=st["thread_id"], first_event_id=first_id)

    async def query_events(self, thread_id: str, after_event_id: int) -> list[dict[str, Any]]:
        data = await self._gql(_QUERY_EVENTS, {
            "thread_id": thread_id, "after_event_id": str(after_event_id),
        }, "QueryThreadEvents")
        return data["thread_events"]

    async def stream_thread(
        self, message: str, llm_config_id: str | None = None, *,
        timeout: float | None = None,
    ) -> AsyncIterator[IREvent]:
        """创建 thread + 发消息 + 轮询回复，逐个 yield IREvent，直到收到 finish 或超时。"""
        started = await self.start_thread(message, llm_config_id)
        after = started.first_event_id
        deadline = time.time() + (timeout if timeout is not None else self._s.request_timeout)
        finished = False
        while time.time() < deadline and not finished:
            try:
                events = await self.query_events(started.thread_id, after)
            except Exception as e:  # noqa: BLE001
                yield IREvent(kind="error", error=f"query events failed: {e}")
                return
            for e in events:
                after = int(e["thread_event_id"])
                for ir in parse_thread_event(e["event_data"]):
                    yield ir
                    if ir.kind == "finish":
                        finished = True
            if not events:
                await asyncio.sleep(self._s.poll_interval)
        if not finished:
            yield IREvent(kind="finish", finish_reason="stop")
