"""接入临时邮箱服务并负责邮箱创建、邮件轮询和验证码提取。"""
import html
import re
import secrets
import string
import time
from typing import Any, Dict, List, Optional, Tuple

from curl_cffi import requests
from registration_flow import VerificationCodeUnavailable

DUCKMAIL_API_BASE = "https://api.duckmail.sbs"

YYDS_API_BASE = "https://maliapi.215.im/v1"


config = {}
_cf_domain_index = 0
_cloudmail_domain_index = 0
_browser_mail_provider_factory = None


def _detail_retry_attempt(state, message_id, now=None):
    """Return a 1-based detail-fetch attempt when due, otherwise 0.

    A message is never permanently discarded merely because it has already
    been inspected several times. Detail retries back off while the enclosing
    mailbox timeout remains authoritative.
    """
    current = time.time() if now is None else float(now)
    key = str(message_id)
    record = state.setdefault(key, {"attempts": 0, "next_retry_at": 0.0})
    if current < float(record.get("next_retry_at") or 0.0):
        return 0
    attempt = int(record.get("attempts") or 0) + 1
    record["attempts"] = attempt
    delay = min(15.0, 2.0 * (2 ** max(0, attempt - 1)))
    record["next_retry_at"] = current + delay
    return attempt

_OWN_NAMES = {'cloudmail_get_email_and_token', 'get_messages', 'cloudflare_get_messages', 'get_yyds_api_key', 'yyds_generate_username', 'yyds_get_domains', 'yyds_get_email_and_token', 'yyds_get_oai_code', 'get_email_provider', 'cloudflare_get_domains', 'extract_verification_code', 'get_cloudflare_api_base', 'cloudflare_apply_auth_params', 'duckmail_get_oai_code', 'create_account', 'get_yyds_jwt', 'get_message_detail', 'yyds_create_account', 'get_duckmail_api_key', 'get_cloudflare_path', 'cloudflare_create_account', 'cloudflare_get_token', 'cloudflare_get_oai_code', 'get_cloudmail_public_token', 'generate_username', 'yyds_get_message_detail', 'cloudflare_next_default_domain', 'yyds_get_messages', 'yyds_get_token', 'get_domains', 'get_token', 'cloudflare_create_temp_address', 'get_cloudflare_api_key', 'get_cloudmail_path', 'get_cloudmail_api_base', 'cloudmail_get_oai_code', 'cloudflare_build_headers', 'cloudflare_is_admin_create_path', 'cloudmail_next_domain', 'cloudflare_get_message_detail', 'cloudmail_get_messages', 'get_user_agent', 'yyds_pick_domain', '_pick_list_payload', 'get_email_and_token', 'get_oai_code', 'get_cloudflare_auth_mode', 'pick_domain'}


def bind_runtime(namespace):
    global config
    config = namespace.get("config", config)
    for name, value in namespace.items():
        if name.startswith("__") or name in _OWN_NAMES or name in {
            "config", "_cf_domain_index", "_cloudmail_domain_index", "_browser_mail_provider_factory"
        }:
            continue
        globals()[name] = value


def normalize_mail_body(*sources):
    """Return normalized text from provider payloads with string/list HTML support."""
    parts = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("text", "raw", "content", "intro", "body", "snippet"):
            value = source.get(key)
            values = value if isinstance(value, (list, tuple)) else [value]
            for item in values:
                if isinstance(item, str) and item.strip():
                    parts.append(item)
        html_value = source.get("html")
        html_items = html_value if isinstance(html_value, (list, tuple)) else [html_value]
        for item in html_items:
            if isinstance(item, str) and item.strip():
                parts.append(re.sub(r"<[^>]+>", " ", item))
    return "\n".join(parts)


def normalize_verification_code(value):
    compact = re.sub(r"[\s\u200b\ufeff\u2010-\u2015\u2212-]+", "", str(value or "")).strip()
    return compact if re.fullmatch(r"[A-Za-z0-9]{4,12}", compact) else ""


_TARGET_BRAND_PATTERN = re.compile(r"\b(?:grok|xai|x\.ai|x\.com|grok\.com|twitter)\b", re.IGNORECASE)
_TARGET_STANDALONE_X_PATTERN = re.compile(r"(?:^|\s)x(?:\s+team)?(?:\s|$)", re.IGNORECASE)
_TARGET_VERIFY_PATTERN = re.compile(
    r"(?:verify(?:\s+your)?\s+(?:email|account)|email\s+verification|"
    r"verification\s+(?:code|number)|security\s+code|confirmation\s+code|"
    r"passcode|one[- ]time|login\s+code|temporary\s+code|otp|"
    r"confirm(?:ation)?\s+(?:your\s+)?email|验证码|驗證碼|确认码|確認碼)",
    re.IGNORECASE,
)
_TARGET_CODE_PATTERN = re.compile(r"(?:\b(?:code|otp|passcode|token)\b|验证码|驗證碼|确认码|確認碼)", re.IGNORECASE)
_TARGET_NEGATIVE_PATTERN = re.compile(
    r"(?:newsletter|unsubscribe|marketing|广告|通知中心|telegram|reset\s+(?:your\s+)?password|password\s+reset)",
    re.IGNORECASE,
)


