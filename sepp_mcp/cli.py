"""命令行入口：serve（默认）/ run-check（配合 cron）/ monitor-add（建监控）/ daemon（常驻）"""
from __future__ import annotations

import argparse
import json
import logging


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sepp-mcp", description="能效平台 SEPP MCP Server / 缺陷监控")
    parser.add_argument("--log-level", default="INFO", help="日志级别，默认 INFO")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("serve", help="以 stdio 方式运行 MCP 服务（默认）")

    run = sub.add_parser("run-check", help="立即执行一次指定监控检查（可配合 cron/自动化定时调用）")
    run.add_argument("--name", required=True, help="监控名称")

    add = sub.add_parser("monitor-add", help="命令行新增监控任务（脱离 agent 也能建监控）")
    add.add_argument("--name", required=True, help="监控名称")
    add.add_argument(
        "--responsers", nargs="*", default=[],
        help="负责人名称列表（可多个），例如 --responsers 郑益2 房航",
    )
    add.add_argument("--fuzzy-responser", default="", help="单个负责人 userId/姓名/账号")
    add.add_argument("--dev-responser-id", default="", help="开发负责人 userId")
    add.add_argument("--test-responser-id", default="", help="测试负责人 userId")
    add.add_argument("--status", default="", help="缺陷状态，逗号分隔多个（默认取配置值）")
    add.add_argument("--interval-minutes", type=int, default=30, help="轮询间隔（分钟），默认 30")
    add.add_argument("--timeout-hours", type=float, default=None, help="超时阈值（小时），默认取配置值")
    add.add_argument("--no-new-alert", action="store_true", help="不提醒新增缺陷")
    add.add_argument("--no-timeout-alert", action="store_true", help="不提醒超时未处理")

    sub.add_parser("daemon", help="后台守护运行所有启用的监控（不启动 MCP，常驻进程）")

    args = parser.parse_args(argv)
    _setup_logging(args.log_level)

    if args.cmd == "run-check":
        from .config import load_config
        from .monitor import DefectMonitor

        cfg = load_config()
        # 先校验监控存在（不存在直接报错，避免无谓登录），再按需触发登录
        result = DefectMonitor(cfg).run_check(args.name)
        print(result)
        return

    if args.cmd == "monitor-add":
        from .config import load_config
        from .monitor import DefectMonitor

        cfg = load_config()
        monitor = DefectMonitor(cfg)
        fuzzy = args.fuzzy_responser
        responsers = list(args.responsers)
        # 与 MCP monitor_add 同语义：不指定负责人时用配置的用户列表，否则默认"我"
        if not (fuzzy or args.dev_responser_id or args.test_responser_id or responsers):
            if cfg.monitor_users:
                responsers = list(cfg.monitor_users)
            else:
                fuzzy = cfg.user_id
        result = monitor.add_monitor(
            name=args.name,
            fuzzy_responser=fuzzy,
            dev_responser_id=args.dev_responser_id,
            test_responser_id=args.test_responser_id,
            responsers=responsers,
            status=args.status,
            interval_minutes=args.interval_minutes,
            new_defect_alert=not args.no_new_alert,
            timeout_alert=not args.no_timeout_alert,
            timeout_hours=args.timeout_hours,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.cmd == "daemon":
        from .config import load_config
        from .monitor import DefectMonitor

        DefectMonitor(load_config()).run_daemon()
        return

    # 默认：MCP stdio 服务
    from .server import main as server_main

    server_main()
