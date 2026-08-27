"""自动登录：优先走 normal_auth HTTP 接口（密码 sha256 加密提交），
Playwright SSO 仅作降级兜底；也支持已有 cookie/token。"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import Config

logger = logging.getLogger("sepp.auth")

# 与 client.py 保持一致
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


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
        """返回可用的 cookies；无有效会话时依次尝试：磁盘缓存 -> 手动 token -> normal_auth API -> Playwright 登录"""
        if self._is_valid():
            return self._cookies
        if self._load_from_disk():
            return self._cookies
        if self.config.auth_token:
            self._cookies = self._cookies_from_token()
            return self._cookies
        if self._login_with_api():
            self._save_to_disk()
            return self._cookies
        if self.config.allow_sso_login:
            self._login_with_playwright()
            self._save_to_disk()
            return self._cookies
        raise RuntimeError(
            "自动登录失败：normal_auth 接口未获取到 sepp-auth。"
            "为避免明文提交触发账号锁定，SSO 浏览器登录已默认禁用。"
            "请配置 SEPP_AUTH_TOKEN 直接提供 token，"
            "或显式设置 SEPP_ALLOW_SSO_LOGIN=true 启用浏览器兜底登录。"
        )

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

    # ---------- normal_auth HTTP 登录（密码 sha256 加密，不落明文） ----------
    def _login_with_api(self) -> bool:
        """通过 /sepp/user/normal_auth 接口登录（密码 sha256 加密提交）。

        平台登录表单提交的密码并非明文，而是 sha256 哈希；走 Keycloak 明文表单
        会登录失败并计入错误次数（连续 5 次锁定账号）。本方法优先使用该接口，
        成功返回 True；失败（网络/WAF 拦截/未返回 sepp-auth）返回 False，由调用方降级到 Playwright。
        """
        cfg = self.config
        if not (cfg.username and cfg.password):
            return False
        pwd_hash = hashlib.sha256(cfg.password.encode("utf-8")).hexdigest()
        url = f"{cfg.base_url}/sepp/user/normal_auth"
        params = {
            "account": cfg.username,
            "password": pwd_hash,
            "userId": "-1",
            "productId": "-1",
        }
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "User-Agent": USER_AGENT,
            "Origin": cfg.base_url,
            "Referer": f"{cfg.base_url}/",
            "Content-Length": "0",
        }
        try:
            resp = httpx.post(url, params=params, headers=headers, content=b"", timeout=30)
        except Exception as exc:  # noqa: BLE001
            logger.warning("normal_auth 接口请求异常: %s", exc)
            return False

        # 1) 从 Set-Cookie 提取 sepp-auth
        token = ""
        for sc in resp.headers.get_list("set-cookie"):
            for part in sc.split(";"):
                name, _, value = part.strip().partition("=")
                if name == "sepp-auth":
                    token = value.strip()
                    break
            if token:
                break
        # 2) 兜底：响应体 JSON 里的 token 字段
        if not token:
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                data = None
            if isinstance(data, dict):
                token = str(
                    data.get("token")
                    or data.get("seppAuth")
                    or data.get("sepp-auth")
                    or ""
                )
        if not token:
            logger.warning("normal_auth 未返回 sepp-auth（status=%s），降级到 Playwright", resp.status_code)
            return False

        self._cookies = {
            "userId": cfg.user_id,
            "productId": cfg.product_id,
            "sepp-auth": token,
        }
        exp = _jwt_exp(token)
        self._expires_at = float(exp - 300) if exp else time.time() + 7 * 86400
        self._cookie_source = "api"
        logger.info(
            "normal_auth 登录成功，sepp-auth 有效期至 %s",
            datetime.fromtimestamp(self._expires_at).strftime("%Y-%m-%d %H:%M:%S"),
        )
        return True

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
                    # 定制登录页默认停在"手机登录"标签，账号密码表单是隐藏的，
                    # 需要先点击"账号登录"（#nav-user）标签才会显示
                    nav_user = page.locator("#nav-user")
                    if nav_user.count() > 0:
                        try:
                            nav_user.first.click(timeout=5_000)
                            logger.info("已切换到账号登录标签")
                            page.wait_for_timeout(500)
                        except Exception:  # noqa: BLE001
                            pass
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
