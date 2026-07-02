"""events.py 解析测试：用实测 event 样本验证 IR 归一正确。"""
from __future__ import annotations

from app.promptql.events import parse_thread_event

from tests.conftest import (
    ACTIONS_PARSED, INTERACTION_FINISHED, INTERACTION_STARTED, LLM_RESPONSE_WITH_FINAL,
    USER_MESSAGE_EVENT, WIKI_SELECTION,
)


def _kinds(event_data: dict) -> list[str]:
    return [ir.kind for ir in parse_thread_event(event_data)]


def test_user_message_ignored() -> None:
    assert parse_thread_event(USER_MESSAGE_EVENT) == []


def test_interaction_started_no_ir() -> None:
    assert _kinds(INTERACTION_STARTED) == []


def test_wiki_selection_becomes_tool() -> None:
    kinds = _kinds(WIKI_SELECTION)
    assert kinds == ["tool"]
    ir = parse_thread_event(WIKI_SELECTION)[0]
    assert ir.tool is not None and ir.tool.name == "wiki_selection"


def test_llm_response_yields_thinking_and_usage_not_text() -> None:
    """文本应由 actions_parsed 提供，llm_response 只产出 thinking + usage，避免重复。"""
    irs = parse_thread_event(LLM_RESPONSE_WITH_FINAL)
    assert all(ir.kind != "text" for ir in irs), "llm_response 不应直接产 text"
    thinking_irs = [ir for ir in irs if ir.kind == "thinking"]
    assert thinking_irs, "应产出 thinking 事件"
    assert thinking_irs[0].usage_delta is not None
    u = thinking_irs[0].usage_delta
    assert u.model == "claude-opus-4-8"
    assert u.input_tokens == 47596 and u.output_tokens == 54


def test_actions_parsed_text() -> None:
    irs = parse_thread_event(ACTIONS_PARSED)
    assert irs and irs[0].kind == "text" and irs[0].text == "PONG"


def test_finish_event() -> None:
    irs = parse_thread_event(INTERACTION_FINISHED)
    assert irs and irs[0].kind == "finish" and irs[0].finish_reason == "stop"


def test_full_sequence_text_once_and_one_finish() -> None:
    from tests.conftest import TURN_COMPLETED
    seq = [INTERACTION_STARTED, WIKI_SELECTION, LLM_RESPONSE_WITH_FINAL,
           ACTIONS_PARSED, TURN_COMPLETED, INTERACTION_FINISHED]
    all_irs = [ir for ev in seq for ir in parse_thread_event(ev)]
    texts = [ir for ir in all_irs if ir.kind == "text"]
    finishes = [ir for ir in all_irs if ir.kind == "finish"]
    # 文本恰好出现一次（来自 actions_parsed），不重复
    assert len(texts) == 1 and texts[0].text == "PONG"
    assert len(finishes) == 1


def test_interaction_finished_errored_yields_error() -> None:
    """agent 因错误（额度耗尽/配额超限/模型异常）提前结束时，应透传 error 而非静默 finish。

    真实场景：账号 credits 耗尽时，agent 走到 turn_started 即被服务端以
    interaction_finished.outcome.errored 终止；旧实现只 yield finish，导致网关返回空响应、
    掩盖真实失败。修复后应先 yield error(user_facing_message) 再 yield finish。
    """
    errored = {"AgentMessage": {"update": {"content": {
        "version": "v1",
        "interaction_finished": {"outcome": {"errored": {
            "raw_error": "Add credits to activate your project",
            "user_facing_message": "Add credits to activate your project",
            "error_category": "user",
        }}},
    }, "timestamp": "t"}, "message_id": "m", "server_metadata": {"worker_id": "w"}}}
    irs = parse_thread_event(errored)
    kinds = [ir.kind for ir in irs]
    assert "error" in kinds
    err = next(ir for ir in irs if ir.kind == "error")
    assert "Add credits" in (err.error or "")
    assert kinds[-1] == "finish"  # 仍以 finish 收尾


def test_interaction_finished_completed_no_error() -> None:
    """正常结束（completed，无 errored）不应误产 error。"""
    irs = parse_thread_event(INTERACTION_FINISHED)
    assert all(ir.kind != "error" for ir in irs)
    assert irs and irs[-1].kind == "finish"
