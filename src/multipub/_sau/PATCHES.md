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
| `runtime.py` | — | — | multipub 新增：钩子、截止时间、防重复点击（`PublishGuard`）、有头模式等人机验证（`CaptchaWatch`）、定时核实工具、异常类型、收尾工具 |

## 改动清单

编号与任务要求一致（a–i），之后的修复按 j、k… 续编；另有若干为 Windows / worker 环境做的小修（w*）。

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
- **任意内容类型声明（2026-10-24）**：`__init__` 再加 `declaration=None`（视频、图文都透传）；
  `set_ai_content_declaration()` 的选择和核实逻辑泛化成 `set_content_declaration(page, text)`，AI 声明改为调用它。
  `apply_declarations()`：有 `declaration` 时选这一项（不再尽力选 AI 声明），核实不了就截图并抛 `DeclarationFailed`（拒绝发布）。
  原因：网信办 2026-05 要求发布时做内容标注，原来只能选 AI 合成和来源转载，「虚构演绎」「营销广告」选不了。
  适配器侧见 `platforms/xiaohongshu.py` 的 `resolve_declaration()` 和 validate。
- **加入合集（2026-10-25）**：`__init__` 再加 `collection=None`；新增 `apply_collection()`，在视频、图文流程的 `apply_declarations()` 之后调用。
  用页面脚本 `XHS_COLLECTION_JS` 操作发布页「内容设置 → 加入合集」（结构为真实页面实测：未选时 `.collection-plugin-button`，
  选中后换成 `.collection-plugin-choose > .collection-name`；浮层 `.collection-plugin-popover .item > .item-content > .item-label`）。
  按名称精确匹配点选，再核对 `.collection-name`；任何一步失败只记入 `declaration_warnings`，照常发布。
  用页面脚本而不是 locator：patchright 的 CSS locator 在这个页面上时常定位不到这些元素。在真实发布页上实测过选中和「列表里没有」两种情况（没发布）。

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
  - `PublishGuard.request_sent()`：被平台弹窗拦下（`blocked_by_platform()`）之后又观察到发布请求（页面自己、或用户在窗口里
    重新提交了），撤销「允许再点一次」，避免补点造成重复发布。新增 `clear_rejection()`：弹出验证时之前的接口失败不算数，
    但不额外允许再点。
  - 有头模式（`--headed`）下的人机验证：两个发布循环每轮先问 `runtime.CaptchaWatch.blocking()`——页面停在人机验证
    （`diagnose()` 的同一套特征）时本轮不点「发布」、清掉之前的接口失败（否则 15 秒后就被判成平台拒绝，用户来不及拖滑块），
    第一次出现时把发布截止时间重置一次（用户最多有 `publish_timeout` 秒），之后照常超时（`CaptchaRequired`）。无头模式行为不变。
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
  - `_read_verify_code()` 只读文件，删除 stdin 提示（worker 的 stdin 不是终端）。按 BOM 解码（Windows PowerShell 5.1 的
    `echo 123456 > 文件` / Out-File 写的是带 BOM 的 UTF-16LE；无 BOM 时试 UTF-16 特征、UTF-8、再按系统编码），
    只保留数字（全角数字转半角）。读不出验证码（解码失败 / 没有数字）时 `_wait_verify_code()` 告警一次并继续等，
    不再抛 `UnicodeDecodeError` 中断验证；读到后隔 0.3 秒再读一次，内容一致才用（避免读到写了一半的文件）。
  - 旧的 `verify_code.txt` 删不掉（Windows 上被占用）时记下旧内容，内容变了才读；提交后删不掉只告警。
  - 短信处理从 `DouYinVideo` 的发布循环抽到基类 `_handle_sms_verification()`，图文发布循环也调用（上游图文流程遇到短信弹窗会空转）。
    图文流程用 `strict=True`：除了输入框，还要求出现验证组件特征（`SMS_DIALOG_MARKERS`），避免误判。
  - 检测到弹窗：先删除残留的 `verify_code.txt`，点「获取验证码」，`hooks.sms_code_required(path)` 通知一次，
    按 `poll_interval` 轮询文件，超过 `sms_timeout` 截图并抛 `SmsCodeTimeout`（适配器映射：点发布前为 `NeedsAction(SMS_CODE_REQUIRED)`；
    点过发布之后为 `unknown_outcome`——短信/人机验证出现在点击之后时作品是否已创建并不确定，发布记录要能拦住重发）。
    每轮开始都会删掉 `verify_code.txt` 再发新短信，所以验证码只能在发布等待期间提交，适配器的提示也这样写。
    处理完后重置发布循环的截止时间。一检测到弹窗就告诉 `PublishGuard` 上次点击被拦下（允许再点一次「发布」，与上游一致；
    之后若观察到页面自己重新提交就不再补点），等完验证码再清一次接口失败（「需要验证」的回执可能晚到）。
  - 在窗口里手动验证：等验证码期间短信输入框连续 `SMS_CLOSED_POLLS`（2）轮不可见，就当作用户已在 `--headed` 窗口里
    完成了验证（或页面已跳走）——`_wait_verify_code()` 返回 None，不填验证码，继续发布循环；拿到验证码时弹窗已关也不再填。
    上游（以及之前的 multipub）只认验证码文件，用户在窗口里验证后仍会空等到 `sms_timeout`，再报 `unknown_outcome`。
  - `Hooks.sms_timeout` 默认 600 秒（原 300；适配器另按任务剩余时间封顶，见 `platforms/_sau_bridge.py`）。
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
- w10 小红书登录兜底：`_open_xhs_qrcode_panel()` 改为尽力而为（依次试 `img.css-wemwzq`、「扫码登录」文字、「APP扫一扫登录」，
  返回是否切到扫码面板）；`_save_xhs_qrcode()` 定位不到二维码时（上游直接抛错，`xiaohongshu_cookie_gen` 失败并关掉窗口）
  改用 `_save_xhs_login_page()` 把整页截图经 `qrcode_callback` 上报（payload 带 `fallback: True`，同 w6），
  并照常进入登录完成轮询，用户可以在有头窗口里手动登录。

