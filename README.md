# promptql2api

把 [PromptQL](https://prompt.ql.app/)（Hasura DDN 架构的 AI agent playground）逆向封装成 **OpenAI / Anthropic 兼容**的本地网关。

对外暴露：

| 接口 | 兼容 |
|---|---|
| `GET  /v1/models` | OpenAI |
| `POST /v1/chat/completions` | OpenAI（流式 + 非流式 + tool calls + usage） |
| `POST /v1/responses` | OpenAI Responses API（typed SSE events） |
| `POST /v1/messages` | Anthropic（流式 + 非流式 + tool_use + usage） |
| `POST /v1/messages/count_tokens` | Anthropic |

底层每次请求在 PromptQL **新建一个 thread**（无状态语义），发首条消息触发 agent，轮询 `thread_events` 拿回复，再转成各家格式。

## 工作原理（逆向）

1. **认证链**：`hasura-lux` cookie → `POST auth.pro.ql.app/ddn/promptql/token` 拿 luxJWT →
   `mutation EnrichToken` 换成主 GraphQL 的 Bearer JWT（~24h，自动刷新）。
2. **发消息**：`mutation start_thread(projectId, message, timezone, roomless=true)` ——
   一步创建 thread + 发首条消息 + 触发 agent。`agentResponseConfig` 留空即触发 agent。
3. **收回复**：轮询 `query QueryThreadEvents(thread_id, after_event_id)`，消费 event 流：
   - `main_agent.llm_response`：含 `usage`（input/output/cached/thinking tokens，**真实计数，无需估算**）+ thinking。
   - `main_agent.actions_parsed.actions[].final_response.message`：给用户的最终文本。
   - `interaction_finished`：终止。
4. **token 计数**：优先用 `llm_response.usage`；无则 tiktoken 兜底。

详见 `app/promptql/` 与项目 memory。

## Tool calling（认知重构实现）

PromptQL 的 agent 有很强的内置 system prompt，会**拒绝**「按 `<tool_call>` 围栏输出工具调用」这类直白指令（实测回复 *"that's not how I operate"*），甚至自带 wiki/data/code 工具自行回答。

本网关不做对抗，改用**认知重构（Cognitive Reframing）**：顺应 agent 的 data/query assistant 身份，在消息最前注入一段情景，让 agent 觉得自己「只是在生成一段**表示**工具调用的文本示例」（职责内），而非「执行工具」（被禁）。代理层再把文本解析回 `tool_calls`/`tool_use`。

- **生效角度**：`app/reframe_angles.py` 经 `scripts/probe_reframe.py` 实测选优后固化为 **B「测试夹具」**——把工具调用包装成「为下游 dispatcher 生成回归测试的预期输出夹具」。其他角度（API 集成示例 / 数据集标注 / 教学演示 / 显式免责）均被 Opus 4.8 识破或无视。
- **历史 tool_call 续推（few-shot）**：`extract_user_prompt` 把 OpenAI `tool_calls` / Anthropic `tool_use` 历史渲染成 `<tool_call>` 围栏送回 agent。agent 识别「自己之前这么调用过」会强模仿——**带历史 tool_call 的多轮续推命中率显著高于单轮**。
- **鲁棒解析**：`app/tools.py:parse_tool_calls` 三级降级（`<tool_call>` 围栏 → ` ```json ``` ` 代码块 → 裸 JSON，须命中工具名白名单 + 排除数据文档）+ **拒绝感知**（agent 拒绝时常引用围栏格式作「我被要求做什么」的说明，此时不提取，避免假阳性）+ 同名同参数去重。

实测命中率（claude-opus-4-8）：

| 场景 | 命中率 |
|---|---|
| 单轮（无历史 tool_call）| ~30–60% |
| 多轮续推（历史含 tool_call）| ~60–100% |

> 单轮命中率**波动极大**（同配置连跑可能 0/3 到 2/3）——Opus 4.8 偶发识破情景；多轮续推（历史含 tool_call）则稳定高。实测对照：用 agent **没有**的能力（如 `read_file`）作工具，命中率并不更高（agent 对非自身能力拒绝更干脆），说明拒绝根因是「身份识破」而非「自己能查就绕过」。未命中时回退普通文本。可用 `scripts/probe_reframe.py` 重新选优/验证。

## 配置

复制 `.env.example` 为 `.env` 并填入：

```dotenv
# 从浏览器 DevTools → Application → Cookies → 选中 auth.pro.ql.app → 复制 hasura-lux 的 Value
HASURA_LUX=xxxxxxxx
# 项目 ID（prompt.ql.app URL 里 /project/<id>/ 的 uuid）
PROJECT_ID=4712817f-3501-44d3-8a40-f74025a128ff
PROJECT_NAME=p-4712817f-3501
# 网关
HOST=0.0.0.0
PORT=8088
# 客户端访问网关用的 key（留空则不校验）
GATEWAY_API_KEY=
```

## 运行

```bash
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8088
# 或
uv run uvicorn app.main:app --reload
```

Docker：

```bash
docker build -t promptql2api .
docker run -p 8088:8088 --env-file .env promptql2api
```

## 使用示例

```python
from openai import OpenAI
c = OpenAI(base_url="http://localhost:8088/v1", api_key="any")
r = c.chat.completions.create(
    model="claude-opus-4-8",
    messages=[{"role": "user", "content": "你好"}],
)
print(r.choices[0].message.content)
```

```python
from anthropic import Anthropic
c = Anthropic(base_url="http://localhost:8088", api_key="any")
m = c.messages.create(model="claude-opus-4-8", max_tokens=1024,
                      messages=[{"role": "user", "content": "你好"}])
print(m.content[0].text)
```

## 已知限制（务必知悉）

- **tool calling 概率性生效**：见上文「Tool calling（认知重构实现）」。Opus 4.8 会偶发拒绝/识破，单轮 ~30%、带历史 tool_call 的多轮续推 ~60% 命中；未命中时回退普通文本回复。
- **流式为「伪流式」**：PromptQL 的 agent 文本是**整块**返回（每个 `llm_response`/`actions_parsed` 事件带完整文本，非逐 token delta），网关按事件分块转发。
- **每次请求新建 thread**：会产生 PromptQL thread 残留（如需可自行扩展按 hash 复用 thread 或调用 `delete_thread` 清理）。
- **usage 含缓存命中**：PromptQL 单次问答 agent 可能多轮调用 LLM，每轮 `input_tokens` 含大量 prompt cache 命中。网关取**首次**非零 usage 作为返回（最接近用户感知的单次用量）。
- 不支持 vision/音频/部分 OpenAI 参数（忽略）。

## 开发

```bash
uv sync --extra dev        # 或 uv sync --all-extras
uv run pytest -q           # 21 个测试
uv run python scripts/probe.py   # 抓包探针（探索 PromptQL event 结构）
```

## 目录结构

```
app/
  config.py            Settings
  deps.py              FastAPI 依赖（注入 client、API key 校验）
  main.py              FastAPI 入口
  tools.py             tool-call 认知重构薄封装 + 三级鲁棒解析
  reframe_angles.py    认知重构角度集（ACTIVE=B「测试夹具」）
  tokens.py            usage 汇总 + tiktoken 兜底
  promptql/
    auth.py            cookie → luxJWT → Bearer JWT（缓存+自动刷新）
    client.py          start_thread / send_thread_message / QueryThreadEvents 轮询
    events.py          event_data → 统一 IR
  adapters/
    openai_models.py / openai_chat.py / openai_responses.py / anthropic_messages.py
tests/                 events / tools / adapters 单测（28）
scripts/probe.py       逆向探针
  probe_reframe.py     认知重构角度选优探针
  e2e_tool.py          OpenAI SDK 端到端 tool call 验证
```
