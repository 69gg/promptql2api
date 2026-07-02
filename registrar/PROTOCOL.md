# PromptQL 注册协议（chrome 隔离上下文 + curl 实测，2026-07-02）

注册机协议式实现的依据。`prompt.ql.app` 登录入口 = **邮箱 OTP**（非 magic link、非 OAuth）。

## 0. 入口

- 登录页：`https://prompt.ql.app/login`
- 表单：邮箱输入框 + `Continue` 按钮 + Cloudflare Turnstile widget + `Continue with GitHub/Google`（OAuth 不用于自动注册）
- **Turnstile sitekey**：`0x4AAAAAADsy_TOiX96NjTFT`（从 widget iframe URL 提取，固定）

### Turnstile 求解（实测重要结论，2026-07-02）

prompt.ql.app 的 Turnstile **严格检测自动化指纹**：实测以下自动化浏览器全部**过不了**（`input[name="cf-turnstile-response"]` 一直为空，60-90s 超时）：

- playwright chromium headless
- playwright chromium **非无头**
- camoufox (firefox) headless
- camoufox 非无头 + humanize

而真实日常 chrome / 人类交互能过。故 `registrar/turnstile.py` 把 solver 抽象为 3 种可配置策略（`config.toml [turnstile].method`）：

| method | 原理 | 适用 |
|---|---|---|
| `semi`（默认） | playwright 弹浏览器到登录页，等 widget 自动过或人类手动点一下 | 交互式跑，最稳，需桌面 |
| `cdp` | `connect_over_cdp` 连接你已开的 debug chrome（`--remote-debugging-port=9222`），真实指纹自动过 | 你开一个带调试端口的日常 chrome |
| `api` | 第三方打码（CapSolver AntiTurnstileTaskProxyLess），无浏览器 | 付费、全自动、无桌面 |

`semi` 的核心是 `_wait_token` 循环读 `cf-turnstile-response`，检测到 token 后全自动继续（收码/验证/查 project 全程无需人）。所有策略都返回同一 token 字符串，`pipeline.py` 不感知差异。

## 1. 发送 OTP：`POST https://auth.pro.ql.app/otp/send`

实测请求（reqid=2149）：

```
POST /otp/send HTTP/2
host: auth.pro.ql.app
content-type: application/json
origin: https://prompt.ql.app
referer: https://prompt.ql.app/
```

```json
{"email": "pqreg8367@omg.dadongbei.asia", "captcha_token": "<Turnstile token>"}
```

响应：

```json
{"message": "If the email exists, an OTP has been sent", "nonce": "kHIdJL19IE5eWm_ukWk5DA"}
```

- **`captcha_token` = Turnstile token**（字段名就是 `captcha_token`）。Turnstile 只在此步校验。
- `nonce` 返回但不需回传（verify 不要求）。

## 2. 收 OTP（临邮）

- 邮件 `From: Team PromptQL <noreply@auth.promptql.app>`
- `Subject: Your PromptQL sign-in code: <6位数字>`
- 正文含 6 位数字 code，**5 分钟有效**
- 提取：正则 `sign-in code: (\d{6})` 或正文 `>(\d{6})<`

## 3. 验证 OTP：`POST https://auth.pro.ql.app/otp/verify`

前端 bundle（`bWr` 函数）实测 body 三字段：

```
POST /otp/verify HTTP/2
host: auth.pro.ql.app
content-type: application/json
origin: https://prompt.ql.app
referer: https://prompt.ql.app/
```

```json
{"email": "pqreg8367@omg.dadongbei.asia", "otp": "678075", "nonce": "<otp/send 返回的 nonce>"}
```

- **body = `{email, otp, nonce}`**（字段名是 `otp`，不是 `code`！`nonce` 来自第 1 步 otp/send 响应，前端 `return r.nonce` 保存后回传）。
- ⚠️ 早期 curl 误用 `{email, code}` 得 `400 {"error":"Invalid or expired code"}`——实为字段名错（`code` 被忽略、`otp` 缺失），并非 code 过期。
- 成功响应（otp 有效）：`200` + **`Set-Cookie: hasura-lux=...`**（httpOnly；新账号登录态根凭据）。注册机用 curl-cffi 从响应 set-cookie 提取全值。

## 4. 拿 project：`POST https://data.pro.ql.app/v1/graphql`（实测确认）

新账号 verify 成功后，用 `hasura-lux` cookie 调控制平面 graphql 查 DDN project（主账号实测返回 `4712817f-...`）：

```
POST /v1/graphql HTTP/2
host: data.pro.ql.app
content-type: application/json
hasura-client-name: hasura-console
cookie: hasura-lux=...
```

```json
{"query": "{ ddn_projects { id name } }"}
```

响应（主账号）：

```json
{"data": {"ddn_projects": [{"id": "4712817f-3501-44d3-8a40-f74025a128ff", "name": "p-4712817f-3501"}]}}
```

取 `ddn_projects[0].id` / `.name`。控制平面 graphql 只认 `ddn_projects`（无 `getProjects`，introspection 实测）。

⚠️ 若新账号首次登录无默认 project（`ddn_projects` 返回空数组），需走 prompt.ql.app 的 onboarding 创建首个 project——该 mutation 待端到端实跑确认。注册机现实现「查 ddn_projects，空则抛错并保留已注册邮箱信息」，待首个真账号 verify 成功后补 onboarding。

## 注册机 pipeline（registrar/pipeline.py）

```
create_email() → address + jwt
solve_turnstile(sitekey=0x4AAAAAADsy_TOiX96NjTFT) → captcha_token
POST /otp/send {email, captcha_token} → 200
poll_code(jwt) → 6位 code
POST /otp/verify {email, code} → 200 + Set-Cookie hasura-lux
提取 hasura-lux
getProjects(hasura-lux) → project_id, project_name
写 account/<name>.json
```

## 端点汇总

| 用途 | 方法 | URL |
|---|---|---|
| 发 OTP | POST | `https://auth.pro.ql.app/otp/send` |
| 验 OTP | POST | `https://auth.pro.ql.app/otp/verify` |
| 控制平面 graphql（查 project） | POST | `https://data.pro.ql.app/v1/graphql` |
| Turnstile widget | - | sitekey `0x4AAAAAADsy_TOiX96NjTFT` |
