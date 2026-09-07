"""邮箱 provider 共享上下文和纯函数。"""

import html
import re
import secrets
import string


class ProviderContext:
    """把主程序的配置、HTTP 和取消控制能力注入 provider。"""

    def __init__(
        self,
        config,
        http_get,
        http_post,
        raise_if_cancelled,
        sleep_with_cancel,
        cancelled_error=None,
    ):
        self.config = config
        self.http_get = http_get
        self.http_post = http_post
        self.raise_if_cancelled = raise_if_cancelled
        self.sleep_with_cancel = sleep_with_cancel
        self.cancelled_error = cancelled_error


def generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def normalize_verification_code(value):
    """统一验证码输入格式：只保留字母和数字，去掉空格及分隔符。"""
    compact = re.sub(r"[\s\u200b\ufeff\u2010-\u2015\u2212-]+", "", str(value or "")).strip()
    return compact if re.fullmatch(r"[A-Za-z0-9]{4,12}", compact) else ""


_TARGET_BRAND_PATTERN = re.compile(
    r"\b(?:grok|xai|x\.ai|x\.com|grok\.com|twitter)\b", re.IGNORECASE
)
_TARGET_STANDALONE_X_PATTERN = re.compile(r"(?:^|\s)x(?:\s+team)?(?:\s|$)", re.IGNORECASE)
_TARGET_VERIFY_PATTERN = re.compile(
    r"(?:verify(?:\s+your)?\s+(?:email|account)|email\s+verification|"
    r"verification\s+(?:code|number)|security\s+code|confirmation\s+code|"
    r"passcode|one[- ]time|login\s+code|temporary\s+code|otp|"
    r"confirm(?:ation)?\s+(?:your\s+)?email|验证码|驗證碼|确认码|確認碼|"
    r"验证邮件|驗證郵件|認證碼|認証コード|인증\s*코드|인증번호)",
    re.IGNORECASE,
)
_TARGET_CODE_PATTERN = re.compile(
    r"(?:\b(?:code|otp|passcode|token)\b|验证码|驗證碼|确认码|確認碼)",
    re.IGNORECASE,
)
_TARGET_NEGATIVE_PATTERN = re.compile(
    r"(?:newsletter|unsubscribe|marketing|广告|通知中心|telegram|"
    r"reset\s+(?:your\s+)?password|password\s+reset)",
    re.IGNORECASE,
)


def _target_email_searchable(item):
    if not isinstance(item, dict):
        return ""
    values = []
    for key in (
        "sender", "from", "from_address", "fromAddress", "subject", "preview",
        "snippet", "intro", "text", "content", "body", "raw", "verification_code",
        "code", "title",
    ):
        value = item.get(key, "")
        if isinstance(value, (dict, list)):
            value = str(value)
        if value:
            values.append(str(value))
    return " ".join(values).strip()


def score_target_verification_email(item):
    """按 Grok/xAI 品牌与验证语义给邮件候选打分。"""
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
    """选择目标验证码邮件；目标不明确时返回 None，避免猜错邮件。"""
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
    has_brand = bool(
        _TARGET_BRAND_PATTERN.search(searchable)
        or _TARGET_STANDALONE_X_PATTERN.search(searchable)
    )
    has_code_semantics = bool(
        _TARGET_VERIFY_PATTERN.search(searchable)
        or _TARGET_CODE_PATTERN.search(searchable)
    )
    if has_brand and best_score >= 20:
        return (best, ranked) if has_code_semantics else (None, ranked)
    if best_score < 12:
        return None, ranked
    if second_score is not None and best_score - second_score < 4:
        return None, ranked
    return best, ranked


def select_messages_for_code(candidates):
    """供 API provider 使用：多封邮件需锁定目标，单封邮件保留兼容兜底。"""
    values = list(candidates or [])
    target, ranked = select_target_verification_email(values)
    if target is not None:
        return [target], ranked
    if len(values) == 1:
        return values, ranked
    return [], ranked


