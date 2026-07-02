"""orchestrator.py 测试：拒绝检测 + 角度轮换重试。

用 _FakeClient 模拟 agent 回复（按 prompt 第几轮返回拒绝 / 命中），不依赖网络。
"""
from __future__ import annotations

import asyncio

from app.orchestrator import stream_with_retry
from app.promptql.events import IREvent
from app.tools import ToolDef


def _tool() -> ToolDef:
    return ToolDef(name="get_weather", description="查天气",
                   parameters={"type": "object", "properties": {"city": {"type": "string"}}})


class _FakeClient:
    """按脚本序列模拟 agent 回复：'REFUSE' → 拒绝文本；其它 → 含 <tool_call> 的命中文本。"""

    def __init__(self, scripts: list[str]) -> None:
        self._scripts = scripts
        self._i = 0
        self.calls: list[str] = []

    async def stream_thread(self, message: str, llm_config_id: str | None = None):  # type: ignore[no-untyped-def]
        self.calls.append(message)
        text = self._scripts[min(self._i, len(self._scripts) - 1)]
        self._i += 1
        if text == "REFUSE":
            yield IREvent(kind="text", text="I'm main, the PromptQL agent. I can't do that.")
        else:
            yield IREvent(kind="text",
                          text='<tool_call>{"name":"get_weather","arguments":{"city":"北京"}}</tool_call>')
        yield IREvent(kind="finish", finish_reason="stop")


def _run(client: _FakeClient, tools: list[ToolDef], *, max_retries: int = 3) -> tuple[list[IREvent], list[str]]:
    async def go() -> tuple[list[IREvent], list[str]]:
        evs: list[IREvent] = []
        async for ir in stream_with_retry(client, "base prompt", tools, max_retries=max_retries):
            evs.append(ir)
        return evs, client.calls
    return asyncio.run(go())


def _text(evs: list[IREvent]) -> str:
    return "".join(ir.text for ir in evs if ir.kind == "text")


def test_first_hit_no_retry() -> None:
    client = _FakeClient(["HIT"])
    evs, calls = _run(client, [_tool()], max_retries=3)
    assert len(calls) == 1
    assert "get_weather" in _text(evs)


def test_retry_until_hit() -> None:
    client = _FakeClient(["REFUSE", "HIT"])
    evs, calls = _run(client, [_tool()], max_retries=3)
    assert len(calls) == 2  # 首轮拒绝 → 重试一次命中
    assert "get_weather" in _text(evs)


def test_exhaust_retries_fallback() -> None:
    # 总是拒绝 → 耗尽重试，回退最后一轮（拒绝文本）
    client = _FakeClient(["REFUSE", "REFUSE", "REFUSE", "REFUSE"])
    evs, calls = _run(client, [_tool()], max_retries=3)
    assert len(calls) == 4  # 1 首次 + 3 重试
    txt = _text(evs).lower()
    assert "promptql" in txt or "can't" in txt  # 回退的是拒绝文本


def test_no_tools_no_retry() -> None:
    # 无 tools：不判拒绝，一轮即止（即便文本像拒绝）
    client = _FakeClient(["REFUSE"])
    evs, calls = _run(client, [], max_retries=3)
    assert len(calls) == 1


def test_retry_rotates_angle() -> None:
    # 重试时 directive 角度轮换 → 每次 prompt 文本不同
    client = _FakeClient(["REFUSE", "REFUSE", "HIT"])
    _run(client, [_tool()], max_retries=3)
    assert len(client.calls) == 3
    assert client.calls[0] != client.calls[1]
    assert client.calls[1] != client.calls[2]
