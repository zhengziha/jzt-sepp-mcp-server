"""缺陷监控：定时轮询 -> 新增缺陷提醒 + 处理超时（默认 >2 小时）提醒"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    import fcntl  # POSIX：多实例跨进程锁
except ImportError:  # Windows 无 fcntl，退化为单进程行为
    fcntl = None

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .alerts import send_alert
from .config import Config

logger = logging.getLogger("sepp.monitor")


def _parse_time(value: Any) -> datetime | None:
    """解析缺陷时间字段（'yyyy-MM-dd HH:mm:ss' / ISO / 毫秒时间戳）"""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        v = value / 1000 if abs(value) > 1e12 else value
        try:
            return datetime.fromtimestamp(v)
        except (ValueError, OSError):
            return None
    s = str(value).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M", "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


class DefectMonitor:
    """缺陷监控器，状态持久化在 state_file（JSON）：

    {"monitors": {"<name>": {
        "fuzzy_responser": "1001967", "dev_responser_id": "", "test_responser_id": "",
        "status": "1,2,3,4,5,6", "interval_minutes": 30,
        "new_defect_alert": true, "timeout_alert": true, "timeout_hours": 2.0,
        "alert_on_baseline": false, "baselined": false, "enabled": true,
        "seen": {"<id>": {"first_seen": ..., "found_time": ..., "last_seen": ...,
                          "timeout_alerted": false, "last": {...}}}
    }}}
    """

    def __init__(self, config: Config):
        self.config = config
        self.state_file: Path = config.state_file
        self._lock = threading.Lock()
        self._state = self._load_state()
        self._scheduler: BackgroundScheduler | None = None
        self._schedule_lock_fd: Any = None
        self._owns_schedule = self._try_take_schedule_lock()

    def _try_take_schedule_lock(self) -> bool:
        """跨进程调度权（非阻塞抢锁）。

        多个 MCP 实例 / daemon 同时运行时会各自启动定时器；这里保证同一份
        monitor_state.json 只有一个实例真正调度轮询，其余实例仅提供服务
        （手动 monitor_run_once 仍可用）。进程退出后锁自动释放。
        """
        if fcntl is None:
            return True  # 非 POSIX 平台退化为原行为
        try:
            fd = open(self.state_file.with_suffix(".sched.lock"), "w")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logger.info("检测到其他实例已持有调度权，本实例只提供服务，不启动定时调度")
            return False
        self._schedule_lock_fd = fd
        logger.info("本实例获得调度权，负责定时轮询监控")
        return True

    @contextlib.contextmanager
    def _check_lock(self):
        """跨进程互斥：检查/写状态期间持有文件锁（阻塞获取）。

        多实例同时触发 run_check 或增删改监控时，串行执行并基于最新 state
        去重，避免重复提醒、避免并发写坏 monitor_state.json。
        """
        if fcntl is None:
            yield
            return
        fd = open(self.state_file.with_suffix(".check.lock"), "w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                fd.close()

    # ---------------- 状态持久化 ----------------
    def _load_state(self) -> dict:
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "monitors" in data:
                    return data
            except Exception:  # noqa: BLE001
                logger.exception("读取状态文件失败: %s", self.state_file)
        return {"monitors": {}}

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_file)

    # ---------------- 监控管理 ----------------
    def add_monitor(
        self,
        name: str,
        *,
        fuzzy_responser: str = "",
        dev_responser_id: str = "",
        test_responser_id: str = "",
        responsers: list[str] | None = None,
        status: str = "",
        interval_minutes: int = 30,
        new_defect_alert: bool = True,
        timeout_alert: bool = True,
        timeout_hours: float | None = None,
        alert_on_baseline: bool = False,
    ) -> dict:
        """新增监控。responsers 为多用户名称列表（如 ["郑益2", "房航"]），
        与 fuzzy_responser（单用户）二选一；都填时以 responsers 为准。"""
        responser_names = [str(n).strip() for n in (responsers or []) if str(n).strip()]
        if not (fuzzy_responser or dev_responser_id or test_responser_id or responser_names):
            raise ValueError("至少指定一个负责人过滤条件：responsers / fuzzy_responser / dev_responser_id / test_responser_id")
        with self._check_lock():
            with self._lock:
                if name in self._state["monitors"]:
                    raise ValueError(f"监控 '{name}' 已存在，请换名称或先删除")
                mon: dict[str, Any] = {
                    "fuzzy_responser": str(fuzzy_responser or ""),
                    "dev_responser_id": str(dev_responser_id or ""),
                    "test_responser_id": str(test_responser_id or ""),
                    "responser_names": responser_names,
                    "status": status or self.config.default_status,
                    "interval_minutes": max(1, int(interval_minutes)),
                    "new_defect_alert": bool(new_defect_alert),
                    "timeout_alert": bool(timeout_alert),
                    "timeout_hours": float(timeout_hours if timeout_hours is not None else self.config.timeout_hours),
                    "alert_on_baseline": bool(alert_on_baseline),
                    "baselined": False,
                    "enabled": True,
                    "seen": {},
                }
                self._state["monitors"][name] = mon
                self._save_state()
        if self._owns_schedule:
            self._schedule(name)
        logger.info("新增监控: %s", name)
        return self._summary_monitor(name)

    def remove_monitor(self, name: str) -> dict:
        with self._check_lock():
            with self._lock:
                if name not in self._state["monitors"]:
                    raise ValueError(f"监控 '{name}' 不存在")
                del self._state["monitors"][name]
                self._save_state()
        if self._scheduler:
            self._scheduler.remove_job(self._job_id(name))
        return {"removed": name}

    def set_enabled(self, name: str, enabled: bool) -> dict:
        with self._check_lock():
            with self._lock:
                mon = self._state["monitors"].get(name)
                if not mon:
                    raise ValueError(f"监控 '{name}' 不存在")
                mon["enabled"] = bool(enabled)
                self._save_state()
        if enabled:
            if self._owns_schedule:
                self._schedule(name)
        elif self._scheduler:
            self._scheduler.remove_job(self._job_id(name))
        return self._summary_monitor(name)

    def list_monitors(self) -> dict:
        with self._lock:
            return {name: self._summary_monitor(name) for name in self._state["monitors"]}

    def _summary_monitor(self, name: str) -> dict:
        mon = self._state["monitors"].get(name)
        if mon is None:
            return {"name": name, "error": "not found"}
        summary = {k: v for k, v in mon.items() if k != "seen"}
        summary["seen_count"] = len(mon.get("seen", {}))
        return summary

    # ---------------- 调度 ----------------
    @staticmethod
    def _job_id(name: str) -> str:
        return f"monitor:{name}"

    def _ensure_scheduler(self) -> BackgroundScheduler:
        if self._scheduler is None:
            self._scheduler = BackgroundScheduler()
            self._scheduler.start()
        return self._scheduler

    def _schedule(self, name: str) -> None:
        with self._lock:
            mon = self._state["monitors"].get(name)
            if not mon or not mon.get("enabled", True):
                return
        scheduler = self._ensure_scheduler()
        scheduler.add_job(
            self.run_check,
            IntervalTrigger(minutes=int(mon["interval_minutes"])),
            args=[name],
            id=self._job_id(name),
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        logger.info("已调度监控 %s（每 %s 分钟）", name, mon["interval_minutes"])

    def restore_schedules(self) -> None:
        """服务启动时恢复所有启用的监控。

        仅"调度权"实例真正调度：多个 MCP 实例 / daemon 同时存在时，只有抢到
        sched.lock 的那个负责定时轮询，其余实例只提供服务，避免重复定时任务。
        """
        if not self._owns_schedule:
            logger.info("非调度权实例，跳过监控调度恢复（已有其他实例在跑定时任务）")
            return
        with self._lock:
            names = [n for n, m in self._state["monitors"].items() if m.get("enabled", True)]
        for name in names:
            self._schedule(name)

    def stop(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    # ---------------- 检查逻辑 ----------------
    def _make_client(self) -> Any:
        from .auth import SessionManager
        from .client import SeppClient

        session = SessionManager(self.config)
        return SeppClient(self.config, session.ensure_session())

    def run_check(self, name: str, client: Any = None) -> dict:
        """执行一次检查：查询缺陷 -> 比对 seen -> 新增/超时告警 -> 更新状态

        首次执行为“基线”检查：仅记录当前缺陷，不发告警（避免历史缺陷刷屏）。

        跨进程互斥：多个 MCP 实例 / daemon 同时触发时，通过 check.lock 串行执行；
        后执行的实例读到最新 seen，不会重复提醒、不会写坏 monitor_state.json。
        """
        with self._check_lock():
            return self._do_run_check(name, client)

    def _do_run_check(self, name: str, client: Any = None) -> dict:
        with self._lock:
            mon = self._state["monitors"].get(name)
            if mon is None:
                raise ValueError(f"监控 '{name}' 不存在")
            if not mon.get("enabled", True):
                return {"monitor": name, "skipped": True, "reason": "disabled"}
            snapshot = dict(mon)
            seen = mon["seen"]

        client = client or self._make_client()
        params = {
            "devResponserId": snapshot["dev_responser_id"],
            "testResponserId": snapshot["test_responser_id"],
            "status": snapshot["status"],
            "pageNum": 1,
            "pageSize": 100,
        }
        query_params = dict(params)
        if snapshot["fuzzy_responser"]:
            query_params["fuzzyResponser"] = snapshot["fuzzy_responser"]
        responser_names = snapshot.get("responser_names") or []
        missing_users: list[str] = []
        try:
            if responser_names:
                # 多用户：名称列表 -> userId 列表，逐个查询后合并去重
                resolved = client.resolve_user_ids(responser_names)
                missing_users = resolved["missing"]
                if missing_users:
                    logger.warning("监控 %s 用户名称解析不到 userId: %s", name, missing_users)
                result = (
                    client.query_defects_multi(resolved["user_ids"], query_params)
                    if resolved["user_ids"] else {"total": 0, "list": []}
                )
            else:
                result = client.query_defects(query_params)
        except Exception as exc:  # noqa: BLE001
            logger.error("监控 %s 查询失败: %s", name, exc)
            return {"monitor": name, "error": str(exc)}

        defects = result.get("list", []) if isinstance(result, dict) else []
        now = datetime.now()
        active_set = set(self.config.active_statuses)
        threshold_hours = float(snapshot["timeout_hours"])

        new_items: list[dict] = []
        timeout_items: list[dict] = []
        messages: list[str] = []
        # 钉钉 @：按负责人姓名查 dingtalk_at_map 映射，命中则 @ 手机号（触发提醒），未命中则名称加粗
        at_mobiles: list[str] = []
        at_map = self.config.alert.dingtalk_at_map

        with self._lock:
            baseline = not snapshot["baselined"]
            for d in defects:
                if not isinstance(d, dict):
                    continue
                did = str(d.get("id") or "")
                if not did or did == "0":
                    continue
                status = str(d.get("status") or "")
                summary = str(d.get("summary") or d.get("title") or "")
                found = _parse_time(d.get("foundTime") or d.get("createdDate") or d.get("createdTime"))
                item = {
                    "id": did,
                    "status": status,
                    "summary": summary[:120],
                    "priority": d.get("priority"),
                    "foundTime": str(d.get("foundTime") or ""),
                    "responserName": str(d.get("responserName") or ""),
                }
                rec = seen.get(did)
                is_new = rec is None
                if is_new:
                    rec = {
                        "first_seen": now.isoformat(timespec="seconds"),
                        "found_time": found.isoformat(timespec="seconds") if found else None,
                        "last_seen": now.isoformat(timespec="seconds"),
                        "timeout_alerted": False,
                        "last": item,
                    }
                    seen[did] = rec
                    if not baseline and snapshot["new_defect_alert"]:
                        new_items.append(item)
                        rname = item["responserName"]
                        if rname:
                            mob = at_map.get(rname)
                            if mob:
                                at_mobiles.append(mob)
                                rname_txt = f" <@{mob}>"
                            else:
                                rname_txt = f" **@{rname}**"
                        else:
                            rname_txt = ""
                        messages.append(
                            f"- **新增缺陷** `#{did}` 状态={status} 优先级={item['priority']}{rname_txt}\n"
                            f"  {summary}"
                        )
                else:
                    rec["last"] = item
                    rec["last_seen"] = now.isoformat(timespec="seconds")

                # 超时检查（基线不告警）
                if not baseline and snapshot["timeout_alert"] and status in active_set:
                    base = found
                    if base is None and rec.get("found_time"):
                        try:
                            base = datetime.fromisoformat(rec["found_time"])
                        except ValueError:
                            base = None
                    if base is None:
                        try:
                            base = datetime.fromisoformat(rec["first_seen"])
                        except ValueError:
                            base = None
                    if base is not None:
                        elapsed = (now - base).total_seconds() / 3600.0
                        if elapsed >= threshold_hours and not rec.get("timeout_alerted"):
                            rec["timeout_alerted"] = True
                            timeout_items.append({**item, "elapsed_hours": round(elapsed, 1)})
                            rname = item["responserName"]
                            if rname:
                                mob = at_map.get(rname)
                                if mob:
                                    at_mobiles.append(mob)
                                    rname_txt = f" <@{mob}>"
                                else:
                                    rname_txt = f" **@{rname}**"
                            else:
                                rname_txt = ""
                            messages.append(
                                f"- **超时提醒** `#{did}` 已处理 {elapsed:.1f} 小时"
                                f"（阈值 {threshold_hours} 小时）{rname_txt}\n"
                                f"  {summary}"
                            )

            # 清理：已关闭且超过 24h 未再出现的记录
            cutoff = now - timedelta(hours=24)
            for did in [k for k, v in seen.items()]:
                v = seen[did]
                last_status = str((v.get("last") or {}).get("status") or "")
                if last_status not in active_set:
                    try:
                        last_seen = datetime.fromisoformat(v["last_seen"])
                    except (KeyError, ValueError):
                        last_seen = now
                    if last_seen < cutoff:
                        del seen[did]

            mon["baselined"] = True
            self._save_state()

        summary = {
            "monitor": name,
            "baseline": baseline,
            "users": responser_names or [snapshot["fuzzy_responser"] or ""],
            "missing_users": missing_users,
            "total": result.get("total", 0) if isinstance(result, dict) else len(defects),
            "fetched": len(defects),
            "new_defects": len(new_items),
            "timeout_defects": len(timeout_items),
            "messages": messages,
        }
        if messages:
            text = "\n\n".join(
                [f"### 【SEPP 缺陷提醒】监控：{name}\n> {now:%Y-%m-%d %H:%M:%S}"] + messages
            )
            summary["alert_delivered"] = send_alert(text, self.config.alert, at_mobiles)
        return summary

    # ---------------- 守护模式 ----------------
    def run_daemon(self) -> None:
        self.restore_schedules()
        jobs = self._scheduler.get_jobs() if self._scheduler else []
        logger.info("daemon 已启动，共 %s 个监控任务", len(jobs))
        if not jobs:
            if self._owns_schedule:
                logger.warning("没有启用的监控任务，daemon 空转。请先 monitor-add 建监控。")
            else:
                logger.warning("本实例未获得调度权（已有其他 MCP 实例/daemon 在跑），daemon 空转。")
        try:
            while True:
                time.sleep(30)
        except KeyboardInterrupt:
            self.stop()
