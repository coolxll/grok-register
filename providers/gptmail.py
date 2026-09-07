"""GPTMail 浏览器 provider。"""

import re
import time

from DrissionPage.errors import PageDisconnectedError

from .common import (
    extract_verification_code,
    score_target_verification_email,
    select_target_verification_email,
)


class GPTMailProvider:
    """管理 GPTMail 独立 tab，不修改主流程的 Grok page 引用。"""

    def __init__(self, context_getter, browser_getter):
        self.context_getter = context_getter
        self.browser_getter = browser_getter
        self.tab = None
        self.address = ""
        self.used_addresses = set()

    def _ctx(self):
        return self.context_getter()

    @classmethod
    def _score_candidate(cls, item):
        return score_target_verification_email(item)

    @classmethod
    def _select_target_candidate(cls, candidates):
        return select_target_verification_email(candidates)

    def reset(self):
        if self.tab is not None:
            try:
                self.tab.close()
            except Exception:
                pass
        self.tab = None
        self.address = ""

    def _page_state(self, tab):
        result = tab.run_js(
            r"""
const heading = document.querySelector('h2.gptmail-current-email');
const raw = heading
  ? (heading.getAttribute('title') || heading.textContent || '')
  : '';
return {
  email: String(raw || '').trim(),
  url: String(location.href || ''),
  title: String(document.title || '')
};
            """
        )
        return result if isinstance(result, dict) else {"email": "", "url": "", "title": ""}

    def _wait_for_address(
        self,
        tab,
        timeout=60,
        different_from="",
        log_callback=None,
        cancel_callback=None,
    ):
        ctx = self._ctx()
        deadline = time.time() + timeout
        previous = str(different_from or "").strip().lower()
        last_error = None
        last_log_at = 0.0
        while time.time() < deadline:
            ctx.raise_if_cancelled(cancel_callback)
            try:
                state = self._page_state(tab)
                email = str(state.get("email", "") or "").strip()
                valid = re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
                if valid and (not previous or email.lower() != previous):
                    return email, state
            except PageDisconnectedError:
                raise
            except Exception as exc:
                last_error = exc
                now = time.time()
                if log_callback and now - last_log_at >= 5:
                    last_log_at = now
                    log_callback(f"[Debug] GPTMail 等待邮箱页面就绪: {exc}")
            ctx.sleep_with_cancel(0.5, cancel_callback)
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise Exception(f"GPTMail 在 {timeout}s 内未生成有效邮箱地址{suffix}")

    @staticmethod
    def _click_random_generate(tab):
        result = tab.run_js(
            r"""
function visible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  const rect = node.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden'
    && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
}
function textOf(node) {
  return [node.innerText, node.textContent, node.getAttribute('aria-label'), node.getAttribute('title')]
    .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const dialog = Array.from(document.querySelectorAll('[role="dialog"]')).find(visible);
if (dialog) {
  const close = Array.from(dialog.querySelectorAll('button,[role="button"]'))
    .find((node) => visible(node) && /^(close|关闭)$/i.test(textOf(node)));
  if (close) {
    close.click();
    return false;
  }
}
const buttons = Array.from(document.querySelectorAll('button,[role="button"]'))
  .filter((node) => visible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true');
const target = buttons.find((node) => {
  const text = textOf(node).toLowerCase();
  return text.includes('随机生成') || text.includes('random generate') || text.includes('generate random');
});
if (!target) return false;
target.click();
return true;
            """
        )
        return bool(result)

    @staticmethod
    def _click_refresh(tab):
        result = tab.run_js(
            r"""
function visible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  const rect = node.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden'
    && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
}
function textOf(node) {
  return [node.innerText, node.textContent, node.getAttribute('aria-label'), node.getAttribute('title')]
    .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const inbox = document.querySelector('.gptmail-home-inbox-column');
if (!inbox) return false;
const buttons = Array.from(inbox.querySelectorAll('button,[role="button"]'))
  .filter((node) => visible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true');
const target = buttons.find((node) => {
  const text = textOf(node).toLowerCase();
  return text.includes('手动刷新') || text === '刷新' || text.includes('refresh') || text.includes('reload');
});
if (!target) return false;
target.click();
return true;
            """
        )
        return bool(result)

    @staticmethod
    def _scan_inbox(tab):
        result = tab.run_js(
            r"""
function visible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  const rect = node.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden'
    && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
}
function cleanText(node, limit) {
  return [
    node && node.innerText,
    node && node.textContent,
    node && node.getAttribute && node.getAttribute('aria-label'),
    node && node.getAttribute && node.getAttribute('title'),
    node && node.getAttribute && node.getAttribute('alt'),
  ].filter(Boolean).join(' ')
    .replace(/\s+/g, ' ').trim().slice(0, limit || 1200);
}
function fieldText(node, selectors, limit) {
  for (const selector of selectors) {
    const field = node.querySelector(selector);
    const value = cleanText(field, limit || 500);
    if (value) return value;
  }
  return '';
}
const inbox = document.querySelector('.gptmail-home-inbox-column');
if (!inbox) return {ready: false, text: '', detail_text: '', body_text: '', candidates: []};
const ignored = /手动刷新|清空收件箱|通知|Telegram|收件箱是空的|等待邮件到达|刷新|refresh|reload/i;
const marked = Array.from(inbox.querySelectorAll(
  '[data-message-id],[data-messageid],[data-mail-id],[data-id],article,li,div,button,a,[role="button"],[role="listitem"],[role="option"]'
));
const candidateItems = Array.from(new Set(marked))
  .filter((node) => visible(node))
  .map((node) => ({
    text: cleanText(node, 1200),
    sender: fieldText(node, [
      '[data-from]', '[data-sender]', '[data-from-address]',
      '[class*="sender" i]', '[class*="from-address" i]', '[class*="email-address" i]'
    ]),
    subject: fieldText(node, [
      '[data-subject]', '[data-field="subject"]', '[class*="subject" i]', '[class*="title" i]'
    ]),
    preview: fieldText(node, [
      '[data-preview]', '[data-snippet]', '[class*="preview" i]', '[class*="snippet" i]'
    ]),
    id: node.getAttribute('data-message-id') || node.getAttribute('data-messageid')
      || node.getAttribute('data-mail-id') || node.getAttribute('data-id') || ''
  }))
  .filter((item) => item.text.length >= 8 && item.text.length <= 1600 && !ignored.test(item.text));
const semantic = /grok|xai|x\.ai|verification|confirmation|security|passcode|one[- ]time|login\s+code|otp|validate|verify|验证码|驗證碼|确认码|確認碼/i;
const candidates = [
  ...candidateItems.filter((item) => semantic.test(item.text)).sort((a, b) => b.text.length - a.text.length),
  ...candidateItems.filter((item) => !semantic.test(item.text)).sort((a, b) => b.text.length - a.text.length),
].slice(0, 40);
const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'))
  .filter(visible).map((node) => cleanText(node, 10000)).filter(Boolean);
const detailSelectors = [
  '.gptmail-email-modal-stack',
  '.gptmail-email-content-shell',
  '.gptmail-email-html',
  '.gptmail-email-pre',
  '.gptmail-home-email-column',
  '.gptmail-home-email-detail',
  '[data-message-detail]',
  '[class*="email-detail" i]',
  '[class*="message-detail" i]',
  '[class*="mail-detail" i]',
];
const detailPanels = Array.from(document.querySelectorAll(detailSelectors.join(',')))
  .filter(visible).map((node) => cleanText(node, 16000)).filter(Boolean);
const bodyText = cleanText(document.body, 20000);
return {
  ready: true,
  text: cleanText(inbox, 14000),
  detail_text: Array.from(new Set([...dialogs, ...detailPanels])).join('\n'),
  body_text: bodyText,
  candidates
};
            """
        )
        return result if isinstance(result, dict) else {"ready": False, "text": "", "detail_text": "", "body_text": "", "candidates": []}

    @staticmethod
    def _click_message(tab, target_id="", target_text=""):
        result = tab.run_js(
            r"""
function visible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  const rect = node.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden'
    && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
}
function textOf(node) {
  return [node && node.innerText, node && node.textContent, node && node.getAttribute && node.getAttribute('aria-label'), node && node.getAttribute && node.getAttribute('title')]
    .filter(Boolean).join(' ')
    .replace(/\s+/g, ' ').trim();
}
const inbox = document.querySelector('.gptmail-home-inbox-column');
if (!inbox) return false;
const ignored = /手动刷新|清空收件箱|通知|Telegram|收件箱是空的|等待邮件到达|刷新|refresh|reload/i;
const nodes = Array.from(new Set(Array.from(inbox.querySelectorAll(
  '[data-message-id],[data-messageid],[data-mail-id],[data-id],article,li,div,button,a,[role="button"],[role="listitem"],[role="option"]'
)))).filter((node) => visible(node) && !node.disabled);
const candidates = nodes.filter((node) => {
  const text = textOf(node);
  return text.length >= 8 && text.length <= 1600 && !ignored.test(text);
});
const targetId = String(arguments[0] || '');
const targetText = String(arguments[1] || '');
const byId = targetId
  ? candidates.find((node) => {
      const id = node.getAttribute('data-message-id') || node.getAttribute('data-messageid')
        || node.getAttribute('data-mail-id') || node.getAttribute('data-id') || '';
      return id === targetId;
    })
  : null;
const byText = targetText
  ? candidates.find((node) => textOf(node) === targetText)
    || candidates.find((node) => {
      const text = textOf(node);
      return text.includes(targetText) || targetText.includes(text);
    })
  : null;
const target = byId || byText;
if (!target) return false;
target.click();
return true;
            """,
            target_id,
            target_text,
        )
        return bool(result)

    @staticmethod
    def _close_message_detail(tab):
        try:
            tab.run_js(
                r"""
function visible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  const rect = node.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden'
    && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
}
function textOf(node) {
  return [node.innerText, node.textContent, node.getAttribute('aria-label'), node.getAttribute('title')]
    .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const dialogs = Array.from(document.querySelectorAll('[role="dialog"]')).filter(visible);
for (const dialog of dialogs) {
  const button = Array.from(dialog.querySelectorAll('button,[role="button"]'))
    .find((node) => visible(node) && /^(close|关闭|返回|back)$/i.test(textOf(node)));
  if (button) { button.click(); return true; }
}
return false;
                """
            )
        except Exception:
            pass

    def open_tab(self, log_callback=None, cancel_callback=None):
        ctx = self._ctx()
        ctx.raise_if_cancelled(cancel_callback)
        browser = self.browser_getter()
        if browser is None:
            raise Exception("浏览器尚未启动，无法打开 GPTMail")
        self.reset()
        tab = None
        try:
            url = str(ctx.config.get("gptmail_url", "") or "https://mail.chatgpt.org.uk/").strip()
            tab = browser.new_tab(url or "https://mail.chatgpt.org.uk/")
            self.tab = tab
            try:
                tab.wait.doc_loaded()
            except Exception:
                pass
            address, state = self._wait_for_address(
                tab,
                timeout=60,
                log_callback=log_callback,
                cancel_callback=cancel_callback,
            )
            used = {item.lower() for item in self.used_addresses}
            if address.lower() in used:
                old_address = address
                for _ in range(3):
                    ctx.raise_if_cancelled(cancel_callback)
                    if not self._click_random_generate(tab):
                        ctx.sleep_with_cancel(0.5, cancel_callback)
                        continue
                    address, state = self._wait_for_address(
                        tab,
                        timeout=45,
                        different_from=old_address,
                        log_callback=log_callback,
                        cancel_callback=cancel_callback,
                    )
                    if address.lower() not in used:
                        break
                    old_address = address
                if address.lower() in {item.lower() for item in self.used_addresses}:
                    raise Exception(f"GPTMail 反复返回已使用邮箱: {address}")
            self.used_addresses.add(address)
            self.address = address
            if log_callback:
                log_callback(f"[*] GPTMail 邮箱已就绪: {address} | URL: {state.get('url', '')}")
            return address
        except Exception:
            if tab is not None:
                try:
                    tab.close()
                except Exception:
                    pass
            self.tab = None
            self.address = ""
            raise

    def get_email_and_token(self, log_callback=None, cancel_callback=None):
        return self.open_tab(log_callback=log_callback, cancel_callback=cancel_callback), "gptmail"

    def read_code_from_page(self, email=None, log_callback=None, cancel_callback=None):
        ctx = self._ctx()
        ctx.raise_if_cancelled(cancel_callback)
        if self.tab is None:
            raise Exception("GPTMail tab 尚未打开")
        expected = str(email or self.address or "").strip().lower()
        state = self._page_state(self.tab)
        current = str(state.get("email", "") or "").strip().lower()
        if expected and current and expected != current:
            raise Exception(f"GPTMail 当前邮箱不匹配: expected={expected}, current={current}")
        try:
            if self._click_refresh(self.tab):
                ctx.sleep_with_cancel(0.4, cancel_callback)
        except PageDisconnectedError:
            raise
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] GPTMail 刷新收件箱失败: {exc}")
        snapshot = self._scan_inbox(self.tab)
        candidates = [
            item for item in (snapshot.get("candidates", []) or [])
            if isinstance(item, dict) and item.get("text")
        ]
        # 不解析整个 document.body 或整个收件箱，避免把页面上的其他数字误当验证码。
        target_candidate, ranked = self._select_target_candidate(candidates)
        candidate_pool = [target_candidate] if target_candidate else []
        if log_callback:
            if target_candidate:
                score, reasons = self._score_candidate(target_candidate)
                log_callback(
                    f"[Debug] GPTMail 已锁定目标邮件: score={score}; "
                    f"reasons={','.join(reasons) or 'none'}; "
                    f"subject={str(target_candidate.get('subject', '') or '')[:100]}"
                )
            else:
                top_detail = f"top_score={ranked[0][0]}" if ranked else "top_score=none"
                log_callback(
                    f"[Debug] GPTMail 未锁定 Grok/xAI 目标邮件: "
                    f"candidates={len(candidates)}; {top_detail}"
                )
        for item in candidate_pool:
            text = str(item.get("text", ""))
            code = extract_verification_code(str(text or ""))
            if code:
                if log_callback:
                    log_callback(f"[*] GPTMail 从目标邮件候选中解析验证码: {code}")
                return code
        if candidate_pool:
            try:
                if self._click_message(
                    self.tab,
                    target_id=str(target_candidate.get("id", "") or ""),
                    target_text=str(target_candidate.get("text", "") or ""),
                ):
                    ctx.sleep_with_cancel(0.6, cancel_callback)
                    detail_snapshot = self._scan_inbox(self.tab)
                    detail_text = str(detail_snapshot.get("detail_text", "") or "")
                    if detail_text:
                        detail_texts = [detail_text]
                    else:
                        # 某些页面把详情挂在候选卡片上，只有在已锁定目标邮件时才使用候选文本。
                        detail_texts = [str(item.get("text", "")) for item in candidate_pool]
                    for text in detail_texts:
                        code = extract_verification_code(str(text or ""))
                        if code:
                            if log_callback:
                                log_callback(f"[*] GPTMail 从目标邮件详情解析验证码: {code}")
                            return code
                    self._close_message_detail(self.tab)
            except PageDisconnectedError:
                raise
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] GPTMail 打开邮件详情失败: {exc}")
        if log_callback:
            inbox_text = str(snapshot.get("text", "") or "").replace("\n", " ")[:240]
            log_callback(
                f"[Debug] GPTMail 当前未解析出验证码: target_candidates={len(candidate_pool)}; "
                f"inbox={inbox_text or 'empty'}"
            )
        return None

    def get_oai_code(
        self,
        dev_token,
        email,
        timeout=180,
        poll_interval=3,
        log_callback=None,
        cancel_callback=None,
        resend_callback=None,
    ):
        del dev_token
        ctx = self._ctx()
        deadline = time.time() + timeout
        last_error = None
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
                code = self.read_code_from_page(
                    email=email,
                    log_callback=log_callback,
                    cancel_callback=cancel_callback,
                )
                if code:
                    return code
            except PageDisconnectedError:
                raise Exception("GPTMail tab 已断开")
            except Exception as exc:
                if ctx.cancelled_error and isinstance(exc, ctx.cancelled_error):
                    raise
                last_error = exc
                if log_callback:
                    log_callback(f"[Debug] GPTMail 读取收件箱失败: {exc}")
            ctx.sleep_with_cancel(poll_interval, cancel_callback)
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise Exception(f"GPTMail 在 {timeout}s 内未解析出验证码（邮件可能已到达）{suffix}")
