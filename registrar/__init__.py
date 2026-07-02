"""PromptQL 全自动注册机（独立包，主程序不 import）。

协议式注册 prompt.ql.app 账号：临邮 + Cloudflare Turnstile 求解 + OTP 验证 → 提取
``hasura-lux`` cookie 与 project → 写 ``account/<name>.json`` 供网关账号池加载。

协议依据见 :doc:`PROTOCOL`。运行需 ``uv sync --extra registrar`` + ``playwright install chromium``。
"""
from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
