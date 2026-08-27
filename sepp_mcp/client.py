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
