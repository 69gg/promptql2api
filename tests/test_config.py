"""get_settings / config.toml 加载测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import clear_settings_cache, get_settings


@pytest.fixture(autouse=True)
def _reset_cache():
    """每个测试前后清缓存，避免 lru_cache 串扰。"""
    clear_settings_cache()
    yield
    clear_settings_cache()


_FULL_TOML = """\
[gateway]
host = "127.0.0.1"
port = 9999
api_key = "secret-key"

[promptql]
timezone = "UTC"
agent_response_config = "force_skip"
poll_interval = 0.5
request_timeout = 30.0
token_refresh_margin = 120
auth_token_url = "https://example.test/token"
graphql_url = "https://example.test/gql"

[registry]
account_dir = "my_accounts"

[email]
base_url = "https://mail.example.com"  # 仅注册机用，主程序忽略
"""


def test_load_full_toml(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(_FULL_TOML, encoding="utf-8")
    s = get_settings(str(cfg))

    assert s.host == "127.0.0.1"
    assert s.port == 9999
    assert s.gateway_api_key == "secret-key"
    assert s.timezone == "UTC"
    assert s.agent_response_config == "force_skip"
    assert s.poll_interval == 0.5
    assert s.request_timeout == 30.0
    assert s.token_refresh_margin == 120
    assert s.auth_token_url == "https://example.test/token"
    assert s.graphql_url == "https://example.test/gql"
    assert s.account_dir == "my_accounts"


def test_defaults_when_file_missing(tmp_path: Path) -> None:
    s = get_settings(str(tmp_path / "nonexistent.toml"))
    assert s.host == "0.0.0.0"
    assert s.port == 8088
    assert s.gateway_api_key == ""
    assert s.timezone == "Asia/Shanghai"
    assert s.agent_response_config == ""
    assert s.account_dir == "account"
    assert s.token_refresh_margin == 300


def test_partial_toml_uses_defaults(tmp_path: Path) -> None:
    """只写 [gateway]，promptql/registry 段缺失时用默认值。"""
    cfg = tmp_path / "config.toml"
    cfg.write_text("[gateway]\nport = 7000\n", encoding="utf-8")
    s = get_settings(str(cfg))
    assert s.port == 7000
    assert s.timezone == "Asia/Shanghai"  # 默认
    assert s.account_dir == "account"      # 默认


def test_email_section_ignored(tmp_path: Path) -> None:
    """[email] 是注册机专用，铺平时被忽略，不影响 Settings。"""
    cfg = tmp_path / "config.toml"
    cfg.write_text("[email]\nbase_url = \"https://x\"\n[gateway]\nport = 1\n", encoding="utf-8")
    s = get_settings(str(cfg))
    # Settings 没有 base_url 字段，不应报错（pydantic 默认忽略额外字段）
    assert s.port == 1


def test_clear_cache_rereads(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("[gateway]\nport = 1111\n", encoding="utf-8")
    assert get_settings(str(cfg)).port == 1111
    cfg.write_text("[gateway]\nport = 2222\n", encoding="utf-8")
    # 未清缓存仍读旧值
    assert get_settings(str(cfg)).port == 1111
    clear_settings_cache()
    assert get_settings(str(cfg)).port == 2222
