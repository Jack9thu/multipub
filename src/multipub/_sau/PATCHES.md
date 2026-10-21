# vendored social-auto-upload：来源与改动清单

- 上游：https://github.com/dreammis/social-auto-upload （MIT，许可证见本目录 `LICENSE`）
- 固定提交：`0012d2c355f88f683cc38dde2a2db209e14091bc`（2026-09-03 merge，main 分支）
- 只取抖音 / 小红书需要的文件；所有代码改动都在源码里以 `# multipub patch:` 注释标出。
- 小红书的 AI 内容声明函数移植自 omnipost（https://github.com/rehatRobot/omnipost ，
  `uploader/xiaohongshu_uploader/main.py:420-492`，提交 `23f30666e0fbbfc6373ed97394daea82acb0a0e5`，MIT，基于 SAU `99a392a`），
  其许可证与版权声明见本目录 `LICENSE-omnipost`。

## 文件对照

| multipub 路径 | 上游路径 | 上游 blob | 处理 |
|---|---|---|---|
| `uploader/base_video.py` | `uploader/base_video.py` | `3179781e` | 原样 |
| `uploader/douyin_uploader/main.py` | 同名 | `f6ac2be7` | 打补丁（见下） |
| `uploader/xiaohongshu_uploader/main.py` | 同名 | `7ca7163d` | 打补丁（见下） |
| `utils/base_social_media.py` | 同名 | `76978753` | 打补丁 |
| `utils/log.py` | 同名 | `031f5696` | 重写（接口保持：`douyin_logger` / `xiaohongshu_logger`） |
| `utils/login_qrcode.py` | 同名 | `04be9390` | 打补丁 |
| `utils/stealth.min.js` | 同名 | `166d1d74` | 原样 |
| `uploader/__init__.py`、`uploader/*/__init__.py` | 同名 | `c8cfe0b7` 等 | 清空（去掉 import 时的 mkdir） |
| `utils/__init__.py` | 同名 | `e69de29b` | 原样（空文件） |
| `LICENSE` | `LICENSE` | `beeaf19c` | 原样 |
| `LICENSE-omnipost` | omnipost 的 `LICENSE` | — | 原文 + 说明（移植函数的版权声明，MIT 要求保留） |
| `conf.py` | （上游 gitignore，`conf.example.py`） | — | multipub 新写的 shim |
| `runtime.py` | — | — | multipub 新增：钩子、截止时间、防重复点击（`PublishGuard`）、异常类型、收尾工具 |

## 改动清单

编号与任务要求一致（a–i），另有若干为 Windows / worker 环境做的小修（w*）。

### a. 抖音视频：不再默认「内容由AI生成」声明
- 文件：`uploader/douyin_uploader/main.py`，`DouYinVideo._upload_video_steps`（原 `upload()` 1051-1053 行）
- 改动：删除 `if not self.declaration: self.declaration = "内容由AI生成"`；只有显式传入 `declaration` 才设置。
  新增常量 `DOUYIN_AI_DECLARATION`，由适配器在 `ai_generated: true` 且未显式填写声明时传入。
- 原因：上游把贡献者自己的合规需求写成了默认值，会给所有作品打上 AI 声明。

### b. 抖音视频：头条/西瓜同步开关改为显式设置
- 文件：同上，`DouYinVideo.__init__` 新增参数 `sync_toutiao=None`；上游无条件打开开关的那段换成 `apply_sync_toutiao()`。
- 改动：三态。`None`（默认，清单里没写）完全不碰开关，保持页面默认；`True` 打开；`False` 把开着的开关关掉。
  只动周围文字含「头条/西瓜」的开关（上游选择器较旧，改版后可能命中别的开关）。改完核实 `semi-switch-checked`：
  要求关闭却关不掉时截图并抛 `DeclarationFailed`（拒绝发布，不能违背调用方的明确选择去同步）；要求打开却打不开只告警。
- 原因：上游强制打开第三方同步，属于隐藏副作用；之前跑过上游的账号页面上开关可能一直开着。

### c. 小红书：「来源转载」声明改为显式开启；新增 AI 内容声明（尽力而为）
- 文件：`uploader/xiaohongshu_uploader/main.py`
- 改动：`XiaoHongShuBaseUploader.__init__` 新增 `ai_generated=False`、`repost_source=None`；
  新增 `apply_declarations()`：有 `repost_source` 才调用上游 `check_original_declaration()`（并真正填入媒体名称），
  `ai_generated` 时调用移植自 omnipost 的 `set_ai_content_declaration()`（选项文字 `XHS_AI_DECLARATION`，
  没能确认选上只告警，不中断发布）。视频和图文流程里原来无条件的 `check_original_declaration()` 换成 `apply_declarations()`。
  移植时把 omnipost 的兜底定位器 `page.locator("div").filter(has_text=...)`（会命中最外层容器、点到页面中央）
  改成非精确 `get_by_text(...)`。
