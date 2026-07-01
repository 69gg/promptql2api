"""Phase 0 探针：走通 PromptQL 认证链 + 发消息 + 抓 event_data 结构。

用法：uv run python scripts/probe.py
需要 .env 里的 HASURA_LUX / PROJECT_ID。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

HASURA_LUX: str = os.environ["HASURA_LUX"]
PROJECT_ID: str = os.environ["PROJECT_ID"]
TIMEZONE: str = os.environ.get("TIMEZONE", "Asia/Shanghai")

AUTH_BASE = "https://auth.pro.ql.app"
PG_GQL = "https://data.prompt.ql.app/promptql/playground-v2-hge/v1/graphql"


def decode_jwt(tok: str) -> dict[str, Any]:
    p = tok.split(".")[1]
    p += "=" * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p))


async def gql(client: httpx.AsyncClient, query: str, variables: dict | None = None,
              op: str | None = None, bearer: str | None = None) -> httpx.Response:
    headers: dict[str, str] = {"content-type": "application/json"}
    if bearer:
        headers["authorization"] = f"Bearer {bearer}"
    body: dict[str, Any] = {"query": query}
    if variables is not None:
        body["variables"] = variables
    if op:
        body["operationName"] = op
    return await client.post(PG_GQL, json=body, headers=headers)


async def main() -> None:
    cookies = {"hasura-lux": HASURA_LUX}
    async with httpx.AsyncClient(timeout=60, cookies=cookies) as c:
        # [1] cookie -> luxJWT
        print("=" * 70, "\n[1] POST /ddn/promptql/token")
        r = await c.post(f"{AUTH_BASE}/ddn/promptql/token",
                         headers={"x-hasura-project-id": PROJECT_ID})
        print(r.status_code, r.text[:300])
        if r.status_code != 200:
            return
        lux: str = r.json()["token"]
        print("luxJWT iss:", decode_jwt(lux)["iss"])

        # [2] /ddn/project/token (探索)
        print("=" * 70, "\n[2] POST /ddn/project/token")
        r = await c.post(f"{AUTH_BASE}/ddn/project/token",
                         headers={"x-hasura-project-id": PROJECT_ID})
        print(r.status_code, r.text[:300])

        # [3] EnrichToken (用 luxJWT 在 variables 里)
        print("=" * 70, "\n[3] EnrichToken")
        q = ("mutation EnrichToken($luxJWT:String!,$projectId:uuid!)"
             "{enrich_token(luxJWT:$luxJWT,projectId:$projectId){userDirectoryJWT}}")
        r = await gql(c, q, {"luxJWT": lux, "projectId": PROJECT_ID}, "EnrichToken")
        print(r.status_code, r.text[:500])
        try:
            udjwt: str = r.json()["data"]["enrich_token"]["userDirectoryJWT"]
            print("userDirectoryJWT payload:\n", json.dumps(decode_jwt(udjwt), indent=2))
        except Exception as e:  # noqa: BLE001
            print("enrich failed:", e)
            return

        # [4] 用 Bearer 测 FetchCapabilities
        print("=" * 70, "\n[4] Bearer test (FetchCapabilities)")
        q = "query FetchCapabilities{capabilities{features{per_thread_model_selection per_room_model_selection roomless_threads}}}"
        r = await gql(c, q, bearer=udjwt)
        print(r.status_code, r.text[:400])

        # [5] introspection: 找创建 thread 的 mutation + threads 表结构
        print("=" * 70, "\n[5] introspection (mutations)")
        iq = "{__schema{mutationType{fields{name}}}}"
        r = await gql(c, iq, bearer=udjwt)
        try:
            fnames = [f["name"] for f in r.json()["data"]["__schema"]["mutationType"]["fields"]]
            print("thread-related mutations:", [n for n in fnames if "thread" in n.lower()])
            print("create*:", [n for n in fnames if n.lower().startswith("create")])
        except Exception:
            print("intro failed:", r.status_code, r.text[:400])
            return

        # [5b] introspection: send_thread_message / create_thread 的参数
        for target in ("send_thread_message", "create_thread", "insert_threads_one"):
            if target not in fnames:
                continue
            aq = ("query($n:String!){__type(name:$n){name args{name type{kind name ofType{kind name ofType{kind name}}}}}}")
            r = await gql(c, aq, {"n": target}, bearer=udjwt)
            print(f"--- args of {target} ---")
            print(r.text[:1200])

        # [5c] threads 表字段
        tq = "{__type(name:\"threads\"){fields{name type{kind name ofType{kind name}}}}}"
        r = await gql(c, tq, bearer=udjwt)
        print("--- threads fields ---")
        print(r.text[:1500])

        # [5d] introspect args of candidate mutations
        for target in ("create_empty_thread", "start_thread", "send_thread_message", "create_room"):
            aq = ("query{__schema{mutationType{fields(includeDeprecated:true){name args{name defaultValue type{kind name ofType{kind name ofType{kind name ofType{kind name}}}}}}}}}")
            r = await gql(c, aq, bearer=udjwt)
            try:
                fs = r.json()["data"]["__schema"]["mutationType"]["fields"]
                tgt = next((f for f in fs if f["name"] == target), None)
                print(f"--- {target} args ---")
                print(json.dumps(tgt, ensure_ascii=False) if tgt else "NOT FOUND")
            except Exception:
                print(r.status_code, r.text[:300])

        # [6] start_thread: 创建 thread + 发首条消息 + 触发 agent
        print("=" * 70, "\n[6] start_thread (roomless, agentResponseConfig=null)")
        # 先 introspect StartThreadOutput 字段
        rtq = "{__type(name:\"StartThreadOutput\"){fields{name type{kind name ofType{kind name}}}}}"
        r = await gql(c, rtq, bearer=udjwt)
        print("StartThreadOutput fields:", r.text[:600])
        # ThreadEvent 字段
        teq = "{__type(name:\"ThreadEvent\"){fields{name}}}"
        r = await gql(c, teq, bearer=udjwt)
        print("ThreadEvent fields:", r.text[:500])
        new_thread: str = ""  # 占位，start_thread 返回里拿
        print("=" * 70, "\n[6] start_thread (roomless, agentResponseConfig=null)")
        new_thread: str = ""  # 占位，start_thread 返回里拿
        st_q = (
            "mutation StartThread($projectId:String!,$message:String!,$timezone:String!,"
            "$roomless:Boolean,$agentResponseConfig:String){"
            "start_thread(projectId:$projectId,message:$message,timezone:$timezone,"
            "roomless:$roomless,agentResponseConfig:$agentResponseConfig){"
            "thread_id message_id title created_at thread_events{thread_event_id event_data created_at}}}")
        r = await gql(c, st_q,
                      {"projectId": PROJECT_ID, "message": "Reply with just the word: PONG",
                       "timezone": TIMEZONE, "roomless": True, "agentResponseConfig": None},
                      "StartThread", bearer=udjwt)
        print(r.status_code, r.text[:800])
        try:
            data = r.json().get("data", {}).get("start_thread") or {}
            print("start_thread keys:", list(data.keys()))
            print("start_thread full:", json.dumps(data, ensure_ascii=False, indent=2)[:1500])
            new_thread = data.get("thread_id", "")
            tevs = data.get("thread_events") or []
            after = int(tevs[-1]["thread_event_id"]) if tevs else 0
            print("thread_id:", new_thread, "| after_event_id:", after)
        except Exception as e:  # noqa: BLE001
            print("parse start_thread err:", e)
            return

        # [7] 轮询 QueryThreadEvents 抓 agent 回复
        print("=" * 70, "\n[7] poll QueryThreadEvents")
        eq = (
            "query QueryThreadEvents($thread_id:uuid!,$after_event_id:bigint!){"
            "thread_events(where:{thread_id:{_eq:$thread_id},thread_event_id:{_gt:$after_event_id}},"
            "order_by:{thread_event_id:asc}){thread_event_id event_data created_at}}")
        deadline = time.time() + 45
        while time.time() < deadline:
            r = await gql(c, eq, {"thread_id": new_thread, "after_event_id": str(after)},
                          "QueryThreadEvents", bearer=udjwt)
            try:
                evs = r.json()["data"]["thread_events"]
            except Exception:
                print("poll err:", r.text[:300])
                break
            for e in evs:
                print(f"--- event {e['thread_event_id']} ---")
                print(json.dumps(e["event_data"], ensure_ascii=False, indent=2)[:2500])
                blob = json.dumps(e["event_data"], ensure_ascii=False)
                if "usage" in blob or "token" in blob.lower() or "usage" in blob:
                    print(">>> 含 token/usage 关键词")
            if evs:
                after = int(evs[-1]["thread_event_id"])
            await asyncio.sleep(1.2)
        print("done polling")


if __name__ == "__main__":
    asyncio.run(main())
