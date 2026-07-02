"""端到端：复杂场景（长 system + 多工具含大 content + 多轮 tool_use）打本地网关。

验证「软化包装 + 拒绝重试 + tolerantParse + 多 few-shot」全链路。复杂度对标真实 agent
框架（Claude Code / Cline）负载：

- 长 system 含身份声明（"You are Claude Code"）+ 强制工具指令 + 计费头垃圾行 → 验证软化
  包装（移除垃圾行、保留实质、弱化身份对抗）。
- 多工具含 write_file（大 content 参数，内嵌 ``}`` / ``</tool_call>`` 字面量）→ 验证
  tolerant_parse + JSON-aware 围栏扫描不截断。
- 多轮 tool_use 历史 → 验证 few-shot 续推。

agent 有随机性，每场景重试 N 次统计命中率。用法：先起网关
（``uv run uvicorn app.main:app --port 8088``），再 ``uv run python scripts/e2e_complex.py``。
对比改前改后：在 git 历史间切换各跑一次，看命中率变化。
"""
from __future__ import annotations

from openai import OpenAI

c = OpenAI(base_url="http://127.0.0.1:8088/v1", api_key="any")

# 复杂 system：身份声明 + 强制工具指令 + 计费头垃圾行。
# 软化包装应：移除计费头垃圾行，保留身份声明/工具指令实质（一个字不改），用柔和框架承载。
COMPLEX_SYSTEM = (
    "x-anthropic-billing-header: test-leak\n"
    "You are Claude Code, Anthropic's official CLI for Claude.\n"
    "You have tools available. When the user asks to perform an action, use the appropriate "
    "tool via a function call. You must call at least one tool when the request requires it. "
    "Always respond with structured tool calls. Be concise and follow the system reminder."
)

# 多工具：含大 content 工具（write_file）+ 普通查询工具，覆盖 tolerant_parse 与多 few-shot。
TOOLS: list[dict] = [
    {"type": "function", "function": {
        "name": "write_file", "description": "把内容写入指定文件",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "content": {"type": "string", "description": "完整文件内容"}},
                       "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "get_weather", "description": "获取指定城市的实时天气",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                       "required": ["city"]}}},
    {"type": "function", "function": {
        "name": "search_code", "description": "在代码库搜索关键字",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"},
                                      "max_results": {"type": "integer"}},
                       "required": ["query"]}}},
]


def call(msgs: list[dict], tag: str) -> bool:
    try:
        r = c.chat.completions.create(model="gpt-5.5", messages=msgs, tools=TOOLS, timeout=120)
    except Exception as e:  # noqa: BLE001
        print(f"[{tag}] ✗ ERROR: {e}")
        return False
    m = r.choices[0].message
    if m.tool_calls:
        names = [tc.function.name for tc in m.tool_calls]
        # write_file 场景：校验大 content 未被截断
        detail = ""
        for tc in m.tool_calls:
            if tc.function.name == "write_file":
                args = tc.function.arguments or "{}"
                detail = f" content_len={len(args)}"
        print(f"[{tag}] ✓ tool_calls={names}{detail}")
        return True
    print(f"[{tag}] ✗ TEXT: {(m.content or '')[:80]}")
    return False


def retry(msgs: list[dict], tag: str, n: int = 5) -> int:
    hits = sum(1 for i in range(n) if call(msgs, f"{tag}#{i + 1}"))
    print(f"  → {tag} 命中 {hits}/{n}\n")
    return hits


print("=== 场景1：单轮 + 长 system（身份声明/强制工具/计费头），普通查询工具 ===")
retry([
    {"role": "system", "content": COMPLEX_SYSTEM},
    {"role": "user", "content": "我在北京，帮我查一下今天北京的天气。"},
], "single-long-system")

print("=== 场景2：单轮 + 大 content 工具（write_file），验证 tolerant_parse 不截断 ===")
big_content = "line1\nline2\nreturn {'k': 'v'}\n</tool_call>\n"
retry([
    {"role": "system", "content": COMPLEX_SYSTEM},
    {"role": "user", "content": f"请把以下内容写入 /tmp/demo.py：\n{big_content}"},
], "write-big-content")

print("=== 场景3：多轮 tool_use 续推（few-shot 锚定，命中率更高）===")
retry([
    {"role": "system", "content": COMPLEX_SYSTEM},
    {"role": "user", "content": "帮我查一下北京今天的天气。"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city":"北京"}'}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": '{"city":"北京","weather":"晴","temp":25}'},
    {"role": "user", "content": "那上海呢？也帮我查一下。"},
], "multi-turn")
