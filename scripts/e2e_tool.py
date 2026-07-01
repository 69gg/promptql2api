"""端到端：OpenAI SDK 打本地网关，验证认知重构 function calling（单轮 + 多轮续推）。

agent 有随机性且偶发拒绝（Opus 4.8），每场景重试 N 次统计命中率。
用法：先启动网关（uv run uvicorn app.main:app --port 8088），再 uv run python scripts/e2e_tool.py
"""
from __future__ import annotations

from openai import OpenAI

c = OpenAI(base_url="http://127.0.0.1:8088/v1", api_key="any")
TOOL = {"type": "function", "function": {
    "name": "get_weather", "description": "获取指定城市的实时天气",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}


def call(msgs: list[dict], tag: str) -> bool:
    try:
        r = c.chat.completions.create(model="claude-opus-4-8", messages=msgs, tools=[TOOL], timeout=120)
    except Exception as e:  # noqa: BLE001
        print(f"[{tag}] ✗ ERROR: {e}")
        return False
    m = r.choices[0].message
    fr = r.choices[0].finish_reason
    if m.tool_calls:
        tc = m.tool_calls[0]
        print(f"[{tag}] ✓ finish={fr} {tc.function.name}({tc.function.arguments})")
        return True
    print(f"[{tag}] ✗ finish={fr} TEXT: {(m.content or '')[:70]}")
    return False


def retry(msgs: list[dict], tag: str, n: int = 3) -> int:
    hits = sum(1 for i in range(n) if call(msgs, f"{tag}#{i + 1}"))
    print(f"  → {tag} 命中 {hits}/{n}\n")
    return hits


print("=== 单轮（无历史 tool_call）===")
retry([{"role": "user", "content": "我在北京，今天该穿什么？帮我查一下北京今天的天气。"}], "single")

print("=== 多轮续推（历史含 tool_call → few-shot，命中率更高）===")
retry([
    {"role": "user", "content": "帮我查一下北京今天的天气。"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city": "北京"}'}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": '{"city":"北京","weather":"晴","temp":25}'},
    {"role": "user", "content": "那上海呢？也帮我查一下。"},
], "multi")
