"""FastMCP Server：能效平台（SEPP）缺陷管理"""
from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from .auth import SessionManager
from .client import SeppClient
from .config import load_config
from .monitor import DefectMonitor

logger = logging.getLogger("sepp.server")

_config = load_config()
_session = SessionManager(_config)
_monitor = DefectMonitor(_config)
_client: SeppClient | None = None


@lifespan
async def _lifespan(_: FastMCP):
    """服务启动时恢复已启用的监控（服务重启后定时提醒不丢），退出时关闭调度器"""
    _monitor.restore_schedules()
    logger.info("sepp-mcp 启动完成，已恢复 %s 个监控", len(_monitor.list_monitors()))
    yield
    _monitor.stop()


mcp = FastMCP("sepp-mcp", lifespan=_lifespan)


def _get_client() -> SeppClient:
    global _client
    if _client is None:
        _client = SeppClient(_config, _session.ensure_session(), refresh=_refresh_session)
    return _client


def _refresh_session() -> dict[str, str]:
    global _client
    _client = None
    return _session.force_relogin()


# ==================== 基础工具 ====================

@mcp.tool()
def login_status() -> dict:
    """查看当前登录状态（是否已登录、账号、sepp-auth 过期时间）及默认用户信息"""
    info = _session.status()
    info["default_user"] = {
        "userId": _config.user_id,
        "userName": _config.user_name,
        "userAccount": _config.user_account,
    }
    return info


@mcp.tool()
def get_user_projects() -> list[dict]:
    """获取当前登录用户在能效平台的项目信息。

    示例返回：[{"productCode": "JKZS", "productId": 17, "roleName": "开发工程师",
    "productName": "健康诊所科技"}]（该信息基本固定）"""
    return _get_client().get_user_projects()


@mcp.tool()
def get_users(keyword: str = "") -> list[dict]:
    """查询能效平台用户列表（含 userId/userName/userAccount），可按姓名或账号模糊过滤。

    用于把负责人姓名/账号映射为 userId，再传给 query_defects 过滤。"""
    users = _get_client().get_users()
    if keyword:
        kw = keyword.strip().lower()
        users = [
            u for u in users
            if kw in str(u.get("userName") or "").lower()
            or kw in str(u.get("userAccount") or "").lower()
            or kw in str(u.get("userId") or "")
        ]
    return users


# ==================== 缺陷查询 ====================

@mcp.tool()
def query_defects(
    fuzzy_responser: str = "",
    dev_responser_id: str = "",
    test_responser_id: str = "",
    status: str = "",
    priority: str = "",
    summary: str = "",
    page_num: int = 1,
    page_size: int = 20,
) -> dict:
    """查询缺陷列表（支持按负责人过滤）。

    参数：
    - fuzzy_responser: 负责人模糊匹配，可填 userId/姓名/账号，例如 "1001967" 或 "郑自航"
    - dev_responser_id: 开发负责人 userId（用 get_users 查询）
    - test_responser_id: 测试负责人 userId
    - status: 缺陷状态，逗号分隔多个，默认 "1,2,3,4,5,6"（全部）
    - priority: 优先级过滤
    - summary: 标题关键字过滤
    - page_num / page_size: 分页

    **默认行为**：三个负责人参数都不填时，默认查"我"（默认用户 郑自航/1001967）的缺陷，
    与 query_my_defects 等价；要查别人请显式传 fuzzy_responser（姓名/账号/userId）。

    返回平台的原始 JSON（含 total / list）。"""
    if not (fuzzy_responser or dev_responser_id or test_responser_id):
        fuzzy_responser = _config.user_id
    return _get_client().query_defects({
        "fuzzyResponser": fuzzy_responser,
        "devResponserId": dev_responser_id,
        "testResponserId": test_responser_id,
        "status": status or _config.default_status,
        "priority": priority,
        "summary": summary,
        "pageNum": page_num,
        "pageSize": min(int(page_size), 200),
    })


@mcp.tool()
def query_my_defects(status: str = "", page_num: int = 1, page_size: int = 20) -> dict:
    """查询"我"（默认用户 郑自航/1001967）负责的缺陷列表。

    等价于 query_defects(fuzzy_responser=默认用户 userId)。"""
    return query_defects(
        fuzzy_responser=_config.user_id,
        status=status,
        page_num=page_num,
        page_size=page_size,
    )


# ==================== 定时监控 / 提醒 ====================

@mcp.tool()
def monitor_add(
    name: str,
    fuzzy_responser: str = "",
    dev_responser_id: str = "",
    test_responser_id: str = "",
    status: str = "1,2,3,4,5,6",
    interval_minutes: int = 30,
    new_defect_alert: bool = True,
    timeout_alert: bool = True,
    timeout_hours: float = 2.0,
) -> dict:
    """新增一个缺陷监控任务（自动定时轮询 + 提醒）。

    - 负责人不填时默认监控"我"（默认用户 郑自航/1001967）；也可指定：
      fuzzy_responser（userId/姓名/账号，查别人），或 dev_responser_id / test_responser_id（用 get_users 查 userId）
    - new_defect_alert: 是否提醒新增缺陷
    - timeout_alert / timeout_hours: 是否提醒处理超时，超过 N 小时未处理（默认 2 小时）
    - 首次执行为基线检查（只记录不提醒），之后开始提醒。
    - 提醒通过 webhook（钉钉/企微/飞书）或邮件发送，未配置时仅打印日志。
    建议先 monitor_run_once 建立基线。"""
    if not (fuzzy_responser or dev_responser_id or test_responser_id):
        fuzzy_responser = _config.user_id
    return _monitor.add_monitor(
        name=name,
        fuzzy_responser=fuzzy_responser,
        dev_responser_id=dev_responser_id,
        test_responser_id=test_responser_id,
        status=status,
        interval_minutes=interval_minutes,
        new_defect_alert=new_defect_alert,
        timeout_alert=timeout_alert,
        timeout_hours=timeout_hours,
    )


@mcp.tool()
def monitor_run_once(name: str) -> dict:
    """立即执行一次指定监控的检查（不等定时周期），返回本次结果（新增/超时数等）"""
    return _monitor.run_check(name, _get_client())


@mcp.tool()
def monitor_list() -> dict:
    """查看所有缺陷监控任务及状态（含已记录缺陷数）"""
    return _monitor.list_monitors()


@mcp.tool()
def monitor_remove(name: str) -> dict:
    """删除一个缺陷监控任务"""
    return _monitor.remove_monitor(name)


@mcp.tool()
def monitor_enable(name: str, enabled: bool) -> dict:
    """启用/停用一个缺陷监控任务"""
    return _monitor.set_enabled(name, enabled)


# ==================== 资源 ====================

@mcp.resource("sepp://status")
def status_resource() -> dict:
    """能效平台登录状态资源"""
    return _session.status()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mcp.run()


if __name__ == "__main__":
    main()
