"""adapter 集成测试：用 mock PromptQLClient（喂固定 IR 序列）验证各家输出格式。"""
from __future__ import annotations

import json
from typing import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from app.promptql.events import IREvent, ToolEvent, Usage


class _MockClient:
    """按预设序列产出 IREvent 的假 client。"""

    def __init__(self, events: list[IREvent]) -> None:
        self._events = events

    async def stream_thread(self, prompt: str, llm_config_id=None, *, timeout=None) -> AsyncIterator[IREvent]:
        for e in self._events:
            yield e


def _make_app(events: list[IREvent]):
    from app.main import app
    app.state.client = _MockClient(events)
    # settings 也需要（get_client 不用，但 lifespan 会建真 client；这里直接覆盖）
    return TestClient(app)


@pytest.fixture
def simple_events() -> list[IREvent]:
    return [
        IREvent(kind="tool", tool=ToolEvent(name="wiki_selection", title="检索 Wiki")),
        IREvent(kind="text", text="Hello!", usage_delta=Usage(input_tokens=10, output_tokens=2,
                                                                model="claude-opus-4-8")),
        IREvent(kind="finish", finish_reason="stop"),
    ]


def test_openai_models(simple_events) -> None:
    c = _make_app(simple_events)
    r = c.get("/v1/models")
    assert r.status_code == 200 and r.json()["object"] == "list"


def test_models_catalog_count() -> None:
    from app.adapters import supported_models
    models = supported_models()
    assert len(models) == 10
    ids = [m["id"] for m in models]
    assert "claude-opus-4-8" in ids and "glm-5.2" in ids and "gpt-5.5" in ids


def test_normalize_model_variants() -> None:
    from app.adapters import llm_config_id_for, normalize_model
    assert normalize_model(None) == "claude-opus-4-8"
    assert normalize_model("claude-sonnet-4-5-20250929") == "claude-sonnet-4-5-20250929"  # 精确 id
    assert normalize_model("Claude Sonnet 4.5") == "claude-sonnet-4-5-20250929"  # 显示名
    assert normalize_model("gpt-5.5") == "gpt-5.5"  # 精确（点分隔 id）
    assert normalize_model("gpt-5-5") == "gpt-5.5"  # 连字符→点 模糊
    assert normalize_model("glm-5.2") == "glm-5.2"
    assert normalize_model("unknown-model") == "claude-opus-4-8"  # 未知→默认
    assert llm_config_id_for("claude-sonnet-4-5-20250929") == "956dd263-53e6-4432-b16e-e84a76d31c4c"
    assert llm_config_id_for("claude-opus-4-8") == "65d9536f-09da-4acd-8301-3b3f48ab42bc"


def test_openai_chat_nonstream(simple_events) -> None:
    c = _make_app(simple_events)
    r = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "Hello!"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 10 and body["usage"]["completion_tokens"] == 2


async def test_openai_chat_stream_no_tool_noise(simple_events) -> None:
    """直接驱动流式生成器，验证文本透传且 tool 事件不污染内容。"""
    from app.adapters.openai_chat import _gen_stream
    client = _MockClient(simple_events)
    chunks: list[str] = []
    async for b in _gen_stream(client, "p", [], "claude-opus-4-8"):
        chunks.append(b.decode("utf-8", errors="ignore"))
    out = "".join(chunks)
    assert '"content": "Hello!"' in out or '"content":"Hello!"' in out
    assert "检索 Wiki" not in out        # tool 事件不透传
    assert "[DONE]" in out


def test_openai_chat_tool_calls() -> None:
    events = [
        IREvent(kind="text", text='Calling: <tool_call>{"name":"f","arguments":{"a":1}}</tool_call>'),
        IREvent(kind="finish", finish_reason="stop"),
    ]
    c = _make_app(events)
    r = c.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"type": "function", "function": {"name": "f", "description": "d",
                    "parameters": {"type": "object", "properties": {}}}}],
    })
    body = r.json()
    msg = body["choices"][0]["message"]
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert msg["tool_calls"][0]["function"]["name"] == "f"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"a": 1}


def test_anthropic_messages_nonstream(simple_events) -> None:
    c = _make_app(simple_events)
    r = c.post("/v1/messages", json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 10})
    body = r.json()
    assert body["content"][0]["text"] == "Hello!"
    assert body["stop_reason"] == "end_turn"
    assert body["usage"]["input_tokens"] == 10


def test_anthropic_count_tokens() -> None:
    c = _make_app([])
    r = c.post("/v1/messages/count_tokens",
               json={"messages": [{"role": "user", "content": "hello world"}]})
    assert r.status_code == 200 and r.json()["input_tokens"] > 0


def test_openai_responses(simple_events) -> None:
    c = _make_app(simple_events)
    r = c.post("/v1/responses", json={"input": "hi"})
    body = r.json()
    assert body["object"] == "response"
    assert body["output"][0]["content"][0]["text"] == "Hello!"


import re  # noqa: E402
