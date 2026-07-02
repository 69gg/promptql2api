"""adapter 公共工具：messages 归一化、model 映射、模型列表。"""
from __future__ import annotations

import json
import re
from typing import Any

from app.reframe_angles import ACTIVE_LANG
from app.system_sanitizer import soften_system

DEFAULT_MODEL = "gpt-5.5"

# 模型目录（id / 显示名 / 厂商 / llmConfigId）。2026-07-01 从 prompt.ql.app 实地抓取：
# 模型选择 dialog 各选项 button 的 data-testid「option-<uuid>」即 llmConfigId，并经
# start_thread mutation 的 variables.llmConfigId 验证一致。模型列表为前端 bundle 硬编码，
# 后端无查询接口，故此处常量化（可按 Playground 上线新模型时更新）。
MODEL_CATALOG: list[dict[str, Any]] = [
    {"id": "claude-opus-4-8", "name": "Claude Opus 4.8", "owner": "anthropic",
     "llm_config_id": "65d9536f-09da-4acd-8301-3b3f48ab42bc"},
    {"id": "claude-sonnet-4-5-20250929", "name": "Claude Sonnet 4.5", "owner": "anthropic",
     "llm_config_id": "956dd263-53e6-4432-b16e-e84a76d31c4c"},
    {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro", "owner": "deepseek",
     "llm_config_id": "4be4fc61-1955-4dca-888d-119983894de4"},
    {"id": "gemini-3.1-pro-preview", "name": "Gemini 3.1 Pro Preview", "owner": "google",
     "llm_config_id": "c6e92cae-e268-492e-bf93-c3e7264c2c02"},
    {"id": "gemini-3.5-flash", "name": "Gemini 3.5 Flash", "owner": "google",
     "llm_config_id": "04d2b372-d1c1-4092-b628-d5f223346065"},
    {"id": "glm-5.2", "name": "GLM 5.2", "owner": "zhipu",
     "llm_config_id": "72eab25d-9761-4acc-a251-f0640108d409"},
    {"id": "gpt-5.5", "name": "GPT 5.5", "owner": "openai",
     "llm_config_id": "664a927e-29d8-42bb-8622-5cde7cf241f5"},
    {"id": "kimi-k2.6", "name": "Kimi K2.6", "owner": "moonshot",
     "llm_config_id": "a75616ad-23b1-43f6-baf2-ca499cbf2723"},
    {"id": "kimi-k2.7-code", "name": "Kimi K2.7 Code", "owner": "moonshot",
     "llm_config_id": "5d096eb2-100e-4f24-bf08-12f627ce8b0d"},
    {"id": "minimax-m3", "name": "Minimax M3", "owner": "minimax",
     "llm_config_id": "925a2142-b0bd-47d7-b31a-918ccbdb1e59"},
]

# id 与显示名（小写）都能定位模型
_BY_KEY: dict[str, dict[str, Any]] = {}
for _m in MODEL_CATALOG:
    _BY_KEY[_m["id"]] = _m
    _BY_KEY[_m["name"].lower()] = _m


def supported_models() -> list[dict[str, Any]]:
    """OpenAI 兼容的 /v1/models 列表。"""
    return [{"id": m["id"], "object": "model", "owned_by": f"{m['owner']}@69gg/promptql2api"}
            for m in MODEL_CATALOG]


def normalize_model(model: str | None) -> str:
    """客户端传的 model 归一化为 catalog id；空或未知→默认。

    匹配：精确 id > 精确显示名（不区分大小写）> 去除非字母数字后模糊匹配。
    """
    if not model:
        return DEFAULT_MODEL
    if model in _BY_KEY:
        return _BY_KEY[model]["id"]
    low = model.lower()
    if low in _BY_KEY:
        return _BY_KEY[low]["id"]
    norm = re.sub(r"[^a-z0-9]", "", low)
    for m in MODEL_CATALOG:
        if (re.sub(r"[^a-z0-9]", "", m["id"]) == norm
                or re.sub(r"[^a-z0-9]", "", m["name"].lower()) == norm):
            return m["id"]
    return DEFAULT_MODEL


def llm_config_id_for(model_id: str) -> str | None:
    """catalog id → start_thread 的 llmConfigId（UUID）。默认模型返回 None（用项目默认）。"""
    m = _BY_KEY.get(model_id)
    return m["llm_config_id"] if m else None


def _thinking_text(block: dict[str, Any]) -> str | None:
    """从 Anthropic thinking / redacted_thinking block 提取文本。"""
    if block.get("type") == "thinking":
        t = block.get("thinking")
        if isinstance(t, str) and t:
            return f"<thinking>\n{t}\n</thinking>"
    if block.get("type") == "redacted_thinking":
        return "<redacted_thinking/>"
    return None


