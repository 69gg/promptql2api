"""token 计数：优先用 PromptQL event 的 usage；兜底 tiktoken 估算。"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.promptql.events import Usage

# 模型名 → tiktoken encoding 名的粗映射（PromptQL 默认 claude-opus-4-8，tiktoken 无对应，
# 用 cl100k_base 作合理近似）。
_MODEL_ENCODING = {
    "gpt-4": "cl100k_base",
    "gpt-3.5-turbo": "cl100k_base",
    "claude-opus-4-8": "cl100k_base",
    "claude-sonnet-5": "cl100k_base",
    "claude": "cl100k_base",
}


@lru_cache(maxsize=8)
def _get_encoding(name: str):  # type: ignore[no-untyped-def]
    import tiktoken
    try:
        return tiktoken.get_encoding(name)
    except Exception:  # noqa: BLE001
        return tiktoken.get_encoding("cl100k_base")


def estimate_tokens(text: str, model: str | None = None) -> int:
    """用 tiktoken 估算文本 token 数（近似）。"""
    if not text:
        return 0
    enc_name = "cl100k_base"
    if model:
        for k, v in _MODEL_ENCODING.items():
            if model.startswith(k):
                enc_name = v
                break
    enc = _get_encoding(enc_name)
    return len(enc.encode(text))


def first_usage(parts: list[Usage | None]) -> Usage:
    """取第一个非零 usage。

    PromptQL agent 一次问答可能跑多轮 llm_response（每轮重读全上下文，input_tokens 含
    大量缓存命中），累加会重复计算系统提示词。取第一轮（final_response 那轮）最接近
    用户感知的单次调用用量。
    """
    for u in parts:
        if u and (u.input_tokens or u.output_tokens):
            return u
    return Usage()


def sum_usage(parts: list[Usage | None]) -> Usage:
    """累加所有 usage（output_tokens 计全量；input 取首次即可，见 first_usage）。"""
    total = Usage()
    for u in parts:
        if u:
            total.add(u)
            if u.model and not total.model:
                total.model = u.model
            if u.provider and not total.provider:
                total.provider = u.provider
    return total


def messages_to_prompt(messages: list[dict[str, Any]]) -> str:
    """把 OpenAI/Anthropic 风格 messages 拍平成单条文本（用于 tiktoken 估算 & 注入 PromptQL）。"""
    parts: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                (c.get("text") if isinstance(c, dict) else str(c)) for c in content if c
            )
        else:
            text = str(content)
        parts.append(f"[{role}]\n{text}")
    return "\n\n".join(parts)
