"""adapter 集成测试：用 mock PromptQLClient（喂固定 IR 序列）验证各家输出格式。"""
from __future__ import annotations

import json
from typing import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from app.promptql.events import IREvent, ToolEvent, Usage
from app.tools import ToolDef


class _MockClient:
    """按预设序列产出 IREvent 的假 client。"""

    def __init__(self, events: list[IREvent]) -> None:
        self._events = events

    async def stream_thread(self, prompt: str, llm_config_id=None, *, timeout=None) -> AsyncIterator[IREvent]:
        for e in self._events:
            yield e


def _make_app(events: list[IREvent]):
    from app.main import app
    from app.deps import get_client
    mock = _MockClient(events)

    async def _override():
        return mock

    app.dependency_overrides[get_client] = _override
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    """每个测试后清理 dependency_overrides，避免互相污染。"""
    yield
    from app.main import app
    app.dependency_overrides.clear()


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
    assert normalize_model(None) == "gpt-5.5"
    assert normalize_model("claude-sonnet-4-5-20250929") == "claude-sonnet-4-5-20250929"  # 精确 id
    assert normalize_model("Claude Sonnet 4.5") == "claude-sonnet-4-5-20250929"  # 显示名
    assert normalize_model("gpt-5.5") == "gpt-5.5"  # 精确（点分隔 id）
    assert normalize_model("gpt-5-5") == "gpt-5.5"  # 连字符→点 模糊
    assert normalize_model("glm-5.2") == "glm-5.2"
    assert normalize_model("unknown-model") == "gpt-5.5"  # 未知→默认
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


async def test_openai_chat_stream_tool_calls() -> None:
    """流式 tool call：文本中不应出现原始 <tool_call> 标签，最后应出现 tool_calls chunk。"""
    from app.adapters.openai_chat import _gen_stream
    events = [
        IREvent(kind="text", text='Calling: <tool_call>{"name":"f","arguments":{"a":1}}</tool_call>'),
        IREvent(kind="finish", finish_reason="stop"),
    ]
    client = _MockClient(events)
    chunks: list[str] = []
    async for b in _gen_stream(client, "p", [
        ToolDef(name="f", description="d", parameters={"type": "object", "properties": {}})
    ], "gpt-5.5"):
        chunks.append(b.decode("utf-8", errors="ignore"))
    out = "".join(chunks)
    assert "<tool_call>" not in out
    assert '"tool_calls"' in out
    assert '"name": "f"' in out or '"name":"f"' in out
    assert '"finish_reason": "tool_calls"' in out or '"finish_reason":"tool_calls"' in out
    assert "[DONE]" in out


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


# ---------- Anthropic /v1/messages tool calls ----------


def test_anthropic_messages_tool_calls() -> None:
    events = [
        IREvent(kind="text", text='Calling: <tool_call>{"name":"f","arguments":{"a":1}}</tool_call>'),
        IREvent(kind="finish", finish_reason="stop"),
    ]
    c = _make_app(events)
    r = c.post("/v1/messages", json={
        "messages": [{"role": "user", "content": "x"}],
        "max_tokens": 10,
        "tools": [{"name": "f", "description": "d", "input_schema": {"type": "object", "properties": {}}}],
    })
    body = r.json()
    assert body["stop_reason"] == "tool_use"
    assert body["content"][0]["type"] == "tool_use"
    assert body["content"][0]["name"] == "f"
    assert body["content"][0]["input"] == {"a": 1}


async def test_anthropic_messages_stream_tool_calls() -> None:
    from app.adapters.anthropic_messages import _gen_stream
    events = [
        IREvent(kind="text", text='Calling: <tool_call>{"name":"f","arguments":{"a":1}}</tool_call>'),
        IREvent(kind="finish", finish_reason="stop"),
    ]
    client = _MockClient(events)
    chunks: list[str] = []
    async for b in _gen_stream(client, "p", [
        ToolDef(name="f", description="d", parameters={"type": "object", "properties": {}})
    ], "claude-opus-4-8"):
        chunks.append(b.decode("utf-8", errors="ignore"))
    out = "".join(chunks)
    assert "<tool_call>" not in out
    assert '"type": "tool_use"' in out
    assert '"name": "f"' in out or '"name":"f"' in out
    assert "tool_use" in out


# ---------- OpenAI /v1/responses tool calls ----------


def test_openai_responses_tool_calls() -> None:
    events = [
        IREvent(kind="text", text='Calling: <tool_call>{"name":"f","arguments":{"a":1}}</tool_call>'),
        IREvent(kind="finish", finish_reason="stop"),
    ]
    c = _make_app(events)
    r = c.post("/v1/responses", json={
        "input": "x",
        "tools": [{"type": "function", "function": {"name": "f", "description": "d",
                    "parameters": {"type": "object", "properties": {}}}}],
    })
    body = r.json()
    assert body["output"][0]["type"] == "function_call"
    assert body["output"][0]["name"] == "f"
    assert json.loads(body["output"][0]["arguments"]) == {"a": 1}


async def test_openai_responses_stream_tool_calls() -> None:
    from app.adapters.openai_responses import _gen_stream
    events = [
        IREvent(kind="text", text='Calling: <tool_call>{"name":"f","arguments":{"a":1}}</tool_call>'),
        IREvent(kind="finish", finish_reason="stop"),
    ]
    client = _MockClient(events)
    chunks: list[str] = []
    async for b in _gen_stream(client, "p", [
        ToolDef(name="f", description="d", parameters={"type": "object", "properties": {}})
    ], "gpt-5.5"):
        chunks.append(b.decode("utf-8", errors="ignore"))
    out = "".join(chunks)
    assert "<tool_call>" not in out
    assert "response.output_item.added" in out
    assert "response.function_call_arguments.delta" in out
    assert "response.output_item.done" in out