def _reasoning_text(block: dict[str, Any]) -> str | None:
    """从 OpenAI Responses reasoning block 提取 summary 文本。"""
    if block.get("type") != "reasoning":
        return None
    summary = block.get("summary") or []
    texts: list[str] = []
    for s in summary:
        if isinstance(s, dict) and s.get("type") == "summary_text":
            texts.append(s.get("text", ""))
    text = "".join(texts)
    if text:
        return f"<reasoning>\n{text}\n</reasoning>"
    return None


def flatten_text(content: Any) -> str:
    """OpenAI/Anthropic content（str 或 content block 数组）→ 纯文本。

    同时保留 content 中的 thinking / reasoning block，用 XML 围栏包装后一并返回，
    避免外部客户端传入的 CoT 在 PromptQL 侧丢失。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for c in content:
            if isinstance(c, dict):
                t = c.get("type")
                if t in ("text", "input_text", "output_text"):
                    out.append(c.get("text", ""))
                elif t == "thinking" or t == "redacted_thinking":
                    cot = _thinking_text(c)
                    if cot:
                        out.append(cot)
                elif t == "reasoning":
                    cot = _reasoning_text(c)
                    if cot:
                        out.append(cot)
                elif "text" in c:
                    out.append(str(c["text"]))
            else:
                out.append(str(c))
        return "\n\n".join(out)
    return str(content)


def _assistant_tool_call_jsons(m: dict[str, Any]) -> list[str]:
    """提取 assistant 消息里的工具调用（兼容 OpenAI tool_calls 与 Anthropic tool_use block），
    返回每个调用的 JSON 字符串（{"name":..., "arguments":...}）。

    PromptQL 的 agent 识别「自己之前输出过的 <tool_call> 围栏」并强模仿（few-shot 效应），
    所以把历史 tool_call 渲染成围栏送过去，比丢弃显著提高后续工具调用成功率。
    """
    blocks: list[str] = []
    for tc in (m.get("tool_calls") or []):  # OpenAI
        fn = (tc or {}).get("function") or {}
        raw = fn.get("arguments", "{}")
        try:
            args = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (json.JSONDecodeError, ValueError):
            args = {}
        blocks.append(json.dumps({"name": fn.get("name", ""), "arguments": args}, ensure_ascii=False))
    content = m.get("content")
    if isinstance(content, list):  # Anthropic tool_use blocks
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                blocks.append(json.dumps(
                    {"name": c.get("name", ""), "arguments": c.get("input") or {}}, ensure_ascii=False))
    return blocks


def extract_user_prompt(messages: list[dict[str, Any]]) -> str:
    """把 messages 拍平成发给 PromptQL 的单条用户消息（带角色与 system 前缀）。

    PromptQL 的 thread 是一次性的（每次请求新建），所以把整段历史压成一条消息。
    assistant 的历史工具调用渲染成 <tool_call> 围栏（few-shot），提高后续工具调用成功率。
    消息里可能携带的 reasoning_content / thinking block 也会保留，避免 CoT 上下文丢失。
    """
    parts: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        # OpenAI 风格：reasoning_content 可能放在 message 根上
        reasoning = m.get("reasoning_content")
        cot_prefix = f"<reasoning>\n{reasoning}\n</reasoning>\n\n" if isinstance(reasoning, str) and reasoning else ""

        if role == "system":
            # 软化包装：去掉硬标签 [system]，用柔和背景框架承载，降低 agent 身份对抗刺激
            # （不改一个字的实质内容，见 app.system_sanitizer）
            parts.append(f"{cot_prefix}{soften_system(flatten_text(m.get('content')), lang=ACTIVE_LANG)}")
        elif role == "assistant":
            body = flatten_text(m.get("content"))
            tc_jsons = _assistant_tool_call_jsons(m)
            if tc_jsons:
                fence = "\n".join(f"<tool_call>{b}</tool_call>" for b in tc_jsons)
                body = f"{body}\n{fence}".strip() if body else fence
            parts.append(f"{cot_prefix}[assistant]\n{body}")
        elif role == "tool":
            # 工具返回自然化为「观测」（tool_call_id 对 PromptQL agent 无意义，去掉），
            # 并引导 agent 在任务未完时继续下一步，而非把生硬协议字段当作终点
            content = flatten_text(m.get("content"))
            parts.append(f"{cot_prefix}[tool_result]\n{content}"
                         "\n\n(Observation above. Continue with the next step if the task isn't finished.)")
        else:
            parts.append(f"{cot_prefix}[user]\n{flatten_text(m.get('content'))}")
    return "\n\n".join(parts)
