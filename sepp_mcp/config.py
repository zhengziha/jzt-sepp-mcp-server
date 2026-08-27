"""配置：环境变量 + 可选 config.yaml（.env 自动加载）"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001
    pass

import yaml

ROOT = Path(__file__).resolve().parent.parent

BASE_URL = "https://sepp.op.yyjzt.com"
AUTH_URL = "https://auth.jztweb.com/auth/realms/jzt/protocol/openid-connect/auth"
REDIRECT_URI = "https://sepp.op.yyjzt.com/sepp/sso/callback?cardId=null&source=portal"

# 默认用户（"我"）：郑自航。查询未指定负责人时默认用它
DEFAULT_USER_ID = "1001967"
DEFAULT_USER_NAME = "郑自航"
DEFAULT_USER_ACCOUNT = "ZHENGZIH"
DEFAULT_PRODUCT_ID = "17"


def build_sso_url(redirect_uri: str = REDIRECT_URI) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "scope": "openid profile email",
            "client_id": "yjj-nx",
            "redirect_uri": redirect_uri,
        }
    )
    return f"{AUTH_URL}?{query}"


@dataclass
class AlertConfig:
    webhook_url: str = ""
    webhook_type: str = "dingtalk"  # dingtalk | wecom | feishu | generic
    # 钉钉 @ 手机号映射：负责人姓名 -> 钉钉手机号（平台用户接口无手机号，需静态配置）。
    # 告警时按缺陷的 responserName 查映射：命中则 @ 手机号（触发钉钉提醒），未命中则负责人名称加粗。
    dingtalk_at_map: dict[str, str] = field(default_factory=dict)
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_ssl: bool = True
    email_to: list[str] = field(default_factory=list)


@dataclass
class Config:
    base_url: str = BASE_URL
    sso_url: str = field(default_factory=build_sso_url)
    username: str = ""
    password: str = ""
    auth_token: str = ""
    user_id: str = DEFAULT_USER_ID
    user_name: str = DEFAULT_USER_NAME
    user_account: str = DEFAULT_USER_ACCOUNT
    product_id: str = DEFAULT_PRODUCT_ID
    headless: bool = True
    # 定时监控默认监控的用户名称列表（如 ["郑益2", "房航"]）。
    # monitor_add 不指定负责人时，优先用它（一次监控多个用户）；为空则监控"我"。
    monitor_users: list[str] = field(default_factory=list)
    # SSO（Keycloak）登录提交的是明文密码，会被计入错误次数（连续 5 次锁定账号）。
    # 默认禁用，仅当显式设置 SEPP_ALLOW_SSO_LOGIN=true 时启用浏览器兜底登录。
    allow_sso_login: bool = False
    login_timeout_seconds: int = 90
    cookie_file: Path = ROOT / "data" / "cookies.json"
    profile_dir: Path = ROOT / "data" / "browser_profile"
    state_file: Path = ROOT / "data" / "monitor_state.json"
    default_status: str = "1,2,3,4,5,6"
    active_statuses: list[str] = field(default_factory=lambda: ["1", "2", "3", "4", "5"])
    timeout_hours: float = 2.0
    alert: AlertConfig = field(default_factory=AlertConfig)


def load_config() -> Config:
    cfg = Config()

    # 1) 可选 config.yaml（复杂配置），可用 SEPP_CONFIG 指定路径
    yaml_path = Path(os.environ.get("SEPP_CONFIG") or (ROOT / "config.yaml"))
    if yaml_path.exists():
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        _apply_yaml(cfg, raw)

    # 2) 环境变量覆盖
    cfg.username = os.getenv("SEPP_USERNAME", cfg.username)
    cfg.password = os.getenv("SEPP_PASSWORD", cfg.password)
    cfg.auth_token = os.getenv("SEPP_AUTH_TOKEN", cfg.auth_token)
    cfg.user_id = os.getenv("SEPP_USER_ID", cfg.user_id)
    cfg.user_name = os.getenv("SEPP_USER_NAME", cfg.user_name)
    cfg.user_account = os.getenv("SEPP_USER_ACCOUNT", cfg.user_account)
    cfg.product_id = os.getenv("SEPP_PRODUCT_ID", cfg.product_id)
    if os.getenv("SEPP_MONITOR_USERS"):
        cfg.monitor_users = [s.strip() for s in os.environ["SEPP_MONITOR_USERS"].split(",") if s.strip()]
    cfg.headless = os.getenv("SEPP_HEADLESS", "true").lower() not in ("false", "0", "no")
    cfg.allow_sso_login = os.getenv("SEPP_ALLOW_SSO_LOGIN", "false").lower() in ("true", "1", "yes", "on")
    if os.getenv("SEPP_LOGIN_TIMEOUT"):
        cfg.login_timeout_seconds = int(os.environ["SEPP_LOGIN_TIMEOUT"])
    if os.getenv("SEPP_COOKIE_FILE"):
        cfg.cookie_file = Path(os.environ["SEPP_COOKIE_FILE"])
    if os.getenv("SEPP_PROFILE_DIR"):
        cfg.profile_dir = Path(os.environ["SEPP_PROFILE_DIR"])
    if os.getenv("SEPP_STATE_FILE"):
        cfg.state_file = Path(os.environ["SEPP_STATE_FILE"])
    if os.getenv("SEPP_DEFAULT_STATUS"):
        cfg.default_status = os.environ["SEPP_DEFAULT_STATUS"]
    if os.getenv("SEPP_ACTIVE_STATUSES"):
        cfg.active_statuses = [s.strip() for s in os.environ["SEPP_ACTIVE_STATUSES"].split(",") if s.strip()]
    if os.getenv("SEPP_TIMEOUT_HOURS"):
        cfg.timeout_hours = float(os.environ["SEPP_TIMEOUT_HOURS"])

    a = cfg.alert
    a.webhook_url = os.getenv("SEPP_WEBHOOK_URL", a.webhook_url)
    a.webhook_type = os.getenv("SEPP_WEBHOOK_TYPE", a.webhook_type)
    if os.getenv("SEPP_DINGTALK_AT_MAP"):
        a.dingtalk_at_map = _parse_kv_map(os.environ["SEPP_DINGTALK_AT_MAP"])
    a.smtp_host = os.getenv("SEPP_SMTP_HOST", a.smtp_host)
    if os.getenv("SEPP_SMTP_PORT"):
        a.smtp_port = int(os.environ["SEPP_SMTP_PORT"])
    a.smtp_user = os.getenv("SEPP_SMTP_USER", a.smtp_user)
    a.smtp_password = os.getenv("SEPP_SMTP_PASSWORD", a.smtp_password)
    a.smtp_use_ssl = os.getenv("SEPP_SMTP_SSL", "true").lower() not in ("false", "0", "no")
    if os.getenv("SEPP_EMAIL_TO"):
        a.email_to = [e.strip() for e in os.environ["SEPP_EMAIL_TO"].split(",") if e.strip()]
    return cfg


def _apply_yaml(cfg: Config, raw: dict[str, Any]) -> None:
    for key in (
        "base_url", "sso_url", "username", "password", "auth_token",
        "user_id", "user_name", "user_account", "product_id", "headless", "allow_sso_login",
        "default_status", "active_statuses", "timeout_hours", "login_timeout_seconds",
        "monitor_users",
    ):
        if key in raw:
            setattr(cfg, key, raw[key])
    alert = raw.get("alert") or {}
    for key in (
        "webhook_url", "webhook_type", "smtp_host", "smtp_port",
        "smtp_user", "smtp_password", "smtp_use_ssl", "email_to",
        "dingtalk_at_map",
    ):
        if key in alert:
            setattr(cfg.alert, key, alert[key])


def _parse_kv_map(raw: str) -> dict[str, str]:
    """解析 "姓名:手机号,姓名2:手机号2" 为 dict"""
    result: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        k, _, v = item.partition(":")
        if k.strip() and v.strip():
            result[k.strip()] = v.strip()
    return result
