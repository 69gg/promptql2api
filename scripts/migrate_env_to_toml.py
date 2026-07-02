"""迁移脚本：.env → config.toml + account/main.json（幂等）。

读现有 .env（用户本地），生成：
- ``account/main.json``：HASURA_LUX/PROJECT_ID/PROJECT_NAME + created_at（source_email 留空）
- ``config.toml``：HOST/PORT/GATEWAY_API_KEY/TIMEZONE 等铺到 [gateway]/[promptql]；临邮/turnstile 留占位

幂等：目标已存在则跳过该文件并提示。不引入 python-dotenv，手写 KEY=VAL 解析。

用法::

    uv run python scripts/migrate_env_to_toml.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

# toml 写出（py3.13+ 无标准 tomllib.dump，用简单手写避免引入新依赖）
_TOML_HEADER = """\
# 由 scripts/migrate_env_to_toml.py 从 .env 生成；可按需手工编辑。
# 完整字段说明见 config.toml.example
"""


def _parse_env(path: Path) -> dict[str, str]:
    """手写解析 .env 的 KEY=VAL（忽略注释/空行，去首尾引号）。"""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if (len(val) >= 2) and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
    return out


def _toml_quote(s: str) -> str:
    """toml 基本字符串：用双引号，转义反斜杠和双引号。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_config_toml(path: Path, env: dict[str, str]) -> None:
    """把 HOST/PORT/GATEWAY_API_KEY/TIMEZONE 等铺到对应 section，临邮/turnstile 留占位。"""
    lines: list[str] = [_TOML_HEADER]

    lines.append("[gateway]")
    lines.append(f"host = {_toml_quote(env.get('HOST', '0.0.0.0'))}")
    lines.append(f"port = {int(env.get('PORT', '8088'))}")
    lines.append(f"api_key = {_toml_quote(env.get('GATEWAY_API_KEY', ''))}")
    lines.append("")

    lines.append("[promptql]")
    lines.append(f"timezone = {_toml_quote(env.get('TIMEZONE', 'Asia/Shanghai'))}")
    lines.append('agent_response_config = ""')
    lines.append("poll_interval = 1.2")
    lines.append("request_timeout = 120.0")
    lines.append("token_refresh_margin = 300")
    lines.append('auth_token_url = "https://auth.pro.ql.app/ddn/promptql/token"')
    lines.append('graphql_url = "https://data.prompt.ql.app/promptql/playground-v2-hge/v1/graphql"')
    lines.append("")

    lines.append("[registry]")
    lines.append('account_dir = "account"')
    lines.append("")

    lines.append("# ===== 以下仅注册机使用（主程序不读）=====")
    lines.append("[email]")
    lines.append('base_url = "https://your-mail-service.example.com"')
    lines.append('admin_auth = ""')
    lines.append('custom_auth = ""')
    lines.append('domain = "your-domain.com"')
    lines.append("poll_timeout = 120")
    lines.append("")
    lines.append("[turnstile]")
    lines.append("headless = true")
    lines.append("browser_count = 2")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def _write_account(path: Path, env: dict[str, str]) -> None:
    """生成 account/main.json（HASURA_LUX/PROJECT_ID/PROJECT_NAME + created_at）。"""
    obj = {
        "name": "main",
        "source_email": "",
        "hasura_lux": env.get("HASURA_LUX", ""),
        "project_id": env.get("PROJECT_ID", ""),
        "project_name": env.get("PROJECT_NAME", ""),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "disabled": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    config_path = root / "config.toml"
    account_path = root / "account" / "main.json"

    env = _parse_env(env_path)
    if not env:
        print(f"[skip] 未找到或为空: {env_path}", file=sys.stderr)
        return 1
    if not env.get("HASURA_LUX") or not env.get("PROJECT_ID"):
        print("[skip] .env 缺少 HASURA_LUX 或 PROJECT_ID，无法生成 account/main.json",
              file=sys.stderr)
        return 1

    # 幂等：已存在则跳过并提示
    wrote_any = False
    if account_path.is_file():
        print(f"[skip] 已存在 {account_path}（不覆盖）")
    else:
        _write_account(account_path, env)
        print(f"[ok]   写出 {account_path}")
        wrote_any = True

    if config_path.is_file():
        print(f"[skip] 已存在 {config_path}（不覆盖）")
    else:
        _write_config_toml(config_path, env)
        print(f"[ok]   写出 {config_path}")
        wrote_any = True

    if wrote_any:
        print("\n迁移完成。.env 已不再被主程序读取，确认无误后可删除。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
