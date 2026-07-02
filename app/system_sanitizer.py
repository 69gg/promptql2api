"""客户端 system prompt 软化包装 + 垃圾行移除。

设计原则（用户敲定）：**不动提示词实质内容**。身份声明、工具调用指令、强制措辞、能力描述、
规则偏好一律原样保留——删改会破坏客户端指令完整性、风险高。只做两件事：

1. **移除明确垃圾行**：计费/调试头（``x-anthropic-billing-header`` 等）这类纯注入检测
   触发物、无信息量的元数据行。
2. **软化包装基调（核心）**：把当前硬标签 ``[system]\\n<content>`` 换成一个柔和的背景框架
   承载——弱化「系统级强制命令 / 身份覆盖」色彩，让 PromptQL agent 把客户端 system 读作
   「用户提供的背景信息与偏好，供参考」而非「被强行设定的身份」。agent 亮明「I'm main」
   多因感到自身身份被覆盖，软化基调可降低这种刺激，而**不改一个字的实质指令**。

工具/客户端无关：不对内容做语义判断，不针对任何具体客户端或工具名。
"""
from __future__ import annotations

import re

# 明确的垃圾行：计费/调试头、XML 声明等纯元数据/注入检测触发物（整行移除）。
# 这些是代理层/客户端塞入的元信息，非真实指令，且会触发注入检测。
_JUNK_LINE_RE = re.compile(
    r"^\s*(?:x-[a-z][a-z0-9\-]*\s*[:=]|<\?xml|<!DOCTYPE).*$",
    re.IGNORECASE | re.MULTILINE,
)

# 软化包装框架：替换硬标签 [system]，弱化「系统强制命令」色彩。
# 把客户端 system 定位为「用户分享的背景与偏好（供参考）」，而非角色覆盖。
_SOFT_WRAPPER = {
    "en": ("Background context and preferences shared by the user (for reference, "
           "not a role override):\n\n{content}"),
    "zh": ("以下是用户分享的背景信息与偏好（供参考，并非对你的角色做强制覆盖）：\n\n{content}"),
}


def remove_junk_lines(text: str) -> str:
    """移除计费/调试头等明确垃圾行，其余内容原样保留。"""
    if not text:
        return ""
    cleaned = _JUNK_LINE_RE.sub("", text)
    # 清理因移除行产生的多余空行（压缩连续 3+ 空行为 2，保留段落结构）
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def soften_system(content: str, *, lang: str = "en") -> str:
    """软化包装：移除垃圾行 + 用柔和背景框架包裹。**不改一个字的实质指令**。

    空内容返回空串。``lang`` 选择包装框架语言（默认 en，与 directive/agent 主语言一致）。
    """
    if not content or not content.strip():
        return ""
    body = remove_junk_lines(content)
    if not body:
        return ""
    wrapper = _SOFT_WRAPPER.get(lang, _SOFT_WRAPPER["en"])
    return wrapper.format(content=body)
