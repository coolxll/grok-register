# GPTMail 集成计划

## 1. 目标

为现有 Grok 注册流程增加 `gptmail` 邮箱服务商，支持：

- 通过 GPTMail 页面生成临时邮箱；
- 在注册流程中保持 GPTMail 收件箱可用；
- 轮询并提取 Grok/xAI 验证码；
- 不影响现有邮箱 provider、资料填写、Turnstile 和 SSO cookie 流程。

本文档记录了 2026-07-13 的页面探索结果、实现约束和当前落地状态。

## 2. 当前结论

双 Tab 方案在技术上可行，但“使用单一全局 `page` 并在两个 tab 之间切换”不够可靠，不能按原设计直接实现。

> 实施状态：浏览器模式 provider、独立 provider 包、配置、GUI、dispatcher 和账号输出目录已经加入项目；已补充邮箱提交成功门槛、验证码解析兜底和 GUI/CLI 人工验证码回退；真实验证码邮件的最终 DOM 仍需要端到端验证。

推荐方案是：

- 保存独立的 `grok_tab` 引用；GPTMail provider 自己持有收件箱 tab；
- 让现有全局 `page` 始终代表 Grok tab；
- GPTMail provider 直接操作自己的 tab，不修改 `page`；
- GPTMail 的重发回调仍操作 Grok tab，避免在错误页面提交注册表单。

## 3. 页面探索结果

