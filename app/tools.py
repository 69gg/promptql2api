"""tool-call「认知重构注入 + 鲁棒输出解析」实现。

PromptQL 的 agent 不暴露原生 function-calling；用户自定义工具靠把 tools 定义注入
消息最前的认知重构情景（见 :mod:`app.reframe_angles`），让 agent 产出表示工具调用的文本，
再解析回 OpenAI/Anthropic 的 tool_calls/tool_use。

解析多级降级（应对 agent 不严格按围栏输出）：
  1. ``<tool_call>{...}</tool_call>`` 围栏 —— 用平衡 JSON 扫描提取（JSON-aware），
     不受 content 内 ``}`` / ``</tool_call>`` 字面量干扰（大 content 场景关键）。
  2. ```` ```json ... ``` ```` 代码块（信任度中，不限白名单）。
  3. 裸 JSON（信任度低，**必须** name 命中 known_names 白名单 + 排除数据文档特征键）。

所有 JSON 解析走 :func:`tolerant_parse`（容错：字符串内控制字符、未闭合括号、尾逗号），
字段名兼容 ``name|tool`` / ``arguments|parameters|input``。拒绝/识破检测见 :mod:`app.refusal`。
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterator

from app.refusal import looks_refusal

_OPEN_FENCE_RE = re.compile(r"<tool_call>", re.IGNORECASE)
_CLOSE_FENCE_TAIL_RE = re.compile(r"\s*</tool_call>", re.IGNORECASE)
_JSONBLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S | re.IGNORECASE)
# 形似「数据文档/查询结果」的 JSON（含这些键）不当作工具调用，避免误判。
_DATA_DOC_KEYS = {"items", "data", "results", "records", "rows", "list", "output"}


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

    实际策略由 :mod:`app.reframe_angles` 的 ACTIVE_ANGLE/ACTIVE_LANG 决定；这里薄封装，
    使 adapter 无需改动即可切换策略。延迟 import 以避免循环依赖。
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


def tolerant_parse(s: str) -> Any:
    """容错 JSON 解析：直接 parse 失败则修复后重试，仍失败返回 None。

    修复手段（全部通用、不依赖字段名）：
    - 字符串内的裸控制字符（``\\n``/``\\r``/``\\t``）转义；
    - 字符串未闭合 → 末尾补 ``"``；
    - 未闭合的 ``{``/``[`` → 按嵌套栈从内到外补全；
    - 尾部多余逗号清理。

    应对 agent 偶发产出的大 content / 未转义引号 / 截断 JSON。
    """
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        pass
    fixed: list[str] = []
    in_str = False
    esc = False
    stack: list[str] = []
    for ch in s:
        if in_str:
            if esc:
                esc = False
                fixed.append(ch)
            elif ch == "\\":
                esc = True
                fixed.append(ch)
            elif ch == '"':
                in_str = False
                fixed.append(ch)
            elif ch == "\n":
                fixed.append("\\n")
            elif ch == "\r":
                fixed.append("\\r")
            elif ch == "\t":
                fixed.append("\\t")
            else:
                fixed.append(ch)
        else:
            if ch == '"':
                in_str = True
                fixed.append(ch)
            elif ch == "{":
                stack.append("}")
                fixed.append(ch)
            elif ch == "[":
                stack.append("]")
                fixed.append(ch)
            elif ch in ("}", "]"):
                if stack and stack[-1] == ch:
                    stack.pop()
                fixed.append(ch)
            else:
                fixed.append(ch)
    if in_str:
        fixed.append('"')
    while stack:
        fixed.append(stack.pop())
    candidate = re.sub(r",\s*([}\]])", r"\1", "".join(fixed))
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None


def _try_parse_tool_obj(raw: str) -> dict[str, Any] | None:
    """解析 JSON 字符串为工具调用 dict（字段名兼容 name|tool）；失败返回 None。"""
    obj = tolerant_parse(raw)
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("tool")
    if not isinstance(name, str):
        return None
    return {"name": name, "arguments": _extract_arguments(obj), "keys": set(obj.keys())}


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


def _scan_fenced_body(text: str, start: int) -> tuple[str, int] | None:
    """从 ``start`` 扫描 ``<tool_call>`` 围栏体，返回 ``(raw, body_end)``。

    JSON-aware：字符串内的 ``}`` / ``</tool_call>`` 不计数。遇平衡 JSON 闭合，或字符串外的
    ``</tool_call>``（未闭合体，交 :func:`tolerant_parse` 补全）即停。免疫大 content 内的
    ``}`` / ``</tool_call>`` 字面量提前截断。无任何 ``{`` 则返回 None。
    """
    n = len(text)
    i = start
    while i < n and text[i] in " \t\r\n":  # 跳过前导空白
        i += 1
    body_start = i
    depth = 0
    in_str = False
    esc = False
    saw_brace = False
    while i < n:
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
            saw_brace = True
        elif c == "}":
            depth -= 1
            if saw_brace and depth == 0:
                return text[body_start:i + 1], i + 1  # 平衡 JSON
        elif c == "<" and text.startswith("</tool_call>", i):
            return text[body_start:i], i  # 字符串外闭标签 → 体结束（可能未闭合）
        i += 1
    if saw_brace:  # 到末尾仍无闭标签 / 未平衡
        return text[body_start:], n
    return None


def _iter_fenced(text: str) -> Iterator[tuple[str, tuple[int, int]]]:
    """扫描 ``<tool_call>`` 围栏，提取体内容（JSON-aware），yield ``(raw, span)``。

    ``raw`` 是围栏体（平衡 JSON，或未闭合体——后者由 :func:`tolerant_parse` 补全）；
    ``span`` 覆盖 ``<tool_call> ... </tool_call>``，供提取与 strip 共用。
    """
    for m in _OPEN_FENCE_RE.finditer(text):
        scanned = _scan_fenced_body(text, m.end())
        if scanned is None:
            continue
        raw, body_end = scanned
        cm = _CLOSE_FENCE_TAIL_RE.match(text[body_end:])  # 体后是否紧跟 </tool_call>
        end = body_end + (cm.end() if cm else 0)
        yield raw, (m.start(), end)


def _overlaps(s: int, e: int, spans: set[tuple[int, int]]) -> bool:
    return any(not (e <= a or s >= b) for a, b in spans)


def parse_tool_calls(text: str, known_names: set[str] | None = None) -> list[ParsedToolCall]:
    """从模型回复文本提取工具调用（多级降级）。

    - 若文本含拒绝/身份声明（agent 拒绝时常引用围栏格式作说明），返回空，避免假阳性。
    - 围栏（JSON-aware）/ markdown json 块：信任度高，不限白名单。
    - 裸 JSON：仅当传入 ``known_names`` 且 name 命中白名单、非数据文档、长度 ≤600 时才采纳。
    - 同名同参数的重复调用去重。
    """
    if looks_refusal(text):
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

    for json_sub, span in _iter_fenced(text):  # 1. 围栏（JSON-aware）
        obj = _try_parse_tool_obj(json_sub)
        if obj:
            add(obj, span)

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
    """把 ``<tool_call>`` 围栏块从文本移除，返回纯文本部分（JSON-aware，鲁棒）。"""
    fenced_spans = [span for _, span in _iter_fenced(text)]
    out = text
    for s, e in sorted(fenced_spans, reverse=True):
        out = out[:s] + out[e:]
    # 兜底：清理残留的孤立开/闭标签
    out = re.sub(r"</?tool_call>", "", out, flags=re.IGNORECASE)
    return out.strip()


def new_tool_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"
