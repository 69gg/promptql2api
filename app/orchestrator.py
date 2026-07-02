"""语义级重试编排：在 ``PromptQLClient.stream_thread`` 与 adapter 之间插一层。

整轮 buffer 一轮回复 → 检测 agent 拒绝/身份识破 → 换认知重构角度重建 prompt 重试，把
「单次命中率」累积成「多次命中率」。与 :class:`app.deps._RetryingClient`（账号级 503 换号）
正交：那层处理认证失败，本层处理 agent 语义级拒绝。

PromptQL 文本整块返回（非逐 token），故「整轮 buffer 后再吐」不破坏伪流式体验；且只在
命中拒绝时才重试（少数情况），多数请求一次过。

``client`` duck-type：任何带 ``async stream_thread(message, llm_config_id=...)`` 的对象均可
（:class:`PromptQLClient` / :class:`_RetryingClient`）。
"""
from __future__ import annotations

import sys
from typing import Any, AsyncIterator

from app.promptql.events import IREvent
from app.reframe_angles import ACTIVE_ANGLE, ACTIVE_LANG, RETRY_ORDER, Angle, Lang, build_directive
from app.refusal import is_refusal
from app.tools import ToolDef, parse_tool_calls


async def _collect_round(
    client: Any, prompt: str, llm_config_id: str | None,
) -> tuple[list[IREvent], str, bool]:
    """跑一轮 ``stream_thread``，收集全部 IREvent 并拼接 text。

    返回 ``(events, full_text, had_error)``。``had_error`` 表示本轮含 error 事件（底层异常，
    透传不重试；认证失败已由 :class:`_RetryingClient` 捕获换号）。
    """
    events: list[IREvent] = []
    parts: list[str] = []
    had_error = False
    async for ir in client.stream_thread(prompt, llm_config_id=llm_config_id):
        events.append(ir)
        if ir.kind == "error":
            had_error = True
            break
        if ir.kind == "text" and ir.text:
            parts.append(ir.text)
        if ir.kind == "finish":
            break
    return events, "".join(parts), had_error


async def stream_with_retry(
    client: Any,
    base_prompt: str,
    tools: list[ToolDef],
    llm_config_id: str | None = None,
    *,
    max_retries: int | None = None,
    lang: Lang = ACTIVE_LANG,
) -> AsyncIterator[IREvent]:
    """驱动 ``client.stream_thread``，agent 拒绝/识破时换角度重试，yield 最终轮 IREvent 流。

    - ``base_prompt`` **不含** directive；directive 由本函数按角度拼接（重试换角度时变化）。
    - 无 tools 时不判拒绝（纯对话请求 agent 拒绝可能是合理的），一轮即止。
    - 有 tools 时：产出 tool_call，或非拒绝的纯文本回复 → 即止；命中拒绝/识破 → 换
      :data:`RETRY_ORDER` 下一角度重试，最多 ``max_retries`` 次（默认读 ``config.tool_call_retries``）。
    - 底层 error 事件透传，不重试。
    - 耗尽重试仍拒绝 → 输出最后一轮（拒绝文本回退，与 README「未命中回退文本」一致）。
    """
    has_tools = bool(tools)
    if max_retries is None:
        from app.config import get_settings
        max_retries = get_settings().tool_call_retries
    max_attempts = 1 + (max_retries if has_tools and max_retries > 0 else 0)
    known = {t.name for t in tools} if has_tools else set()

    chosen: list[IREvent] = []
    for attempt in range(max_attempts):
        angle: Angle = RETRY_ORDER[attempt % len(RETRY_ORDER)] if has_tools else ACTIVE_ANGLE
        directive = build_directive(angle, lang, tools) if has_tools else ""
        prompt = f"{directive}\n\n{base_prompt}" if directive else base_prompt
        events, full_text, had_error = await _collect_round(client, prompt, llm_config_id)
        if had_error:
            for ev in events:
                yield ev
            return
        chosen = events
        if not has_tools:
            break
        if parse_tool_calls(full_text, known_names=known):
            break  # 成功产出 tool_call
        if not is_refusal(full_text, has_tools=True):
            break  # 非拒绝的纯文本回复（agent 选择不用工具）→ 不重试
        # 命中拒绝/识破：还有名额则换角度重来，否则保留这轮（回退）
        if attempt + 1 >= max_attempts:
            break
        next_angle = RETRY_ORDER[(attempt + 1) % len(RETRY_ORDER)]
        print(f"[orchestrator] agent refusal detected (angle {angle}); retry with {next_angle}",
              file=sys.stderr)
    for ev in chosen:
        yield ev
