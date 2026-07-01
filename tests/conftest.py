"""共享夹具：Probe 实测的真实 event_data 样本，供 events/adapters 测试。"""
from __future__ import annotations

import pytest

# 来自 scripts/probe.py 实抓的 agent 事件序列（PONG 问答）

USER_MESSAGE_EVENT = {
    "UserMessage": {
        "message": {"message": "Reply with just the word: PONG",
                    "timestamp": "2026-07-01T07:26:53.585Z", "timezone": "Asia/Shanghai", "uploads": []},
        "message_id": "0bdd4738-5223-4803-b159-29998d603651",
        "provenance": {"user": {}}, "user_id": "19d86342-881e-44a7-9d18-94a35d15789d",
    }
}

INTERACTION_STARTED = {"AgentMessage": {"update": {"content": {
    "version": "v1",
    "interaction_started": {"triggered_by": {"user_message": {"message_id": "x"}}},
}, "timestamp": "2026-07-01T07:27:58.669Z"}, "message_id": "m1", "server_metadata": {"worker_id": "w"}}}

WIKI_SELECTION = {"AgentMessage": {"update": {"content": {
    "version": "v1",
    "interaction_update": {"version": "v1", "wiki_selection": {"started": {}, "version": "v1"}},
}, "timestamp": "t"}, "message_id": "m", "server_metadata": {"worker_id": "w"}}}

LLM_RESPONSE_WITH_FINAL = {"AgentMessage": {"update": {"content": {
    "version": "v1",
    "interaction_update": {"version": "v1", "main_agent": {"version": "v1", "llm_response": {
        "usage": {"model": "claude-opus-4-8", "provider": "bedrock",
                  "input_tokens": 47596, "cached_tokens": 46039, "output_tokens": 54,
                  "thinking_tokens": 0, "cache_creation_tokens": 0},
        "response_text": "<action>\n<final_response>\nPONG\n</final_response>\n</action>",
        "thinking_text": "The user is asking for a simple \"PONG\" response.",
    }}},
}, "timestamp": "t"}, "message_id": "m", "server_metadata": {"worker_id": "w"}}}

ACTIONS_PARSED = {"AgentMessage": {"update": {"content": {
    "version": "v1",
    "interaction_update": {"version": "v1", "main_agent": {"version": "v1", "actions_parsed": {
        "actions": [{"final_response": {"message": "PONG"}}],
    }}},
}, "timestamp": "t"}, "message_id": "m", "server_metadata": {"worker_id": "w"}}}

TURN_COMPLETED = {"AgentMessage": {"update": {"content": {
    "version": "v1",
    "interaction_update": {"version": "v1", "main_agent": {"version": "v1", "turn_completed": {}}},
}, "timestamp": "t"}, "message_id": "m", "server_metadata": {"worker_id": "w"}}}

INTERACTION_FINISHED = {"AgentMessage": {"update": {"content": {
    "version": "v1",
    "interaction_finished": {"completed": {}},
}, "timestamp": "t"}, "message_id": "m", "server_metadata": {"worker_id": "w"}}}


@pytest.fixture
def agent_events() -> list[dict]:
    return [
        USER_MESSAGE_EVENT, INTERACTION_STARTED, WIKI_SELECTION,
        LLM_RESPONSE_WITH_FINAL, ACTIONS_PARSED, TURN_COMPLETED, INTERACTION_FINISHED,
    ]