def _target_email_searchable(item):
    if not isinstance(item, dict):
        return ""
    values = []
    for key in (
        "sender", "from", "from_address", "fromAddress", "subject", "preview", "snippet",
        "intro", "text", "content", "body", "raw", "verification_code", "code", "title",
    ):
        value = item.get(key, "")
        if isinstance(value, (dict, list)):
            value = str(value)
        if value:
            values.append(str(value))
    return " ".join(values).strip()


def score_target_verification_email(item):
    searchable = _target_email_searchable(item)
    if not searchable:
        return 0, []
    score = 0
    reasons = []
    if _TARGET_BRAND_PATTERN.search(searchable) or _TARGET_STANDALONE_X_PATTERN.search(searchable):
        score += 20
        reasons.append("brand")
    if _TARGET_VERIFY_PATTERN.search(searchable):
        score += 12
        reasons.append("verification")
    if _TARGET_CODE_PATTERN.search(searchable):
        score += 5
        reasons.append("code")
    if _TARGET_NEGATIVE_PATTERN.search(searchable):
        score -= 14
        reasons.append("negative")
    return score, reasons


def select_target_verification_email(candidates):
    ranked = []
    for item in candidates or []:
        score, reasons = score_target_verification_email(item)
        if score > 0:
            ranked.append((score, reasons, item))
    ranked.sort(key=lambda value: (value[0], -len(str(value[2].get("text", "")))), reverse=True)
    if not ranked:
        return None, []
    best_score, _, best = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else None
    searchable = _target_email_searchable(best)
    has_brand = bool(_TARGET_BRAND_PATTERN.search(searchable) or _TARGET_STANDALONE_X_PATTERN.search(searchable))
    has_code_semantics = bool(_TARGET_VERIFY_PATTERN.search(searchable) or _TARGET_CODE_PATTERN.search(searchable))
    if has_brand and best_score >= 20:
        return (best, ranked) if has_code_semantics else (None, ranked)
    if best_score < 12:
        return None, ranked
    if second_score is not None and best_score - second_score < 4:
        return None, ranked
    return best, ranked


def select_messages_for_code(candidates):
    values = list(candidates or [])
    target, ranked = select_target_verification_email(values)
    if target is not None:
        return [target], ranked
    if len(values) == 1:
        return values, ranked
    return [], ranked


def extract_flexible_verification_code(text, subject=""):
    """更宽松的 provider 解析器；不改变上游共享解析器的返回格式。"""
    direct = extract_verification_code(text, subject)
    if direct:
        return normalize_verification_code(direct) or direct

    source = html.unescape(f"{subject or ''}\n{text or ''}")
    source = re.sub(r"<[^>]*>", " ", source)
    source = re.sub(r"[\u200b\ufeff]", "", source)
    source = re.sub(r"[\u2010-\u2015\u2212]", "-", source)
    source = re.sub(r"\s+", " ", source)
    candidate = r"([A-Z0-9]{4,10}|[A-Z0-9]{3}[\s-]+[A-Z0-9]{3}|\d{3}[\s-]+\d{3})"
    patterns = [
        rf"(?:verification code|security code|confirmation code|passcode|one[- ]time (?:password|code)|login code|otp|验证码|驗證碼|确认码|確認碼)(?:\s|:|：|is|为|是){{0,18}}{candidate}",
        rf"(?:use|enter|copy)(?:\s|:|：|this|the|temporary|verification|code){{0,18}}{candidate}(?:\s|:|：|to|for|as){{0,18}}(?:continue|verify|sign in)",
        rf"{candidate}(?:\s|:|：|is|为|是){{0,18}}(?:your verification code|your security code|your passcode|您的验证码|你的验证码)",
    ]
    for pattern in patterns:
        match = re.search(pattern, source, re.IGNORECASE)
        if not match:
            continue
        raw = match.group(1)
        grouped = bool(re.fullmatch(r"[A-Za-z0-9]{3}[\s-]+[A-Za-z0-9]{3}", raw))
        code = normalize_verification_code(raw)
        if code and (any(char.isdigit() for char in code) or grouped):
            return code
    return None


def set_browser_mail_provider_factory(factory):
    global _browser_mail_provider_factory
    _browser_mail_provider_factory = factory


def _get_browser_mail_provider():
    if _browser_mail_provider_factory is None:
        raise RuntimeError("浏览器邮箱 provider 尚未初始化")
    provider = _browser_mail_provider_factory()
    if provider is None:
        raise RuntimeError("浏览器邮箱 provider 不可用")
    return provider


