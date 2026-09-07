"""mail.tm provider。"""

import re
import secrets
import time

from .common import extract_verification_code, generate_username, select_messages_for_code


def get_api_base(ctx):
    return str(ctx.config.get("mailtm_api_base", "") or "https://api.mail.tm").rstrip("/")


def get_domains(ctx):
    resp = ctx.http_get(f"{get_api_base(ctx)}/domains")
    resp.raise_for_status()
    members = resp.json().get("hydra:member", [])
    if not members:
        raise Exception("mail.tm 没有返回任何可用域名")
    active = [m for m in members if m.get("isActive")]
    if not active:
        raise Exception("mail.tm 没有活跃的域名")
    return active


def create_account(ctx, address, password):
    resp = ctx.http_post(
        f"{get_api_base(ctx)}/accounts",
        json={"address": address, "password": password},
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("id"):
        raise Exception(f"mail.tm 创建账号失败: {data}")
    return data.get("id")


def get_token(ctx, address, password):
    resp = ctx.http_post(
        f"{get_api_base(ctx)}/token",
        json={"address": address, "password": password},
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    token = resp.json().get("token")
    if not token:
        raise Exception("mail.tm 获取 token 失败")
    return token


def get_messages(ctx, token):
    resp = ctx.http_get(
        f"{get_api_base(ctx)}/messages",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def get_message_detail(ctx, message_id, token):
    resp = ctx.http_get(
        f"{get_api_base(ctx)}/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    return resp.json()


def get_email_and_token(ctx):
    domains = get_domains(ctx)
    domain = domains[0].get("domain")
    if not domain:
        raise Exception("mail.tm 域名数据格式错误")
    address = f"{generate_username(10)}@{domain}"
    password = secrets.token_urlsafe(12)
    create_account(ctx, address, password)
    return address, get_token(ctx, address, password)


def get_oai_code(
    ctx,
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    del email, resend_callback
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        ctx.raise_if_cancelled(cancel_callback)
        try:
            messages = get_messages(ctx, dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] mail.tm 拉取邮件列表失败: {exc}")
            ctx.sleep_with_cancel(poll_interval, cancel_callback)
            continue
        messages, _ = select_messages_for_code(messages)
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            subject = msg.get("subject", "")
            try:
                detail = get_message_detail(ctx, msg_id, dev_token)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] mail.tm 获取邮件详情失败: {exc}")
                continue
            parts = []
            if detail.get("text"):
                parts.append(detail["text"])
            html_value = detail.get("html") or []
            if isinstance(html_value, list):
                parts.extend(html_value)
            else:
                parts.append(str(html_value))
            combined = "\n".join(re.sub(r"<[^>]+>", " ", str(value)) for value in parts)
            code = extract_verification_code(combined, subject)
            if log_callback:
                log_callback(f"[Debug] mail.tm 收到邮件: {subject}")
            if code:
                if log_callback:
                    log_callback(f"[*] mail.tm 解析验证码: {code}")
                return code
        ctx.sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"mail.tm 在 {timeout}s 内未收到验证码邮件")