### j. 定时发布核实
- 文件：两个 uploader 的 `set_schedule_time_douyin()` / `set_schedule_time_xiaohongshu()`；`runtime.py` 新增
  `ScheduleFailed(DeclarationFailed)`、`toggle_state()`、`input_value()`、`schedule_value_matches()`
- 改动：设置完、点「发布」之前核实——「定时发布」单选 / 开关处于选中状态（`toggle_state()` 依次看 aria-checked、
  data-checked、内部原生 radio/checkbox、类名里的 checked/active/selected；判断不了时至少要出现日期时间框），
  日期时间框的值（只比较数字，精确到分钟）等于要求的时间；否则截图 `<平台>-schedule.png` 并抛 `ScheduleFailed`
  （适配器按 `DeclarationFailed` 映射为 `validation`：作品没有发出）。已经选中时不再点（小红书上游会把已打开的开关点关；
  看不出开关状态但日期框已在时同样不点）。抖音单选普通点击没生效时用 `_native_click` 再点一次；输入方式不变（Ctrl+A、输入、回车）。
  小红书 fill 之后派发 change 并 `blur()` 提交，不按回车（输入框若在 form 里会触发隐式提交）。
  日期框选择器加了兜底 `input[placeholder*="日期和时间"]`；两处 `asyncio.sleep(1)` 改为 `page.wait_for_timeout(1000)`。
- 原因：上游点完就算，点击被组件吞掉时会直接立即发布（结果却报成已定时），时间没填进去时会按页面默认时间定时。

### k. 打开页面不等 load 事件（慢网络）

- `runtime.goto_resilient(page, url)`：`wait_until="domcontentloaded"`、60 秒、超时重试一次。
- 替换了上游默认 `page.goto(url)`（等 load、30 秒）的 5 处：抖音登录首页；小红书登录页、cookie 校验页、视频/图文发布页。
- 起因：2026-10-22 实机冒烟测试中，`creator.douyin.com` 首页 30 秒内没有触发 load，登录直接失败。

### dy. 抖音：本地仿真测试发现的问题（`tests/sim/douyin`，真实 CLI + 真 Chromium + 仿真页面）

文件：`uploader/douyin_uploader/main.py`（dy9 另改 `runtime.py`）。「仿真」一栏是能复现该问题的场景（`tests/sim/test_sim_douyin.py`）。

- dy1 登录二维码：`_extract_douyin_qrcode()` 在 60 秒内轮询全部候选选择器，页面已跳到登录后页面时立即放弃
  （`DouyinLoginPageLeft`），`_save_douyin_qrcode()` 此时不再上报截图。上游先等 networkidle 15 秒、「扫码登录」60 秒、
  再逐个选择器各等 10 秒：二维码失效刷新后用户马上扫码，上游要白等一百多秒、再把登录后页面的截图当二维码上报，
  还可能超出登录任务总时限（仿真 `login_with_qr_refresh`：上游扫码后 82 秒才结束）。
