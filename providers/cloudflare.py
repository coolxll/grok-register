"""Cloudflare 临时邮箱 provider。"""

import re
import secrets
import time

from .common import extract_verification_code, generate_username, select_messages_for_code


_domain_index = 0


def get_api_base(ctx):
    return str(ctx.config.get("cloudflare_api_base", "") or "").rstrip("/")


def get_api_key(ctx):
    return str(ctx.config.get("cloudflare_api_key", "") or "")


def get_auth_mode(ctx):
    return str(ctx.config.get("cloudflare_auth_mode", "none") or "none").lower()


def get_path(ctx, key, default_path):
    raw = str(ctx.config.get(key, default_path) or default_path).strip()
    return raw if raw.startswith("/") else "/" + raw


def build_headers(ctx, content_type=False, api_key=None):
    headers = {"Content-Type": "application/json"} if content_type else {}
    key = api_key or get_api_key(ctx)
    mode = get_auth_mode(ctx)
    if key:
        if mode == "x-api-key":
            headers["X-API-Key"] = key
        elif mode == "x-admin-auth":
            headers["x-admin-auth"] = key
        elif mode != "none":
            headers["Authorization"] = f"Bearer {key}"
    return headers


def apply_auth_params(ctx, params=None, api_key=None):
    merged = dict(params or {})
    key = api_key or get_api_key(ctx)
    if key and get_auth_mode(ctx) == "query-key":
        merged["key"] = key
    return merged


def next_default_domain(ctx):
    global _domain_index
    domains = [
        x.strip()
        for x in str(ctx.config.get("defaultDomains", "") or "").split(",")
        if x.strip()
    ]
    if not domains:
        return ""
    domain = domains[_domain_index % len(domains)]
    _domain_index += 1
    return domain


def is_admin_create_path(path):
    return str(path or "").rstrip("/").lower() == "/admin/new_address"


def pick_list_payload(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "hydra:member", "data", "messages"):
            if isinstance(data.get(key), list):
                return data[key]
        nested = data.get("data")
        if isinstance(nested, dict) and isinstance(nested.get("messages"), list):
            return nested["messages"]
    return []


def create_temp_address(ctx, api_base):
    path = get_path(ctx, "cloudflare_path_accounts", "/api/new_address")
    domain = next_default_domain(ctx)
    if is_admin_create_path(path):
        payload = {"name": generate_username(10), "enablePrefix": True}
        if domain:
            payload["domain"] = domain
        headers = build_headers(ctx, content_type=True)
    else:
        payload = {"domain": domain} if domain else {}
        headers = {"Content-Type": "application/json"}
    resp = ctx.http_post(f"{api_base}{path}", json=payload, headers=headers)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare {path} 返回非JSON: {resp.text[:300]}")
    address = data.get("address")
    jwt = data.get("jwt")
    if not address or not jwt:
        raise Exception(f"Cloudflare {path} 缺少 address/jwt: {data}")
    return address, jwt


def get_domains(ctx, api_key=None):
    headers = build_headers(ctx, api_key=api_key)
    path = get_path(ctx, "cloudflare_path_domains", "/domains")
    resp = ctx.http_get(
        f"{get_api_base(ctx)}{path}",
        headers=headers,
        params=apply_auth_params(ctx, api_key=api_key),
    )
    resp.raise_for_status()
    return pick_list_payload(resp.json())


def create_account(ctx, api_base, address, password, api_key=None, expires_in=0):
    headers = build_headers(ctx, content_type=True, api_key=api_key)
    payload = {"address": address, "password": password, "expiresIn": expires_in}
    path = get_path(ctx, "cloudflare_path_accounts", "/accounts")
    resp = ctx.http_post(
        f"{api_base}{path}",
        json=payload,
        headers=headers,
        params=apply_auth_params(ctx, api_key=api_key),
    )
    resp.raise_for_status()
    return resp.json()


