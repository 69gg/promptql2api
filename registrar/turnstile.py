"""Cloudflare Turnstile 求解器（多策略，可配置）。

实测（2026-07-02）：prompt.ql.app 的 Turnstile 严格检测自动化指纹，playwright chromium
（headless/非无头）与 camoufox firefox 都过不了；只有真实日常浏览器（或人类交互）能过。
故 solver 抽象为三种可配置策略，由 config.toml ``[turnstile].method`` 选择：

- ``semi``（默认）：playwright 弹浏览器到登录页，等 widget 自动过或用户手动点一下
  （人类交互能过）。注册机自动填邮箱、检测到 token 后全自动继续。
- ``cdp``：playwright ``connect_over_cdp`` 连接你已开的 debug chrome
  （``--remote-debugging-port=9222``），真实指纹自动过。
- ``api``：第三方打码 API（CapSolver 等），无浏览器全自动，付费。

各策略都返回 ``cf-turnstile-response`` token 字符串。
"""
from __future__ import annotations

import time
from typing import Protocol

from .models import TurnstileConfig

TOKEN_SELECTOR = 'input[name="cf-turnstile-response"]'


class TurnstileSolver(Protocol):
    """求解器接口：返回 Turnstile token。"""

    def solve(self, url: str, sitekey: str, *, timeout_ms: int) -> str: ...


def make_solver(cfg: TurnstileConfig, *, proxy: str | None = None) -> TurnstileSolver:
    """按 config 的 method 选策略。"""
    method = (cfg.method or "semi").lower()
    if method == "semi":
        return _SemiSolver(cfg, proxy)
    if method == "cdp":
        return _CdpSolver(cfg, proxy)
    if method == "api":
        return _ApiSolver(cfg)
    raise ValueError(f"未知 turnstile method: {method!r}（可选 semi/cdp/api）")


def _wait_token(page: object, timeout_ms: int, *, click: bool = True) -> str:
    """循环读 cf-turnstile-response；click=True 时尝试点 widget checkbox。"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            loc = page.locator(TOKEN_SELECTOR)  # type: ignore[attr-defined]
            if loc.count() > 0:
                val = loc.first.input_value()
                if val:
                    return val
        except Exception:  # noqa: BLE001
            pass
        if click:
            _try_click_widget(page)
        time.sleep(1.5)
    return ""


def _try_click_widget(page: object) -> None:
    try:
        for frame in page.frames:  # type: ignore[attr-defined]
            if "challenges.cloudflare.com" in (frame.url or ""):
                box = frame.locator("input[type=checkbox]")
                if box.count() > 0:
                    box.first.click(timeout=1000)
    except Exception:  # noqa: BLE001
        pass


class _SemiSolver:
    """半自动：playwright 弹浏览器，等用户点或自动过。"""

    def __init__(self, cfg: TurnstileConfig, proxy: str | None) -> None:
        self._cfg = cfg
        self._proxy = proxy or cfg.proxy_url or None

    def solve(self, url: str, sitekey: str, *, timeout_ms: int) -> str:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            launch_kwargs: dict[str, object] = {"headless": self._cfg.headless}
            if self._proxy:
                launch_kwargs["proxy"] = {"server": self._proxy}
            browser = p.chromium.launch(**launch_kwargs)  # type: ignore[arg-type]
            try:
                page = browser.new_context().new_page()
                page.goto(url, wait_until="domcontentloaded")
                token = _wait_token(page, timeout_ms)
                if not token:
                    raise RuntimeError(
                        "Turnstile 半自动超时：请在弹出的浏览器里完成验证；"
                        "若一直弹不出/过不了，可改用 cdp（连 debug chrome）或 api（打码）方法"
                    )
                return token
            finally:
                browser.close()


class _CdpSolver:
    """CDP：connect_over_cdp 连接你已开的 debug chrome（真实指纹自动过）。"""

    def __init__(self, cfg: TurnstileConfig, proxy: str | None) -> None:
        self._cfg = cfg
        self._endpoint = cfg.cdp_endpoint or "http://localhost:9222"

    def solve(self, url: str, sitekey: str, *, timeout_ms: int) -> str:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(self._endpoint)
            try:
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded")
                token = _wait_token(page, timeout_ms, click=False)
                if not token:
                    raise RuntimeError(
                        f"Turnstile CDP 超时：确认 chrome 用 "
                        f"--remote-debugging-port 已开（{self._endpoint}）"
                    )
                return token
            finally:
                # 只关页面，不断 CDP（不关用户 chrome）
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass


class _ApiSolver:
    """打码 API：CapSolver（可扩展 2captcha 等）。"""

    def __init__(self, cfg: TurnstileConfig) -> None:
        self._cfg = cfg

    def solve(self, url: str, sitekey: str, *, timeout_ms: int) -> str:
        provider = (self._cfg.api_provider or "capsolver").lower()
        if not self._cfg.api_key:
            raise RuntimeError("turnstile api 方法需在 config [turnstile].api_key 配 key")
        if provider == "capsolver":
            return _solve_capsolver(url, sitekey, self._cfg.api_key, timeout_ms)
        raise NotImplementedError(
            f"打码 provider {provider!r} 暂未实现，可在 _ApiSolver 补充"
        )


def _solve_capsolver(url: str, sitekey: str, key: str, timeout_ms: int) -> str:
    """CapSolver AntiTurnstileTaskProxyLess。"""
    from curl_cffi import requests as cffi_requests

    create = cffi_requests.post(
        "https://api.capsolver.com/createTask",
        json={
            "clientKey": key,
            "task": {
                "type": "AntiTurnstileTaskProxyLess",
                "websiteURL": url,
                "websiteKey": sitekey,
            },
        },
        timeout=30,
        impersonate="chrome136",
    ).json()
    task_id = create.get("taskId")
    if not task_id:
        raise RuntimeError(f"capsolver createTask 失败: {create}")
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        r = cffi_requests.post(
            "https://api.capsolver.com/getTaskResult",
            json={"clientKey": key, "taskId": task_id},
            timeout=30,
            impersonate="chrome136",
        ).json()
        if r.get("status") == "ready":
            return str(r["solution"]["token"])
        if r.get("errorId"):
            raise RuntimeError(f"capsolver 求解失败: {r}")
        time.sleep(3)
    raise RuntimeError("capsolver 求解超时")
