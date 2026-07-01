"""tools.py 测试：prompt 注入与 tool_call 解析。"""
from __future__ import annotations

import json

from app.tools import (
    ToolDef, build_tool_directive, parse_tool_calls, strip_tool_calls,
)


def test_build_directive_empty_when_no_tools() -> None:
    assert build_tool_directive([]) == ""


def test_build_directive_contains_tool_spec() -> None:
    tools = [ToolDef(name="get_weather", description="查天气",
                     parameters={"type": "object", "properties": {"city": {"type": "string"}}})]
    d = build_tool_directive(tools)
    assert "<tool_call>" in d
    assert "get_weather" in d
    assert "查天气" in d


def test_parse_single_tool_call() -> None:
    text = '<tool_call>{"name":"get_weather","arguments":{"city":"北京"}}</tool_call>'
    calls = parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].name == "get_weather"
    assert calls[0].arguments == {"city": "北京"}
    assert calls[0].id.startswith("call_")


def test_parse_arguments_as_string_json() -> None:
    args = json.dumps({"city": "上海"})
    text = f'<tool_call>{{"name":"x","arguments":{args}}}</tool_call>'
    calls = parse_tool_calls(text)
    assert calls[0].arguments == {"city": "上海"}


def test_parse_invalid_json_skipped() -> None:
    text = "<tool_call>not json</tool_call>"
    assert parse_tool_calls(text) == []


def test_strip_tool_calls_leaves_text() -> None:
    text = '前缀 <tool_call>{"name":"x","arguments":{}}</tool_call> 后缀'
    assert strip_tool_calls(text) == "前缀  后缀"


def test_from_openai_anthropic() -> None:
    o = {"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}}}
    # OpenAI 顶层是 {function:{...}}；这里直接用 function 内容
    td = ToolDef.from_openai({"name": "f", "description": "d", "parameters": {"type": "object"}})
    assert td.name == "f"
    td2 = ToolDef.from_anthropic({"name": "g", "description": "d2", "input_schema": {"type": "object"}})
    assert td2.name == "g" and td2.parameters == {"type": "object"}
