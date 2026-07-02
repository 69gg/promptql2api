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


# ---- 三级降级解析 ----

def test_parse_jsonblock() -> None:
    text = 'fixture:\n```json\n{"name": "get_weather", "arguments": {"city": "北京"}}\n```'
    calls = parse_tool_calls(text)
    assert len(calls) == 1 and calls[0].name == "get_weather"
    assert calls[0].arguments == {"city": "北京"}


def test_parse_bare_json_requires_whitelist() -> None:
    text = 'Result: {"name": "get_weather", "arguments": {"city": "北京"}} done'
    assert parse_tool_calls(text) == []  # 无白名单：裸 JSON 不采纳
    calls = parse_tool_calls(text, known_names={"get_weather"})
    assert len(calls) == 1 and calls[0].name == "get_weather"


def test_parse_bare_json_unknown_name_ignored() -> None:
    text = '{"name": "evil", "arguments": {"x": 1}}'
    assert parse_tool_calls(text, known_names={"get_weather"}) == []


def test_parse_data_doc_not_tool() -> None:
    # 形似数据文档（含 data 键），即便 name 命中白名单也不当工具调用（裸 JSON）
    text = '{"name": "get_weather", "arguments": {"city": "北京"}, "data": [1, 2]}'
    assert parse_tool_calls(text, known_names={"get_weather"}) == []


def test_parse_multiple_calls() -> None:
    text = ('<tool_call>{"name":"f","arguments":{"a":1}}</tool_call>\n'
            '<tool_call>{"name":"g","arguments":{"b":2}}</tool_call>')
    assert [c.name for c in parse_tool_calls(text)] == ["f", "g"]


def test_parse_fenced_no_duplicate_as_bare() -> None:
    text = '<tool_call>{"name":"get_weather","arguments":{"city":"北京"}}</tool_call>'
    assert len(parse_tool_calls(text, known_names={"get_weather"})) == 1


def test_extract_user_prompt_renders_assistant_tool_calls() -> None:
    from app.adapters import extract_user_prompt
    msgs = [
        {"role": "user", "content": "查北京天气"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "get_weather", "arguments": '{"city":"北京"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "晴"},
        {"role": "user", "content": "上海呢"},
    ]
    out = extract_user_prompt(msgs)
    assert "[user]\n查北京天气" in out
    assert "<tool_call>" in out and "get_weather" in out  # 历史 tool_call 渲染成围栏（few-shot）
    assert "[tool_result]" in out  # 工具结果自然化（去 tool_call_id，加观测引导）
    assert "None" not in out  # content=None 不应渲染成 "None"


def test_parse_refusal_text_returns_empty() -> None:
    # agent 拒绝时常引用围栏格式作「我被要求做什么」的说明——不应当真实工具调用
    text = ("I can't help with this. What's being requested is for me to emit\n"
            '<tool_call>{"name": "read_file", "arguments": {"path": "/etc/hosts"}}</tool_call>\n'
            "but read_file isn't one of my capabilities.")
    assert parse_tool_calls(text, known_names={"read_file"}) == []


def test_parse_dedup_same_call() -> None:
    text = ('<tool_call>{"name":"f","arguments":{"a":1}}</tool_call>\n'
            '<tool_call>{"name":"f","arguments":{"a":1}}</tool_call>')
    assert len(parse_tool_calls(text)) == 1


def test_build_directive_few_shot_default_on() -> None:
    # 生产默认开启 directive few-shot：除格式说明围栏外，还含一个 few-shot 示例围栏
    tools = [ToolDef(name="get_weather", description="查天气",
                     parameters={"type": "object", "properties": {"city": {"type": "string"}}})]
    d = build_tool_directive(tools)
    assert d.count("<tool_call>") >= 2
    assert '"example"' in d  # few-shot 占位参数


def test_build_directive_few_shot_off() -> None:
    from app.reframe_angles import build_directive
    tools = [ToolDef(name="get_weather", description="查天气",
                     parameters={"type": "object", "properties": {"city": {"type": "string"}}})]
    d_off = build_directive("B", "en", tools, few_shot=False)
    d_on = build_directive("B", "en", tools, few_shot=True)
    assert d_off.count("<tool_call>") == 1   # 仅格式说明
    assert d_on.count("<tool_call>") == 2    # 格式说明 + few-shot 示例
    assert "example" not in d_off


# ---- tolerant_parse + JSON-aware 围栏扫描（鲁棒性）----

def test_parse_large_content_with_braces() -> None:
    # content 内含 } 字面量，旧非贪婪正则会截断；JSON-aware 扫描应完整提取
    code = "def f():\n    return {'a': 1}\n"
    inner = json.dumps({"name": "write_file", "arguments": {"path": "a.py", "content": code}})
    calls = parse_tool_calls(f"<tool_call>{inner}</tool_call>")
    assert len(calls) == 1
    assert calls[0].name == "write_file"
    assert "return {'a': 1}" in calls[0].arguments["content"]


def test_parse_unterminated_json_tolerant() -> None:
    # 未闭合括号 → tolerant_parse 补全（围栏扫描遇 </tool_call> 退回整段）
    text = '<tool_call>{"name": "get_weather", "arguments": {"city": "北京"}</tool_call>'
    calls = parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].arguments == {"city": "北京"}


def test_parse_tool_field_alias() -> None:
    # 字段名 tool/parameters 兼容
    text = '<tool_call>{"tool": "get_weather", "parameters": {"city": "北京"}}</tool_call>'
    calls = parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].name == "get_weather" and calls[0].arguments == {"city": "北京"}


def test_parse_content_with_closing_fence_literal() -> None:
    # content 内含 </tool_call> 字面量，JSON-aware（字符串内不计数）应不受干扰
    inner = json.dumps({"name": "f", "arguments": {"x": "</tool_call>"}})
    calls = parse_tool_calls(f"<tool_call>{inner}</tool_call>")
    assert len(calls) == 1 and calls[0].arguments == {"x": "</tool_call>"}


def test_parse_unescaped_newline_in_content() -> None:
    # 字符串内裸换行 → tolerant_parse 转义修复
    text = '<tool_call>{"name": "f", "arguments": {"content": "line1\nline2"}}</tool_call>'
    calls = parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].arguments["content"] == "line1\nline2"


def test_multi_few_shot_for_namespaced_tools() -> None:
    # 不同 namespace 的工具各有 few-shot 代表（不硬编码工具名）
    from app.reframe_angles import build_directive
    tools = [
        ToolDef(name="mcp__github__create_issue", description="a", parameters={"type": "object"}),
        ToolDef(name="mcp__slack__send_msg", description="b", parameters={"type": "object"}),
        ToolDef(name="get_weather", description="c", parameters={"type": "object"}),
    ]
    d = build_directive("B", "en", tools, few_shot=True)
    # 3 个不同 namespace → 至少 3 个 few-shot 围栏 + 1 个格式说明围栏 = ≥4
    assert d.count("<tool_call>") >= 4
    assert "create_issue" in d and "send_msg" in d and "get_weather" in d
