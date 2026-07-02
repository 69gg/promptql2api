"""单账号注册编排。

流程：申请临邮 → 解 Turnstile → POST /otp/send → 收 OTP → POST /otp/verify
→ 提取 ``hasura-lux`` → 查 ``ddn_projects`` → 写 ``account/<name>.json``。

协议依据见 :doc:`PROTOCOL`。注册机依赖主程序的 :class:`app.account.Account`
（单向：注册机 import app，主程序不 import registrar）。
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import json
from pathlib import Path

from app.account import Account

from .email_client import create_email, poll_code
from .http_client import HttpClient
from .models import RegistrarConfig
from .turnstile import make_solver

LOGIN_URL = "https://prompt.ql.app/login"
TURNSTILE_SITEKEY = "0x4AAAAAADsy_TOiX96NjTFT"
OTP_SEND_URL = "https://auth.pro.ql.app/otp/send"
OTP_VERIFY_URL = "https://auth.pro.ql.app/otp/verify"
CONSOLE_GQL_URL = "https://data.pro.ql.app/v1/graphql"

_JSON_HEADERS: dict[str, str] = {
    "content-type": "application/json",
    "origin": "https://prompt.ql.app",
    "referer": "https://prompt.ql.app/",
}


def _now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def register_one(
    cfg: RegistrarConfig,
    http: HttpClient,
    *,
    proxy: str | None = None,
    turnstile_method: str | None = None,
) -> Account:
    """注册一个 PromptQL 账号，写盘 ``account/<name>.json`` 并返回 Account。"""
    # 1. 临时邮箱
    email = create_email(cfg.email)

    # 2. 解 Turnstile（sitekey 固定，见 PROTOCOL；策略由 config [turnstile].method 决定）
    if turnstile_method:
        cfg.turnstile.method = turnstile_method
    solver = make_solver(cfg.turnstile, proxy=proxy)
    captcha_token = solver.solve(LOGIN_URL, TURNSTILE_SITEKEY, timeout_ms=180000)

    # 3. 发送 OTP → 拿 nonce（verify 要回传）
    r = http.post_json(
        OTP_SEND_URL,
        {"email": email.address, "captcha_token": captcha_token},
        headers=_JSON_HEADERS,
    )
    if r.status_code != 200:
        raise RuntimeError(f"otp/send 失败: {r.status_code} {r.text[:200]}")
    nonce = (r.json() or {}).get("nonce", "")
    if not nonce:
        raise RuntimeError("otp/send 未返回 nonce")

    # 4. 轮询收 OTP
    code = poll_code(email.jwt, cfg.email)

    # 5. 验证 OTP（body: email + otp + nonce，见 PROTOCOL）→ 拿 hasura-lux
    r2 = http.post_json(
        OTP_VERIFY_URL,
        {"email": email.address, "otp": code, "nonce": nonce},
        headers=_JSON_HEADERS,
    )
    if r2.status_code != 200:
        raise RuntimeError(f"otp/verify 失败: {r2.status_code} {r2.text[:200]}")
    hasura_lux = r2.cookies.get("hasura-lux")
    if not hasura_lux:
        raise RuntimeError("otp/verify 返回 200 但未带 hasura-lux cookie")

    # 6. 查 project
    project_id, project_name = _resolve_project(http, hasura_lux)

    # 7. 写盘
    name = _unique_name(cfg.account_dir, email.localpart)
    acc = Account(
        name=name,
        source_email=email.address,
        hasura_lux=hasura_lux,
        project_id=project_id,
        project_name=project_name,
        created_at=_now_iso(),
        disabled=False,
    )
    _save_account(cfg.account_dir, acc)
    return acc


def _resolve_project(http: HttpClient, hasura_lux: str) -> tuple[str, str]:
    """用 hasura-lux 查控制平面 ``ddn_projects``，返回 (project_id, project_name)。"""
    headers = {
        "content-type": "application/json",
        "hasura-client-name": "hasura-console",
        "cookie": f"hasura-lux={hasura_lux}",
    }
    r = http.post_json(
        CONSOLE_GQL_URL, {"query": "{ ddn_projects { id name } }"}, headers=headers
    )
    if r.status_code != 200:
        raise RuntimeError(f"查 project 失败: {r.status_code} {r.text[:200]}")
    data = (r.json() or {}).get("data", {}) or {}
    projects = data.get("ddn_projects", []) or []
    if not projects:
        raise RuntimeError(
            "新账号 ddn_projects 为空：需在 prompt.ql.app 完成 onboarding 创建首个 project，"
            "再手动补 account json 的 project_id/project_name"
        )
    first = projects[0]
    return str(first.get("id", "")), str(first.get("name", ""))


def _unique_name(account_dir: Path, base: str) -> str:
    """account/<name>.json 重名时加 -2/-3...。"""
    name = base
    idx = 2
    while (account_dir / f"{name}.json").exists():
        name = f"{base}-{idx}"
        idx += 1
    return name


def _save_account(account_dir: Path, acc: Account) -> None:
    """原子写 account/<name>.json（fcntl.flock，与 AccountPool.mark_disabled 一致）。"""
    account_dir.mkdir(parents=True, exist_ok=True)
    target = account_dir / f"{acc.name}.json"
    tmp = target.with_suffix(".json.tmp")
    payload = acc.model_dump(mode="json")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    tmp.replace(target)
