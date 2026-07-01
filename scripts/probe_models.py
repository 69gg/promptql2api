"""逐模型测试 tool call 遵循情况（认知重构 B 角度 + directive few-shot，simple 场景）。

固定已知最佳注入配置，遍历 10 个模型各跑 N 次，看哪些模型对认知重构注入的 tool call
配合度更高（Opus 4.8 最易识破，其他模型可能更配合）。

用法：uv run python scripts/probe_models.py [--runs 3]
需要 .env 里的 HASURA_LUX / PROJECT_ID。每次请求会在 PromptQL 新建 thread（有残留）。
"""
from __future__ import annotations

import argparse
import asyncio

import httpx

from app.adapters import MODEL_CATALOG, extract_user_prompt
from app.config import get_settings
from app.promptql.auth import AuthManager
from app.reframe_angles import build_directive
from app.tools import ToolDef
from probe_reframe import SCENARIOS, classify, send_and_collect


async def run(args: argparse.Namespace) -> int:
    s = get_settings()
    sc = SCENARIOS["simple"]
    tool_defs = [ToolDef.from_openai(t) for t in sc["tools"]]
    known = {t["name"] for t in sc["tools"]}
    directive = build_directive("B", "en", tool_defs)  # type: ignore[arg-type]
    message = directive + "\n\n" + extract_user_prompt(sc["messages"])

    print(f"配置: B/en/simple + directive-few-shot，每模型 {args.runs} 次\n", flush=True)
    results: list[tuple[str, int, int]] = []
    async with httpx.AsyncClient(timeout=180, cookies=s.auth_cookies) as c:
        auth = AuthManager(s, c)
        for m in MODEL_CATALOG:
            hits = 0
            for i in range(args.runs):
                label = f"{m['id']:34} #{i + 1}"
                try:
                    ft, _raw = await send_and_collect(
                        c, auth, s, message, llm_config_id=m["llm_config_id"], timeout=args.timeout)
                    cls = classify(ft, known)
                    ok = bool(cls["parseable"])
                    if ok:
                        hits += 1
                    preview = (ft or "").replace("\n", " ")[:60]
                    print(f"  {label} {'✓' if ok else '✗'} 「{preview}」", flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"  {label} ERROR: {e}", flush=True)
            results.append((m["id"], hits, args.runs))
            print(f"  → {m['id']:34} hit {hits}/{args.runs}\n", flush=True)

    print("=" * 64 + "\n模型 tool-call 遵循榜（命中率降序）")
    for mid, h, n in sorted(results, key=lambda r: -r[1]):
        print(f"  {mid:34} {h}/{n} ({100 * h // n}%)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="逐模型 tool call 遵循测试")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--timeout", type=float, default=120.0)
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
