"""thread_event 解析 → 统一中间表示（IR）。

PromptQL agent 一次问答产生一串 thread_events，每个 event_data 顶层单 key：
- UserMessage：用户消息（忽略，我们已知道内容）
- AgentMessage.update.content：agent 事件流

agent 事件序列（实测）：
  interaction_started → interaction_update(interaction_decision)
  → interaction_update(wiki_selection, 0..n) → interaction_update(main_agent.llm_response)
  → interaction_update(main_agent.actions_parsed) → interaction_update(main_agent.action_completed)
  → interaction_update(turn_completed) → [可能多轮 turn_started/llm_response/...]
  → interaction_finished.completed

adapter 只关心 IR：
- 文本增量（来自 main_agent.llm_response.response_text，或 actions_parsed.final_response.message）
- thinking 增量
- 工具调用（agent 内置工具的展示，如 wiki_selection；可选透传）
- usage（每个 llm_response.usage，累加）
- 终止（interaction_finished）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

# event_data 里要识别的「内容种类」
IRKind = Literal["text", "thinking", "tool", "finish", "error"]


@dataclass
class ToolEvent:
    name: str  # 如 "wiki_selection"
    title: str  # 给前端看的简述
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0
    cache_creation_tokens: int = 0
    model: str | None = None
    provider: str | None = None

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.thinking_tokens += other.thinking_tokens
        self.cached_tokens += other.cached_tokens
        self.cache_creation_tokens += other.cache_creation_tokens


@dataclass
class IREvent:
    """归一后的单个中间事件，供 adapter 转成各家流式格式。"""
    kind: IRKind
    text: str = ""
    thinking: str = ""
    tool: ToolEvent | None = None
    usage_delta: Usage | None = None
    finish_reason: str | None = None  # "stop" / "length" / "tool_use" / ...
    error: str | None = None


_FINAL_RESPONSE_RE = re.compile(r"<final_response>\s*(.*?)\s*</final_response>", re.S)


def _extract_final_message(response_text: str) -> str | None:
    """从 llm_response.response_text 的 <action><final_response>...</final_response></action> 提取正文。"""
    m = _FINAL_RESPONSE_RE.search(response_text)
    return m.group(1) if m else None


def parse_thread_event(event_data: dict[str, Any]) -> list[IREvent]:
    """把单个 thread_event 的 event_data 解析成 0..n 个 IR 事件。

    返回多个是因为一个 AgentMessage 事件里可能同时含 final_response 文本 + usage + 终止。
    """
    out: list[IREvent] = []
    if "UserMessage" in event_data:
        return out  # 用户消息，忽略
    agent = event_data.get("AgentMessage")
    if not isinstance(agent, dict):
        return out

    content = (agent.get("update") or {}).get("content") or {}
    # content 顶层第二个 key（version 之后）即 body 类型
    for key, body in content.items():
        if key == "version":
            continue
        out.extend(_parse_body(key, body))
    return out


def _parse_body(key: str, body: Any) -> list[IREvent]:
    out: list[IREvent] = []
    if key == "interaction_started":
        return out
    if key == "interaction_finished":
        out.append(IREvent(kind="finish", finish_reason="stop"))
        return out
    if key != "interaction_update":
        return out

    update = body or {}
    # main_agent.llm_response / actions_parsed / action_completed / turn_started / turn_completed
    main_agent = update.get("main_agent") or {}
    if "llm_response" in main_agent:
        lr = main_agent["llm_response"] or {}
        usage_raw = lr.get("usage") or {}
        usage = Usage(
            input_tokens=int(usage_raw.get("input_tokens", 0) or 0),
            output_tokens=int(usage_raw.get("output_tokens", 0) or 0),
            thinking_tokens=int(usage_raw.get("thinking_tokens", 0) or 0),
            cached_tokens=int(usage_raw.get("cached_tokens", 0) or 0),
            cache_creation_tokens=int(usage_raw.get("cache_creation_tokens", 0) or 0),
            model=usage_raw.get("model"),
            provider=usage_raw.get("provider"),
        )
        resp_text: str = lr.get("response_text") or ""
        thinking: str = lr.get("thinking_text") or ""
        # 注意：文本由 actions_parsed 提供（更干净，且只出现一次）；
        # llm_response 这里只产出 thinking + usage，不重复产 text。
        out.append(IREvent(kind="thinking", thinking=thinking, usage_delta=usage))
        return out

    if "actions_parsed" in main_agent:
        actions = (main_agent["actions_parsed"] or {}).get("actions") or []
        for act in actions:
            if isinstance(act, dict) and "final_response" in act:
                msg = (act["final_response"] or {}).get("message")
                if msg:
                    out.append(IREvent(kind="text", text=msg))
        return out

    if "wiki_selection" in update:
        ws = update["wiki_selection"] or {}
        out.append(IREvent(kind="tool", tool=ToolEvent(
            name="wiki_selection",
            title="检索项目 Wiki",
            detail=ws,
        )))
        return out

    if "interaction_decision" in update:
        return out
    if "action_completed" in main_agent or "turn_completed" in main_agent or "turn_started" in main_agent:
        return out
    return out
