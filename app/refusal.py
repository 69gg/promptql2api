"""agent 拒绝 / 身份识破检测。

PromptQL agent 拒绝「伪造工具调用」时常出现固定措辞：亮明自身身份（"I'm main, the
PromptQL agent"）、声明越权（"that's not how I operate"、"isn't one of my capabilities"）、
或直接拒绝（"I can't / I won't"）。检测到这些信号即判定为拒绝，:mod:`app.orchestrator`
据此重试（换认知重构角度 / 加强制约束），把单次命中率累积成多次命中率。

注意：agent 拒绝时常**引用** ``<tool_call>`` 围栏格式来解释「我被要求做什么」，此时文本里
虽含围栏，却并非真实工具调用——:func:`app.tools.parse_tool_calls` 也会据此跳过提取。
"""
from __future__ import annotations

# agent 拒绝 / 亮明身份 / 声明越权时常出现的措辞（中英）。命中任一即视为拒绝信号。
# 这些是 PromptQL agent 识破「伪造工具调用」时的典型反应，与具体客户端/工具无关。
REFUSAL_PHRASES: tuple[str, ...] = (
    # 直接拒绝
    "i can't", "i cannot", "i won't", "i will not",
    "i'm not going to", "i am not going to",
    "i'm not able", "i am not able",
    "not able to produce", "not able to help",
    "can't help", "cannot help", "can't generate", "can't produce", "can't emit",
    # 操作方式声明（"that's not how I operate"）
    "i don't operate", "not how i operate", "isn't how i operate",
    # 亮明 PromptQL agent 身份（识破的强信号）
    "i'm main", "i am main", "i'm the promptql", "i am the promptql", "the promptql agent",
    "i'm the ai agent", "i am the ai agent", "as the promptql agent", "as main,",
    # 声明越权 / 不在能力范围
    "isn't one of my capabilities", "doesn't correspond", "outside what i do",
    "outside my capabilities", "beyond what i do", "not within my capabilities",
    "i'm not able to fabricate", "i won't fabricate",
    # 中文
    "我不能", "我无法", "我不会", "我做不到",
    "不是我的操作", "不是我的能力", "超出我的能力", "不在我的能力范围",
    "我是 promptql", "我是 main", "作为 promptql",
)


def looks_refusal(text: str) -> bool:
    """文本是否命中拒绝 / 身份识破措辞（子串匹配，大小写不敏感）。"""
    if not text:
        return False
    low = text.lower()
    return any(p in low for p in REFUSAL_PHRASES)


def is_refusal(text: str, *, has_tools: bool) -> bool:
    """判定一次 agent 回复是否构成「拒绝 / 识破」需要重试。

    - 无 tools 的纯对话请求不判拒绝（agent 拒绝可能是合理的，如用户请求越界内容）。
    - 有 tools 的请求里命中拒绝措辞 → True（需重试换角度）。
    - 成功产出 tool_call 的情况由调用方结合 parse 结果判断（parse 在拒绝时本就返回空）。
    """
    if not has_tools or not text:
        return False
    return looks_refusal(text)