def get_token(ctx, api_base, address, password, api_key=None):
    path = get_path(ctx, "cloudflare_path_token", "/token")
    resp = ctx.http_post(
        f"{api_base}{path}",
        json={"address": address, "password": password},
        headers=build_headers(ctx, content_type=True, api_key=api_key),
        params=apply_auth_params(ctx, api_key=api_key),
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        if data.get("token"):
            return data["token"]
        if isinstance(data.get("data"), dict) and data["data"].get("token"):
            return data["data"]["token"]
    return None


def get_messages(ctx, api_base, token):
    path = get_path(ctx, "cloudflare_path_messages", "/messages")
    resp = ctx.http_get(
        f"{api_base}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=apply_auth_params(ctx, {"limit": 20, "offset": 0}),
    )
    resp.raise_for_status()
    try:
        return pick_list_payload(resp.json())
    except Exception:
        raise Exception(f"Cloudflare messages 返回非JSON: {resp.text[:300]}")


def get_message_detail(ctx, api_base, token, message_id):
    candidates = [
        f"{api_base}/api/mail/{message_id}",
        f"{api_base}{get_path(ctx, 'cloudflare_path_messages', '/messages')}/{message_id}",
    ]
    last_error = None
    for url in candidates:
        try:
            resp = ctx.http_get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params=apply_auth_params(ctx),
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                return data["data"]
            return data
        except Exception as exc:
            last_error = exc
    raise Exception(f"Cloudflare 获取邮件详情失败: {last_error}")


def get_email_and_token(ctx):
    api_base = get_api_base(ctx)
    if not api_base:
        raise Exception("Cloudflare API Base 未配置")
    try:
        return create_temp_address(ctx, api_base)
    except Exception as primary_exc:
        key = get_api_key(ctx)
        domains = get_domains(ctx, api_key=key)
        if not domains:
            raise Exception(f"Cloudflare 创建邮箱失败: {primary_exc}")
        verified = [d for d in domains if d.get("isVerified")]
        target = verified[0] if verified else domains[0]
        domain = target.get("domain")
        if not domain:
            raise Exception("Cloudflare 域名数据格式错误，缺少 domain 字段")
        address = f"{generate_username(10)}@{domain}"
        password = secrets.token_urlsafe(12)
        create_account(ctx, api_base, address, password, api_key=key, expires_in=0)
        token = get_token(ctx, api_base, address, password, api_key=key)
        if not token:
            raise Exception("获取 Cloudflare 邮箱 token 失败")
        return address, token


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
    api_base = get_api_base(ctx)
    if not api_base:
        raise Exception("Cloudflare API Base 未配置")
    deadline = time.time() + timeout
    seen_attempts = {}
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        ctx.raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 35
        try:
            messages = get_messages(ctx, api_base, dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Cloudflare 拉取邮件列表失败: {exc}")
            ctx.sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] Cloudflare 本轮邮件数量: {len(messages)}")
        eligible_messages = []
        for msg in messages:
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            msg_addr = str(msg.get("address", "")).lower()
            address_matched = True
            if recipients:
                address_matched = email.lower() in recipients
            elif msg_addr:
                address_matched = msg_addr == email.lower()
            if address_matched:
                eligible_messages.append(msg)
            elif log_callback:
                log_callback(f"[Debug] 跳过疑似非目标邮件 address={msg_addr} to={recipients}")
        messages, _ = select_messages_for_code(eligible_messages)
        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            msg_addr = str(msg.get("address", "")).lower()
            address_matched = True
            if recipients:
                address_matched = email.lower() in recipients
            elif msg_addr:
                address_matched = msg_addr == email.lower()
            if not address_matched:
                if log_callback:
                    log_callback(f"[Debug] 跳过疑似非目标邮件 id={msg_id} address={msg_addr} to={recipients}")
                continue
            parts = []
            for field in ("text", "raw", "content", "intro", "body", "snippet"):
                value = msg.get(field)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            html_list = msg.get("html") or []
            if isinstance(html_list, str):
                html_list = [html_list]
            for html in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", html))
            subject = str(msg.get("subject", "") or "")
            combined = "\n".join(parts)
            try:
                detail = get_message_detail(ctx, api_base, dev_token, msg_id)
                for field in ("text", "raw", "content", "intro", "body", "snippet"):
                    value = detail.get(field)
                    if isinstance(value, str) and value.strip():
                        combined += "\n" + value
                html_list = detail.get("html") or []
                if isinstance(html_list, str):
                    html_list = [html_list]
                for html in html_list:
                    combined += "\n" + re.sub(r"<[^>]+>", " ", html)
                if not subject:
                    subject = str(detail.get("subject", "") or "")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] Cloudflare detail接口失败，改用列表内容解析: {exc}")
            if log_callback:
                log_callback(f"[Debug] Cloudflare 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Cloudflare 从邮件中提取到验证码: {code}")
                return code
            if log_callback:
                log_callback(f"[Debug] 邮件已解析但未提取到验证码 id={msg_id} attempt={seen_attempts[msg_id]}")
        ctx.sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"Cloudflare 在 {timeout}s 内未收到验证码邮件")
