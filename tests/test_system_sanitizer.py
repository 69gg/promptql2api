"""system_sanitizer.py 测试：软化包装 + 垃圾行移除（不动实质内容）。"""
from __future__ import annotations

from app.system_sanitizer import remove_junk_lines, soften_system


def test_remove_junk_lines_drops_billing_header() -> None:
    text = "x-anthropic-billing-header: secret\nYou are helpful.\nDo X."
    out = remove_junk_lines(text)
    assert "x-anthropic-billing-header" not in out
    assert "You are helpful." in out
    assert "Do X." in out


def test_remove_junk_lines_keeps_substantive_content() -> None:
    # 身份声明、工具指令、强制措辞一律保留（不动实质内容）
    text = ("You are Claude Code, Anthropic's official CLI.\n"
            "Use the provider-native tool-calling mechanism.\n"
            "You must call at least one tool.")
    out = remove_junk_lines(text)
    assert "You are Claude Code" in out
    assert "provider-native" in out
    assert "must call at least one tool" in out


def test_soften_system_replaces_hard_label() -> None:
    content = "You are a coding assistant. Always use Python."
    out = soften_system(content)
    assert "[system]" not in out  # 硬标签被替换
    assert "You are a coding assistant." in out  # 实质内容保留
    assert "Always use Python." in out
    assert "reference" in out.lower()  # 柔和框架（en 模板）


def test_soften_system_zh_wrapper() -> None:
    out = soften_system("你是一个助手。", lang="zh")
    assert "参考" in out
    assert "你是一个助手。" in out


def test_soften_system_empty() -> None:
    assert soften_system("") == ""
    assert soften_system("   ") == ""


def test_soften_system_does_not_mutate_substance() -> None:
    # 关键：身份声明 / 工具指令一个字都不改，只移除垃圾行 + 包一层柔和框架
    content = ("You are Claude Code.\n"
               "x-anthropic-billing-header: leak\n"
               "You must call tools.")
    out = soften_system(content)
    assert "You are Claude Code." in out
    assert "You must call tools." in out
    assert "x-anthropic-billing-header" not in out  # 仅垃圾行移除
