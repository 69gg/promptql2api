"""/admin/* 管理端点测试：鉴权、上传、列表、删除、重载。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.account import Account, AccountPool
from app.config import clear_settings_cache, get_settings


@pytest.fixture
def admin_client(tmp_path: Path):
    """构造一个启用 admin 的 TestClient，使用临时 account_dir。"""
    clear_settings_cache()
    account_dir = tmp_path / "account"
    account_dir.mkdir()

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[admin]\nauth_key = "admin-secret"\n[registry]\naccount_dir = "{account_dir}"\n',
        encoding="utf-8",
    )

    from app.main import app

    with TestClient(app) as client:
        settings = get_settings(str(cfg))
        pool = AccountPool([], account_dir)
        client.app.state.settings = settings
        client.app.state.pool = pool
        client.app.state.clients = {}
        yield client, pool, account_dir, settings

    clear_settings_cache()


def _sample_account(name: str = "uploaded") -> dict:
    return {
        "name": name,
        "source_email": "u@example.com",
        "hasura_lux": "lux-value",
        "project_id": "4712817f-3501-44d3-8a40-f74025a128ff",
        "project_name": "p-4712817f-3501",
        "created_at": "2026-07-02T14:22:33",
        "disabled": False,
    }


def test_admin_disabled_when_key_empty(tmp_path: Path) -> None:
    """admin_auth_key 为空时，/admin 端点返回 404。"""
    clear_settings_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text("[admin]\nauth_key = \"\"\n", encoding="utf-8")

    from app.main import app

    with TestClient(app) as client:
        client.app.state.settings = get_settings(str(cfg))
        r = client.get("/admin/accounts", headers={"Authorization": "Bearer x"})
        assert r.status_code == 404

    clear_settings_cache()


def test_admin_invalid_key_returns_401(admin_client) -> None:
    client, *_ = admin_client
    r = client.get("/admin/accounts", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401

    r = client.get("/admin/accounts?auth_key=wrong")
    assert r.status_code == 401


def test_admin_list_empty(admin_client) -> None:
    client, *_ = admin_client
    r = client.get("/admin/accounts", headers={"Authorization": "Bearer admin-secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert body["data"] == []


def test_admin_upload_creates_json_and_updates_pool(admin_client) -> None:
    client, pool, account_dir, _ = admin_client
    payload = _sample_account("acc1")

    r = client.post(
        "/admin/accounts",
        json=payload,
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "acc1"

    # 磁盘文件已写入
    file_path = account_dir / "acc1.json"
    assert file_path.is_file()
    on_disk = json.loads(file_path.read_text(encoding="utf-8"))
    assert on_disk["hasura_lux"] == "lux-value"

    # 内存 pool 已同步
    assert len(pool.all()) == 1
    assert pool.all()[0].name == "acc1"

    # clients 已同步
    assert "acc1" in client.app.state.clients


def test_admin_list_hides_sensitive_fields(admin_client) -> None:
    client, *_ = admin_client
    client.post(
        "/admin/accounts",
        json=_sample_account("acc1"),
        headers={"Authorization": "Bearer admin-secret"},
    )

    r = client.get("/admin/accounts?auth_key=admin-secret")
    item = r.json()["data"][0]
    assert item["name"] == "acc1"
    assert "hasura_lux" not in item
    assert "project_id" not in item
    assert "project_name" in item


def test_admin_get_detail(admin_client) -> None:
    client, *_ = admin_client
    client.post(
        "/admin/accounts",
        json=_sample_account("acc1"),
        headers={"Authorization": "Bearer admin-secret"},
    )

    r = client.get("/admin/accounts/acc1?auth_key=admin-secret")
    assert r.status_code == 200
    body = r.json()
    assert body["hasura_lux"] == "lux-value"
    assert body["project_id"] == "4712817f-3501-44d3-8a40-f74025a128ff"


def test_admin_update_existing_account(admin_client) -> None:
    client, pool, account_dir, _ = admin_client
    client.post(
        "/admin/accounts",
        json=_sample_account("acc1"),
        headers={"Authorization": "Bearer admin-secret"},
    )

    updated = _sample_account("acc1")
    updated["hasura_lux"] = "lux-updated"
    updated["disabled"] = True

    r = client.post(
        "/admin/accounts",
        json=updated,
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert r.status_code == 200

    on_disk = json.loads((account_dir / "acc1.json").read_text(encoding="utf-8"))
    assert on_disk["hasura_lux"] == "lux-updated"
    assert on_disk["disabled"] is True
    assert pool.all()[0].hasura_lux == "lux-updated"


def test_admin_delete_account(admin_client) -> None:
    client, pool, account_dir, _ = admin_client
    client.post(
        "/admin/accounts",
        json=_sample_account("acc1"),
        headers={"Authorization": "Bearer admin-secret"},
    )

    r = client.delete("/admin/accounts/acc1?auth_key=admin-secret")
    assert r.status_code == 200
    assert r.json()["deleted"] is True

    assert not (account_dir / "acc1.json").exists()
    assert pool.all() == []
    assert "acc1" not in client.app.state.clients


def test_admin_delete_not_found(admin_client) -> None:
    client, *_ = admin_client
    r = client.delete("/admin/accounts/no-such?auth_key=admin-secret")
    assert r.status_code == 404


def test_admin_reload_syncs_from_disk(admin_client) -> None:
    client, pool, account_dir, _ = admin_client
    # 直接写盘一个账号
    acc = Account(name="manual", hasura_lux="m", project_id="p")
    (account_dir / "manual.json").write_text(
        json.dumps(acc.model_dump(mode="json")), encoding="utf-8"
    )

    assert pool.all() == []

    r = client.post("/admin/reload?auth_key=admin-secret")
    assert r.status_code == 200
    assert r.json()["reloaded"] is True
    assert r.json()["count"] == 1

    assert len(pool.all()) == 1
    assert pool.all()[0].name == "manual"
    assert "manual" in client.app.state.clients
