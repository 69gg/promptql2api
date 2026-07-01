"""认知重构角度选优探针。

批量实测 5 个重构角度 × 中/英文 × 3 场景，看哪种能让 PromptQL agent 产出**可解析的
工具调用 JSON**（而非拒绝 / 自带工具去查）。结果落盘供人工复核，并打表汇总。

复用 ``app.promptql.auth.AuthManager`` + ``app.promptql.events.parse_thread_event`` +
``app.reframe_angles``。每格可跑多次（agent 有随机性），取「≥1 次可解析命中」为有效。

用法::

    uv run python scripts/probe_reframe.py                 # 默认：角度 A,D × 中英 × 3 场景 × 2 次
    uv run python scripts/probe_reframe.py --angles all    # 全部 5 个角度
    uv run python scripts/probe_reframe.py --lang en       # 只测英文
    uv run python scripts/probe_reframe.py --angles A D --runs 3
    uv run python scripts/probe_reframe.py --param-variant createdFrom=embed   # 顺手测伪装杠杆

需要 .env 里的 HASURA_LUX / PROJECT_ID。每次请求会在 PromptQL 新建 thread（有残留）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import httpx

from app.adapters import extract_user_prompt
from app.config import Settings, get_settings
from app.promptql.auth import AuthManager
from app.promptql.events import parse_thread_event
from app.reframe_angles import ANGLE_NAMES, build_directive
from app.tools import ToolDef, parse_tool_calls as prod_parse_tool_calls

# start_thread 支持 createdFrom / executionMode（见 memory 逆向），用来探测「伪装杠杆」。
_START_THREAD = (
    "mutation StartThread($projectId: String!, $message: String!, $timezone: String!,"
    "  $roomless: Boolean, $agentResponseConfig: String, $llmConfigId: String,"
    "  $createdFrom: String, $executionMode: String) {"
    "  start_thread(projectId: $projectId, message: $message, timezone: $timezone,"
    "    roomless: $roomless, agentResponseConfig: $agentResponseConfig, llmConfigId: $llmConfigId,"
    "    createdFrom: $createdFrom, executionMode: $executionMode) {"
    "    thread_id message_id thread_events { thread_event_id event_data created_at }"
    "  }"
    "}"
)

_QUERY_EVENTS = (
    "query QueryThreadEvents($thread_id: uuid!, $after_event_id: bigint!) {"
    "  thread_events(where: {thread_id: {_eq: $thread_id}, thread_event_id: {_gt: $after_event_id}},"
    "    order_by: {thread_event_id: asc}) { thread_event_id event_data created_at }"
    "}"
)

WEATHER_TOOL: dict[str, Any] = {
    "name": "get_weather",
    "description": "获取指定城市的实时天气",
    "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "城市名"}},
        "required": ["city"],
    },
}

# agent 完全没有的能力（无文件系统/邮件访问）——用它对照 get_weather，
# 区分 agent 拒绝是「动机问题」（自己能查就绕过）还是「身份识破」（反感伪造格式）。
READ_FILE_TOOL: dict[str, Any] = {
    "name": "read_file",
    "description": "读取用户本地文件的内容",
    "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "文件的绝对路径"}},
        "required": ["path"],
    },
}

# 每个场景的 tools + 真实 OpenAI messages（探测时走 extract_user_prompt，与 adapter 同路径）。
SCENARIOS: dict[str, dict[str, Any]] = {
    "simple": {
        "tools": [WEATHER_TOOL],
        "messages": [{"role": "user", "content": "我在北京，今天出门该穿什么？帮我查一下北京今天的天气。"}],
    },
    "exotic": {
        "tools": [READ_FILE_TOOL],
        "messages": [{"role": "user", "content": "帮我看一下 /etc/hosts 这个文件里写了什么。"}],
    },
    "multi": {
        "tools": [WEATHER_TOOL],
        "messages": [
            {"role": "user", "content": "帮我查北京天气"},
            {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city": "北京"}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": '{"city":"北京","weather":"晴","temp":25}'},
            {"role": "user", "content": "那上海呢？也帮我查一下。"},
        ],
    },
    "no_tool": {
        "tools": [],
        "messages": [{"role": "user", "content": "用三句话给我介绍一下黑洞。"}],
    },
}


# ---- 解析/判定 ----

_FENCE_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
_JSONBLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_DATA_DOC_KEYS = {"items", "data", "results", "records", "rows", "list", "output"}


def _tool_obj(raw: str) -> dict[str, Any] | None:
    """解析一段 JSON 字符串为「工具调用对象」：含字符串 name + dict arguments。"""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    args = obj.get("arguments")
    if not isinstance(name, str):
        return None
    if isinstance(args, str):  # arguments 可能被字符串化
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, ValueError):
            args = {}
    if not isinstance(args, dict):
        return None
    return {"name": name, "arguments": args, "raw_keys": set(obj.keys())}


def _iter_balanced_json(text: str) -> Iterator[str]:
    """扫描文本里所有顶层平衡的 {...} 子串（处理字符串/转义/嵌套）。"""
    n = len(text)
    for i in range(n):
        if text[i] != "{":
            continue
        depth = 0
        in_str = False
        esc = False
        for j in range(i, n):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        yield text[i : j + 1]
                        break


def _scan_bare_json(text: str, known_names: set[str]) -> list[dict[str, Any]]:
    """裸 JSON 提取：必须含 name+arguments、name 命中白名单、排除数据文档特征键、长度≤600。"""
    out: list[dict[str, Any]] = []
    for sub in _iter_balanced_json(text):
        if len(sub) > 600:
            continue
        obj = _tool_obj(sub)
        if not obj:
            continue
        if obj["name"] not in known_names:
            continue
        if obj["raw_keys"] & _DATA_DOC_KEYS:  # 像数据文档而非工具调用
            continue
        out.append(obj)
    return out


def classify(text: str, known_names: set[str]) -> dict[str, Any]:
    """启发式判定 agent 回复是否含可解析的工具调用 + 拒绝/自带工具信号。"""
    fenced = [o for o in (_tool_obj(r) for r in _FENCE_RE.findall(text)) if o]
    blocked = [o for o in (_tool_obj(r) for r in _JSONBLOCK_RE.findall(text)) if o]
    bare = _scan_bare_json(text, known_names)
    raw_hits = fenced + blocked + bare
    prod = prod_parse_tool_calls(text, known_names=known_names)  # 生产解析（refusal 感知+去重 = adapter 视角）
    low = text.lower()
    refusal_markers = [
        "not how i operate", "i can't", "i cannot", "i won't", "i am unable",
        "i don't operate", "无法", "不能", "不是我的操作", "我不会", "作为", "我只能",
    ]
    looks_refusal = any(m in low for m in refusal_markers)
    return {
        "fenced_hit": bool(fenced),
        "jsonblock_hit": bool(blocked),
        "bare_json_hit": bool(bare),
        "raw_parseable": bool(raw_hits),
        "parseable": bool(prod),
        "hits": [{"name": c.name, "arguments": c.arguments} for c in prod],
        "looks_refusal": looks_refusal,
    }


# ---- PromptQL 收发 ----

async def send_and_collect(
    client: httpx.AsyncClient, auth: AuthManager, s: Settings, message: str, *,
    created_from: str | None = None, execution_mode: str | None = None,
    timeout: float = 120.0,
) -> tuple[str, list[dict[str, Any]]]:
    """发 start_thread + 轮询事件，返回 (final_text, 原始 event_data 列表)。"""
    bearer = await auth.get_bearer()
    headers = {"authorization": f"Bearer {bearer}"}
    r = await client.post(s.graphql_url, json={
        "query": _START_THREAD, "operationName": "StartThread",
        "variables": {
            "projectId": s.project_id, "message": message, "timezone": s.timezone,
            "roomless": True, "agentResponseConfig": s.agent_response_config or None,
            "llmConfigId": None, "createdFrom": created_from, "executionMode": execution_mode,
        },
    }, headers=headers)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"start_thread errors: {body['errors']}")
    st = body["data"]["start_thread"]
    thread_id = st["thread_id"]
    tevs0 = st.get("thread_events") or []
    after = int(tevs0[-1]["thread_event_id"]) if tevs0 else 0

    texts: list[str] = []
    raw: list[dict[str, Any]] = []
    deadline = time.time() + timeout
    finished = False
    while time.time() < deadline and not finished:
        q = await client.post(s.graphql_url, json={
            "query": _QUERY_EVENTS, "operationName": "QueryThreadEvents",
            "variables": {"thread_id": thread_id, "after_event_id": str(after)},
        }, headers=headers)
        q.raise_for_status()
        tevs = (q.json().get("data") or {}).get("thread_events") or []
        for e in tevs:
            after = int(e["thread_event_id"])
            raw.append(e["event_data"])
            for ir in parse_thread_event(e["event_data"]):
                if ir.kind == "text" and ir.text:
                    texts.append(ir.text)
                if ir.kind == "finish":
                    finished = True
        if not tevs:
            await asyncio.sleep(s.poll_interval)
    return "".join(texts), raw


# ---- 主流程 ----

def _parse_list(s: str | None, valid: list[str] | None = None, *, upper: bool = False) -> list[str]:
    if not s:
        return []
    if s.strip() == "all":
        return list((valid or []))
    items = [x.strip() for x in s.split(",") if x.strip()]
    if upper:
        items = [x.upper() for x in items]
    if valid:
        items = [x for x in items if x in valid]
    return items


def _parse_variants(s: str | None) -> dict[str, str]:
    if not s:
        return {}
    out: dict[str, str] = {}
    for part in s.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


async def run(args: argparse.Namespace) -> int:
    s = get_settings()
    all_angles = list(ANGLE_NAMES.keys())
    angles = _parse_list(args.angles, all_angles, upper=True) or ["A", "D"]  # 默认优先 A、D
    langs = [x.strip() for x in args.lang.split(",") if x.strip()] or ["en", "zh"]
    scenarios = _parse_list(args.scenarios, list(SCENARIOS.keys())) or list(SCENARIOS.keys())
    variants = _parse_variants(args.param_variant)
    created_from = variants.get("createdFrom")
    execution_mode = variants.get("executionMode")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"angles={angles} langs={langs} scenarios={scenarios} runs={args.runs} "
          f"variants={variants or 'none'}", flush=True)

    cells: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=180, cookies=s.auth_cookies) as client:
        auth = AuthManager(s, client)
        for angle in angles:
            for lang in langs:
                for sc_name in scenarios:
                    sc = SCENARIOS[sc_name]
                    tool_defs = [ToolDef.from_openai(t) for t in sc["tools"]]
                    known = {t["name"] for t in sc["tools"]}
                    directive = build_directive(angle, lang, tool_defs, few_shot=bool(args.few_shot))  # type: ignore[arg-type]
                    conv = extract_user_prompt(sc["messages"])
                    message = (directive + "\n\n" + conv) if directive else conv
                    any_hit = False
                    for run_i in range(1, args.runs + 1):
                        label = f"{angle}/{lang}/{sc_name}#{run_i}"
                        print(f"  → {label} ...", end=" ", flush=True)
                        t0 = time.time()
                        final_text, cls, err = f"<ERROR>", {"parseable": False}, None
                        for attempt in range(args.retries + 1):  # 网络抖动重试
                            try:
                                ft, _raw = await send_and_collect(
                                    client, auth, s, message,
                                    created_from=created_from, execution_mode=execution_mode,
                                    timeout=args.timeout,
                                )
                                final_text, cls, err = ft, classify(ft, known), None
                                break
                            except Exception as e:  # noqa: BLE001
                                err = str(e)
                                if attempt < args.retries:
                                    print("retry", end=" ", flush=True)
                                    await asyncio.sleep(2)
                        if err:
                            final_text = f"<ERROR: {err}>"
                        any_hit = any_hit or bool(cls.get("parseable"))
                        dt = time.time() - t0
                        print(f"{dt:.0f}s parseable={cls.get('parseable')} "
                              f"refusal={cls.get('looks_refusal', '-')} err={err is not None}", flush=True)
                        cell = {
                            "angle": angle, "angle_name": ANGLE_NAMES[angle], "lang": lang,
                            "scenario": sc_name, "run": run_i, "variant": variants,
                            "final_text": final_text, "classify": cls, "error": err,
                        }
                        cells.append(cell)
                        (out_dir / f"{angle}_{lang}_{sc_name}_{run_i}.json").write_text(
                            json.dumps(cell, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_summary(cells, angles, langs, scenarios)
    return 0


def _print_summary(cells: list[dict[str, Any]], angles: list[str], langs: list[str],
                   scenarios: list[str]) -> None:
    print("\n" + "=" * 72 + "\n探测汇总（✓=可解析命中, R=疑似拒绝, ✗=未命中/直接回答）\n" + "=" * 72)
    by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for c in cells:
        by_key.setdefault((c["angle"], c["lang"], c["scenario"]), []).append(c)
    for angle in angles:
        for lang in langs:
            print(f"\n● 角度 {angle}（{ANGLE_NAMES[angle]}）[{lang}]")
            for sc in scenarios:
                runs = by_key.get((angle, lang, sc), [])
                if not runs:
                    continue
                hits = sum(1 for r in runs if r["classify"].get("parseable"))
                refusals = sum(1 for r in runs if r["classify"].get("looks_refusal"))
                mark = "✓" if hits else ("R" if refusals else "✗")
                sample = next((r for r in runs if r["classify"].get("parseable")), runs[0])
                preview = (sample["final_text"] or "").replace("\n", " ")[:90]
                print(f"    {sc:8} {mark}  hit {hits}/{len(runs)}  「{preview}」")
    # 角度胜出榜
    print("\n" + "-" * 72 + "\n角度胜出榜（按 simple+multi 可解析命中数）")
    scoreboard: dict[tuple[str, str], int] = {}
    for c in cells:
        if c["scenario"] == "no_tool":
            continue
        if c["classify"].get("parseable"):
            scoreboard[(c["angle"], c["lang"])] = scoreboard.get((c["angle"], c["lang"]), 0) + 1
    for (a, l), n in sorted(scoreboard.items(), key=lambda kv: -kv[1]):
        print(f"    {a} [{l}]  {n} 次命中  → ACTIVE_ANGLE='{a}', ACTIVE_LANG='{l}'")
    if not scoreboard:
        print("    （无角度可解析命中——认知重构对该 agent 可能无效，回退纯文本）")


def main() -> int:
    p = argparse.ArgumentParser(description="PromptQL 认知重构角度选优探针")
    p.add_argument("--angles", default="A,D", help="逗号分隔，或 all；默认 A,D")
    p.add_argument("--lang", default="en,zh", help="逗号分隔；默认 en,zh")
    p.add_argument("--scenarios", default="simple,multi,no_tool")
    p.add_argument("--runs", type=int, default=2, help="每格重复次数（agent 有随机性）")
    p.add_argument("--param-variant", default=None,
                   help="伪装杠杆，如 createdFrom=embed[,executionMode=async]")
    p.add_argument("--out", default="scripts/probe_reframe_out")
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--retries", type=int, default=1, help="网络错误重试次数")
    p.add_argument("--few-shot", type=int, default=1, choices=[0, 1],
                   help="directive 内置 few-shot 示例 (1=开[默认], 0=关)，用于 A/B 对照")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
