"""PromptQL 账号凭据与账号池。

每个账号一份 ``account/<name>.json``，启动时全量加载；每次请求 round-robin
轮换一个账号，认证失败标记 disabled 并自动换号（见 :mod:`app.deps`）。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import fcntl
from pydantic import BaseModel


class Account(BaseModel):
    """单个 PromptQL 账号的凭据。"""

    name: str
    source_email: str = ""
    hasura_lux: str
    project_id: str
    project_name: str = ""
    created_at: str = ""
    disabled: bool = False

    @property
    def auth_cookies(self) -> dict[str, str]:
        """请求 auth.pro.ql.app 用的 cookie 头。"""
        return {"hasura-lux": self.hasura_lux}

    @classmethod
    def from_file(cls, path: Path) -> "Account":
        """从 json 文件加载一个 Account。"""
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)


class AccountPool:
    """账号池：启动加载全部，请求时 round-robin 轮换可用子集（同步线程安全）。"""

    def __init__(self, accounts: list[Account], account_dir: Path) -> None:
        # 按 name 排序保证 round-robin 游标确定性
        self._all: list[Account] = sorted(accounts, key=lambda a: a.name)
        self._dir = account_dir
        # 可用子集（非 disabled）的游标；用 threading.Lock 保证 next() 同步安全
        self._idx = 0
        self._lock = threading.Lock()

    @classmethod
    def load(cls, account_dir: Path) -> "AccountPool":
        """扫 account_dir 下的 *.json 构造账号池。

        目录不存在或无 json 抛 RuntimeError（提示运行注册机）。
        """
        if not account_dir.is_dir():
            raise RuntimeError(
                f"账号目录不存在: {account_dir}（请运行注册机注册账号，或用 "
                "scripts/migrate_env_to_toml.py 迁移）"
            )
        files = sorted(account_dir.glob("*.json"))
        if not files:
            raise RuntimeError(
                f"账号目录无 *.json: {account_dir}（请运行注册机注册账号，或迁移现有 .env）"
            )
        accounts = [Account.from_file(f) for f in files]
        return cls(accounts, account_dir)

    def all(self) -> list[Account]:
        """返回全部账号（含 disabled）。"""
        return list(self._all)

    def _available(self) -> list[Account]:
        return [a for a in self._all if not a.disabled]

    def next(self) -> Account:
        """round-robin 返回下一个可用账号；无可用抛 RuntimeError。"""
        with self._lock:
            avail = self._available()
            if not avail:
                raise RuntimeError("无可用 PromptQL 账号（全部 disabled）")
            if self._idx >= len(avail):
                self._idx = 0
            acc = avail[self._idx]
            self._idx = (self._idx + 1) % len(avail)
            return acc

    def mark_disabled(self, acc: Account) -> None:
        """把 acc 标记为 disabled 并原子写回对应 json，从轮换集移除。"""
        with self._lock:
            acc.disabled = True
            # 找到对应文件并写回（fcntl.flock 原子写）
            target = self._dir / f"{acc.name}.json"
            if target.is_file():
                tmp = target.with_suffix(".json.tmp")
                payload = acc.model_dump(mode="json")
                with tmp.open("w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                tmp.replace(target)
