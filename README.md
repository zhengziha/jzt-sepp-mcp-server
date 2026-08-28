# jzt-sepp-mcp-server

能效平台（SEPP，`sepp.op.yyjzt.com`）缺陷管理的 **MCP Server**，基于 FastMCP 封装。

## 功能

- **自动登录**：支持三种方式，按顺序尝试：
  1. 磁盘缓存的登录态（`data/cookies.json`，有效期约 7 天）；
  2. `SEPP_AUTH_TOKEN`（浏览器里复制的 `sepp-auth` JWT）；
  3. **账号密码登录**：调用平台 `normal_auth` 接口，密码 **sha256 加密**后提交，无需浏览器。
  - ~~Playwright Keycloak SSO 明文表单~~ **默认禁用**（明文密码会被计入错误次数、连续 5 次锁定账号），仅当显式设置 `SEPP_ALLOW_SSO_LOGIN=true` 才作为最后兜底。
- **获取用户项目信息**：`get_user_projects`（健康诊所科技 / 开发工程师）。
- **查询缺陷，支持多用户**：`query_defects` / `query_my_defects` / `get_users`（姓名/账号 → userId 映射）。
  - `query_defects(responsers=["郑益2", "房航"])` **一次查多个用户**：自动把名称解析为 userId、逐个查询后合并去重；
  - 不指定负责人时默认查"我"（默认用户 郑自航/1001967）。
- **定时提醒**：
  - `monitor_add` 创建监控任务，支持**一次监控多个用户**（`responsers` 名称列表），内置 APScheduler 定时轮询；
  - **新增缺陷提醒**：缺陷 ID 首次出现时提醒；
  - **超时提醒**：缺陷超过 `timeout_hours`（默认 **2 小时**）仍未处理时提醒；
  - 提醒渠道：钉钉/企微/飞书 webhook、SMTP 邮件（至少配置一个，否则仅打印日志）；
  - 监控状态持久化在 `data/monitor_state.json`，重启后自动恢复；
  - **负责人 @**：配置 `SEPP_DINGTALK_AT_MAP`（姓名 → 钉钉手机号）后，命中映射的负责人用手机号 `@`（触发钉钉提醒）；未命中或未配置则负责人名称**加粗**显示。
- **脱离 agent 独立运行**：CLI 提供 `monitor-add` / `run-check` / `daemon`，可完全通过命令行建监控并常驻轮询（见下文"定时任务"）。

## 目录结构

```
.
├── sepp_mcp/
│   ├── server.py     # FastMCP 服务（工具定义）
│   ├── auth.py       # 自动登录（normal_auth API / token / cookie 缓存，SSO 默认禁用）
│   ├── client.py     # HTTP 客户端（登录态复用 + 多用户解析/合并查询）
│   ├── monitor.py    # 缺陷监控（新增/超时提醒 + APScheduler + 多用户）
│   ├── alerts.py     # 告警通知（webhook / 邮件，支持钉钉 @ 手机号）
│   ├── cli.py        # 命令行入口（serve / monitor-add / run-check / daemon）
│   └── config.py     # 配置加载（.env / config.yaml）
├── .env.example
└── pyproject.toml
```

## 安装

```bash
cd jzt-sepp-mcp-server

# 方式一：uv（推荐）
uv sync

# 方式二：pip
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Playwright Chromium 浏览器（仅在启用 SSO 兜底登录时需要）
uv run playwright install chromium   # 或 .venv/bin/playwright install chromium
```

## 配置

```bash
cp .env.example .env   # 然后编辑
```

必填（两种方式任选其一）：

```
# 方式 A：账号密码（自动走 normal_auth 接口，密码 sha256 加密提交）
SEPP_USERNAME=你的账号
SEPP_PASSWORD=你的密码

# 方式 B：直接提供 sepp-auth token（浏览器 F12 复制，有效期约 7 天）
SEPP_AUTH_TOKEN=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9...
```

可选：

```
SEPP_USER_ID=1001967          # 默认用户（"我"），查询未指定负责人时使用
SEPP_USER_NAME=郑自航
SEPP_USER_ACCOUNT=ZHENGZIH

# 定时监控默认监控的用户名称列表（逗号分隔，一次监控多个用户）
# monitor_add 不指定负责人时自动使用；也可调用时显式传 responsers
SEPP_MONITOR_USERS=郑益2,房航

# 钉钉 @ 负责人映射（姓名 -> 钉钉手机号）：命中则 @ 手机号触发提醒，未命中则名称加粗
SEPP_DINGTALK_AT_MAP=郑益2:13800000000,房航:13900000000

# 告警渠道（至少配一个才有推送）
SEPP_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=xxx
SEPP_WEBHOOK_TYPE=dingtalk   # dingtalk | wecom | feishu | generic
SEPP_EMAIL_TO=you@example.com
SEPP_SMTP_HOST=...

SEPP_TIMEOUT_HOURS=2         # 超时提醒阈值（小时）
SEPP_ALLOW_SSO_LOGIN=false   # 是否允许 Playwright SSO 浏览器兜底登录（默认禁用，防账号锁定）
```

## 运行 MCP（接入 Claude Code / CodeBuddy / 其他客户端）

```bash
uv run python -m sepp_mcp serve   # 或 .venv/bin/python -m sepp_mcp serve
```