- 来源转载要核实：`check_original_declaration()` 改为返回 bool——找不到入口/选项/「确认」、点「确认」后媒体名称输入框没关、
  或页面上（输入区以外，`runtime.find_visible_text`）没显示来源名称或「来源转载」，都返回 False；
  `apply_declarations()` 据此截图并抛 `DeclarationFailed`（在点击发布之前，拒绝发布）。上游失败只记 warning 照常发出去，
  会把用户要求声明转载的内容当原创发布。
- AI 声明要核实：去掉 omnipost「选项附近有 取消/×/已选 就当作已选」的判断；先按 Esc 收起面板，页面上已显示该声明才跳过，
  否则点选项后核实——点过的选项已随面板收起、且页面上显示着这几个字，才算成功，否则记入 `declaration_warnings`。
- 原因：上游无条件声明「来源转载」且媒体名为空，会把原创内容标成转载。

### d. 所有等待循环加截止时间；区分「点击发布前/后」
- 文件：`runtime.py`（新增）、两个 uploader 的 `main.py`
- 改动：
  - `Hooks`：`before_publish_click`、`sms_code_required`、`on_evidence` 回调，`evidence_dir`，以及
    `page_timeout` / `upload_timeout` / `publish_timeout` / `sms_timeout`。uploader 构造函数新增 `hooks=` 参数。
  - 抖音视频：等待进入发布页、等待上传完成、发布循环；抖音图文：等待进入图文发布页、发布循环；
    小红书视频/图文：等待上传完成、发布循环（视频/图文共用 `_publish_until_success`）——全部改为 `Deadline` 控制。
  - 超时调用 `fail_step()`：截图到 `evidence_dir`，按页面特征抛 `CaptchaRequired` / `LoginPageDetected` / `StepTimeout`，
    异常带 `after_submit` 标记（是否已点过最终「发布」）。
  - 第一次点击最终「发布」按钮前调用 `_before_publish_click()` → `hooks.before_publish_click`（适配器接到
    `ctx.mark_submit_started()`）。只在按钮能真正点下去时才点击/记账/标记：`runtime.publish_button_ready()` 要求
    选择器只命中一个按钮、可见、可用（`is_enabled()`，且没有 `disabled` / `aria-disabled="true"`）；否则不点，
    继续在发布截止时间内等（抖音视频用 `force=True` 点击，点在 disabled 按钮上什么也不会发生；非 force 点击会等到按钮可用；
    命中多个按钮时 click 直接报错——这些以前都会被算成一次点击，耗尽次数后报 `unknown_outcome`）。
    点击带 5 秒超时。小红书的子串选择器 `button:has-text("发布")` 命中多个按钮时改用文字完全一致的那个，仍不唯一就不点。
  - **防重复发布**：上游循环每 ~3.5 秒点一次「发布」直到跳转，平台处理慢时可能提交两次。现在最终发布按钮只经
    `_click_publish()` 点击，受 `runtime.PublishGuard` 限制：第一次之后，只有 (1) 平台弹窗拦下了上次点击
    （短信验证已处理、抖音「请设置封面后再发布」已选推荐封面）或 (2) 距上次点击 ≥ `reclick_gap`（20 秒）且期间没观察到
    发布请求，才允许再点；有发布请求未返回、已拿到成功回执、或已点 `max_publish_clicks`（3）次时不再点。
    发布请求靠被动监听页面网络事件（`request` / `response` / `requestfailed`，不拦截不修改）：
    抖音 `POST creator.douyin.com/web/api/media/aweme/create(_v2)/`，小红书 `POST *.xiaohongshu.com/web_api/sns/v2/note`
    （与开源 xhs 客户端 ReaJason/xhs 一致）。路径未在真实账号上验证；监听不到时只剩间隔与次数两道限制。
  - 发布接口明确返回失败（抖音 `status_code != 0`、小红书 `success: false`），`reject_grace`（15 秒）内没有弹出验证：
    `fail_rejected()` 截图并抛 `PublishRejected`（适配器映射为 `platform_error`，不重试），不再空等到超时。
  - 抖音视频/图文的两个发布循环合并为基类 `DouYinBaseUploader._publish_until_success(page, what, success_url)`；
    视频特有行为（`force=True` 点击、每轮移除浮层、`handle_auto_video_cover`、宽松短信判定）通过
    `PUBLISH_CLICK_KWARGS` / `_prepare_publish_attempt` / `_platform_blocked_publish` / `STRICT_SMS_CHECK` 保持上游原样；
    选择器、成功 URL、日志文字不变。
  - 抖音视频上传失败重传最多 3 次（`MAX_UPLOAD_RETRIES`），之后抛 `UploadFailed`（上游无限重传）。
  - 循环里的 `except Exception` 前加 `except SauError: raise`，保证这些异常不被吞掉。
  - `upload()` 出错时先整页截图（`save_screenshot`）再抛出；抖音视频补上 `finally` 关闭浏览器。
  - 发布成功后的收尾（回写 cookie `storage_state`、关闭 context/browser）改用 `save_state_quietly()` / `close_quietly()`：
    出错只告警，不会把已成功的发布报成失败，也不会掩盖原异常；被 worker 超时取消时同样会关闭浏览器。
  - `validate_base_args()` 改抛 `CookieInvalid`（原 `RuntimeError`），适配器据此提示重新登录。
  - 抖音 `apply_self_declaration()` 失败改抛 `DeclarationFailed`（原 `RuntimeError`）并先截图；适配器映射为 `validation`，
    提示检查 `declaration` 文字。仍然“设置失败就拒绝发布”（上游行为）。
  - 抖音 `set_self_declaration()` 不再“点完就返回 True”：选项要变成选中（`label.semi-radio` 带 `semi-radio-checked`
    或内部 radio 已勾选，普通点击没生效时用 `_native_click` 再点一次），点「确定」后弹窗要关闭（没关再原生点一次），
    页面没有 `label.semi-radio` 结构时改为核实入口处显示了所选声明（`find_visible_text`）；否则返回 False → `DeclarationFailed`。
    抖音组件常吞掉点击且不抛异常，上游在这种情况下会不带声明发出去。
  - 抖音视频：version_2 发布页（`post/video`）要等视频传完才渲染表单，上游在 `fill_title_and_description` 里固定等 120 秒，
    大文件会在这里失败（报成 patchright 的 TimeoutError → internal）。现在进入发布页后先 `_wait_for_publish_form()`：
    在 `upload_timeout` 截止时间内等标题框出现（期间照常检查「上传失败」并重传），与后面等「重新上传」共用同一个截止时间。
  - 删除循环里 `if self.debug: page.screenshot(full_page=True)`（没有路径，什么也不保存）。
