"""自动登录：Playwright 走 Keycloak SSO 流程获取 sepp-auth cookie；也支持已有 cookie/token。"""
from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config

logger = logging.getLogger("sepp.auth")


class AuthError(RuntimeError):
    pass


def _jwt_exp(token: str) -> int | None:
    """解析 sepp-auth JWT 的 exp 字段（unix 秒）"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return int(data["exp"])
    except Exception:  # noqa: BLE001
        return None


class SessionManager:
    """管理登录态（sepp-auth cookie / token），支持自动登录并本地持久化。"""

    def __init__(self, config: Config):
        self.config = config
        self._cookies: dict[str, str] = {}
        self._expires_at: float | None = None
        self._cookie_source = "none"

    # ---------- 对外接口 ----------
    def ensure_session(self) -> dict[str, str]:
        """返回可用的 cookies；无有效会话时依次尝试：磁盘缓存 -> 手动 token -> Playwright 登录"""
        if self._is_valid():
            return self._cookies
        if self._load_from_disk():
            return self._cookies
        if self.config.auth_token:
            self._cookies = self._cookies_from_token()
            return self._cookies
        self._login_with_playwright()
        self._save_to_disk()
        return self._cookies

    def force_relogin(self) -> dict[str, str]:
        """主动重新登录（清空当前会话后重新 ensure）"""
        self._cookies = {}
        self._expires_at = None
        self._cookie_source = "none"
        return self.ensure_session()

    def is_valid(self) -> bool:
        return self._is_valid()

    def status(self) -> dict:
        return {
            "valid": self._is_valid(),
            "source": self._cookie_source,
            "user_id": self.config.user_id,
            "product_id": self.config.product_id,
            "username": self.config.username,
            "expires_at": (
                datetime.fromtimestamp(self._expires_at).strftime("%Y-%m-%d %H:%M:%S")
                if self._expires_at
                else None
            ),
        }

    # ---------- 有效性判断 ----------
    def _is_valid(self) -> bool:
        if not self._cookies or not self._cookies.get("sepp-auth"):
            return False
        if self._expires_at is not None and time.time() >= self._expires_at:
            logger.info("sepp-auth 已过期")
            return False
        return True

    # ---------- 手动 token ----------
    def _cookies_from_token(self) -> dict[str, str]:
        token = self.config.auth_token.strip()
        exp = _jwt_exp(token)
        if exp is not None and time.time() >= exp:
            raise AuthError(
                "SEPP_AUTH_TOKEN 已过期，请重新从浏览器获取，或改用 SEPP_USERNAME/SEPP_PASSWORD 自动登录"
            )
        self._expires_at = float(exp - 300) if exp else None
        self._cookie_source = "token"
        return {
            "userId": self.config.user_id,
            "productId": self.config.product_id,
            "sepp-auth": token,
        }

    # ---------- 磁盘持久化 ----------
    def _load_from_disk(self) -> bool:
        path: Path = self.config.cookie_file
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cookies: dict[str, str] = data["cookies"]
            expires = data.get("expires_at")
        except Exception:  # noqa: BLE001
            logger.warning("cookie 文件解析失败: %s", path)
            return False
        token = cookies.get("sepp-auth")
        if not token:
            return False
        exp = _jwt_exp(token)
        if exp is not None and time.time() >= exp:
            logger.info("磁盘中的 sepp-auth 已过期")
            return False
        if exp is None and expires is not None and time.time() >= float(expires):
            return False
        self._cookies = dict(cookies)
        self._expires_at = float(exp - 300) if exp else (float(expires) if expires else None)
        self._cookie_source = "disk"
        return True

    def _save_to_disk(self) -> None:
        path: Path = self.config.cookie_file
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cookies": self._cookies,
            "expires_at": self._expires_at,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("登录态已保存到 %s", path)

    # ---------- Playwright 自动登录 ----------
    def _login_with_playwright(self) -> None:
        from playwright.sync_api import sync_playwright  # 延迟导入，未安装也可用其它功能

        cfg = self.config
        if not (cfg.username and cfg.password):
            raise AuthError(
                "未配置 SEPP_USERNAME/SEPP_PASSWORD，且没有有效 cookie/token，无法自动登录。"
                "请参考 .env.example 配置后重试。"
            )
        cfg.profile_dir.mkdir(parents=True, exist_ok=True)

        logger.info("开始 Playwright 自动登录...")
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(cfg.profile_dir),
                headless=cfg.headless,
                viewport={"width": 1280, "height": 800},
                locale="zh-CN",
            )
            page = context.new_page()
            cookies: list[dict[str, Any]] = []
            try:
                page.goto(cfg.sso_url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(2_000)

                # Keycloak 已有 SSO 会话时自动跳回；否则出现登录表单
                if "auth.jztweb.com" in page.url:
                    page.wait_for_selector("#username", timeout=30_000)
                    page.fill("#username", cfg.username)
                    page.fill("#password", cfg.password)
                    login_btn = page.locator("#kc-login")
                    if login_btn.count() == 0:
                        login_btn = page.locator("button[name='login']")
                    login_btn.first.click()
                    logger.info("已提交登录表单，等待跳转...")
                    page.wait_for_timeout(3_000)

                # 等待回到能效平台
                deadline = time.time() + cfg.login_timeout_seconds
                while time.time() < deadline:
                    if "sepp.op.yyjzt.com" in page.url:
                        break
                    page.wait_for_timeout(1_000)
                else:
                    raise AuthError(f"登录后未跳转回能效平台，当前地址: {page.url}")

                # 等 SPA 完成登录并写入 userId/productId cookie
                page.wait_for_timeout(5_000)
                cookies = context.cookies()
            finally:
                page.close()
                context.close()

        self._cookies = {
            c["name"]: c["value"]
            for c in cookies
            if "sepp.op.yyjzt.com" in c["domain"]
        }
        self._cookies.setdefault("userId", cfg.user_id)
        self._cookies.setdefault("productId", cfg.product_id)

        token = self._cookies.get("sepp-auth", "")
        if not token:
            raise AuthError(
                "登录成功但未获取到 sepp-auth cookie，请检查登录流程"
                "（可能需要 SEPP_HEADLESS=false 手动过验证）"
            )
        exp = _jwt_exp(token)
        self._expires_at = float(exp - 300) if exp else time.time() + 7 * 86400
        self._cookie_source = "playwright"
        logger.info(
            "Playwright 登录成功，sepp-auth 有效期至 %s",
            datetime.fromtimestamp(self._expires_at).strftime("%Y-%m-%d %H:%M:%S"),
        )
