"""命令行入口：serve（默认）/ run-check（配合 cron）/ daemon"""
from __future__ import annotations

import argparse
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

    if args.cmd == "daemon":
        from .config import load_config
        from .monitor import DefectMonitor

        DefectMonitor(load_config()).run_daemon()
        return

    # 默认：MCP stdio 服务
    from .server import main as server_main

    server_main()
