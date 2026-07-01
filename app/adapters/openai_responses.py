"""OpenAI /v1/responses 兼容接口（typed SSE events + tool calls）。"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.adapters import extract_user_prompt, normalize_model
from app.promptql.client import PromptQLClient
from app.tools import ToolDef, build_tool_directive, parse_tool_calls
from app.tokens import estimate_tokens, first_usage

from app.deps import get_client

router = APIRouter()


class ResponseInputItem(BaseModel):
    role: str
    content: Any = None
    model_config = {"extra": "allow"}


class ResponsesRequest(BaseModel):
    model: str | None = None
    input: Any = None
    instructions: str | None = None
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    model_config = {"extra": "ignore"}


def _resp_id() -> str:
    return f"resp_{uuid.uuid4().hex[:24]}"


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _input_to_messages(inp: Any, instructions: str | None) -> list[dict[str, Any]]:
    """把 Responses 的 input 归一成 [{role,content}]。input 可以是 str 或 messages 数组。"""
    if isinstance(inp, str):
        msgs: list[dict[str, Any]] = [{"role": "user", "content": inp}]
    elif isinstance(inp, list):
        msgs = []
        for it in inp:
            if isinstance(it, dict):
                msgs.append({"role": it.get("role", "user"),
                             "content": it.get("content", it)})
            else:
                msgs.append({"role": "user", "content": str(it)})
    else:
        msgs = [{"role": "user", "content": str(inp)}]
    if instructions:
        msgs = [{"role": "system", "content": instructions}, *msgs]
    return msgs


def _build_prompt(req: ResponsesRequest) -> tuple[str, list[ToolDef]]:
    tools = [ToolDef.from_openai(t) for t in (req.tools or [])]  # responses tools 与 chat 同构
    msgs = _input_to_messages(req.input, req.instructions)
    prompt = extract_user_prompt(msgs)
    if tools:
        prompt = prompt + build_tool_directive(tools)
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


async def _gen_stream(client: PromptQLClient, prompt: str, tools: list[ToolDef],
                      model: str) -> AsyncIterator[bytes]:
    rid = _resp_id()
    created = int(time.time())

    yield _sse("response.created", {
        "type": "response.created",
        "response": {"id": rid, "object": "response", "created_at": created, "model": model,
                     "status": "in_progress", "output": []},
    })

    parts: list[str] = []
    usages = []
    async for ir in client.stream_thread(prompt):
        if ir.kind == "error":
            yield _sse("error", {"type": "error", "message": ir.error or "unknown"})
            return
        if ir.kind == "text" and ir.text:
            parts.append(ir.text)
            yield _sse("response.output_text.delta", {
                "type": "response.output_text.delta", "delta": ir.text,
            })
        if ir.usage_delta:
            usages.append(ir.usage_delta)
        if ir.kind == "finish":
            break

    full_text = "".join(parts)
    output: list[dict[str, Any]] = [{
        "type": "message", "id": f"msg_{uuid.uuid4().hex[:24]}",
        "status": "completed", "role": "assistant",
        "content": [{"type": "output_text", "text": full_text}],
    }]
    status = "completed"

    if tools:
        calls = parse_tool_calls(full_text)
        if calls:
            status = "completed"
            for c in calls:
                yield _sse("response.function_call_arguments.delta", {
                    "type": "response.function_call_arguments.delta",
                    "output_index": len(output), "delta": json.dumps(c.arguments, ensure_ascii=False),
                })
                output.append({
                    "type": "function_call", "id": c.id, "call_id": c.id,
                    "name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False),
                    "status": "completed",
                })

    u = first_usage(usages)
    usage = {"input_tokens": u.input_tokens or estimate_tokens(prompt),
             "output_tokens": u.output_tokens or estimate_tokens(full_text)}
    yield _sse("response.completed", {
        "type": "response.completed",
        "response": {"id": rid, "object": "response", "created_at": created, "model": model,
                     "status": status, "output": output, "usage": usage},
    })


@router.post("/v1/responses")
async def responses(req: ResponsesRequest, client: PromptQLClient = Depends(get_client)) -> Any:
    model = normalize_model(req.model)
    prompt, tools = _build_prompt(req)
    if req.stream:
        return StreamingResponse(_gen_stream(client, prompt, tools, model),
                                 media_type="text/event-stream")

    full_text, usages = await _collect(client, prompt)
    output: list[dict[str, Any]] = [{
        "type": "message", "id": f"msg_{uuid.uuid4().hex[:24]}",
        "status": "completed", "role": "assistant",
        "content": [{"type": "output_text", "text": full_text}],
    }]
    if tools:
        calls = parse_tool_calls(full_text)
        if calls:
            output = [{"type": "function_call", "id": c.id, "call_id": c.id,
                       "name": c.name,
                       "arguments": json.dumps(c.arguments, ensure_ascii=False),
                       "status": "completed"} for c in calls]
    u = first_usage(usages)
    return {
        "id": _resp_id(), "object": "response", "created_at": int(time.time()),
        "model": model, "status": "completed", "output": output,
        "usage": {"input_tokens": u.input_tokens or estimate_tokens(prompt),
                  "output_tokens": u.output_tokens or estimate_tokens(full_text)},
    }
