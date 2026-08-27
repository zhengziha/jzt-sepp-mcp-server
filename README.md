# jzt-sepp-mcp-server

能效平台（SEPP，`sepp.op.yyjzt.com`）缺陷管理的 **MCP Server**，基于 FastMCP 封装。

## 功能

- **自动登录**：通过 Playwright 自动走 Keycloak SSO 登录，拿到 `sepp-auth` cookie 后直接 HTTP 调用接口（无需每次开浏览器）；登录态本地持久化，7 天内免登录。
  - 也支持直接粘贴浏览器里的 `sepp-auth` JWT（`SEPP_AUTH_TOKEN`）跳过自动登录。
- **获取用户项目信息**：`get_user_projects`（健康诊所科技 / 开发工程师）。
- **查询缺陷列表，支持按负责人过滤**：`query_defects` / `query_my_defects` / `get_users`（姓名/账号 → userId 映射）。
- **定时提醒**：
  - `monitor_add` 创建监控任务（支持按负责人过滤），内置 APScheduler 定时轮询；
  - **新增缺陷提醒**：缺陷 ID 首次出现时提醒；
  - **超时提醒**：缺陷超过 `timeout_hours`（默认 **2 小时**）仍未处理时提醒；
  - 提醒渠道：钉钉/企微/飞书 webhook、SMTP 邮件（至少配置一个，否则仅打印日志）；
  - 监控状态持久化在 `data/monitor_state.json`，服务重启后自动恢复。

## 目录结构

```
.
├── sepp_mcp/
│   ├── server.py     # FastMCP 服务（工具定义）
│   ├── auth.py       # 自动登录（Playwright SSO / token / cookie 缓存）
│   ├── client.py     # HTTP 客户端（登录态复用 + 401 自动重登）
│   ├── monitor.py    # 缺陷监控（新增/超时提醒 + APScheduler）
│   ├── alerts.py     # 告警通知（webhook / 邮件）
│   ├── cli.py        # 命令行入口（serve / run-check / daemon）
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

# 安装 Playwright Chromium 浏览器（自动登录必需）
uv run playwright install chromium   # 或 .venv/bin/playwright install chromium
```

## 配置

```bash
cp .env.example .env   # 然后编辑
```

必填：

```
SEPP_USERNAME=你的账号
SEPP_PASSWORD=你的密码
```

可选（告警渠道至少配一个才有推送）：

```
SEPP_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=xxx
SEPP_WEBHOOK_TYPE=dingtalk   # dingtalk | wecom | feishu | generic
SEPP_EMAIL_TO=you@example.com
SEPP_SMTP_HOST=...
SEPP_TIMEOUT_HOURS=2         # 超时提醒阈值（小时）
```

## 运行 MCP（接入 Claude Code / CodeBuddy / 其他客户端）

```bash
# 方式一：uv
uv run python -m sepp_mcp serve

# 方式二：venv
.venv/bin/python -m sepp_mcp serve
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

或使用 venv 的 python：

```json
{
  "mcpServers": {
    "sepp": {
      "command": "/Users/zhengzihang/Documents/my-mcp/jzt-sepp-mcp-server/.venv/bin/python",
      "args": ["-m", "sepp_mcp"]
    }
  }
}
```

## 提供的工具

| 工具 | 说明 |
|---|---|
| `login_status` | 登录状态、token 过期时间 |
| `get_user_projects` | 当前用户的项目信息 |
| `get_users(keyword)` | 用户列表（按姓名/账号过滤），用于查 userId |
| `query_defects(...)` | 查询缺陷，支持 `fuzzy_responser` / `dev_responser_id` / `test_responser_id` / `status` / `priority` / `summary` 过滤；**不指定负责人时默认查"我"**（默认用户 郑自航/1001967） |
| `query_my_defects(...)` | 查询"我"的缺陷 |
| `monitor_add(...)` | 新增监控（负责人不填默认监控"我"；轮询间隔 + 新增/超时提醒开关） |
| `monitor_run_once(name)` | 立即检查一次 |
| `monitor_list` / `monitor_remove` / `monitor_enable` | 监控管理 |

## 定时任务（提醒）

提醒能力内置在 MCP 服务内（服务运行时自动轮询并恢复监控）。另外提供两种独立运行方式：

### 1) 常驻 daemon（推荐做长期提醒）

```bash
nohup .venv/bin/python -m sepp_mcp daemon > data/daemon.log 2>&1 &
```

### 2) 配合 cron / CodeBuddy 自动化

```bash
.venv/bin/python -m sepp_mcp run-check --name my_defects
```

可放到 crontab（如工作日每 30 分钟一次）：

```
*/30 * * * 1-5 cd /Users/zhengzihang/Documents/my-mcp/jzt-sepp-mcp-server && .venv/bin/python -m sepp_mcp run-check --name my_defects >> data/cron.log 2>&1
```

### 使用流程（以监控自己为例）

1. 首次启动 MCP 服务，调用 `monitor_add(name="my_defects")` 建立监控（默认监控"我"=郑自航/1001967，会自动记录基线）；
2. 调用 `monitor_run_once(name="my_defects")` 立即跑一次基线；
3. 之后每 30 分钟自动检查：**出现新缺陷** 或 **超过 2 小时未处理** 都会推送提醒；
4. 服务重启后监控自动恢复（无需重新添加）。

## 常见问题

- **WAF / 验证码拦截导致登录失败**：设置 `SEPP_HEADLESS=false`，首次登录时手动过验证，之后登录态会持久化。
- **登录报 "未配置账号密码"**：`SEPP_USERNAME` / `SEPP_PASSWORD` 为空，且没有有效 cookie。
- **`sepp-auth` 有效期约 7 天**：过期后会自动重新 Playwright 登录（需浏览器已安装）。
- **缺陷返回字段以平台实际为准**：本工具直接透传平台原始 JSON；监控告警按 `foundTime`（或首次发现时间）计算超时。

## 安全说明

- 账号密码、cookie、浏览器登录态均保存在本地 `data/`（已加入 `.gitignore`），请勿提交到仓库。
- webhook/邮件仅做提醒，不做敏感数据外发。