def extract_verification_code(text, subject=""):
    # 邮件客户端经常把 HTML、Unicode 连字符和验证码拆成不同的文本节点。
    # 统一成近似 innerText 的内容，再优先按验证码上下文提取，避免先命中正文中的
    # Message ID、订单号或日期。
    def normalize_source(value):
        value = html.unescape(str(value or ""))
        value = re.sub(r"<[^>]*>", " ", value)
        value = re.sub(r"[\u200b\ufeff]", "", value)
        return re.sub(r"[\u2010-\u2015\u2212]", "-", value)

    def normalize_candidate(value):
        raw_value = str(value or "").strip()
        grouped_code = bool(re.fullmatch(r"[A-Za-z0-9]{3}-[A-Za-z0-9]{3}", raw_value))
        value = re.sub(r"[\s-]+", "", raw_value)
        if not re.fullmatch(r"[A-Za-z0-9]{4,10}", value):
            return None
        # 普通正文单词不应当被当成验证码；但 xAI 会使用 GEU-TAB 这种
        # 明确分组的纯字母验证码，因此只在 3+3 分组格式中放行纯字母。
        if not any(char.isdigit() for char in value) and not grouped_code:
            return None
        return value

    candidate_pattern = re.compile(
        r"\b(?:[A-Za-z0-9]{4,10}|[A-Za-z0-9]{3}[\s-]+[A-Za-z0-9]{3})\b"
    )

    def first_candidate(window):
        matches = list(candidate_pattern.finditer(window))
        # xAI 常见验证码是 GEU-TAB 这种明确的 3+3 分组格式；页面文本中
        # 可能同时出现 96Validate 之类的 CSS/按钮拼接噪声，优先选择分组码。
        matches.sort(
            key=lambda match: not bool(
                re.fullmatch(r"[A-Za-z0-9]{3}[\s-]+[A-Za-z0-9]{3}", match.group(0))
            )
        )
        for match in matches:
            code = normalize_candidate(match.group(0))
            if code:
                return code
        return None

    normalized_subject = normalize_source(subject)
    normalized_text = normalize_source(text)

    # 参考 GPTMail 前端的 Mr/Nr：先匹配更明确的数字验证码上下文，再匹配
    # 通用字母数字验证码上下文。这样 API provider 和浏览器 provider 的行为一致。
    numeric_patterns = [
        r"\b(?:one[- ]time password|one[- ]time code)\b(?:[\s:：#=,.;()\-]|(?:is|was|your|the|following|temporary|use|please|enter|copy|to|continue)){0,40}(?<!\d)(\d{4,10})(?!\d)",
        r"\botp\b(?:[\s:：#=,.;()\-]|(?:is|was|your|the|following|temporary|use|please|enter|copy|to|continue)){0,40}(?<!\d)(\d{4,10})(?!\d)",
        r"(?:verification code|security code|confirmation code|passcode|login code)(?:[\s:：#=,.;()\-]|(?:is|was|为|是|為|the|following|temporary|use|please|enter|copy|to|continue)){0,40}(?<!\d)(\d{4,10})(?!\d)",
    ]
    generic_candidate = r"([A-Z0-9]{4,10}|[A-Z0-9]{3}[\s-]+[A-Z0-9]{3})"
    generic_patterns = [
        rf"(?:verification code|security code|confirmation code|passcode|one[- ]time password|one[- ]time code|login code|otp|code|verification number|验证码|驗證碼|确认码|確認碼|認證碼|認証コード|인증\s*코드|인증번호)(?:[\s:：#=,.;()\-]|(?:is|was|为|是|為|your|為您|給您)){{0,16}}{generic_candidate}",
        rf"(?:use|enter|copy)(?:[\s:：#=,.;()\-]|(?:this|the|temporary|verification|code)){{0,16}}{generic_candidate}(?=[\s:：#=,.;()\-]|$)(?:[\s:：#=,.;()\-]|(?:to|for|as|verify|sign|continue)){{0,20}}(?:to verify|to sign in|to continue|as your code|continue)",
    ]
    reverse_patterns = [
        rf"\b{generic_candidate}\b(?:[\s:：#=,.;()\-]|(?:is|was|your|为|是|為)){{0,16}}(?:is your verification code|is your security code|is your passcode|is your one[- ]time password|is your code|为您的验证码|是您的验证码|是你的验证码)",
    ]

    def first_pattern_candidate(source, patterns):
        for pattern in patterns:
            for match in re.finditer(pattern, source, re.IGNORECASE):
                code = normalize_candidate(match.group(1))
                if code:
                    return code
        return None

    for source in (normalized_subject, normalized_text):
        code = first_pattern_candidate(source, numeric_patterns)
        if code:
            return code
        code = first_pattern_candidate(source, generic_patterns)
        if code:
            return code
        code = first_pattern_candidate(source, reverse_patterns)
        if code:
            return code

    context_pattern = re.compile(
        r"(?:"
        r"verification\s+code|confirm(?:ation)?\s+code|security\s+code|"
        r"one[- ]time\s+(?:code|password)|login\s+code|sign[- ]in\s+code|"
        r"passcode|otp|(?:your\s+)?code|"
        r"验证码|驗證碼|确认码|確認碼|安全码|安全碼"
        r")",
        re.IGNORECASE,
    )
    for source in (normalized_subject, normalized_text):
        for match in context_pattern.finditer(source):
            # “verification code is 482731” 中的 is/冒号等连接词不应阻断匹配。
            # GPTMail 列表文本可能把发件人、主题、摘要和正文拼在一起，
            # 验证码有时出现在上下文后较远的位置；保留有限窗口避免扫描整页。
            code = first_candidate(source[match.end() : match.end() + 300])
            if code:
                return code

    # 兼容只在主题或目标邮件正文中出现的 xAI 风格 ABC-123；上下文匹配优先，
    # 这样正文里的其他编号不会覆盖真正的验证码。
    for source in (normalized_subject, normalized_text):
        for match in re.finditer(r"\b([A-Za-z0-9]{3}-[A-Za-z0-9]{3})\b", source):
            code = normalize_candidate(match.group(1))
            if code:
                return code
    return None
