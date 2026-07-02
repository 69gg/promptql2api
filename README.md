# promptql2api

把 [PromptQL](https://prompt.ql.app/)（Hasura DDN 架构的 AI agent playground）逆向封装成 **OpenAI / Anthropic 兼容**的本地网关，并附带**全自动账号注册机**（多账号池）。

对外暴露：

| 接口 | 兼容 |
|---|---|
| `GET  /v1/models` | OpenAI |
| `POST /v1/chat/completions` | OpenAI（流式 + 非流式 + tool calls + usage） |
| `POST /v1/responses` | OpenAI Responses API（typed SSE events） |
| `POST /v1/messages` | Anthropic（流式 + 非流式 + tool_use + usage） |
| `POST /v1/messages/count_tokens` | Anthropic |

底层每次请求在 PromptQL **新建一个 thread**（无状态语义），发首条消息触发 agent，轮询 `thread_events` 拿回复，再转成各家格式。

## 架构

- **多账号池**：`account/<name>.json` 存每个 PromptQL 账号的 `hasura-lux` cookie + project 信息。网关启动加载全部，**每次请求 round-robin 轮换**一个账号，分散速率/配额限制；某账号认证失败自动标记 `disabled` 并换号（返回 503，客户端重试即换号）。
- **配置**：`config.toml`（gitignored）放网关/行为/端点/注册机配置；账号凭据分离到 `account/`。
- **注册机**（`registrar/`，独立包）：协议式自动注册 prompt.ql.app 新账号——临邮 + Turnstile + OTP → 提取 `hasura-lux` + project → 写 `account/<name>.json`。主程序不依赖它（`uv sync --extra registrar` 才装）。

## 工作原理（逆向）

1. **认证链**：`hasura-lux` cookie → `POST auth.pro.ql.app/ddn/promptql/token` 拿 luxJWT →
   `mutation EnrichToken` 换成主 GraphQL 的 Bearer JWT（~24h，自动刷新）。
2. **发消息**：`mutation start_thread(projectId, message, timezone, roomless=true)` ——
   一步创建 thread + 发首条消息 + 触发 agent。`agentResponseConfig` 留空即触发 agent。
3. **收回复**：轮询 `query QueryThreadEvents(thread_id, after_event_id)`，消费 event 流：
   - `main_agent.llm_response`：含 `usage`（input/output/cached/thinking tokens，**真实计数**）+ `thinking_text`。
   - `main_agent.actions_parsed.actions[].final_response.message`：给用户的最终文本。
   - `interaction_finished`：终止。
4. **token 计数**：优先用 `llm_response.usage`；无则 tiktoken 兜底。
5. **CoT / Thinking 透传**：`llm_response.thinking_text` 会按各家格式返回；客户端传入的 thinking/reasoning 内容也会保留进 PromptQL prompt。

详见 `app/promptql/` 与项目 memory。

## CoT / Thinking 透传

PromptQL 的 agent 会在 `llm_response.thinking_text` 中返回推理过程。本网关把它透传给客户端，并保留客户端回传的历史 CoT：

| 接口 | 返回字段 / 格式 | 请求体中 CoT 的保留方式 |
|---|---|---|
| `POST /v1/chat/completions` | `choices[0].message.reasoning_content`（DeepSeek 兼容的事实标准） | message 根字段 `reasoning_content` 会包装成 `<reasoning>...</reasoning>` 放进 prompt |
| `POST /v1/responses` | `output` 中增加 `{ "type": "reasoning", "summary": [{ "type": "summary_text", "text": "..." }] }` | `input` 中 `type: "reasoning"` 的 item 会保留进 prompt |
| `POST /v1/messages` | `content` 中增加 `{ "type": "thinking", "thinking": "...", "signature": "" }` | content 中 `type: "thinking"` 的 block 会包装成 `<thinking>...</thinking>` 放进 prompt；请求体支持 `thinking` 参数 |

流式响应同样会按上述格式 emit CoT 增量事件。

注意：Anthropic 官方返回的 `thinking` block 含 `signature`，但 PromptQL 上游未提供，因此网关返回的 `signature` 为空字符串。

## Tool calling（认知重构实现）

PromptQL 的 agent 有很强的内置 system prompt，会**拒绝**「按 `<tool_call>` 围栏输出工具调用」这类直白指令（实测回复 *"that's not how I operate"*），甚至自带 wiki/data/code 工具自行回答。

本网关不做对抗，改用**认知重构（Cognitive Reframing）**：顺应 agent 的 data/query assistant 身份，在消息最前注入一段情景，让 agent 觉得自己「只是在生成一段**表示**工具调用的文本示例」（职责内），而非「执行工具」（被禁）。代理层再把文本解析回 `tool_calls`/`tool_use`。

- **生效角度**：`app/reframe_angles.py` 经 `scripts/probe_reframe.py` 实测选优后固化为 **B「测试夹具」**——把工具调用包装成「为下游 dispatcher 生成回归测试的预期输出夹具」。
- **历史 tool_call 续推（few-shot）**：`extract_user_prompt` 把 OpenAI `tool_calls` / Anthropic `tool_use` 历史渲染成 `<tool_call>` 围栏送回 agent，命中率显著提升。
- **directive 内置 few-shot（生产默认）**：`build_tool_directive` 默认在情景末尾附一个示例围栏，让**单轮请求**也获得 few-shot 锚定（单轮命中率 ~10% → ~30%）。
- **鲁棒解析**：`app/tools.py:parse_tool_calls` 三级降级（`<tool_call>` 围栏 → ` ```json ``` ` 块 → 裸 JSON，须命中工具名白名单 + 排除数据文档）+ **拒绝感知** + 同名同参数去重。

**模型差异巨大**——认知重构对几乎所有模型生效，唯独 **claude-opus-4-8 会识破**（B/en/simple + directive-few-shot，各 3 次，`scripts/probe_models.py`）：

| 模型 | tool call 命中率 |
|---|---|
| gpt-5.5 / claude-sonnet-4-5-20250929 / deepseek-v4-pro / gemini-3.1-pro-preview / gemini-3.5-flash / kimi-k2.6 / kimi-k2.7-code / minimax-m3 | ~100% |
| glm-5.2 | ~66% |
| claude-opus-4-8 | ~0%（识破："I'm main, the AI agent..."） |

故默认模型为 **gpt-5.5**（tool call 友好 + 质量强）。未命中回退普通文本。

## 模型

`/v1/models` 返回实地从 prompt.ql.app 抓取的 **10 个模型**（模型选择 dialog 各选项 button 的 `data-testid` 即 `llmConfigId`；模型列表为前端 bundle 硬编码，后端无查询接口）：

`gpt-5.5`（默认）/ `claude-opus-4-8` / `claude-sonnet-4-5-20250929` / `deepseek-v4-pro` / `gemini-3.1-pro-preview` / `gemini-3.5-flash` / `glm-5.2` / `kimi-k2.6` / `kimi-k2.7-code` / `minimax-m3`

客户端传的 `model` 经 `normalize_model` 归一化（支持 id、显示名、模糊匹配）后映射到 `llmConfigId`，通过 `start_thread` 的 `llmConfigId` 参数**真正切换底层模型**。未知 model 回退默认 gpt-5.5。映射表见 `app/adapters/__init__.py:MODEL_CATALOG`。

## 配置

复制 `config.toml.example` 为 `config.toml` 并填值（`config.toml` 已 gitignore）：

```toml
[gateway]
host = "0.0.0.0"
port = 8088
api_key = ""                 # 客户端访问网关的 key（Authorization: Bearer <key>）；留空则不校验

[promptql]
timezone = "Asia/Shanghai"
agent_response_config = ""   # 空=触发 agent
poll_interval = 1.2
request_timeout = 120.0
token_refresh_margin = 300
auth_token_url = "https://auth.pro.ql.app/ddn/promptql/token"
graphql_url = "https://data.prompt.ql.app/promptql/playground-v2-hge/v1/graphql"

[registry]
account_dir = "account"      # 账号凭据目录（account/*.json，gitignored）

# ===== 以下仅注册机用 =====
[email]                      # Cloudflare Temp Email：https://github.com/dreamhunter2333/cloudflare_temp_email
base_url = "https://your-mail-service.example.com"
admin_auth = ""              # 后台管理员密码（x-admin-auth）
custom_auth = ""             # 后台自定义鉴权（x-custom-auth）
domain = "your-domain.com"   # 已绑定的收件域名（不带 @）

[turnstile]
method = "semi"              # semi（默认）/ cdp / api，详见下方「注册机」
headless = false             # semi 策略：prompt.ql.app 无头过不了，建议 false

# ===== 管理后台 =====
[admin]
auth_key = ""                # /admin/* 管理端点鉴权（Authorization: Bearer <key> 或 ?auth_key=<key>）；留空=关闭 admin 端点
```

账号凭据放 `account/<name>.json`（gitignored）：

```json
{
  "name": "main",
  "source_email": "abc@your-domain.com",
  "hasura_lux": "<auth.pro.ql.app 的 hasura-lux cookie 全值>",
  "project_id": "<uuid>",
  "project_name": "p-<uuid>",
  "created_at": "2026-07-02T14:22:33",
  "disabled": false
}
```

**从旧 `.env` 迁移**（已有 `HASURA_LUX/PROJECT_ID`）：

```bash
uv run python scripts/migrate_env_to_toml.py   # 幂等：生成 account/main.json + config.toml
```

## 运行

```bash
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8088
```

Docker：

```bash
docker build -t promptql2api .
docker run -p 8088:8088 \
  -v $(pwd)/config.toml:/app/config.toml \
  -v $(pwd)/account:/app/account \
  promptql2api
```

启动时若无可用账号，会提示「请运行注册机或迁移 `.env`」。

## 注册机（自动注册新账号）

注册机是独立可选组件（重依赖 curl-cffi/playwright/camoufox，主程序不依赖）：

```bash
# 1. 装注册机依赖 + 浏览器
uv sync --extra registrar
uv run playwright install chromium

# 2. 在 config.toml 填 [email]（你的临邮服务）+ [turnstile]

# 3. 注册（默认 semi 策略）
uv run python -m registrar -n 5 -w 2 --proxy http://host:port
uv run python -m registrar --count 0 --workers 3   # 无限运行直到 Ctrl+C
```

**CLI**：`-n/--count`（0=无限）、`-w/--workers`（并发线程）、`--proxy`、`--turnstile-method`（覆盖 config）、`--config`。

注册成功自动写 `account/<name>.json`（用临邮 local-part 命名，重名加序号），网关下次启动即加载。

### Turnstile 三策略（重要：实测无法绕过）

prompt.ql.app 的 Turnstile **严格反自动化**（`registrar/PROTOCOL.md` 有完整逆向）：服务端强校验 `captcha_token`（空值 → `400 Captcha verification is required`），且 playwright chromium / camoufox firefox 等自动化浏览器（headless、非无头都）过不了——只有真实日常浏览器或人类交互能过。故 solver 抽象为三种可配置策略：

| `method` | 原理 | 适用 |
|---|---|---|
| `semi`（默认） | playwright 弹浏览器到登录页，等 widget 自动过或人手动点一下；检测到 token 后全自动继续 | 交互式跑，需桌面 |
| `cdp` | `connect_over_cdp` 连你已开的 debug chrome（`--remote-debugging-port=9222`），真实指纹自动过 | 有日常 chrome |
| `api` | 第三方打码（CapSolver AntiTurnstileTaskProxyLess），无浏览器 | 付费、全自动、无桌面 |

由 `config.toml [turnstile].method` 或 CLI `--turnstile-method` 切换。注册协议（otp/send `+captcha_token`、otp/verify `{email, otp, nonce}`、查 `ddn_projects`）见 `registrar/PROTOCOL.md`。

## 使用示例

```python
from openai import OpenAI
c = OpenAI(base_url="http://localhost:8088/v1", api_key="any")
r = c.chat.completions.create(model="gpt-5.5", messages=[{"role": "user", "content": "你好"}])
print(r.choices[0].message.content)
```

```python
from anthropic import Anthropic
c = Anthropic(base_url="http://localhost:8088", api_key="any")
m = c.messages.create(model="gpt-5.5", max_tokens=1024,
                      messages=[{"role": "user", "content": "你好"}])
print(m.content[0].text)
```

## 管理后台（账号上传）

在 `config.toml` 配置 `[admin].auth_key` 后，可启用 `/admin/*` 管理端点：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/accounts` | 列出账号摘要（不暴露 cookie/project_id） |
| GET | `/admin/accounts/{name}` | 查看单个账号完整字段 |
| POST | `/admin/accounts` | 上传/新增账号，请求体同 `account/<name>.json` |
| DELETE | `/admin/accounts/{name}` | 删除账号 |
| POST | `/admin/reload` | 重新从磁盘加载账号池 |

鉴权方式（二选一）：

```bash
curl -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8088/admin/accounts
# 或
curl "http://localhost:8088/admin/accounts?auth_key=$ADMIN_KEY"
```

上传账号示例：

```bash
curl -X POST "http://localhost:8088/admin/accounts?auth_key=$ADMIN_KEY" \
  -H "content-type: application/json" \
  -d '{
    "name": "main",
    "source_email": "user@example.com",
    "hasura_lux": "...",
    "project_id": "...",
    "project_name": "p-...",
    "created_at": "2026-07-02T14:22:33",
    "disabled": false
  }'