客户端 `mcpServers` 配置示例：

```json
{
  "mcpServers": {
    "sepp": {
      "command": "uv",
      "args": ["--directory", "/Users/zhengzihang/Documents/my-mcp/jzt-sepp-mcp-server", "run", "python", "-m", "sepp_mcp"]
    }
  }
}
```

## 提供的工具

| 工具 | 说明 |
|---|---|
| `login_status` | 登录状态、token 过期时间、默认用户、监控用户配置 |
| `get_user_projects` | 当前用户的项目信息 |
| `get_users(keyword)` | 用户列表（按姓名/账号过滤），用于查 userId |
| `query_defects(...)` | 查询缺陷。支持 `responsers`（**名称列表，一次查多人**）、`fuzzy_responser` / `dev_responser_id` / `test_responser_id` / `status` / `priority` / `summary` 过滤；**不指定负责人时默认查"我"**。多用户返回附加字段 `matched` / `missing` |
| `query_my_defects(...)` | 查询"我"的缺陷 |
| `monitor_add(...)` | 新增监控。`responsers=["郑益2","房航"]` 一次监控多人；不填时用 `SEPP_MONITOR_USERS` 配置，否则默认"我"；支持轮询间隔 + 新增/超时提醒开关 |
| `monitor_run_once(name)` | 立即检查一次 |
| `monitor_list` / `monitor_remove` / `monitor_enable` | 监控管理 |

## 定时任务（提醒）

提醒能力内置在 MCP 服务内（服务运行时自动轮询并恢复监控）。也可以**完全脱离 agent 独立运行**：

### 0) 命令行建监控

```bash
# 一次监控多个用户（名称列表）
uv run python -m sepp_mcp monitor-add --name 开发组缺陷 --responsers 郑益2 房航 --interval-minutes 30

# 或指定单个负责人 / 不指定（用 SEPP_MONITOR_USERS 配置，否则默认"我"）
uv run python -m sepp_mcp monitor-add --name my_defects
```

### 1) 常驻 daemon（推荐做长期提醒）

```bash
nohup uv run python -m sepp_mcp daemon > data/daemon.log 2>&1 &
```

daemon 会恢复**所有启用的监控**并按各自间隔轮询；若一个监控都没有会空转，请先 `monitor-add` 建任务。

### 2) 配合 cron（无需常驻进程）

```bash
uv run python -m sepp_mcp run-check --name 开发组缺陷
```

放到 crontab（如工作日每 30 分钟一次）：

```
*/30 * * * 1-5 cd /Users/zhengzihang/Documents/my-mcp/jzt-sepp-mcp-server && uv run python -m sepp_mcp run-check --name 开发组缺陷 >> data/cron.log 2>&1
```

### 使用流程（以监控多人小组为例）

1. 配置 `.env`：`SEPP_MONITOR_USERS=郑益2,房航`（和可选 `SEPP_DINGTALK_AT_MAP` 手机号映射）；
2. 通过 MCP `monitor_add(name="组内缺陷")` 或命令行 `monitor-add` 建立监控（首次执行为**基线检查**，只记录不提醒）；
3. 之后每 `interval_minutes` 自动检查：**出现新缺陷** 或 **超过 2 小时未处理** 都会推送，消息中负责人按映射 `@手机号`（触发钉钉提醒）或加粗显示；
4. 服务重启后监控自动恢复（无需重新添加）。

## 常见问题

- **登录报 "用户名或密码错误" 并被计数**：SSO 明文表单默认已禁用；请确认走的是 `normal_auth`（账号密码自动 sha256）或 `SEPP_AUTH_TOKEN`。
- **`normal_auth` 失败 / 想手动过验证码**：`SEPP_HEADLESS=false` 首次登录手动过验证；或直接配置 `SEPP_AUTH_TOKEN`。
- **登录报 "未配置账号密码"**：`SEPP_USERNAME` / `SEPP_PASSWORD` 为空，且没有有效 cookie / token。
- **`sepp-auth` 有效期约 7 天**：过期后会自动重新登录（normal_auth API，无需浏览器）。
- **多用户查询某名称解析不到**：返回的 `missing` 字段会列出；请核对名称与 `get_users` 中的 `userName` 是否一致。
- **MCP 配到多个 agent / 多个客户端会不会重复定时任务**：不会重复。服务用跨进程文件锁保证：
  - `data/monitor_state.sched.lock`（调度权）：多个 MCP 实例 / daemon 同时运行只有**一个**真正定时轮询，其余仅提供服务；
  - `data/monitor_state.check.lock`（检查互斥）：`run_check` 串行执行、基于最新 state 去重，即使手动并发触发也不会重复提醒或写坏状态。
  - 锁文件位于 `data/`（已 gitignore）；Windows 无 `fcntl` 时退化为单进程行为，建议只跑一个实例。
- **缺陷返回字段以平台实际为准**：本工具直接透传平台原始 JSON；监控告警按 `foundTime`（或首次发现时间）计算超时。

## 安全说明

- 账号密码、cookie、浏览器登录态均保存在本地 `data/`（已加入 `.gitignore`），请勿提交到仓库。
- 密码通过 sha256 摘要提交给平台 `normal_auth` 接口，本地不落盘明文。
- webhook/邮件仅做提醒，不做敏感数据外发。
