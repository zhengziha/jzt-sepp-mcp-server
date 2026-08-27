"""告警通知：钉钉/企微/飞书 webhook、SMTP 邮件、本地日志"""
from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText

import httpx

from .config import AlertConfig

logger = logging.getLogger("sepp.alerts")


def send_alert(text: str, alert_cfg: AlertConfig) -> list[str]:
    """发送告警，返回实际送达的渠道列表（空 = 仅本地日志）"""
    delivered: list[str] = []
    if alert_cfg.webhook_url:
        try:
            _send_webhook(text, alert_cfg)
            delivered.append("webhook")
        except Exception as e:  # noqa: BLE001
            logger.error("webhook 发送失败: %s", e)
    if alert_cfg.smtp_host and alert_cfg.email_to:
        try:
            _send_email(text, alert_cfg)
            delivered.append("email")
        except Exception as e:  # noqa: BLE001
            logger.error("邮件发送失败: %s", e)
    if not delivered:
        logger.info("[ALERT] %s", text)
    return delivered


def _send_webhook(text: str, cfg: AlertConfig) -> None:
    if cfg.webhook_type == "feishu":
        payload = {"msg_type": "text", "content": {"text": text}}
    elif cfg.webhook_type == "generic":
        payload = {"text": text}
    else:  # dingtalk / wecom
        payload = {"msgtype": "text", "text": {"content": text}}
    resp = httpx.post(cfg.webhook_url, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    code = data.get("errcode", data.get("code", 0))
    if isinstance(code, int) and code != 0:
        raise RuntimeError(f"webhook 返回错误: {data}")


def _send_email(text: str, cfg: AlertConfig) -> None:
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = "[SEPP 缺陷提醒]"
    msg["From"] = cfg.smtp_user
    msg["To"] = ", ".join(cfg.email_to)
    if cfg.smtp_use_ssl:
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=15)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15)
    try:
        server.login(cfg.smtp_user, cfg.smtp_password)
        server.sendmail(cfg.smtp_user, cfg.email_to, msg.as_string())
    finally:
        server.quit()
