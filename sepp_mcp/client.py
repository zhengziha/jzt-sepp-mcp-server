"""SEPP HTTP 客户端：用登录后的 cookies 直连接口（无需每次走浏览器）"""
from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

from .config import Config

logger = logging.getLogger("sepp.client")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


class SeppClient:
    def __init__(
        self,
        config: Config,
        cookies: dict[str, str],
        refresh: Callable[[], dict[str, str]] | None = None,
    ):
        self.config = config
        self.cookies = dict(cookies)
        self._refresh = refresh
        # 平台用户列表缓存（get_users），会话内首次解析后复用
        self._users_cache: list[dict[str, Any]] | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "User-Agent": USER_AGENT,
            "Referer": f"{self.config.base_url}/",
            "Origin": self.config.base_url,
            "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
        }

    def _post(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.config.base_url}{path}"
        for attempt in range(2):
            resp = httpx.post(url, params=params, headers=self._headers(), content=b"", timeout=30)
            if resp.status_code in (401, 403) and attempt == 0 and self._refresh:
                logger.warning("接口返回 %s，尝试重新登录...", resp.status_code)
                self.cookies = self._refresh()
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("登录态刷新失败")

    # ---------- 业务接口 ----------
    def get_user_projects(self) -> list[dict[str, Any]]:
        """获取用户项目信息（示例账号固定返回：健康诊所科技/开发工程师）"""
        data = self._post("/sepp/role/p_r_query_user", {"userId": self.config.user_id})
        return data if isinstance(data, list) else []

    def get_users(self) -> list[dict[str, Any]]:
        """查询平台用户列表（userId/userName/userAccount）"""
        data = self._post(
            "/sepp/user/query_p",
            {"userId": self.config.user_id, "productId": self.config.product_id},
        )
        return data if isinstance(data, list) else []

    def _get_users_cached(self) -> list[dict[str, Any]]:
        if self._users_cache is None:
            self._users_cache = self.get_users()
        return self._users_cache

    def resolve_user_ids(self, names: list[str]) -> dict[str, Any]:
        """把用户名称列表解析为 userId 列表（精确匹配姓名优先，姓名/账号模糊兜底）。

        返回 {"user_ids": [去重后的 userId...], "matched": {名称: [userId...]},
              "missing": [找不到的名称...]}。
        同名用户全部纳入查询（避免漏），完全找不到才记入 missing。
        """
        users = self._get_users_cached()
        norm: list[tuple[str, str, str]] = []  # (userName, userId, userAccount)
        for u in users:
            if not isinstance(u, dict):
                continue
            uname = str(u.get("userName") or "").strip()
            uid = str(u.get("userId") or "").strip()
            if uname and uid:
                norm.append((uname, uid, str(u.get("userAccount") or "").strip()))

        user_ids: list[str] = []
        matched: dict[str, list[str]] = {}
        missing: list[str] = []
        for name in names:
            n = str(name).strip()
            if not n:
                continue
            hits = [uid for uname, uid, _ in norm if uname == n]
            if not hits:
                hits = [uid for uname, uid, acc in norm if n in uname or n in acc]
            if hits:
                matched[n] = hits
                for uid in hits:
                    if uid not in user_ids:
                        user_ids.append(uid)
            else:
                missing.append(n)
        return {"user_ids": user_ids, "matched": matched, "missing": missing}

    def query_defects_multi(self, responser_ids: list[str], params: dict[str, Any]) -> dict[str, Any]:
        """按多个负责人 userId 分别查询缺陷并合并去重（平台一次只支持单个 fuzzyResponser）。

        params 中的 fuzzyResponser 会被逐个覆盖；单个用户查询失败仅记日志不中断。
        返回 {"total", "list", "pageNum", "pageSize", "queried_users"}。
        """
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for uid in responser_ids:
            p = dict(params)
            p["fuzzyResponser"] = uid
            try:
                result = self.query_defects(p)
            except Exception as exc:  # noqa: BLE001
                logger.warning("查询负责人 %s 的缺陷失败: %s", uid, exc)
                continue
            if not isinstance(result, dict):
                continue
            for item in result.get("list") or []:
                if not isinstance(item, dict):
                    continue
                did = str(item.get("id") or "")
                if did and did not in seen:
                    seen.add(did)
                    merged.append(item)
        return {
            "total": len(merged),
            "list": merged,
            "pageNum": params.get("pageNum", 1),
            "pageSize": params.get("pageSize", 20),
            "queried_users": responser_ids,
        }

    def query_defects(self, params: dict[str, Any]) -> dict[str, Any]:
        """查询缺陷列表，返回平台原始 JSON（total / list / pageNum ...）"""
        base: dict[str, Any] = {
            "relId": "", "submitter": "", "id": "", "reqId": "", "outerSystemNo": "",
            "priority": "", "influence": "", "foundPeriod": "", "defectPeriod": "",
            "prodModules": "", "prodModule2": "", "defectType": "", "summary": "",
            "defectBelonging": "", "devResponserId": "", "testResponserId": "",
            "fuzzyResponser": "", "prodIds": "",
            "status": self.config.default_status,
            "foundTimeBegin": "", "foundTimeEnd": "",
            "fixTimeBegin": "", "fixTimeEnd": "",
            "fixedTimeBegin": "", "fixedTimeEnd": "",
            "pageNum": "1", "pageSize": "20",
            "userId": self.config.user_id, "productId": self.config.product_id,
        }
        for k, v in (params or {}).items():
            if v is None:
                v = ""
            base[k] = str(v)
        return self._post("/sepp/defect/query", base)
