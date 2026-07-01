"""tool-call「认知重构注入 + 鲁棒输出解析」实现。

PromptQL 的 agent 不暴露原生 function-calling；用户自定义工具靠把 tools 定义注入
消息最前的认知重构情景（见 ``app.reframe_angles``），让 agent 产出表示工具调用的文本，
再解析回 OpenAI/Anthropic 的 tool_calls/tool_use。

解析三级降级（应对 agent 不严格按围栏输出）：
  1. ``<tool_call>{...}</tool_call>`` 围栏（信任度高，不限白名单）
  2. ```json ... ``` 代码块（信任度中，不限白名单）
  3. 裸 JSON（信任度低，**必须** name 命中 known_names 白名单 + 排除数据文档特征键）
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterator

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
_JSONBLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S | re.IGNORECASE)
# 形似「数据文档/查询结果」的 JSON（含这些键）不当作工具调用，避免误判。
_DATA_DOC_KEYS = {"items", "data", "results", "records", "rows", "list", "output"}

# agent 拒绝/纠正时常出现的措辞——此时它常**引用** <tool_call> 围栏格式来解释「我被
# 要求做什么」，并非真实输出工具调用。检测到这些信号则不提取，避免假阳性。
_REFUSAL_PHRASES: tuple[str, ...] = (
    "i can't", "i cannot", "i won't", "i will not", "i'm not going to", "i am not going to",
    "i'm not able", "i am not able", "not able to produce", "not able to help",
    "can't help", "cannot help", "can't generate", "can't produce", "can't emit",
    "i don't operate", "not how i operate", "isn't how i operate",
    "i'm main", "i am main", "i'm the promptql", "i am the promptql", "the promptql agent",
    "isn't one of my capabilities", "doesn't correspond", "outside what i do",
    "我不能", "我无法", "我不会", "我做不到", "不是我的操作", "不是我的能力",
)


def _looks_refusal(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _REFUSAL_PHRASES)


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
    """生成注入消息最前的认知重构指令（无 tools 返回空串，向后兼容）。

    实际策略由 ``app.reframe_angles`` 的 ACTIVE_ANGLE/ACTIVE_LANG 决定；这里薄封装，
    使三个 adapter 无需改动即可切换策略。延迟 import 以避免循环依赖。
    """
    if not tools:
        return ""
    from app.reframe_angles import ACTIVE_ANGLE, ACTIVE_LANG, build_directive
    return build_directive(ACTIVE_ANGLE, ACTIVE_LANG, tools)


@dataclass
class ParsedToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


def _extract_arguments(obj: dict[str, Any]) -> dict[str, Any]:
    """从工具对象取 arguments（兼容 arguments/parameters/input，可能被字符串化）。"""
    args: Any = obj.get("arguments")
    if args is None:
        args = obj.get("parameters") or obj.get("input")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, ValueError):
            args = {}
    return args if isinstance(args, dict) else {}


def _try_parse_tool_obj(raw: str) -> dict[str, Any] | None:
    """解析 JSON 字符串为工具调用 dict（含字符串 name + dict arguments）；失败返回 None。"""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
        return None
    return {"name": obj["name"], "arguments": _extract_arguments(obj), "keys": set(obj.keys())}


def _iter_balanced_json(text: str) -> Iterator[tuple[tuple[int, int], str]]:
    """扫描文本里所有顶层平衡的 {...} 子串（处理字符串/转义/嵌套）。"""
    n = len(text)
    for i in range(n):
        if text[i] != "{":
            continue
        depth = 0
        in_str = False
        esc = False
        for j in range(i, n):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield ((i, j + 1), text[i:j + 1])
                    break


def _overlaps(s: int, e: int, spans: set[tuple[int, int]]) -> bool:
    return any(not (e <= a or s >= b) for a, b in spans)


def parse_tool_calls(text: str, known_names: set[str] | None = None) -> list[ParsedToolCall]:
    """从模型回复文本提取工具调用（三级降级）。

    - 若文本含拒绝/身份声明（agent 拒绝时常引用围栏格式作说明），返回空，避免假阳性。
    - 围栏 / markdown json 块：信任度高，不限白名单（保旧测试通过）。
    - 裸 JSON：仅当传入 ``known_names`` 且 name 命中白名单、非数据文档、长度 ≤600 时才采纳。
    - 同名同参数的重复调用去重。
    """
    if _looks_refusal(text):
        return []
    calls: list[ParsedToolCall] = []
    spans: set[tuple[int, int]] = set()
    seen_keys: set[tuple[str, str]] = set()

    def add(obj: dict[str, Any], span: tuple[int, int]) -> None:
        if _overlaps(span[0], span[1], spans):
            return
        key = (obj["name"], json.dumps(obj["arguments"], sort_keys=True, ensure_ascii=False))
        if key in seen_keys:
            return
        seen_keys.add(key)
        spans.add(span)
        calls.append(ParsedToolCall(
            id=f"call_{uuid.uuid4().hex[:24]}", name=obj["name"], arguments=obj["arguments"]))

    for m in TOOL_CALL_RE.finditer(text):  # 1. 围栏
        obj = _try_parse_tool_obj(m.group(1))
        if obj:
            add(obj, (m.start(), m.end()))

    for m in _JSONBLOCK_RE.finditer(text):  # 2. markdown json 块
        obj = _try_parse_tool_obj(m.group(1))
        if obj:
            add(obj, (m.start(), m.end()))

    if known_names:  # 3. 裸 JSON（白名单兜底）
        for span, sub in _iter_balanced_json(text):
            if len(sub) > 600 or _overlaps(span[0], span[1], spans):
                continue
            obj = _try_parse_tool_obj(sub)
            if not obj or obj["name"] not in known_names or (obj["keys"] & _DATA_DOC_KEYS):
                continue
            add(obj, span)

    return calls


def strip_tool_calls(text: str) -> str:
    """把 ``<tool_call>`` 块从文本移除，返回纯文本部分。"""
    return TOOL_CALL_RE.sub("", text).strip()


def new_tool_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"
