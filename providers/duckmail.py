"""DuckMail provider。"""

import re
import secrets
import time

from .common import extract_verification_code, generate_username, select_messages_for_code


API_BASE = "https://api.duckmail.sbs"


def get_api_key(ctx):
    return ctx.config.get("duckmail_api_key", "")


def get_domains(ctx, api_key=None):
    headers = {}
    key = api_key or get_api_key(ctx)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = ctx.http_get(f"{API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def create_account(ctx, address, password, api_key=None, expires_in=0):
    headers = {"Content-Type": "application/json"}
    key = api_key or get_api_key(ctx)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = {"address": address, "password": password, "expiresIn": expires_in}
    resp = ctx.http_post(f"{API_BASE}/accounts", json=data, headers=headers)
    resp.raise_for_status()
    return resp.json()


def get_token(ctx, address, password):
    resp = ctx.http_post(f"{API_BASE}/token", json={"address": address, "password": password})
    resp.raise_for_status()
    return resp.json().get("token")


def get_messages(ctx, token):
    headers = {"Authorization": f"Bearer {token}"}
    resp = ctx.http_get(f"{API_BASE}/messages", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def get_message_detail(ctx, token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    resp = ctx.http_get(f"{API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    return resp.json()


def get_email_and_token(ctx, api_key=None):
    key = api_key or get_api_key(ctx)
    domains = get_domains(ctx, api_key=key)
    if not domains:
        raise Exception("DuckMail 没有返回任何可用域名")
    private = [d for d in domains if d.get("ownerId")]
    verified_private = [d for d in private if d.get("isVerified")]
    public = [d for d in domains if d.get("isVerified")]
    target = (verified_private or public or [None])[0]
    if not target or not target.get("domain"):
        raise Exception("DuckMail 没有可用的已验证域名")
    address = f"{generate_username(10)}@{target['domain']}"
    password = secrets.token_urlsafe(12)
    create_account(ctx, address, password, api_key=key, expires_in=0)
    token = get_token(ctx, address, password)
    if not token:
        raise Exception("获取 DuckMail token 失败")
    return address, token


def get_oai_code(
    ctx,
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        ctx.raise_if_cancelled(cancel_callback)
        try:
            messages = get_messages(ctx, dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 拉取 DuckMail 邮件列表失败: {exc}")
            ctx.sleep_with_cancel(poll_interval, cancel_callback)
            continue
        eligible_messages = []
        for msg in messages:
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if not recipients or email.lower() in recipients:
                eligible_messages.append(msg)
        messages, _ = select_messages_for_code(eligible_messages)
        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if recipients and email.lower() not in recipients:
                continue
            try:
                detail = get_message_detail(ctx, dev_token, msg_id)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 获取 DuckMail 邮件详情失败: {exc}")
                continue
            parts = []
            if detail.get("text"):
                parts.append(detail["text"])
            for html in detail.get("html") or []:
                parts.append(re.sub(r"<[^>]+>", " ", html))
            subject = detail.get("subject", "")
            code = extract_verification_code("\n".join(parts), subject)
            if log_callback:
                log_callback(f"[Debug] DuckMail 收到邮件: {subject}")
            if code:
                if log_callback:
                    log_callback(f"[*] DuckMail 从邮件中提取到验证码: {code}")
                return code
        ctx.sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"DuckMail 在 {timeout}s 内未收到验证码邮件")