def _pick_list_payload(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return data.get("results")
        if isinstance(data.get("hydra:member"), list):
            return data.get("hydra:member")
        if isinstance(data.get("data"), list):
            return data.get("data")
        if isinstance(data.get("messages"), list):
            return data.get("messages")
        if isinstance(data.get("data"), dict):
            nested = data.get("data")
            if isinstance(nested.get("messages"), list):
                return nested.get("messages")
    return []

def cloudflare_apply_auth_params(params=None):
    merged = dict(params or {})
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key and mode == "query-key":
        merged["key"] = key
    return merged

def cloudflare_build_headers(content_type=False):
    headers = {"Content-Type": "application/json"} if content_type else {}
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key:
        if mode == "x-api-key":
            headers["X-API-Key"] = key
        elif mode == "x-admin-auth":
            headers["x-admin-auth"] = key
        elif mode not in ("none", "query-key"):
            headers["Authorization"] = f"Bearer {key}"
    return headers

def cloudflare_create_account(api_base, address, password, api_key=None, expires_in=0):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    payload = {"address": address, "password": password, "expiresIn": expires_in}
    path = get_cloudflare_path("cloudflare_path_accounts", "/accounts")
    params = cloudflare_apply_auth_params()
    resp = http_post(f"{api_base}{path}", json=payload, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()

def cloudflare_create_temp_address(api_base):
    """适配 cloudflare_temp_email 新建地址接口并兼容 admin 创建模式。"""
    path = get_cloudflare_path("cloudflare_path_accounts", "/api/new_address")
    url = f"{api_base}{path}"
    domain = cloudflare_next_default_domain()
    is_admin_create = cloudflare_is_admin_create_path(path)
    if is_admin_create:
        payload = {"name": generate_username(10), "enablePrefix": True}
        if domain:
            payload["domain"] = domain
    else:
        payload = {}
        if domain:
            payload["domain"] = domain
    headers = cloudflare_build_headers(content_type=True)
    resp = http_post(url, json=payload, headers=headers, params=cloudflare_apply_auth_params())
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

def cloudflare_get_domains(api_base, api_key=None):
    headers = cloudflare_build_headers(content_type=False)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_domains", "/domains")
    params = cloudflare_apply_auth_params()
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    return _pick_list_payload(resp.json())

def cloudflare_get_message_detail(api_base, token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    candidates = [
        f"{api_base}/api/mail/{message_id}",
        f"{api_base}{get_cloudflare_path('cloudflare_path_messages', '/messages')}/{message_id}",
    ]
    last_err = None
    for url in candidates:
        try:
            resp = http_get(
                url,
                headers=headers,
                params=cloudflare_apply_auth_params(),
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                return data["data"]
            return data
        except Exception as exc:
            last_err = exc
            continue
    raise Exception(f"Cloudflare 获取邮件详情失败: {last_err}")

def cloudflare_get_messages(api_base, token):
    headers = {"Authorization": f"Bearer {token}"}
    path = get_cloudflare_path("cloudflare_path_messages", "/messages")
    params = {"limit": 20, "offset": 0}
    params = cloudflare_apply_auth_params(params)
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare messages 返回非JSON: {resp.text[:300]}")
    return _pick_list_payload(data)

def cloudflare_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    api_base = get_cloudflare_api_base()
    if not api_base:
        raise Exception("Cloudflare API Base 未配置")
    deadline = time.time() + timeout
    detail_retries = {}
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
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
            messages = cloudflare_get_messages(api_base, dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Cloudflare 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] Cloudflare 本轮邮件数量: {len(messages)}")

        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id:
                continue
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

            # Always inspect the latest list payload. Some providers fill the
            # same message object in-place after the initial notification.
            subject = str(msg.get("subject", "") or "")
            combined = normalize_mail_body(msg)

            detail_attempt = _detail_retry_attempt(detail_retries, msg_id)
            if detail_attempt:
                try:
                    detail = cloudflare_get_message_detail(api_base, dev_token, msg_id)
                    detail_body = normalize_mail_body(detail)
                    detail_subject = str(detail.get("subject", "") or "")
                    if detail_body:
                        combined += "\n" + detail_body
                    if detail_subject:
                        combined += "\n" + detail_subject
                    if not subject:
                        subject = detail_subject
                except Exception as exc:
                    if log_callback:
                        log_callback(
                            f"[Debug] Cloudflare detail接口失败，保留列表内容继续等待: "
                            f"id={msg_id} attempt={detail_attempt}: {exc}"
                        )

            if log_callback:
                log_callback(f"[Debug] Cloudflare 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Cloudflare 从邮件中提取到验证码: {code}")
                return code
            if log_callback:
                attempts = int(detail_retries.get(str(msg_id), {}).get("attempts") or 0)
                log_callback(f"[Debug] 邮件已解析但未提取到验证码 id={msg_id} detail_attempts={attempts}")
        sleep_with_cancel(poll_interval, cancel_callback)
    raise VerificationCodeUnavailable(f"Cloudflare 在 {timeout}s 内未收到验证码邮件")

def cloudflare_get_token(api_base, address, password, api_key=None):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_token", "/token")
    resp = http_post(
        f"{api_base}{path}",
        json={"address": address, "password": password},
        headers=headers,
        params=cloudflare_apply_auth_params(),
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        if data.get("token"):
            return data.get("token")
        if isinstance(data.get("data"), dict) and data["data"].get("token"):
            return data["data"].get("token")
    return None

def cloudflare_is_admin_create_path(path):
    """判断当前创建邮箱路径是否为 cloudflare_temp_email 管理员创建接口。"""
    return str(path or "").rstrip("/").lower() == "/admin/new_address"

def cloudflare_next_default_domain():
    """按配置轮换选择 Cloudflare 临时邮箱域名。"""
    global _cf_domain_index
    domains = [x.strip() for x in str(config.get("defaultDomains", "") or "").split(",") if x.strip()]
    if not domains:
        return ""
    allocator = globals().get("domain_allocator")
    if allocator is not None:
        return allocator.next("cloudflare", domains)
    domain = domains[_cf_domain_index % len(domains)]
    _cf_domain_index += 1
    return domain

def cloudmail_get_email_and_token():
    """生成无需预创建账号的 Cloud Mail 收件地址。"""
    if not get_cloudmail_api_base():
        raise Exception("Cloud Mail API Base 未配置")
    if not get_cloudmail_public_token():
        raise Exception("Cloud Mail Public Token 未配置")
    domain = cloudmail_next_domain()
    if not domain:
        raise Exception("Cloud Mail 收件域名未配置")
    address = f"{generate_username(12)}@{domain}"
    # 仅返回非敏感占位凭证；公共 Token 始终只从 config.json 读取。
    return address, f"cloudmail:{address}"

def cloudmail_get_messages(address):
    api_base = get_cloudmail_api_base()
    public_token = get_cloudmail_public_token()
    if not api_base:
        raise Exception("Cloud Mail API Base 未配置")
    if not public_token:
        raise Exception("Cloud Mail Public Token 未配置")
    payload = {
        "toEmail": address,
        "type": 0,
        "isDel": 0,
        "timeSort": "desc",
        "num": 1,
        "size": 20,
    }
    resp = http_post(
        f"{api_base}{get_cloudmail_path()}",
        headers={
            "Authorization": public_token,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=20,
        replay_safe=True,
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloud Mail 邮件接口返回非JSON: {resp.text[:300]}")
    if not isinstance(data, dict):
        raise Exception(f"Cloud Mail 邮件接口返回格式错误: {data}")
    result_code = data.get("code")
    if result_code not in (None, 200, "200"):
        raise Exception(
            f"Cloud Mail 邮件接口失败: code={result_code}, message={data.get('message', '')}"
        )
    messages = data.get("data")
    if isinstance(messages, list):
        return messages
    return _pick_list_payload(data)

def cloudmail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    _ = dev_token
    deadline = time.time() + timeout
    seen_attempts = {}
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
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
            messages = cloudmail_get_messages(email)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Cloud Mail 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] Cloud Mail 本轮邮件数量: {len(messages)}")
        for msg in messages:
            msg_id = msg.get("emailId") or msg.get("email_id") or msg.get("id")
            if not msg_id:
                continue
            seen_attempts[msg_id] = int(seen_attempts.get(msg_id, 0)) + 1
            target_address = str(msg.get("toEmail") or msg.get("to_email") or "").strip().lower()
            if target_address and target_address != email.lower():
                continue
            code_value = str(msg.get("code", "") or "").strip()
            combined = normalize_mail_body(msg)
            subject = str(msg.get("subject", "") or "")
            if log_callback:
                log_callback(f"[Debug] Cloud Mail 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if not code and code_value:
                code = extract_verification_code(f"verification code: {code_value}")
            if code:
                if log_callback:
                    log_callback(f"[*] Cloud Mail 从邮件中提取到验证码: {code}")
                return code
            if log_callback:
                log_callback(
                    f"[Debug] Cloud Mail 邮件已解析但未提取到验证码 "
                    f"id={msg_id} attempt={seen_attempts[msg_id]}"
                )
        sleep_with_cancel(poll_interval, cancel_callback)
    raise VerificationCodeUnavailable(f"Cloud Mail 在 {timeout}s 内未收到验证码邮件")

def cloudmail_next_domain():
    """按配置轮换选择 Cloud Mail 无人收件域名。"""
    global _cloudmail_domain_index
    domains = [
        item.strip().lstrip("@")
        for item in str(config.get("cloudmail_domains", "") or "").split(",")
        if item.strip().lstrip("@")
    ]
    if not domains:
        return ""
    allocator = globals().get("domain_allocator")
    if allocator is not None:
        return allocator.next("cloudmail", domains)
    domain = domains[_cloudmail_domain_index % len(domains)]
    _cloudmail_domain_index += 1
    return domain

def create_account(address, password, api_key=None, expires_in=0):
    headers = {"Content-Type": "application/json"}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = {"address": address, "password": password, "expiresIn": expires_in}
    resp = http_post(f"{DUCKMAIL_API_BASE}/accounts", json=data, headers=headers)
    resp.raise_for_status()
    return resp.json()

def duckmail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    detail_retries = {}
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = get_messages(dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id:
                continue
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if email.lower() not in recipients:
                continue

            subject = str(msg.get("subject", "") or "")
            combined = normalize_mail_body(msg)
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] 从邮件列表中提取到验证码: {code}")
                return code

            detail_attempt = _detail_retry_attempt(detail_retries, msg_id)
            if not detail_attempt:
                continue
            try:
                detail = get_message_detail(dev_token, msg_id)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 获取邮件详情失败 id={msg_id} attempt={detail_attempt}: {exc}")
                continue
            combined = normalize_mail_body(detail)
            subject = str(detail.get("subject", "") or "")
            if log_callback:
                log_callback(f"[Debug] 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] 从邮件中提取到验证码: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise VerificationCodeUnavailable(f"在 {timeout}s 内未收到验证码邮件")

def extract_verification_code(text, subject=""):
    if subject:
        subject_patterns = [
            r"^([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI\b",
            r"\b(?:confirmation|verification)\s+code\s*:\s*([A-Z0-9]{3}-[A-Z0-9]{3})\b",
        ]
        for pattern in subject_patterns:
            match = re.search(pattern, subject, re.IGNORECASE)
            if match:
                return match.group(1)
    match = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", text, re.IGNORECASE)
    if match:
        return match.group(1)
    patterns = [
        r"verification\s+code[:\s]+(\d{4,8})",
        r"your\s+code[:\s]+(\d{4,8})",
        r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None

def generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))

def get_cloudflare_api_base():
    return str(config.get("cloudflare_api_base", "") or "").rstrip("/")

def get_cloudflare_api_key():
    return config.get("cloudflare_api_key", "")

def get_cloudflare_auth_mode():
    return str(config.get("cloudflare_auth_mode", "none") or "none").lower()

def get_cloudflare_path(key, default_path):
    raw = str(config.get(key, default_path) or default_path).strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw

def get_cloudmail_api_base():
    return str(config.get("cloudmail_api_base", "") or "").strip().rstrip("/")

def get_cloudmail_path():
    raw = str(
        config.get("cloudmail_path_messages", "/api/public/emailList")
        or "/api/public/emailList"
    ).strip()
    return raw if raw.startswith("/") else "/" + raw

def get_cloudmail_public_token():
    return str(config.get("cloudmail_public_token", "") or "").strip()

def get_domains(api_key=None):
    headers = {}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = http_get(f"{DUCKMAIL_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])

def get_duckmail_api_key():
    return config.get("duckmail_api_key", "")


def get_freemail_api_base():
    value = str(config.get("freemail_api_base", "") or "").rstrip("/")
    if not value:
        raise Exception("freemail_api_base 未配置")
    return value


def freemail_build_headers():
    jwt = str(config.get("freemail_jwt", "") or "").strip()
    headers = {"Content-Type": "application/json"}
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    return headers


def freemail_get_email_and_token():
    resp = http_get(f"{get_freemail_api_base()}/api/generate", headers=freemail_build_headers())
    resp.raise_for_status()
    data = resp.json()
    address = data.get("email") if isinstance(data, dict) else None
    if not address:
        raise Exception(f"freemail /api/generate 返回数据缺少 email: {data}")
    # registration_browser expects a truthy mailbox credential.
    return address, "freemail"


def freemail_get_messages(address, limit=50):
    resp = http_get(
        f"{get_freemail_api_base()}/api/emails",
        params={"mailbox": address, "limit": limit},
        headers=freemail_build_headers(),
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def freemail_get_message_detail(message_id):
    resp = http_get(
        f"{get_freemail_api_base()}/api/email/{message_id}",
        headers=freemail_build_headers(),
    )
    resp.raise_for_status()
    return resp.json()


def freemail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    del dev_token
    deadline = time.time() + timeout
    seen_ids = set()
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
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
            messages = freemail_get_messages(email)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] freemail 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        messages, _ = select_messages_for_code(messages)
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            subject = str(msg.get("subject", "") or "")
            code = normalize_verification_code(msg.get("verification_code"))
            if code:
                if log_callback:
                    log_callback(f"[*] freemail 自动提取验证码: {code}")
                return code
            try:
                detail = freemail_get_message_detail(msg_id)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] freemail 获取邮件详情失败: {exc}")
                continue
            code = normalize_verification_code(detail.get("verification_code")) if isinstance(detail, dict) else ""
            if not code and isinstance(detail, dict):
                content = "\n".join(
                    str(value or "")
                    for value in (detail.get("content"), detail.get("html_content"))
                    if value
                )
                code = extract_flexible_verification_code(content, subject)
            if log_callback:
                log_callback(f"[Debug] freemail 收到邮件: {subject}")
            if code:
                if log_callback:
                    log_callback(f"[*] freemail 解析验证码: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise VerificationCodeUnavailable(f"freemail 在 {timeout}s 内未收到验证码邮件")


def get_mailtm_api_base():
    return str(config.get("mailtm_api_base", "") or "https://api.mail.tm").rstrip("/")


def mailtm_get_email_and_token():
    base = get_mailtm_api_base()
    resp = http_get(f"{base}/domains")
    resp.raise_for_status()
    domains = resp.json().get("hydra:member", [])
    active = [item for item in domains if item.get("isActive")]
    if not active:
        raise Exception("mail.tm 没有活跃的域名")
    domain = active[0].get("domain")
    if not domain:
        raise Exception("mail.tm 域名数据格式错误")
    address = f"{generate_username(10)}@{domain}"
    password = secrets.token_urlsafe(12)
    resp = http_post(
        f"{base}/accounts",
        json={"address": address, "password": password},
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    if not resp.json().get("id"):
        raise Exception("mail.tm 创建账号失败")
    resp = http_post(
        f"{base}/token",
        json={"address": address, "password": password},
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    token = resp.json().get("token")
    if not token:
        raise Exception("mail.tm 获取 token 失败")
    return address, token


def mailtm_get_messages(token):
    resp = http_get(
        f"{get_mailtm_api_base()}/messages",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def mailtm_get_message_detail(message_id, token):
    resp = http_get(
        f"{get_mailtm_api_base()}/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    return resp.json()


def mailtm_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    del email
    deadline = time.time() + timeout
    seen_ids = set()
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
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
            messages = mailtm_get_messages(dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] mail.tm 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        messages, _ = select_messages_for_code(messages)
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            subject = str(msg.get("subject", "") or "")
            try:
                detail = mailtm_get_message_detail(msg_id, dev_token)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] mail.tm 获取邮件详情失败: {exc}")
                continue
            code = extract_flexible_verification_code(normalize_mail_body(detail), subject)
            if log_callback:
                log_callback(f"[Debug] mail.tm 收到邮件: {subject}")
            if code:
                if log_callback:
                    log_callback(f"[*] mail.tm 解析验证码: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise VerificationCodeUnavailable(f"mail.tm 在 {timeout}s 内未收到验证码邮件")

def get_email_and_token(api_key=None):
    provider = get_email_provider()
    if provider == "yyds":
        return yyds_get_email_and_token(api_key=api_key, jwt=get_yyds_jwt())
    if provider == "cloudmail":
        return cloudmail_get_email_and_token()
    if provider == "freemail":
        return freemail_get_email_and_token()
    if provider == "mailtm":
        return mailtm_get_email_and_token()
    if provider == "gptmail":
        return _get_browser_mail_provider().get_email_and_token()
    if provider == "cloudflare":
        api_base = get_cloudflare_api_base()
        if not api_base:
            raise Exception("Cloudflare API Base 未配置")
        create_path = get_cloudflare_path(
            "cloudflare_path_accounts", "/api/new_address"
        )
        try:
            # cloudflare_temp_email 专用模式
            return cloudflare_create_temp_address(api_base)
        except Exception as primary_exc:
            # 保留现有 Mail.tm 风格回退；仅在回退也失败时补充两次异常。
            fallback_stage = "获取域名列表"
            try:
                key = api_key or get_cloudflare_api_key()
                domains = cloudflare_get_domains(api_base, api_key=key)
                if not domains:
                    raise Exception("兼容接口未返回可用域名")
                fallback_stage = "选择可用域名"
                verified = [d for d in domains if d.get("isVerified")]
                target = verified[0] if verified else domains[0]
                domain = target.get("domain")
                if not domain:
                    raise Exception("Cloudflare 域名数据格式错误，缺少 domain 字段")
                username = generate_username(10)
                address = f"{username}@{domain}"
                password = secrets.token_urlsafe(12)
                fallback_stage = "创建兼容邮箱账户"
                cloudflare_create_account(
                    api_base, address, password, api_key=key, expires_in=0
                )
                fallback_stage = "获取兼容邮箱 token"
                token = cloudflare_get_token(
                    api_base, address, password, api_key=key
                )
                if not token:
                    raise Exception("获取 Cloudflare 邮箱 token 失败")
                return address, token
            except Exception as fallback_exc:
                raise RuntimeError(
                    "Cloudflare 创建邮箱失败；"
                    f"主接口 {create_path}: "
                    f"{primary_exc.__class__.__name__}: {primary_exc}；"
                    f"兼容回退（{fallback_stage}）: "
                    f"{fallback_exc.__class__.__name__}: {fallback_exc}"
                ) from fallback_exc
    key = api_key or get_duckmail_api_key()
    domain = pick_domain(api_key=key)
    username = generate_username(10)
    address = f"{username}@{domain}"
    password = secrets.token_urlsafe(12)
    create_account(address, password, api_key=key, expires_in=0)
    token = get_token(address, password)
    if not token:
        raise Exception("获取 DuckMail token 失败")
    return address, token

def get_email_provider():
    return config.get("email_provider", "duckmail")

def get_message_detail(token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    return resp.json()

def get_messages(token):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])

def get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    provider = get_email_provider()
    if provider == "yyds":
        return yyds_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            jwt=get_yyds_jwt(),
            cancel_callback=cancel_callback,
        )
    if provider == "cloudmail":
        return cloudmail_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "freemail":
        return freemail_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "mailtm":
        return mailtm_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "gptmail":
        return _get_browser_mail_provider().get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "cloudflare":
        return cloudflare_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    return duckmail_get_oai_code(
        dev_token,
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )

def get_token(address, password):
    data = {"address": address, "password": password}
    resp = http_post(f"{DUCKMAIL_API_BASE}/token", json=data)
    resp.raise_for_status()
    return resp.json().get("token")

def get_user_agent():
    return config.get(
        "user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    )

def get_yyds_api_key():
    return config.get("yyds_api_key", "")

def get_yyds_jwt():
    return config.get("yyds_jwt", "")

def pick_domain(api_key=None):
    domains = get_domains(api_key=api_key)
    if not domains:
        raise Exception("DuckMail 没有返回任何可用域名")
    private = [d for d in domains if d.get("ownerId")]
    verified_private = [d for d in private if d.get("isVerified")]
    if verified_private:
        return verified_private[0]["domain"]
    public = [d for d in domains if d.get("isVerified")]
    if public:
        return public[0]["domain"]
    raise Exception("DuckMail 无已验证域名可用")

def yyds_create_account(address=None, domain=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
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
    resp = http_post(f"{YYDS_API_BASE}/accounts", json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 创建邮箱失败: {data}")

def yyds_generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))

def yyds_get_domains(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", []) if data.get("success") else []

def yyds_get_email_and_token(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    if not token and not key:
        raise Exception("YYDS API Key 或 JWT 未配置")
    domain = yyds_pick_domain(api_key=key, jwt=token)
    username = yyds_generate_username(10)
    result = yyds_create_account(
        address=username, domain=domain, api_key=key, jwt=token
    )
    address = result.get("address") or f"{username}@{domain}"
    temp_token = result.get("token")
    if not temp_token:
        temp_token = yyds_get_token(address, api_key=key, jwt=token)
    if not temp_token:
        raise Exception("获取 YYDS token 失败")
    print(f"[*] 已创建 YYDS 邮箱: {address}")
    return address, temp_token

def yyds_get_message_detail(message_id, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 获取邮件详情失败: {data}")

def yyds_get_messages(address, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(
        f"{YYDS_API_BASE}/messages",
        params={"address": address},
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("messages", [])
    return []

def yyds_get_oai_code(
    token,
    address,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    jwt=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    detail_retries = {}
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = yyds_get_messages(address, token=token, jwt=jwt)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] YYDS 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id:
                continue
            to_addrs = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if address.lower() not in to_addrs:
                continue

            subject = str(msg.get("subject", "") or "")
            combined = normalize_mail_body(msg)
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] YYDS 从邮件列表中提取到验证码: {code}")
                return code

            detail_attempt = _detail_retry_attempt(detail_retries, msg_id)
            if not detail_attempt:
                continue
            try:
                detail = yyds_get_message_detail(msg_id, token=token, jwt=jwt)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] YYDS 获取邮件详情失败 id={msg_id} attempt={detail_attempt}: {exc}")
                continue
            combined = normalize_mail_body(detail)
            subject = str(detail.get("subject", "") or "")
            if log_callback:
                log_callback(f"[Debug] YYDS 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] YYDS 从邮件中提取到验证码: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise VerificationCodeUnavailable(f"YYDS 在 {timeout}s 内未收到验证码邮件")

def yyds_get_token(address, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_post(
        f"{YYDS_API_BASE}/token", json={"address": address}, headers=headers
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("token")
    raise Exception(f"YYDS 获取token失败: {data}")

def yyds_pick_domain(api_key=None, jwt=None):
    domains = yyds_get_domains(api_key=api_key, jwt=jwt)
    if not domains:
        raise Exception("YYDS 没有返回任何可用域名")
    private = [d for d in domains if d.get("isVerified") and not d.get("isPublic")]
    if private:
        return private[0]["domain"]
    public = [d for d in domains if d.get("isVerified") and d.get("isPublic")]
    if public:
        return public[0]["domain"]
    verified = [d for d in domains if d.get("isVerified")]
    if verified:
        return verified[0]["domain"]
    raise Exception("YYDS 无已验证域名可用")



class CloudflareMailClient:
    """Standalone Cloudflare mail client used by the debug CLI."""
    def __init__(self, api_base, auth_mode="none", api_key="", create_path="/api/new_address", timeout=20):
        self.api_base = str(api_base or "").rstrip("/")
        self.auth_mode = str(auth_mode or "none").lower()
        self.api_key = str(api_key or "")
        self.create_path = self.normalize_path(create_path, "/api/new_address")
        self.timeout = int(timeout)

    @staticmethod
    def normalize_path(path, default_path):
        raw = (path or default_path).strip() or default_path
        return raw if raw.startswith("/") else "/" + raw

    def build_auth_params(self, params=None):
        merged = dict(params or {})
        if self.api_key and self.auth_mode == "query-key":
            merged["key"] = self.api_key
        return merged

    def build_auth_headers(self, content_type=False):
        headers = {"Content-Type": "application/json"} if content_type else {}
        if not self.api_key:
            return headers
        if self.auth_mode == "x-admin-auth":
            headers["x-admin-auth"] = self.api_key
        elif self.auth_mode == "x-api-key":
            headers["X-API-Key"] = self.api_key
        elif self.auth_mode == "bearer":
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    @staticmethod
    def json_or_text(response):
        try:
            return response.json(), ""
        except Exception:
            return None, str(getattr(response, "text", "") or "")[:400]

    def create_address(self, domain="", name=""):
        is_admin = self.create_path.rstrip("/").lower() == "/admin/new_address"
        payload = {}
        headers = {"Content-Type": "application/json"}
        if is_admin:
            payload = {"name": name.strip() if str(name).strip() else generate_username(), "enablePrefix": True}
            if str(domain).strip():
                payload["domain"] = str(domain).strip()
            headers = self.build_auth_headers(content_type=True)
        elif str(domain).strip():
            payload["domain"] = str(domain).strip()
        headers = self.build_auth_headers(content_type=True)
        response = requests.post(self.api_base + self.create_path, json=payload, headers=headers, params=self.build_auth_params(), timeout=self.timeout)
        response.raise_for_status()
        data, raw = self.json_or_text(response)
        if not data:
            raise RuntimeError("%s 非JSON: %s" % (self.create_path, raw))
        address = str(data.get("address", "")).strip()
        jwt = str(data.get("jwt", "")).strip()
        if not address or not jwt:
            raise RuntimeError("%s 缺少 address/jwt: %r" % (self.create_path, data))
        return address, jwt

    def fetch_box(self, jwt, path, params):
        response = requests.get(
            self.api_base + path,
            params=params,
            headers={"Authorization": "Bearer " + str(jwt)},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            return []
        data, _ = self.json_or_text(response)
        return _pick_list_payload(data)

    def probe_all_boxes(self, jwt):
        probes = [
            ("/api/mails", {"limit": 20, "offset": 0}),
            ("/api/sendbox", {"limit": 20, "offset": 0}),
            ("/api/mails", {"limit": 20, "offset": 0, "box": "trash"}),
            ("/api/mails", {"limit": 20, "offset": 0, "folder": "trash"}),
            ("/api/mails", {"limit": 20, "offset": 0, "deleted": "1"}),
            ("/api/mails", {"limit": 20, "offset": 0, "status": "deleted"}),
        ]
        return [("%s?%s" % (path, params), self.fetch_box(jwt, path, params)) for path, params in probes]

    def get_detail(self, jwt, mail_id):
        for path in ("/api/mail/%s" % mail_id, "/api/mails/%s" % mail_id):
            try:
                response = requests.get(
                    self.api_base + path,
                    headers={"Authorization": "Bearer " + str(jwt)},
                    timeout=self.timeout,
                )
                if response.status_code >= 400:
                    continue
                data, _ = self.json_or_text(response)
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
        return {}

    @staticmethod
    def flatten_mail_text(item, detail):
        subject = str(item.get("subject") or detail.get("subject") or "")
        parts = []
        for source in (item, detail):
            for key in ("text", "raw", "content", "intro", "body", "snippet"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            html_value = source.get("html")
            if isinstance(html_value, str):
                html_value = [html_value]
            if isinstance(html_value, list):
                parts.extend(re.sub(r"<[^>]+>", " ", item) for item in html_value if isinstance(item, str))
        return subject, "\n".join(parts)
