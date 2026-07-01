"""adapter 公共工具：messages 归一化、model 映射、模型列表。"""
from __future__ import annotations

import json
from typing import Any

DEFAULT_MODEL = "claude-opus-4-8"

# 对外暴露的模型列表（PromptQL 默认 Opus 4.8；可按需扩展）
SUPPORTED_MODELS: list[dict[str, Any]] = [
    {"id": "claude-opus-4-8", "object": "model", "created": 1717200000, "owned_by": "anthropic"},
    {"id": "claude-sonnet-5", "object": "model", "created": 1717200000, "owned_by": "anthropic"},
    {"id": "claude-haiku-4-5", "object": "model", "created": 1717200000, "owned_by": "anthropic"},
]


def normalize_model(model: str | None) -> str:
    """客户端传的 model 归一化；空或未知→默认。"""
    if not model:
        return DEFAULT_MODEL
    return model


def flatten_text(content: Any) -> str:
    """OpenAI/Anthropic content（str 或 content block 数组）→ 纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") in ("text", "input_text", "output_text"):
                    out.append(c.get("text", ""))
                elif "text" in c:
                    out.append(str(c["text"]))
            else:
                out.append(str(c))
        return "".join(out)
    return str(content)


def _assistant_tool_call_jsons(m: dict[str, Any]) -> list[str]:
    """提取 assistant 消息里的工具调用（兼容 OpenAI tool_calls 与 Anthropic tool_use block），
    返回每个调用的 JSON 字符串（{"name":..., "arguments":...}）。

    PromptQL 的 agent 识别「自己之前输出过的 <tool_call> 围栏」并强模仿（few-shot 效应），
    所以把历史 tool_call 渲染成围栏送过去，比丢弃显著提高后续工具调用成功率。
    """
    blocks: list[str] = []
    for tc in (m.get("tool_calls") or []):  # OpenAI
        fn = (tc or {}).get("function") or {}
        raw = fn.get("arguments", "{}")
        try:
            args = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (json.JSONDecodeError, ValueError):
            args = {}
        blocks.append(json.dumps({"name": fn.get("name", ""), "arguments": args}, ensure_ascii=False))
    content = m.get("content")
    if isinstance(content, list):  # Anthropic tool_use blocks
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                blocks.append(json.dumps(
                    {"name": c.get("name", ""), "arguments": c.get("input") or {}}, ensure_ascii=False))
    return blocks


def extract_user_prompt(messages: list[dict[str, Any]]) -> str:
    """把 messages 拍平成发给 PromptQL 的单条用户消息（带角色与 system 前缀）。

    PromptQL 的 thread 是一次性的（每次请求新建），所以把整段历史压成一条消息。
    assistant 的历史工具调用渲染成 <tool_call> 围栏（few-shot），提高后续工具调用成功率。
    """
    parts: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        if role == "system":
            parts.append(f"[system]\n{flatten_text(m.get('content'))}")
        elif role == "assistant":
            body = flatten_text(m.get("content"))
            tc_jsons = _assistant_tool_call_jsons(m)
            if tc_jsons:
                fence = "\n".join(f"<tool_call>{b}</tool_call>" for b in tc_jsons)
                body = f"{body}\n{fence}".strip() if body else fence
            parts.append(f"[assistant]\n{body}")
        elif role == "tool":
            parts.append(f"[tool_result {m.get('tool_call_id','')}]\n{flatten_text(m.get('content'))}")
        else:
            parts.append(f"[user]\n{flatten_text(m.get('content'))}")
    return "\n\n".join(parts)