实际打开了 [GPTMail](https://mail.chatgpt.org.uk/) 和其 [API 文档](https://mail.chatgpt.org.uk/zh/api/)。

### 已确认

- 根 URL 会跳转到类似 `/zh/<email>` 的地址，并显示当前邮箱。
- 当前邮箱有较稳定的 DOM 标识：`h2.gptmail-current-email`，同时带有 `title` 属性。
- 收件箱区域有稳定的容器类：`.gptmail-home-inbox-column`。
- 页面有“手动刷新”按钮，空收件箱显示“收件箱是空的”。
- 页面支持“随机生成”邮箱；点击后地址会变化。
- 页面存在 API 文档，接口包括：
  - `GET/POST /api/generate-email`
  - `GET /api/emails?email=...`
  - `GET /api/email/{id}`
  - `DELETE /api/email/{id}`
- API 请求需要 `X-API-Key`。当前页面显示公共测试 Key 不可用，因此不能把公共 Key 作为生产依赖。

### 已发现的风险

- 同一 Chrome 会话中新开 GPTMail 根 URL 时，可能复用之前的邮箱地址，不能假设“打开根 URL”必然创建新邮箱。
- 页面生成邮箱和异步加载存在明显延迟，不能只依赖 `doc_loaded()` 或固定的 2 秒等待。
- 本次探索没有实际收到测试邮件，因此邮件卡片、邮件详情和验证码字段的最终 DOM 选择器尚未验证。
- 页面默认使用中文路径和中文按钮文本，选择器不能只依赖中文可见文本；应优先使用自定义 class、属性和结构化 DOM。

## 4. 当前代码中的阻塞点

### 4.1 `refresh_active_page()` 不能选中最后一个 tab

这是实施前发现的阻塞点：原实现会把 `page` 指向 `browser.get_tabs()[-1]`，GPTMail 作为第二个 tab 打开后会导致 SSO cookie 检查跑到错误页面。

现在 `refresh_active_page()` 会始终恢复显式保存的 `grok_tab`，`wait_for_sso_cookie()` 因此不会误操作 GPTMail tab。

### 4.2 tab 下标不应作为长期身份

原实现使用 tab 下标获取 Grok 页面。现在使用 `grok_tab` 引用，避免依赖 tab 顺序。

### 4.3 占位 token 不应当被当作真实凭据保存

现有注册流程会把 `email` 和 `dev_token` 写入 `mail_credentials.txt`。GPTMail 返回 `(address, "gptmail")` 后，GUI/CLI 已跳过该文件写入并输出诊断日志。

## 5. 推荐实现设计

### 5.1 浏览器状态

主程序只保留：

```python
grok_tab = None
gptmail_provider = None
```

建议保持以下约束：

- `page` 始终指向 `grok_tab`；
- `providers/gptmail.py` 中的 `GPTMailProvider` 持有 GPTMail tab 和当前邮箱；
- 不通过 `browser.get_tabs()[-1]` 猜测当前页面；
- 浏览器重启、异常和取消时调用 provider 的 `reset()`；
- GPTMail tab 失效时直接报错并进入现有邮箱重试流程。

### 5.2 `gptmail_open_tab()`

建议流程：

1. 检查当前浏览器和 `grok_tab` 是否存在。
2. 创建 GPTMail tab，打开 `gptmail_url`。
3. 循环等待 `h2.gptmail-current-email` 出现，而不是只等待文档加载完成。
4. 从 DOM 文本和 `title` 属性提取邮箱，并用邮箱格式校验。
5. 如果发现地址与本次流程中已有地址相同，按策略点击“随机生成”，等待地址发生变化。
6. 由 `GPTMailProvider` 保存 tab 和当前邮箱。
7. 返回 `(address, "gptmail")`，但调用方必须知道该 token 是接口占位值。

邮箱地址应同时记录页面 URL，便于诊断页面是否被重定向或恢复了旧邮箱。

### 5.3 `gptmail_read_code_from_page()`

建议流程：

1. 验证 provider 持有的 tab 和期望邮箱地址仍然匹配。
2. 在 `.gptmail-home-inbox-column` 范围内刷新收件箱；不要使用全页面第一个按钮。
3. 读取邮件列表，先由共享筛选器综合发件人、主题、摘要和正文，按 Grok/xAI/X 品牌及验证语义锁定目标邮件；其他 provider 也复用该筛选器。
4. 优先读取列表中已提取的验证码；只有列表没有验证码时才打开邮件详情。
5. 将纯文本和 HTML 文本统一交给现有 `extract_verification_code()`。
6. 找不到验证码时返回 `None`，让外层轮询继续。
7. 所有异常路径都要保留 `page` 指向 Grok；provider 直接操作自己的 tab，不需要修改全局 `page`。

在收到真实测试邮件并确认 DOM 前，不应把具体邮件卡片选择器写死为最终实现。

### 5.4 `gptmail_get_oai_code()`

沿用现有 provider 的轮询签名：

- 支持 `timeout`、`poll_interval` 和 `cancel_callback`；
- 每次轮询前确认 GPTMail tab 未关闭；
- 超时抛出带邮箱地址和最后页面状态的异常；
- 返回验证码前确保 `page` 仍指向 Grok；
- 与现有 `resend_callback` 配合时，重发按钮操作必须发生在 Grok tab。

### 5.5 验证码失败与人工介入

- 邮箱提交后必须检测到验证码输入步骤，不能把“点击提交按钮”当成成功；页面明确提示邮箱不可用时，当前邮箱进入邮箱级重试。
- 邮箱级重试优先复用当前浏览器和 Grok tab，只重新打开注册页并生成新邮箱；只有页面/tab 已断开时才允许走浏览器重启兜底。
- GPTMail 及其他 provider 先锁定 Grok/xAI 目标邮件，再解析候选卡片、列表内容和邮件详情；不再把整个页面文本或多封邮件的第一封直接交给解析器，避免误把其他邮件、Message ID、页码等数字当成验证码。
- 所有 provider 的自动验证码获取失败后，GUI/CLI 都保留当前浏览器流程并允许人工输入验证码；人工放弃后才更换邮箱。

### 5.5 浏览器生命周期

需要同步修改：

- `start_browser()`：保存明确的 `grok_tab`；
- `stop_browser()`：关闭浏览器并调用 GPTMail provider 的 `reset()`；
- `restart_browser()`：重启后不能复用旧 tab 对象；
- 邮箱重试：关闭或丢弃旧 GPTMail tab 后再创建新邮箱；
- 注册完成后的清理：确认不会残留 GPTMail tab 或旧邮箱状态。

## 6. 配置与 dispatcher

### 已完成

- `DEFAULT_CONFIG` 已增加：

  ```json
  "gptmail_url": "https://mail.chatgpt.org.uk/"
  ```

- GUI provider 下拉框已增加 `gptmail`；
- 两个 dispatcher 已增加 GPTMail 分支；
- `config.example.json`、README 和 provider 说明已同步更新；
- `start_registration()` 已增加 GPTMail 日志和状态检查；
- provider 已拆分到 `providers/`，主程序只保留共享上下文、dispatcher 和生命周期；
- 成功账号已统一写入 `accounts/accounts_*.txt`。

### 推荐预留

由于 GPTMail 已提供 API，建议预留以下配置，后续可增加 API 模式：

```json
"gptmail_mode": "browser",
"gptmail_api_base": "https://mail.chatgpt.org.uk",
"gptmail_api_key": ""
```

建议模式：`browser`、`api`、`auto`。`auto` 只有在 API Key 明确可用时才启用 API，否则回退浏览器模式。

## 7. 实施阶段

### 阶段一：状态和生命周期

- 增加 `grok_tab` 和 GPTMail provider 状态；
- 修正 `refresh_active_page()`，始终恢复 Grok tab；
- 修改启动、停止、重启和邮箱重试清理逻辑；
- 保持现有 provider 行为不变。

### 阶段二：GPTMail 页面 provider

- 已实现邮箱打开和地址提取；
- 已实现稳定等待、超时、取消和异常恢复；
- 已实现收件箱刷新和邮件扫描；
- 真实测试邮件确认列表/详情 DOM：待完成；
- 已接入两个 dispatcher。

### 阶段三：配置、GUI 和文档

- 增加 provider 选项和配置字段；
- 更新 `config.example.json`、README；
- 明确浏览器 provider 不产生真实 token；
- 调整 `mail_credentials.txt` 的记录逻辑。

### 阶段四：provider 模块化

- `providers/common.py`：共享上下文、用户名和验证码解析；
- `providers/duckmail.py`、`cloudflare.py`、`yyds.py`、`freemail.py`、`mailtm.py`：HTTP provider；
- `providers/gptmail.py`：浏览器 provider；
- 主流程通过统一 `ProviderContext` 注入配置、HTTP 请求和取消控制。

### 阶段五：API 模式（可选）

- 增加 API Key 鉴权；
- 用 API 拉取邮件列表和详情；
- 浏览器模式作为无 Key 或 API 不可用时的 fallback；
- 对 API 响应格式和额度错误增加明确日志。

## 8. 验收标准

### 功能验收

- 单次注册能打开 Grok tab 和 GPTMail tab；
- 地址提取成功，且不会误用旧邮箱；
- Grok 表单提交邮箱后，GPTMail tab 能持续保留；
- 收到验证码后能提取并填回 Grok；
- 验证码填充完成后 `page` 仍为 Grok tab；
- 后续资料填写、Turnstile 和 SSO cookie 流程正常；
- 重试、取消和浏览器重启不会使用失效 GPTMail tab。

### 稳定性验收

- 连续完成至少 3 个账号注册；
- 人为延迟邮箱生成、邮件到达和页面刷新；
- GPTMail tab 被关闭时能进入受控重试；
- 网络错误时不会把 GPTMail 页面当作 Grok 页面继续操作；
- 不把 `"gptmail"` 占位 token 当作真实凭据持久化。

### 选择器验收

- 地址选择器基于 `h2.gptmail-current-email` 或等价稳定属性；
- 刷新按钮和邮件列表选择器经过真实邮件验证；
- 不依赖固定 tab 下标、随机 Mantine class 或单一中文文案。

## 9. 未决问题

- GPTMail 邮件列表中发件人、主题、验证码和详情的最终 DOM 结构需要真实邮件验证。
- API 公共 Key 当前不可用，需要决定是否申请独立 API Key。
- 是否将 API 模式作为默认 provider，取决于 API Key 的额度和长期可用性。