- 原因：页面改版时上游会无限卡住；也无法区分“没点发布”与“点了但不知道结果”。

### e. 浏览器启动遵循配置
- 文件：`conf.py`（`launch_kwargs()`）、两个 uploader 的所有 `chromium.launch(...)` 调用
- 改动：统一 `launch(**sau_conf.launch_kwargs(headless, args))`；`executable_path` 优先，否则用 `channel`。
  启动参数保持上游原样（抖音带 `--no-sandbox --disable-blink-features=AutomationControlled`，小红书无）。
- 原因：上游写死 `channel="chromium"`，`LOCAL_CHROME_PATH` 只在小红书 cookie 校验里生效。

### f. 日志
- 文件：`utils/log.py`（重写）
- 改动：import 时不再 `logger.remove()`、不再 reconfigure stdout/stderr、不再加 stdout sink 和 12 个文件 sink；
  `setup(log_dir)` 由 `conf.configure()` 调用：移除 loguru 默认 sink，加 stderr sink（`colorize=False`），
  在 `settings.sau_home/logs/` 下为 douyin / xiaohongshu / sau 建文件 sink（10 MB 轮转、保留 10 天、`diagnose=False`、`delay=True`）。
  只保留 `douyin_logger`、`xiaohongshu_logger`，新增 `sau_logger`（runtime.py 使用）。
- 原因：worker 的 stdout 是 JSON 事件通道；diagnose 会把变量值（可能含 cookie）写进日志。

### g. 登录二维码
- 文件：`utils/login_qrcode.py`；适配器侧 `platforms/_sau_bridge.py: qrcode_callback`
- 改动：`cv2` / `numpy` 改为可选导入（extra `terminal-qr`），缺失时 `decode_qrcode_from_path()` 返回 None，静默跳过终端二维码；
  `print_terminal_qrcode()` 全部输出到 stderr（`segno ... terminal(out=sys.stderr)`），不再 reconfigure stdout。
  新增 `terminal_qr_available()`。适配器把 `qrcode_callback` 接到：复制图片到 `settings.qrcode_dir/<平台>-<账号>.png` 并 `ctx.qrcode(path)`。
- `douyin_setup` / `xiaohongshu_setup` 新增 `login_timeout` 参数（换算成 `max_checks`），让扫码等待落在 worker 超时之内
  （适配器传入 任务超时 − 120 秒：cookie 校验、抓二维码、每轮轮询的开销都不计入 `max_checks`）。

