"""OpenAI /v1/chat/completions 兼容接口（流式 + 非流式 + tool calls + usage）。"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.adapters import extract_user_prompt, llm_config_id_for, normalize_model
from app.promptql.client import PromptQLClient
from app.tools import ToolDef, build_tool_directive, parse_tool_calls
from app.tokens import estimate_tokens, first_usage

from app.deps import get_client

router = APIRouter()


class ChatMessage(BaseModel):
    role: str
    content: Any = None
    name: str | None = None
    tool_call_id: str | None = None

    model_config = {"extra": "allow"}


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    model_config = {"extra": "ignore"}


def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def _now() -> int:
    return int(time.time())


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


def _usage_obj(u, prompt: str, completion: str) -> dict:
    """OpenAI usage：PromptQL usage 优先，否则 tiktoken 估算。"""
    if u.input_tokens or u.output_tokens:
        return {
            "prompt_tokens": u.input_tokens,
            "completion_tokens": u.output_tokens,
            "total_tokens": u.input_tokens + u.output_tokens,
        }
    p, c = estimate_tokens(prompt), estimate_tokens(completion)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _build_prompt(req: ChatCompletionRequest) -> tuple[str, list[ToolDef]]:
    tools = [ToolDef.from_openai(t.get("function", t)) for t in (req.tools or [])]
    prompt = extract_user_prompt([m.model_dump() for m in req.messages])
    directive = build_tool_directive(tools)
    if directive:  # 认知重构情景前置（无 tools 时为空，行为不变）
        prompt = directive + "\n\n" + prompt
    return prompt, tools


async def _collect(client: PromptQLClient, prompt: str, llm_cid: str | None = None) -> tuple[str, list]:
    """驱动 PromptQL，返回 (完整文本, usage 列表)。"""
    parts: list[str] = []
    usages = []
    async for ir in client.stream_thread(prompt, llm_config_id=llm_cid):
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
                      model: str, llm_cid: str | None = None) -> AsyncIterator[bytes]:
    cid, created = _completion_id(), _now()

    def chunk(delta: dict, finish: str | None = None) -> dict:
        return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    yield _sse(chunk({"role": "assistant"}))

    parts: list[str] = []
    usages = []
    async for ir in client.stream_thread(prompt, llm_config_id=llm_cid):
        if ir.kind == "error":
            yield _sse({**chunk({}), "error": {"message": ir.error or "unknown"}})
            return
        if ir.kind == "text" and ir.text:
            parts.append(ir.text)
            yield _sse(chunk({"content": ir.text}))
        # tool(thinking) 事件不透传，保持回复干净
        if ir.usage_delta:
            usages.append(ir.usage_delta)
        if ir.kind == "finish":
            break

    full_text = "".join(parts)
    finish_reason = "stop"
    if tools:
        calls = parse_tool_calls(full_text, known_names={t.name for t in tools})
        if calls:
            finish_reason = "tool_calls"
            for i, c in enumerate(calls):
                yield _sse(chunk({"tool_calls": [{
                    "index": i, "id": c.id, "type": "function",
                    "function": {"name": c.name,
                                 "arguments": json.dumps(c.arguments, ensure_ascii=False)},
                }]}))
    yield _sse(chunk({}, finish=finish_reason) | {
        "usage": _usage_obj(first_usage(usages), prompt, full_text),
    })
    yield b"data: [DONE]\n\n"


@router.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest,
                           client: PromptQLClient = Depends(get_client)) -> Any:
    model = normalize_model(req.model)
    prompt, tools = _build_prompt(req)
    llm_cid = llm_config_id_for(model)

    if req.stream:
        return StreamingResponse(_gen_stream(client, prompt, tools, model, llm_cid),
                                 media_type="text/event-stream")

    full_text, usages = await _collect(client, prompt, llm_cid)
    message: dict[str, Any] = {"role": "assistant", "content": full_text}
    finish_reason = "stop"
    if tools:
        calls = parse_tool_calls(full_text, known_names={t.name for t in tools})
        if calls:
            finish_reason = "tool_calls"
            message["content"] = None
            message["tool_calls"] = [{
                "id": c.id, "type": "function",
                "function": {"name": c.name,
                             "arguments": json.dumps(c.arguments, ensure_ascii=False)},
            } for c in calls]
    return {
        "id": _completion_id(), "object": "chat.completion", "created": _now(), "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": _usage_obj(first_usage(usages), prompt, full_text),
    }