```

## 油猴脚本自动上交账号

项目提供 `scripts/promptql_account_uploader.user.js`：

1. 安装 **Tampermonkey Beta**（仅 Beta 版支持读取 httpOnly cookie；稳定版读不到 `hasura-lux`）。Violentmonkey 无 `GM_cookie`，会走下面的手动兜底。
2. 将 `scripts/promptql_account_uploader.user.js` 添加为新脚本。脚本已声明 `@match https://auth.pro.ql.app/*`，这是 `GM_cookie` 跨域读取 `hasura-lux` 的授权前提。
3. 在脚本菜单中设置 `ADMIN_URL`（默认 `http://localhost:8088`）与 `ADMIN_AUTH_KEY`。
4. 登录 `https://prompt.ql.app`，点击右下角「上交账号」按钮即可自动提取 `hasura-lux` cookie、查询 project 并上传。

> **关于 `hasura-lux`**：它是 `pro.ql.app` 域的 **httpOnly** cookie（domain=`pro.ql.app`，故 `auth.pro.ql.app`、`data.pro.ql.app` 等子域请求都带它；但 `prompt.ql.app`、`data.prompt.ql.app` 等非 pro.ql.app 域不带）。脚本通过 `GM_cookie.list({ url: 'https://auth.pro.ql.app/' })` 跨域读取，**需 Tampermonkey Beta** 且首次运行授权 cookie 权限。若自动读取失败（稳定版 / 未授权 / 跨域被拒），脚本会弹窗引导：DevTools → Application → Cookies → `https://auth.pro.ql.app`（或任一 pro.ql.app 子域）→ 复制 `hasura-lux` 的 Value 粘贴即可（值会缓存在本机便于续期）。