- dy2 二维码 `src` 不是 data URL 时截取二维码元素（上游 `save_data_url_image` 直接抛错、登录失败）。
- dy3 登录轮询里 `count()`、点击「二维码失效」刷新都包进 try（扫码后页面跳转中 locator 会报错，上游把这当成登录失败）。
- dy4 简介编辑框点击被新手引导（shepherd）等浮层挡住时，清浮层后再点（上游只在设封面、点发布前才清，
  引导一出现就卡 30 秒后报 internal；仿真 `note_guide_and_sticky_topic_dropdown`、`slow_v1_upload_…`）。
- dy5 `_handle_entry_verification()`：进入上传页、发布表单出现后先处理短信身份验证弹窗（发 `needs_input`、等
  `mpub verify-code`）。上游只在发布循环里处理，弹窗遮罩先挡住「发布图文」/ 简介编辑框，30 秒后报一个与验证无关的超时
  （仿真 `note_identity_verification_on_entry`：上游没有发出 `needs_input`）。
- dy6 `_classify_failure()`：流程里的普通异常（多为点击超时）先看页面停在哪——人机验证 / 登录页 / 短信验证弹窗时抛
  `CaptchaRequired` / `LoginPageDetected` / `SmsCodeTimeout`（适配器给出 captcha / login_required / sms_code_required），
  而不是 internal（仿真 `captcha_before_click`）。
- dy7 `_clear_blocking_overlays()`、`_prepare_publish_attempt()` 移到基类：图文流程填完话题后同样清掉残留的话题下拉 / 引导层。
- dy8 `_click_publish()`：非 force 点击先 `trial=True` 做可点性检查，被挡住时本轮不点、不记账、不标记已提交。
- dy9 创建接口已返回成功回执（`status_code == 0` + 作品 ID）但页面一直不跳转：`Hooks.receipt_grace`（10 秒）后按成功处理
  （上游等到 `publish_timeout` 报 unknown_outcome，查重随后拦住重发；仿真 `success_receipt_without_redirect`）。
- dy10 回执读取失败时记日志；创建接口返回非 2xx 时记下 HTTP 状态（`publish_http_note`），写进最后的超时报错
  （仿真 `gateway_error_after_click`）。
- dy11 `_wait_on_upload_page()`：等上传入口 / 「发布图文」时，页面被踢回登录页立即抛 `LoginPageDetected`
  （cookie 校验刚通过、打开上传页却掉线；上游干等 60 秒后报 Playwright 超时，适配器只能给 internal；
  仿真 `session_expires_after_cookie_check`）。
- dy12 `_verify_title()`：填完标题读回，和要填的不一致时重填一次，仍不一致抛 `DeclarationFailed`（validation，没有点发布）。
  网页 maxlength 按 UTF-16 计数，30 个字符里有 emoji 的标题会被静默截掉末尾，上游照样发出（仿真 `emoji_title_truncated_by_page`）。
  `_check_description()`：读回简介，正文行或话题没有完整出现时只告警并记入 `notes`（编辑器会把话题、表情转换成别的节点）。
- dy13 `_publish_button_covered()`：点「发布」前在按钮中心做命中测试，中心点上是别的弹窗 / 遮罩层（类名含 modal/mask/overlay、
  role=dialog、`#captcha_container`，且不包含按钮本身）时本轮不点，先尝试关掉通知弹窗或按 Esc。视频流程的 force 点击会落在
  遮罩上、什么也没发出，上游却记作已点击，最后报 unknown_outcome；现在一直被挡就按「发布前超时」返回 timeout
  （仿真 `publish_button_masked`、`cover_modal_stuck_…`）。
- dy14 封面弹窗关不掉（封面一直「检测中」、「完成」一直禁用）：等「完成」解禁从 15 秒放宽到 30 秒；一直禁用时不再白点 4 轮
  （约 35 秒），直接 `_dismiss_cover_modal()` 点「取消」放弃自定义封面（与弹窗打不开时一样交给推荐封面），记入 `notes`。
- dy15 `_dismiss_notice_modals()`：关掉 `.semi-modal` 里只有「我知道了 / 知道了 / 我已知晓」的通知弹窗（平台公告等，
  遮罩挡住表单和「发布」；不点「同意」之类有含义的按钮）。在 `_clear_blocking_overlays()` 和 dy13 里调用
  （仿真 `notice_modal_dismissed`：上游 30 秒后报 internal）。
