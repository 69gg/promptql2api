"""Account / AccountPool 单元测试：解析、round-robin、mark_disabled 写回。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.account import Account, AccountPool


def _write_acc(d: Path, name: str, *, hasura: str = "h", pid: str = "p",
               disabled: bool = False) -> None:
    """写一个 account json 到目录 d。"""
    obj = {
        "name": name, "source_email": "", "hasura_lux": hasura, "project_id": pid,
        "project_name": f"p-{pid}", "created_at": "", "disabled": disabled,
    }
    (d / f"{name}.json").write_text(json.dumps(obj), encoding="utf-8")


def test_account_from_file(tmp_path: Path) -> None:
    _write_acc(tmp_path, "a", hasura="cookie-xyz", pid="uuid-1")
    acc = Account.from_file(tmp_path / "a.json")
    assert acc.name == "a"
    assert acc.hasura_lux == "cookie-xyz"
    assert acc.project_id == "uuid-1"
    assert acc.auth_cookies == {"hasura-lux": "cookie-xyz"}
    assert acc.disabled is False


def test_account_from_dict_minimal() -> None:
    """只给必填字段也能构造。"""
    acc = Account(name="x", hasura_lux="h", project_id="p")
    assert acc.source_email == ""
    assert acc.project_name == ""
    assert acc.created_at == ""
    assert acc.disabled is False


def test_account_pool_load_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="账号目录不存在"):
        AccountPool.load(tmp_path / "nope")


def test_account_pool_load_empty_dir(tmp_path: Path) -> None:
    # tmp_path 本身已存在但为空
    with pytest.raises(RuntimeError, match="无 \\*.json"):
        AccountPool.load(tmp_path)


def test_account_pool_load_and_round_robin(tmp_path: Path) -> None:
    """三个账号（含一个 disabled），next() 只命中可用子集并 round-robin。"""
    _write_acc(tmp_path, "z", pid="1")
    _write_acc(tmp_path, "a", pid="2")
    _write_acc(tmp_path, "m", pid="3", disabled=True)  # 被排除
    pool = AccountPool.load(tmp_path)

    names = [pool.next().name for _ in range(5)]
    # 可用子集按 name 排序 = [a, z]，round-robin 循环
    assert names == ["a", "z", "a", "z", "a"]


def test_mark_disabled_writes_json_and_skips(tmp_path: Path) -> None:
    _write_acc(tmp_path, "a", pid="1")
    _write_acc(tmp_path, "b", pid="2")
    pool = AccountPool.load(tmp_path)
    acc_a = next(a for a in pool.all() if a.name == "a")

    pool.mark_disabled(acc_a)
    assert acc_a.disabled is True

    # json 被写回 disabled:true
    on_disk = json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))
    assert on_disk["disabled"] is True

    # 重新加载验证 a 已被排除
    pool2 = AccountPool.load(tmp_path)
    names = [pool2.next().name for _ in range(4)]
    assert names == ["b", "b", "b", "b"]


def test_all_disabled_raises(tmp_path: Path) -> None:
    _write_acc(tmp_path, "a", pid="1", disabled=True)
    _write_acc(tmp_path, "b", pid="2", disabled=True)
    pool = AccountPool.load(tmp_path)
    with pytest.raises(RuntimeError, match="无可用"):
        pool.next()
