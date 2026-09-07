"""YYDS 邮箱 provider。"""

import re
import secrets
import string
import time

from .common import extract_verification_code, select_messages_for_code


API_BASE = "https://maliapi.215.im/v1"


def get_api_key(ctx):
    return ctx.config.get("yyds_api_key", "")


def get_jwt(ctx):
    return ctx.config.get("yyds_jwt", "")


def get_domains(ctx, api_key=None, jwt=None):
    key = api_key or get_api_key(ctx)
    token = jwt or get_jwt(ctx)
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = ctx.http_get(f"{API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", []) if data.get("success") else []


def create_account(ctx, address=None, domain=None, api_key=None, jwt=None):
    key = api_key or get_api_key(ctx)
    token = jwt or get_jwt(ctx)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    payload = {}
    if address:
        payload["address"] = address
    if domain:
        payload["domain"] = domain
    elif key or token:
        payload["autoDomainStrategy"] = "prefer_owned"
    resp = ctx.http_post(f"{API_BASE}/accounts", json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 创建邮箱失败: {data}")


def get_token(ctx, address, api_key=None, jwt=None):
    key = api_key or get_api_key(ctx)
    token = jwt or get_jwt(ctx)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = ctx.http_post(f"{API_BASE}/token", json={"address": address}, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("token")
    raise Exception(f"YYDS 获取 token 失败: {data}")


def get_messages(ctx, address, token=None, api_key=None, jwt=None):
    key = api_key or get_api_key(ctx)
    temp_token = token or jwt or get_jwt(ctx)
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = ctx.http_get(
        f"{API_BASE}/messages",
        params={"address": address},
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", {}).get("messages", []) if data.get("success") else []


def get_message_detail(ctx, message_id, token=None, api_key=None, jwt=None):
    key = api_key or get_api_key(ctx)
    temp_token = token or jwt or get_jwt(ctx)
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = ctx.http_get(f"{API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 获取邮件详情失败: {data}")


def generate_username(length=10):
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(length))


def pick_domain(ctx, api_key=None, jwt=None):
    domains = get_domains(ctx, api_key=api_key, jwt=jwt)
    if not domains:
        raise Exception("YYDS 没有返回任何可用域名")
    private = [d for d in domains if d.get("isVerified") and not d.get("isPublic")]
    public = [d for d in domains if d.get("isVerified") and d.get("isPublic")]
    verified = [d for d in domains if d.get("isVerified")]
    target = (private or public or verified or [None])[0]
    if not target or not target.get("domain"):
        raise Exception("YYDS 没有可用的已验证域名")
    return target["domain"]


def get_email_and_token(ctx, api_key=None, jwt=None):
    key = api_key or get_api_key(ctx)
    token = jwt or get_jwt(ctx)
    if not token and not key:
        raise Exception("YYDS API Key 或 JWT 未配置")
    domain = pick_domain(ctx, api_key=key, jwt=token)
    username = generate_username(10)
    result = create_account(ctx, address=username, domain=domain, api_key=key, jwt=token)
    address = result.get("address") or f"{username}@{domain}"
    temp_token = result.get("token") or get_token(ctx, address, api_key=key, jwt=token)
    if not temp_token:
        raise Exception("获取 YYDS token 失败")
    return address, temp_token


def get_oai_code(
    ctx,
    token,
    address,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    jwt=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        ctx.raise_if_cancelled(cancel_callback)
        try:
            messages = get_messages(ctx, address, token=token, jwt=jwt)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] YYDS 拉取邮件列表失败: {exc}")
            ctx.sleep_with_cancel(poll_interval, cancel_callback)
            continue
        eligible_messages = []
        for msg in messages:
            to_addrs = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if not to_addrs or address.lower() in to_addrs:
                eligible_messages.append(msg)
        messages, _ = select_messages_for_code(eligible_messages)
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            to_addrs = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if to_addrs and address.lower() not in to_addrs:
                continue
            try:
                detail = get_message_detail(ctx, msg_id, token=token, jwt=jwt)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] YYDS 获取邮件详情失败: {exc}")
                continue
            parts = []
            if detail.get("text"):
                parts.append(detail["text"])
            for html in detail.get("html") or []:
                parts.append(re.sub(r"<[^>]+>", " ", html))
            subject = detail.get("subject", "")
            code = extract_verification_code("\n".join(parts), subject)
            if log_callback:
                log_callback(f"[Debug] YYDS 收到邮件: {subject}")
            if code:
                if log_callback:
                    log_callback(f"[*] YYDS 从邮件中提取到验证码: {code}")
                return code
        ctx.sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"YYDS 在 {timeout}s 内未收到验证码邮件")