- dy16 `notes`：与清单不一致、需要用户知道的地方（自定义封面没设上、合集没找到 / 可能没选上、背景音乐没加上、简介可能不完整），
  适配器附在成功结果的消息后。合集选完后核对页面上显示了合集名（上游点完就当成功）。
- dy17 登录：扫码后卡在短信二次验证直到超时，结果带 `saw_2fa: True`、消息改为「扫码后抖音要求短信二次验证，但一直没有完成」
  （上游和扫码超时一样报「等待扫码超时」；适配器目前仍按 status=timeout 生成提示，见 CORE 建议）。

### xs. 小红书：本地仿真测试发现的问题（`tests/sim/xhs`，真实 CLI + 真 Chromium + 仿真页面）

文件：`uploader/xiaohongshu_uploader/main.py`（xs19 另改适配器侧 `platforms/_sau_bridge.py`）。「仿真」一栏是能复现该问题的场景
（`tests/sim/test_sim_xhs.py`）。

- xs1 成功页判定改为正则 `/publish/success(?:[/?#]|$)`：上游的 glob `**/publish/success?**` 要求 success 后面带 `?…`，
  成功页不带查询串时一直等到超时、报 unknown_outcome（仿真 `video_success_bare_url`）。
- xs2 视频上传失败：预览区先认「上传失败 / 上传出错 / 上传异常」（失败页上也有「重新上传」，上游把它当上传成功关键字，
  接着去点「发布」只弹提示，最后报结果未知）；`_retry_video_upload()` 清空再设上传框重传，最多 2 次，仍失败截图并抛
  `UploadFailed`（platform_error，没有点发布、可重试）（仿真 `video_upload_retry`、`video_upload_keeps_failing`）。
- xs3 `_raise_if_login_page()`：上传等待、发布循环里页面被踢回 `/login` 立即抛 `LoginPageDetected`
  （上游等到上传 / 发布截止时间，视频默认约 50 分钟）（仿真 `session_drop_video`）。
- xs4 `_wait_upload_input()`：等上传框渲染出来（最多 `page_timeout`，期间跳登录页立即报错），不再直接 `set_input_files`
  （30 秒后报 internal）；图文页两种上传框选择器一起等（上游先 `count()` 图片上传框，SPA 没渲染完时为 0 会错选兜底选择器）。
- xs5 `_wait_uploads_settled()`：图文的标题框在图片传完之前就出来了；点「发布」前等页面上不再显示「上传中 / 正在上传」，
  有图片显示「上传失败」时截图并抛 `UploadFailed`（没有点发布）。上游上传中点「发布」只会弹提示，PublishGuard 补点几次后报
  unknown_outcome（查重随后拦住重发）；有图传失败时页面要么一直不让发（同样报 unknown_outcome），要么少一张图照样发出去
  （仿真 `note_slow_images_toast`、`note_image_failed_block`、`note_image_failed_drop`）。
- xs6 创建笔记接口已返回成功回执但页面一直不跳成功页：`Hooks.receipt_grace`（与 dy9 共用）后按成功处理，不再点击
  （仿真 `video_receipt_no_redirect`）。
- xs7 `_desc_editor()`：发布页恢复了上次的草稿时正文没有 `data-placeholder`，上游在 click 上等 30 秒后报 internal；
  退回到可编辑区本身（随后 Ctrl+A 清空重写）（仿真 `video_draft`）。
- xs8 `_matching_topic_item()`：话题候选里挑名字与标签相同（忽略 `#`、`[话题]`、大小写）的一项，没有时退而求其次取名字包含标签的，
  约 3 秒内都没有就跳过该标签。上游无条件点第一个候选：下拉框先显示热门话题、联想结果稍后才刷新时会挂上不相干的话题
  （仿真 `video_topics_instant`）。
- xs9 `_js_click_by_text()`：原生 click 本来就会冒泡，上游再连点元素和 3 层祖先，同一个选项的处理函数被触发好几次
  （「来源转载」弹出好几层媒体名称框、开关被点回去）；现在每点一层等 100 毫秒，元素已被移除或隐藏就停（仿真 `video_repost`）。
