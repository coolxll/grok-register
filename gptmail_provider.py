"""GPTMail browser-backed mailbox provider.

This remains separate from the HTTP providers because it must share the
registration Chromium instance while keeping its own tab.
"""

import re
import time

from DrissionPage.errors import PageDisconnectedError

from mail_service import (
    extract_flexible_verification_code,
    score_target_verification_email,
    select_target_verification_email,
)


class GPTMailProvider:
    def __init__(
        self,
        config_getter,
        browser_getter,
        raise_if_cancelled,
        sleep_with_cancel,
        cancelled_error=None,
    ):
        self.config_getter = config_getter
        self.browser_getter = browser_getter
        self.raise_if_cancelled = raise_if_cancelled
        self.sleep_with_cancel = sleep_with_cancel
        self.cancelled_error = cancelled_error
        self.tab = None
        self.address = ""
        self.used_addresses = set()

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

    @staticmethod
    def _page_state(tab):
        result = tab.run_js(
            r"""
const heading = document.querySelector('h2.gptmail-current-email');
const raw = heading ? (heading.getAttribute('title') || heading.textContent || '') : '';
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
        deadline = time.time() + timeout
        previous = str(different_from or "").strip().lower()
        last_error = None
        last_log_at = 0.0
        while time.time() < deadline:
            self.raise_if_cancelled(cancel_callback)
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
            self.sleep_with_cancel(0.5, cancel_callback)
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
  if (close) { close.click(); return false; }
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
  return [node && node.innerText, node && node.textContent,
    node && node.getAttribute && node.getAttribute('aria-label'),
    node && node.getAttribute && node.getAttribute('title')]
    .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim().slice(0, limit || 1200);
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
if (!inbox) return {ready: false, text: '', detail_text: '', candidates: []};
const ignored = /手动刷新|清空收件箱|通知|Telegram|收件箱是空的|等待邮件到达|刷新|refresh|reload/i;
const marked = Array.from(inbox.querySelectorAll(
  '[data-message-id],[data-messageid],[data-mail-id],[data-id],article,li,div,button,a,[role="button"],[role="listitem"],[role="option"]'
));
const items = Array.from(new Set(marked)).filter(visible).map((node) => ({
  text: cleanText(node, 1200),
  sender: fieldText(node, ['[data-from]','[data-sender]','[class*="sender" i]','[class*="from-address" i]']),
  subject: fieldText(node, ['[data-subject]','[data-field="subject"]','[class*="subject" i]','[class*="title" i]']),
  preview: fieldText(node, ['[data-preview]','[data-snippet]','[class*="preview" i]','[class*="snippet" i]']),
  id: node.getAttribute('data-message-id') || node.getAttribute('data-messageid')
    || node.getAttribute('data-mail-id') || node.getAttribute('data-id') || ''
})).filter((item) => item.text.length >= 8 && item.text.length <= 1600 && !ignored.test(item.text));
const semantic = /grok|xai|x\.ai|verification|confirmation|security|passcode|one[- ]time|login\s+code|otp|validate|verify|验证码|驗證碼|确认码|確認碼/i;
const candidates = [
  ...items.filter((item) => semantic.test(item.text)).sort((a,b) => b.text.length - a.text.length),
  ...items.filter((item) => !semantic.test(item.text)).sort((a,b) => b.text.length - a.text.length)
].slice(0, 40);
const detailSelectors = [
  '.gptmail-email-modal-stack','.gptmail-email-content-shell','.gptmail-email-html',
  '.gptmail-email-pre','.gptmail-home-email-column','.gptmail-home-email-detail',
  '[data-message-detail]','[class*="email-detail" i]','[class*="message-detail" i]','[class*="mail-detail" i]'
];
const dialogs = Array.from(document.querySelectorAll('[role="dialog"]')).filter(visible).map((node) => cleanText(node, 10000));
const details = Array.from(document.querySelectorAll(detailSelectors.join(','))).filter(visible).map((node) => cleanText(node, 16000));
return {
  ready: true,
  text: cleanText(inbox, 14000),
  detail_text: Array.from(new Set([...dialogs, ...details])).filter(Boolean).join('\n'),
  candidates
};
            """
        )
        return result if isinstance(result, dict) else {"ready": False, "text": "", "detail_text": "", "candidates": []}

    @staticmethod
    def _click_message(tab, target_id="", target_text=""):
        return bool(
            tab.run_js(
                r"""
function visible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  const rect = node.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
}
function textOf(node) { return String(node.innerText || node.textContent || '').replace(/\s+/g,' ').trim(); }
const inbox = document.querySelector('.gptmail-home-inbox-column');
if (!inbox) return false;
const nodes = Array.from(new Set(Array.from(inbox.querySelectorAll(
  '[data-message-id],[data-messageid],[data-mail-id],[data-id],article,li,div,button,a,[role="button"],[role="listitem"],[role="option"]'
)))).filter(visible);
const targetId = String(arguments[0] || '');
const targetText = String(arguments[1] || '');
let target = null;
if (targetId) target = nodes.find((node) => (node.getAttribute('data-message-id') || node.getAttribute('data-messageid') || node.getAttribute('data-mail-id') || node.getAttribute('data-id') || '') === targetId);
if (!target && targetText) target = nodes.find((node) => textOf(node) === targetText) || nodes.find((node) => textOf(node).includes(targetText));
if (!target) return false;
target.click();
return true;
                """,
                target_id,
                target_text,
            )
        )

    @staticmethod
    def _close_message_detail(tab):
        try:
            tab.run_js(
                r"""
function visible(node) { const r=node.getBoundingClientRect(); return r.width>0 && r.height>0; }
const dialogs = Array.from(document.querySelectorAll('[role="dialog"]')).filter(visible);
for (const dialog of dialogs) {
  const button = Array.from(dialog.querySelectorAll('button,[role="button"]')).find((node) => /^(close|关闭|返回|back)$/i.test(String(node.innerText || node.getAttribute('aria-label') || '').trim()));
  if (button) { button.click(); return true; }
}
return false;
                """
            )
        except Exception:
            pass

    def open_tab(self, log_callback=None, cancel_callback=None):
        self.raise_if_cancelled(cancel_callback)
        browser = self.browser_getter()
        if browser is None:
            raise Exception("浏览器尚未启动，无法打开 GPTMail")
        self.reset()
        tab = None
        try:
            config = self.config_getter() or {}
            url = str(config.get("gptmail_url", "") or "https://mail.chatgpt.org.uk/").strip()
            tab = browser.new_tab(url or "https://mail.chatgpt.org.uk/")
            self.tab = tab
            try:
                tab.wait.doc_loaded()
            except Exception:
                pass
            address, state = self._wait_for_address(tab, log_callback=log_callback, cancel_callback=cancel_callback)
            used = {item.lower() for item in self.used_addresses}
            if address.lower() in used:
                old_address = address
                for _ in range(3):
                    self.raise_if_cancelled(cancel_callback)
                    if not self._click_random_generate(tab):
                        self.sleep_with_cancel(0.5, cancel_callback)
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
                if address.lower() in used:
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
        self.raise_if_cancelled(cancel_callback)
        if self.tab is None:
            raise Exception("GPTMail tab 尚未打开")
        expected = str(email or self.address or "").strip().lower()
        state = self._page_state(self.tab)
        current = str(state.get("email", "") or "").strip().lower()
        if expected and current and expected != current:
            raise Exception(f"GPTMail 当前邮箱不匹配: expected={expected}, current={current}")
        try:
            if self._click_refresh(self.tab):
                self.sleep_with_cancel(0.4, cancel_callback)
        except PageDisconnectedError:
            raise
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] GPTMail 刷新收件箱失败: {exc}")
        snapshot = self._scan_inbox(self.tab)
        candidates = [item for item in snapshot.get("candidates", []) or [] if isinstance(item, dict) and item.get("text")]
        target, ranked = self._select_target_candidate(candidates)
        if log_callback and target:
            score, reasons = self._score_candidate(target)
            log_callback(f"[Debug] GPTMail 已锁定目标邮件: score={score}; reasons={','.join(reasons) or 'none'}")
        elif log_callback:
            top = ranked[0][0] if ranked else "none"
            log_callback(f"[Debug] GPTMail 未锁定 Grok/xAI 目标邮件: candidates={len(candidates)}; top_score={top}")
        if target:
            code = extract_flexible_verification_code(str(target.get("text", "") or ""), str(target.get("subject", "") or ""))
            if code:
                return code
            try:
                if self._click_message(self.tab, str(target.get("id", "") or ""), str(target.get("text", "") or "")):
                    self.sleep_with_cancel(0.6, cancel_callback)
                    detail = self._scan_inbox(self.tab)
                    code = extract_flexible_verification_code(str(detail.get("detail_text", "") or ""))
                    if code:
                        return code
                    self._close_message_detail(self.tab)
            except PageDisconnectedError:
                raise
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] GPTMail 打开邮件详情失败: {exc}")
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
        deadline = time.time() + timeout
        last_error = None
        next_resend_at = time.time() + 35
        while time.time() < deadline:
            self.raise_if_cancelled(cancel_callback)
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
                code = self.read_code_from_page(email, log_callback, cancel_callback)
                if code:
                    return code
            except PageDisconnectedError:
                raise Exception("GPTMail tab 已断开")
            except Exception as exc:
                if self.cancelled_error and isinstance(exc, self.cancelled_error):
                    raise
                last_error = exc
                if log_callback:
                    log_callback(f"[Debug] GPTMail 读取收件箱失败: {exc}")
            self.sleep_with_cancel(poll_interval, cancel_callback)
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise Exception(f"GPTMail 在 {timeout}s 内未解析出验证码{suffix}")
