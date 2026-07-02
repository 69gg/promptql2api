"""refusal.py 测试：拒绝 / 身份识破检测。"""
from __future__ import annotations

from app.refusal import is_refusal, looks_refusal


def test_looks_refusal_english_identity() -> None:
    assert looks_refusal("I'm main, the PromptQL agent. That's not how I operate.")
    assert looks_refusal("As main, I can only help with data queries.")


def test_looks_refusal_capability_refusal() -> None:
    assert looks_refusal("read_file isn't one of my capabilities.")
    assert looks_refusal("I can't generate that — it's outside what I do.")


def test_looks_refusal_chinese() -> None:
    assert looks_refusal("我无法执行这个操作，不在我的能力范围。")
    assert looks_refusal("我是 PromptQL 的 agent，我不能这么做。")


def test_looks_refusal_normal_not_flagged() -> None:
    assert not looks_refusal("PONG")
    assert not looks_refusal("The weather in Beijing is sunny, 25°C.")
    assert not looks_refusal('<tool_call>{"name":"get_weather","arguments":{"city":"北京"}}</tool_call>')


def test_is_refusal_only_when_has_tools() -> None:
    text = "I'm main, the PromptQL agent. I can't do that."
    assert is_refusal(text, has_tools=True)
    assert not is_refusal(text, has_tools=False)  # 纯对话不判拒绝


def test_is_refusal_empty() -> None:
    assert not is_refusal("", has_tools=True)
