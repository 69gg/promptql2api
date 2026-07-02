"""Cloudflare Temp Email 客户端：创建临时邮箱 + 轮询 PromptQL 验证码。

参考 ``/data0/CrazyRegistrants/lib/email_client.py``，对接自建 Cloudflare Temp Email
服务（https://github.com/dreamhunter2333/cloudflare_temp_email）。
"""
from __future__ import annotations

import random
import re
import string
import time
from dataclasses import dataclass

from curl_cffi import requests as cffi_requests

from .models import EmailConfig

# PromptQL 验证码邮件特征：Subject "Your PromptQL sign-in code: 678075"
_SUBJECT_CODE_RE = re.compile(r"sign-in code[:\s]*(\d{6})", re.IGNORECASE)
# 兜底：正文里大字号、字间距的 6 位数字块
_BODY_CODE_RE = re.compile(r"letter-spacing[^>]*>\s*(\d{6})", re.IGNORECASE)


@dataclass
class TempEmail:
    """一个临时邮箱地址及其访问令牌。"""

    address: str
    jwt: str
    address_id: int

    @property
    def localpart(self) -> str:
        """@ 前的本地部分，用作 account 名。"""
        return self.address.split("@", 1)[0]


def _rand_name(prefix: str = "pq") -> str:
    """生成随机邮箱本地名。"""
    return prefix + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def create_email(cfg: EmailConfig) -> TempEmail:
    """向 Temp Email 服务申请新地址（最多重试 5 次应对偶发冲突）。"""
    headers = {
        "Content-Type": "application/json",
        "x-admin-auth": cfg.admin_auth,
        "x-custom-auth": cfg.custom_auth,
    }
    last = ""
    for _ in range(5):
        body = {"name": _rand_name(), "enablePrefix": False, "domain": cfg.domain}
        r = cffi_requests.post(
            f"{cfg.base_url}/admin/new_address",
            json=body,
            headers=headers,
            timeout=15,
            impersonate="chrome136",
        )
        if r.status_code == 200:
            d = r.json()
            return TempEmail(
                address=str(d.get("address", "")).strip(),
                jwt=str(d.get("jwt", "")).strip(),
                address_id=int(d.get("address_id", 0)),
            )
        last = f"{r.status_code} {r.text[:200]}"
        time.sleep(0.5)
    raise RuntimeError(f"create_email 失败: {last}")


def _extract_code(raw: str) -> str | None:
    m = _SUBJECT_CODE_RE.search(raw)
    if m:
        return m.group(1)
    m = _BODY_CODE_RE.search(raw)
    return m.group(1) if m else None


def poll_code(jwt: str, cfg: EmailConfig, *, timeout: int | None = None) -> str:
    """轮询收件箱，返回 PromptQL 6 位验证码（超时抛错）。

    列表接口返回的 mail 含完整 raw，无需再拉单封。
    """
    deadline = time.time() + (timeout if timeout is not None else cfg.poll_timeout)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {jwt}",
        "x-custom-auth": cfg.custom_auth,
    }
    seen: set[str] = set()
    while time.time() < deadline:
        try:
            r = cffi_requests.get(
                f"{cfg.base_url}/api/mails?limit=10&offset=0",
                headers=headers,
                timeout=15,
                impersonate="chrome136",
            )
            if r.status_code == 200:
                for mail in r.json().get("results", []):
                    mid = str(mail.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    code = _extract_code(str(mail.get("raw", "")))
                    if code:
                        return code
        except Exception:  # noqa: BLE001 - 轮询容错
            pass
        time.sleep(1.5)
    raise RuntimeError("poll_code 超时：未收到 PromptQL 验证邮件")