> **关于跨域上传**：上传走 `GM_xmlhttpRequest`（非页面 `fetch`），因此可跨源、跨 HTTP↔HTTPS 访问内网/本地网关（如 `http://192.168.x.x:8089`），不受浏览器 CORS 预检与混合内容（Mixed Content）拦截。脚本声明 `@connect *` 兜底任意 `ADMIN_URL`，首次连接目标主机时 Tampermonkey 会弹窗请求授权，点允许即可。

## 已知限制

- **Turnstile 反自动化**：注册机的 Turnstile 必须真实求解（semi/cdp/api），**无法协议层绕过**（服务端强校验 + 前端无旁路 + 无 password/signup 端点）。
- **tool calling 依赖模型**：默认 gpt-5.5 下认知重构 ~100% 生效；唯独 claude-opus-4-8 会识破（~0%）。未命中回退普通文本。
- **流式为「伪流式」**：PromptQL 的 agent 文本是**整块**返回（每个事件带完整文本，非逐 token delta）。
- **每次请求新建 thread**：会产生 PromptQL thread 残留。
- **usage 含缓存命中**：网关取**首次**非零 usage 作为返回。
- 不支持 vision/音频/部分 OpenAI 参数。

## 开发

```bash
uv sync --extra dev
uv run pytest -q           # 55 个测试（adapters/tools/account/config/events）
uv run python scripts/probe.py   # 抓包探针
```

