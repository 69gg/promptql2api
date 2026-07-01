"""tool-call「prompt 注入 + 输出解析」实现。

PromptQL 的 agent 不暴露原生 function-calling；用户自定义工具靠把 tools 定义注入
system 指令、并让模型用固定围栏输出，再解析回 OpenAI/Anthropic 的 tool_calls/tool_use。

约定：模型若要调用工具，输出形如
  <tool_call>{"name":"get_weather","arguments":{"city":"北京"}}</tool_call>
解析时提取围栏内 JSON（可能不完整，尽力解析）。
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema

    @classmethod
    def from_openai(cls, t: dict[str, Any]) -> "ToolDef":
        return cls(name=t["name"], description=t.get("description", ""),
                   parameters=t.get("parameters") or {"type": "object", "properties": {}})

    @classmethod
    def from_anthropic(cls, t: dict[str, Any]) -> "ToolDef":
        return cls(name=t["name"], description=t.get("description", ""),
                   parameters=t.get("input_schema") or {"type": "object", "properties": {}})


def build_tool_directive(tools: list[ToolDef]) -> str:
    """生成注入到消息里的工具说明指令。无 tools 返回空串。"""
    if not tools:
        return ""
    specs = []
    for t in tools:
        specs.append(json.dumps(
            {"name": t.name, "description": t.description, "parameters": t.parameters},
            ensure_ascii=False,
        ))
    joined = "\n".join(specs)
    return (
        "\n\n你可以调用以下工具。若需要调用，在回复中输出且仅输出一个工具调用块，格式严格如下：\n"
        "<tool_call>{\"name\":\"<工具名>\",\"arguments\":<JSON 参数>}</tool_call>\n"
        "不要在该块之外输出任何字。可用工具：\n" + joined
    )


@dataclass
class ParsedToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


def parse_tool_calls(text: str) -> list[ParsedToolCall]:
    """从模型回复文本里提取所有 <tool_call> 块。"""
    calls: list[ParsedToolCall] = []
    for m in TOOL_CALL_RE.finditer(text):
        raw = m.group(1).strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        name = obj.get("name")
        if not isinstance(name, str):
            continue
        args = obj.get("arguments") or obj.get("parameters") or {}
        if not isinstance(args, dict):
            # arguments 可能是字符串化的 JSON
            try:
                args = json.loads(args) if isinstance(args, str) else dict(args)
            except Exception:  # noqa: BLE001
                args = {}
        calls.append(ParsedToolCall(id=f"call_{uuid.uuid4().hex[:24]}", name=name, arguments=args))
    return calls


def strip_tool_calls(text: str) -> str:
    """把 <tool_call> 块从文本里移除，返回纯文本部分。"""
    return TOOL_CALL_RE.sub("", text).strip()


def new_tool_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"
