"""freemail provider。"""

import re
import time

from .common import extract_verification_code, normalize_verification_code, select_messages_for_code


def get_api_base(ctx):
    value = str(ctx.config.get("freemail_api_base", "") or "").rstrip("/")
    if not value:
        raise Exception("freemail_api_base 未配置")
    return value


def build_headers(ctx):
    jwt = str(ctx.config.get("freemail_jwt", "") or "").strip()
    headers = {"Content-Type": "application/json"}
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    return headers


def get_domains(ctx):
    resp = ctx.http_get(f"{get_api_base(ctx)}/api/domains", headers=build_headers(ctx))
    resp.raise_for_status()
    return resp.json()


def generate(ctx, length=None, domain_index=None):
    params = {}
    if length is not None:
        params["length"] = length
    if domain_index is not None:
        params["domainIndex"] = domain_index
    resp = ctx.http_get(
        f"{get_api_base(ctx)}/api/generate",
        headers=build_headers(ctx),
        params=params,
    )
    resp.raise_for_status()
    data = resp.json()
    email = data.get("email")
    if not email:
        raise Exception(f"freemail /api/generate 返回数据缺少 email: {data}")
    return email, None


def get_emails(ctx, address, limit=50):
    resp = ctx.http_get(
        f"{get_api_base(ctx)}/api/emails",
        params={"mailbox": address, "limit": limit},
        headers=build_headers(ctx),
    )
    resp.raise_for_status()
    return resp.json()


def get_email_detail(ctx, email_id):
    resp = ctx.http_get(f"{get_api_base(ctx)}/api/email/{email_id}", headers=build_headers(ctx))
    resp.raise_for_status()
    return resp.json()


def get_email_and_token(ctx):
    return generate(ctx)


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
    del dev_token, resend_callback
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        ctx.raise_if_cancelled(cancel_callback)
        try:
            messages = get_emails(ctx, email)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] freemail 拉取邮件列表失败: {exc}")
            ctx.sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if not isinstance(messages, list):
            messages = []
        messages, _ = select_messages_for_code(messages)
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            code = msg.get("verification_code")
            subject = msg.get("subject", "")
            if code:
                normalized_code = normalize_verification_code(code)
                if not normalized_code:
                    code = None
                else:
                    code = normalized_code
            if code:
                if log_callback:
                    log_callback(f"[*] freemail 自动提取验证码: {code}")
                return code
            try:
                detail = get_email_detail(ctx, msg_id)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] freemail 获取邮件详情失败: {exc}")
                continue
            code = detail.get("verification_code")
            if code:
                code = normalize_verification_code(code)
            if code:
                if log_callback:
                    log_callback(f"[*] freemail 从详情提取验证码: {code}")
                return code
            parts = []
            if detail.get("content"):
                parts.append(detail["content"])
            if detail.get("html_content"):
                parts.append(re.sub(r"<[^>]+>", " ", detail["html_content"]))
            code = extract_verification_code("\n".join(parts), subject)
            if log_callback:
                log_callback(f"[Debug] freemail 收到邮件: {subject}")
            if code:
                if log_callback:
                    log_callback(f"[*] freemail 手动解析验证码: {code}")
                return code
        ctx.sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"freemail 在 {timeout}s 内未收到验证码邮件")