import re  # noqa: E402


class _CapturingMockClient:
    """记录最后一次 stream_thread 收到的 prompt，用于验证请求体透传。"""

    def __init__(self, events: list[IREvent]) -> None:
        self.events = events
        self.last_prompt: str | None = None

    async def stream_thread(self, prompt: str, llm_config_id=None, *, timeout=None) -> AsyncIterator[IREvent]:
        self.last_prompt = prompt
        for e in self.events:
            yield e


def _make_capturing_app(events: list[IREvent]) -> tuple[TestClient, _CapturingMockClient]:
    from app.main import app
    from app.deps import get_client
    mock = _CapturingMockClient(events)

    async def _override():
        return mock

    app.dependency_overrides[get_client] = _override
    return TestClient(app), mock


@pytest.fixture
def cot_events() -> list[IREvent]:
    return [
        IREvent(kind="thinking", thinking="Step 1: understand the question.",
                usage_delta=Usage(input_tokens=5, output_tokens=3)),
        IREvent(kind="text", text="Hello!", usage_delta=Usage(input_tokens=10, output_tokens=2)),
        IREvent(kind="finish", finish_reason="stop"),
    ]


# ---------- OpenAI /v1/chat/completions ----------


def test_openai_chat_nonstream_returns_reasoning_content(cot_events) -> None:
    c = _make_app(cot_events)
    r = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    body = r.json()
    msg = body["choices"][0]["message"]
    assert msg["content"] == "Hello!"
    assert msg["reasoning_content"] == "Step 1: understand the question."


async def test_openai_chat_stream_returns_reasoning_content(cot_events) -> None:
    from app.adapters.openai_chat import _gen_stream
    client = _MockClient(cot_events)
    chunks: list[str] = []
    async for b in _gen_stream(client, "p", [], "gpt-5.5"):
        chunks.append(b.decode("utf-8", errors="ignore"))
    out = "".join(chunks)
    assert '"reasoning_content": "Step 1: understand the question."' in out
    assert '"content": "Hello!"' in out or '"content":"Hello!"' in out
    assert "[DONE]" in out


def test_openai_chat_request_reasoning_content_preserved() -> None:
    events = [IREvent(kind="text", text="ok"), IREvent(kind="finish", finish_reason="stop")]
    c, mock = _make_capturing_app(events)
    r = c.post("/v1/chat/completions", json={
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok", "reasoning_content": "I should say ok."},
        ],
    })
    assert r.status_code == 200
    assert mock.last_prompt is not None
    assert "<reasoning>" in mock.last_prompt
    assert "I should say ok." in mock.last_prompt


# ---------- OpenAI /v1/responses ----------


def test_openai_responses_nonstream_returns_reasoning(cot_events) -> None:
    c = _make_app(cot_events)
    r = c.post("/v1/responses", json={"input": "hi"})
    body = r.json()
    assert body["output"][0]["type"] == "reasoning"
    assert body["output"][0]["summary"][0]["text"] == "Step 1: understand the question."
    assert body["output"][1]["content"][0]["text"] == "Hello!"


async def test_openai_responses_stream_returns_reasoning(cot_events) -> None:
    from app.adapters.openai_responses import _gen_stream
    client = _MockClient(cot_events)
    chunks: list[str] = []
    async for b in _gen_stream(client, "p", [], "gpt-5.5"):
        chunks.append(b.decode("utf-8", errors="ignore"))
    out = "".join(chunks)
    assert "response.reasoning_item.added" in out
    assert "response.reasoning_summary_text.delta" in out
    assert "Step 1: understand the question." in out
    assert "response.output_text.delta" in out


def test_openai_responses_request_reasoning_preserved() -> None:
    events = [IREvent(kind="text", text="ok"), IREvent(kind="finish", finish_reason="stop")]
    c, mock = _make_capturing_app(events)
    r = c.post("/v1/responses", json={
        "input": [
            {"type": "message", "role": "user", "content": "hi"},
            {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "Prior reasoning."}]},
        ],
    })
    assert r.status_code == 200
    assert mock.last_prompt is not None
    assert "<reasoning>" in mock.last_prompt
    assert "Prior reasoning." in mock.last_prompt


# ---------- Anthropic /v1/messages ----------


def test_anthropic_messages_nonstream_returns_thinking(cot_events) -> None:
    c = _make_app(cot_events)
    r = c.post("/v1/messages", json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 10})
    body = r.json()
    assert body["content"][0]["type"] == "thinking"
    assert body["content"][0]["thinking"] == "Step 1: understand the question."
    assert body["content"][1]["type"] == "text"
    assert body["content"][1]["text"] == "Hello!"


async def test_anthropic_messages_stream_returns_thinking(cot_events) -> None:
    from app.adapters.anthropic_messages import _gen_stream
    client = _MockClient(cot_events)
    chunks: list[str] = []
    async for b in _gen_stream(client, "p", [], "claude-opus-4-8"):
        chunks.append(b.decode("utf-8", errors="ignore"))
    out = "".join(chunks)
    assert '"type": "thinking"' in out
    assert "Step 1: understand the question." in out
    assert '"type": "text"' in out
    assert "Hello!" in out


def test_anthropic_messages_request_thinking_preserved() -> None:
    events = [IREvent(kind="text", text="ok"), IREvent(kind="finish", finish_reason="stop")]
    c, mock = _make_capturing_app(events)
    r = c.post("/v1/messages", json={
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "I should say ok."},
                {"type": "text", "text": "ok"},
            ]},
        ],
        "max_tokens": 10,
    })
    assert r.status_code == 200
    assert mock.last_prompt is not None
    assert "<thinking>" in mock.last_prompt
    assert "I should say ok." in mock.last_prompt