## 目录结构

```
app/
  config.py            Settings（pydantic + tomllib，无账号凭据）
  account.py           Account + AccountPool（round-robin + mark_disabled）
  admin.py             /admin/* 管理端点（账号上传/删除/重载）
  deps.py              get_client 走账号池 + _RetryingClient（认证失败 → 503 换号）
  main.py              FastAPI 入口（lifespan 加载账号池）
  tools.py             tool-call 认知重构 + 三级鲁棒解析
  reframe_angles.py    认知重构角度集（B「测试夹具」）
  tokens.py            usage 汇总 + tiktoken 兜底
  promptql/
    auth.py            cookie → luxJWT → Bearer JWT（per-account，缓存+刷新）
    client.py          start_thread / QueryThreadEvents 轮询
    events.py          event_data → 统一 IR
  adapters/
    openai_models.py / openai_chat.py / openai_responses.py / anthropic_messages.py
registrar/             全自动注册机（独立包，主程序不 import）
  cli.py               argparse 入口（python -m registrar）
  pipeline.py          注册编排：临邮→Turnstile→otp/send→收码→otp/verify→查 project
  email_client.py      Cloudflare Temp Email（create_email / poll_code）
  turnstile.py         Turnstile solver（semi/cdp/api 三策略）
  http_client.py       curl-cffi（impersonate chrome + 429 退避）
  models.py            RegistrarConfig（读 config [email]/[turnstile]）
  PROTOCOL.md          注册协议逆向依据
account/               账号凭据 *.json（gitignored）
config.toml            运行配置（gitignored）
config.toml.example    配置模板（传 git）
scripts/migrate_env_to_toml.py   .env → config.toml + account/main.json（幂等）
scripts/probe.py                 抓包探针
scripts/probe_models.py          模型 tool-call 命中率探针
scripts/probe_reframe.py         认知重构角度探针
scripts/promptql_account_uploader.user.js  油猴脚本：自动上交 PromptQL 账号
tests/                 events / tools / adapters / account / config / admin（新增）
```

## License

MIT © 2026 Null