- xs10「来源转载」选项：patchright 里「锚定正则 filter + `.last`」解析不到元素（上游实际总走 JS 兜底），改为数出个数后 `nth(n-1)`。
- xs11 扫码面板：以「二维码图片可见」判断已在扫码面板（上游看 `div:has-text('扫一扫')` 的数量，扫码面板只是隐藏在 DOM 里时也有，
  于是不点切换、上报一张没有二维码的截图）；扫码面板标题已可见、图片还没加载时等图片而不去点切换图标（它在两个面板间来回切）；
  各个切换入口（`img.css-wemwzq`、「扫码登录」、「APP扫一扫登录」）在 `XHS_QR_SWITCH_WAIT`（10 秒）内轮流看、哪个先出现点哪个，
  30% 时间后再试登录框右上角的图标（`_XHS_CORNER_SWITCH_JS`，哈希类名改版后切换图标通常还在那里）。上游只等哈希类名的图标
  10 秒（仿真 `qr_switch`、`qr_switch_renamed`、`qr_unreachable`）。
- xs12 `_refresh_xhs_qrcode()`：登录轮询里二维码显示「已过期 / 已失效 / 点击刷新」时点一下刷新；二维码图片变了就重新保存并上报
  （上游只上报一次，无头登录时用户拿到的是过期的二维码）（仿真 `qr_expires`）。
- xs13 `_click_publish()`：先 `trial=True` 做可点性检查（同 dy8）。按钮被弹窗 / 遮罩挡住时 click 会超时、根本没点下去，
  上游这也记一次点击并标记已提交，最后报 unknown_outcome；现在本轮不点、按一次 Escape，一直被挡就按「发布前超时」报 timeout。
- xs14 `set_thumbnail()`：等封面弹窗（`div.d-modal`）出现；「上传封面」页签 3 秒内没出现就直接在弹窗里找图片上传框
  （postsail 的页面版本没有页签，上游在这里等 10 秒后失败）；点「确定」后弹窗一直不关（图片不合要求等）也算失败（上游照样报
  「封面已经设置完成」）。失败时截图、`_close_modal()` 依次试 Escape、弹窗里的「取消」、关闭图标把弹窗关掉，并在结果消息里注明
  自定义封面没设上（记入 `declaration_warnings`）。上游失败后只按一次 Escape：弹窗不响应 Escape 时一直挡着「发布」，
  点击被遮罩拦下却记作已点击，最后报 unknown_outcome、查重拦住重发（仿真 `video_cover_direct`、`cover_no_upload_entry`、`cover_modal_stuck`）。
- xs15 `fill_title()`：填完读回。标题框按 UTF-16 计数（emoji 算 2 个）时 20 个字里有 emoji 的标题末尾会被静默截掉，上游照样发出；
  现在重填一次仍被截断（读回值是要填的前缀）就截图并抛 `DeclarationFailed`（validation，没有点发布）；其他不一致只告警、写进结果消息；
  读不回来不核对。适配器 `validate()` 对这种标题先给 warning。
- xs16 `fill_desc()`：逐行输入，含 `#` 或 `@` 的行在回车前先按 Escape。编辑器的话题 / @ 候选框开着时回车会选中第一个候选：
  正文里自己写的 `#徒步` 会被换成先弹出来的热门话题、换行也被吞掉（仿真 `video_topics_instant`）。
- xs17 `_classify_failure()`（同 dy6）：`upload()` 里的普通异常（多为填表时 30 秒的 Playwright 超时）先看页面停在哪——登录页 /
  人机验证时抛 `LoginPageDetected` / `CaptchaRequired`（适配器给 login_required / captcha，点过发布则为 unknown_outcome），
  而不是 internal（仿真 `session_drop_note`、`captcha_on_load`）。
- xs18 创建笔记接口返回 HTTP 4xx/5xx 时记下 `publish_http_note`，写进最后的超时报错（仿真 `gateway_500`）。
- xs19 `_sau_bridge.QrRelay`：无头登录上报的是整页截图（没定位到二维码）时，提示里加「截图里没有二维码时去掉 --headless 重新登录」
  （截图可能停在手机号登录面板，无头模式下没有窗口可用）。
- 两处新加的轮询（xs8、xs11）都用 `Deadline` 计时（`test_vendored_source_invariants` 要求每个 `while True` 都有截止时间）。

## 升级上游时

1. 在上游新提交上 diff 上表中的文件；
2. 逐条重放 a–k、w1–w10、dy1–dy17、xs1–xs19（搜索 `# multipub patch:`）；
3. 特别确认：上游是否又加了默认声明 / 强制开关、是否新增了 `while True` 循环、是否有新的 `print()` 或 stdout 输出、
   是否有新的直接点击最终「发布」按钮的地方（必须改走 `_click_publish()`）；
4. 跑 `pytest tests/test_douyin_xhs.py`，更新本文件的提交号与 blob。
