"""Anthropic /v1/messages 兼容接口（流式 + 非流式 + tool_use + usage）+ /v1/messages/count_tokens。"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.adapters import extract_user_prompt
from app.promptql.client import PromptQLClient
from app.tools import ToolDef, build_tool_directive, new_tool_call_id, parse_tool_calls
from app.tokens import estimate_tokens, first_usage

from app.deps import get_client

router = APIRouter()

MODEL = "claude-opus-4-8"


class AnthropicMessage(BaseModel):
    role: str
    content: Any = None
    model_config = {"extra": "allow"}


class MessagesRequest(BaseModel):
    model: str | None = None
    messages: list[AnthropicMessage]
    system: Any = None
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    max_tokens: int | None = None
    model_config = {"extra": "ignore"}


def _msg_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _build_prompt(req: MessagesRequest) -> tuple[str, list[ToolDef]]:
    tools = [ToolDef.from_anthropic(t) for t in (req.tools or [])]
    msgs = [m.model_dump() for m in req.messages]
    if req.system is not None:
        sys_text = req.system if isinstance(req.system, str) else json.dumps(req.system, ensure_ascii=False)
        msgs = [{"role": "system", "content": sys_text}, *msgs]
    prompt = extract_user_prompt(msgs)
    directive = build_tool_directive(tools)
    if directive:  # 认知重构情景前置（无 tools 时为空，行为不变）
        prompt = directive + "\n\n" + prompt
    return prompt, tools


async def _collect(client: PromptQLClient, prompt: str) -> tuple[str, list]:
    parts: list[str] = []
    usages = []
    async for ir in client.stream_thread(prompt):
        if ir.kind == "error":
            raise HTTPException(status_code=502, detail=ir.error)
        if ir.kind == "text" and ir.text:
            parts.append(ir.text)
        if ir.usage_delta:
            usages.append(ir.usage_delta)
        if ir.kind == "finish":
            break
    return "".join(parts), usages


def _usage_input_output(u, prompt: str, completion: str) -> dict:
    if u.input_tokens or u.output_tokens:
        return {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens}
    return {"input_tokens": estimate_tokens(prompt), "output_tokens": estimate_tokens(completion)}


async def _gen_stream(client: PromptQLClient, prompt: str, tools: list[ToolDef],
                      ) -> AsyncIterator[bytes]:
    mid = _msg_id()

    yield _sse("message_start", {
        "type": "message_start",
        "message": {"id": mid, "type": "message", "role": "assistant",
                    "model": MODEL, "content": [], "stop_reason": None,
                    "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}},
    })
    yield _sse("content_block_start", {
        "type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""},
    })

    parts: list[str] = []
    usages = []
    async for ir in client.stream_thread(prompt):
        if ir.kind == "error":
            yield _sse("error", {"type": "error", "error": {"type": "api_error",
                                                             "message": ir.error or "unknown"}})
            return
        if ir.kind == "text" and ir.text:
            parts.append(ir.text)
            yield _sse("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": ir.text},
            })
        if ir.usage_delta:
            usages.append(ir.usage_delta)
        if ir.kind == "finish":
            break

    yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})

    full_text = "".join(parts)
    stop_reason = "end_turn"
    if tools:
        calls = parse_tool_calls(full_text, known_names={t.name for t in tools})
        if calls:
            stop_reason = "tool_use"
            for i, c in enumerate(calls, start=1):
                yield _sse("content_block_start", {
                    "type": "content_block_start", "index": i,
                    "content_block": {"type": "tool_use", "id": c.id or new_tool_call_id(),
                                      "name": c.name, "input": {}},
                })
                yield _sse("content_block_delta", {
                    "type": "content_block_delta", "index": i,
                    "delta": {"type": "input_json_delta",
                              "partial_json": json.dumps(c.arguments, ensure_ascii=False)},
                })
                yield _sse("content_block_stop", {"type": "content_block_stop", "index": i})

    usage = _usage_input_output(first_usage(usages), prompt, full_text)
    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": usage,
    })
    yield _sse("message_stop", {"type": "message_stop"})


@router.post("/v1/messages")
async def messages(req: MessagesRequest, client: PromptQLClient = Depends(get_client)) -> Any:
    prompt, tools = _build_prompt(req)
    if req.stream:
        return StreamingResponse(_gen_stream(client, prompt, tools),
                                 media_type="text/event-stream")

    full_text, usages = await _collect(client, prompt)
    content: list[dict[str, Any]] = [{"type": "text", "text": full_text}]
    stop_reason = "end_turn"
    if tools:
        calls = parse_tool_calls(full_text, known_names={t.name for t in tools})
        if calls:
            stop_reason = "tool_use"
            content = [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                       for c in calls]
    usage = _usage_input_output(first_usage(usages), prompt, full_text)
    return {
        "id": _msg_id(), "type": "message", "role": "assistant", "model": req.model or MODEL,
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": usage,
    }


@router.post("/v1/messages/count_tokens")
async def count_tokens(req: MessagesRequest) -> dict:
    """token 计数（用 tiktoken 近似，因为这里不调用 PromptQL）。"""
    prompt, _ = _build_prompt(req)
    return {"input_tokens": estimate_tokens(prompt)}