### h. 抖音短信验证码
- 文件：`uploader/douyin_uploader/main.py`
- 改动：
  - `_read_verify_code()` 只读文件（`utf-8-sig`），删除 stdin 提示（worker 的 stdin 已关闭）。
  - 短信处理从 `DouYinVideo` 的发布循环抽到基类 `_handle_sms_verification()`，图文发布循环也调用（上游图文流程遇到短信弹窗会空转）。
    图文流程用 `strict=True`：除了输入框，还要求出现验证组件特征（`SMS_DIALOG_MARKERS`），避免误判。
  - 检测到弹窗：先删除残留的 `verify_code.txt`，点「获取验证码」，`hooks.sms_code_required(path)` 通知一次，
    按 `poll_interval` 轮询文件，超过 `sms_timeout` 截图并抛 `SmsCodeTimeout`（适配器映射：点发布前为 `NeedsAction(SMS_CODE_REQUIRED)`；
    点过发布之后为 `unknown_outcome`——短信/人机验证出现在点击之后时作品是否已创建并不确定，发布记录要能拦住重发）。
    每轮开始都会删掉 `verify_code.txt` 再发新短信，所以验证码只能在发布等待期间提交，适配器的提示也这样写。
    提交验证码后重置发布循环的截止时间，并告诉 `PublishGuard` 上次点击被拦下（允许再点一次「发布」，与上游一致）。
  - 每次发布最多处理 `MAX_SMS_ROUNDS`（3）轮短信验证；第 4 次检测到弹窗（验证码错误/过期）截图并抛 `SmsVerifyFailed`
    （映射规则同 `SmsCodeTimeout`），避免反复发短信、无限循环。
  - `_submit_sms_verify_code()` 移到基类，日志里不再打印验证码本身。
  - 文件路径来自 `conf.verify_code_file()`（= `settings.sau_home/verify_code.txt`，与 `mpub verify-code douyin` 一致）。

### i. 去掉不需要的部分
- `uploader/__init__.py`、`uploader/douyin_uploader/__init__.py`、`uploader/xiaohongshu_uploader/__init__.py`：
  删除 import 时创建 `BASE_DIR/cookies`、`cookies/douyin_uploader` 等遗留目录的代码。
- `utils/base_social_media.py`：删除未使用的平台常量与 `get_supported_social_media` / `get_cli_action`。
- 抖音 `main.py`：删除重复定义的 `_msg`；`import sys` 不再需要。
- 所有 `print()` 改到 stderr（只剩终端二维码）。

### w. 其他小修（Windows / 运行环境）
- w1 `utils/base_social_media.py`：`stealth.min.js` 从包目录读取（`STEALTH_JS_PATH`），不再依赖 `BASE_DIR/utils/`。
- w2 抖音 `douyin_cookie_gen`：登录后校验 sessionid 时用 `utf-8-sig` 显式读取 cookie 文件（原 `open()` 在中文 Windows 上按 GBK 解码）。
- w3 抖音 `cookie_auth`：异常不再静默吞掉，记录 warning（网络错误同样会被判为 cookie 失效）。
- w4 抖音：新增 `_watch_publish_response()`，点击发布后被动监听 `creator.douyin.com/web/api/media/aweme/create(_v2)/`
  的响应，记录 `item_id`（`published_item_id`）或 `status_msg`（`publish_api_error`）；点击发布后超时时两者都附在错误信息里。
  不拦截、不修改请求；接口路径未在真实账号上验证，拿不到时不影响流程。
- w5 小红书 `cookie_auth`：去掉 `LOCAL_CHROME_PATH` 分支，统一走 `launch_kwargs()`（行为等价）。
- w6 抖音 `_save_douyin_qrcode`：定位不到二维码元素时（上游只告警继续等），把登录页截图当作二维码图片经
  `qrcode_callback` 上报（payload 带 `fallback: True`），无头登录或窗口不在前台时用户也能扫。
- w7 小红书：新增 `_watch_publish_response()`（见 d），创建笔记接口成功时记录 `published_note_id`（24 位十六进制），
  适配器作为 `item_id` 返回（不拼链接：小红书分享链接需要 xsec_token）。
- w8 小红书 `apply_declarations()`：AI 声明没选上时记入 `declaration_warnings`，适配器写进结果消息，提醒人工补充。
- w9 抖音创建接口回执：成功判定增加 HTTP 2xx（与 postsail 的同类监听一致），并把请求进出告知 `PublishGuard`。

## 升级上游时

1. 在上游新提交上 diff 上表中的文件；
2. 逐条重放 a–i、w1–w5（搜索 `# multipub patch:`）；
3. 特别确认：上游是否又加了默认声明 / 强制开关、是否新增了 `while True` 循环、是否有新的 `print()` 或 stdout 输出、
   是否有新的直接点击最终「发布」按钮的地方（必须改走 `_click_publish()`）；
4. 跑 `pytest tests/test_douyin_xhs.py`，更新本文件的提交号与 blob。
