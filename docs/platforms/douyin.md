# 抖音（douyin）

实现：`multipub/platforms/douyin.py` + vendored 的 social-auto-upload（`multipub/_sau`，上游 `0012d2c`，
改动见 [`src/multipub/_sau/PATCHES.md`](../../src/multipub/_sau/PATCHES.md)）。全程浏览器自动化（patchright），
页面是抖音创作者中心 `creator.douyin.com`。

## 支持的内容类型

| kind | 支持 | 说明 |
|---|---|---|
| `video` | ✔ | 标题、简介、话题、封面（竖/横）、定时、自主声明、合集、商品链接、同步头条/西瓜（可选） |
| `note`（图文） | ✔ | 1–35 张图、标题、正文、话题、定时、背景音乐 |
| `article` | ✘ | 自动跳过 |

## 清单字段

通用字段（`title` / `body` / `tags` / `video` / `cover` / `images` / `schedule` / `ai_generated` / `account`）
可写在顶层，也可在 `platforms.douyin` 下覆盖。下表是 `platforms.douyin` 下的平台私有参数：

| 参数 | 适用 | 默认 | 说明 |
|---|---|---|---|
| `declaration` | video | 不声明；`ai_generated: true` 时为 `内容由AI生成` | 「自主声明」选项文字，必须与页面上的单选项完全一致（如 `内容由AI生成`、`内容为个人观点或见解`）。填 `false` / `none` 表示不声明。选完会核实（选项变成选中、点「确定」后弹窗关闭），核实不了就**拒绝发布**（`validation`，作品没有发出）。核实依据的页面结构未在真实账号上验证，发布后仍建议在「作品管理」里看一眼 |
| `collection` | video | 无 | 加入已有合集（名称精确匹配）；找不到时跳过并告警 |
| `product_link` / `product_title` | video | 无 | 购物车商品链接与短标题（≤10 字），两项都填才生效 |
| `sync_toutiao` | video | 不写（不碰开关） | 「同步到今日头条/西瓜视频」开关。不写：保持页面默认；`true`：打开（打不开只告警）；`false`：开关开着就关掉，**关不掉时拒绝发布**（`validation`）。只动旁边写着「头条/西瓜」的开关；找不到这样的开关时只告警 |
| `thumbnail_portrait` | video | 无 | 竖封面（3:4）。路径相对 post.yaml 所在目录 |
| `thumbnail_landscape` | video | 无 | 横封面（4:3）。同时填竖、横时只会应用竖封面 |
| `bgm` | note | 无 | 背景音乐名称，搜索后选第一条；搜不到跳过 |

`cover` 的用法：没有填 `thumbnail_*` 时，按图片宽高自动放进横封面（宽>高）或竖封面（其余）。
封面设置失败不会中断发布，平台会用推荐封面：封面弹窗打不开，或者上传的封面一直「检测中」、「完成」按钮约 30 秒仍不可点时，
multipub 点「取消」关掉弹窗（不然它会盖住「发布」）继续发布，并在结果消息里写明「自定义封面没能设置上」。
同样会写进结果消息的还有：合集没找到 / 可能没选上、背景音乐没加上、简介读回时缺内容。

```yaml
platforms:
  douyin:
    title: 三分钟讲清楚 XXX
    # declaration: 内容由AI生成
    # collection: 我的合集
    # sync_toutiao: false
    # thumbnail_portrait: cover-3x4.jpg
```

### config.toml 可调项（`[platforms.douyin]`）

| 键 | 默认 | 含义 |
|---|---|---|
| `page_timeout` | 300 | 选好视频后等待进入发布表单页（秒） |
| `upload_timeout` | 本次任务超时 − 600（最少 180） | 等待视频/图片上传完成（视频含等待发布表单出现）。任务超时 = `mpub publish --timeout`，没给时为 `timeouts.publish_<kind>`，所以大文件加大 `--timeout` 即可 |
| `publish_timeout` | 240 | 点击「发布」后等待跳转到作品管理页；`--headed` 下出现人机验证时，从验证出现起再等这么久 |
| `sms_timeout` | 600 | 等待短信验证码（验证码只能在这段时间内提交）。Claude Code 要先注意到 `needs_input` 事件再去问用户，所以默认 10 分钟；实际等待不超过本次任务剩下的时间（再留 60 秒给提交后的发布），`needs_input` 消息里写的就是实际秒数 |

环境变量 `DOUYIN_COOKIE_AUTH_HEADLESS=false` 可让 cookie 校验用有头浏览器（上游提示无头偶尔被误判为失效）。

## validate() 检查的限制

| 项 | 规则 | 级别 |
|---|---|---|
| 视频标题 | 必填，≤30 字（上游会静默截断，这里直接报错） | error |
| 视频简介 | >1000 字 | warning |
| 视频格式 | `.mp4 .mov .avi .mkv .m4v .webm .flv .wmv` | error |
| 图文标题 | 必填，≤20 字 | error |
| 图文正文 | ≤1000 字 | error |
| 图文图片 | 1–35 张，格式 `.jpg .jpeg .png .webp .bmp`（不支持 GIF） | error |
| 封面 / thumbnail_* | 图片格式同上，文件必须存在 | error |
| `cover` + `thumbnail_*` | 同时设置时 cover 被忽略 | warning |
| 标题含 emoji 等字符 | 按字符数没超限、按 UTF-16 计数超限（emoji 算 2 个）：网页可能截断 | warning |
| 话题 | >5 个 | warning |
| 正文 + 话题 | 正文不超 1000 字、加上「 #话题」超过 1000 字 | warning |
| 话题内容 | 含空格、`#`、逗号（会被拆开） | warning |
| 定时 | 必须晚于现在 2 小时以上（发布前在 worker 里会再查一次） | error |
| 定时 | 离 2 小时下限不足 15 分钟 / 超过 14 天 | warning |
| `sync_toutiao` | 必须是布尔值 | error |
| `declaration` | 必须是字符串或 `false` | error |
| 图文 + `ai_generated` | 图文流程不支持自动 AI 声明 | warning |
| 图文 + `cover` | 图文以第一张图为封面，cover 被忽略 | warning |
| 未知参数 | 拼写错误等 | warning |

长度按 Python 字符数计算，可能与平台的计数方式（如 emoji）不完全一致。所以填完标题后还会**读回核对**：
网页把标题截短了（输入框上限按 UTF-16 计数时，接近 30 字的标题末尾的 emoji 会被截掉）就重填一次，仍不一致则在点「发布」之前
返回 `failed` / `validation`（作品没有发出），缩短标题后重发即可。简介也会读回，缺内容只告警（编辑器会把话题、表情转换成别的节点）。

## 登录

```
mpub login douyin --account main          # 默认弹出浏览器窗口
mpub login douyin --account main --headless
```

1. 已有 cookie 且仍有效时直接返回「登录仍有效」。
2. 否则打开 `https://creator.douyin.com/`，抓取二维码图片，复制到 `<数据目录>/qrcode/douyin-<账号>.png`
   并上报 `qrcode` 事件（二维码失效会自动刷新并再次上报）。定位不到二维码元素时，改为上报整页截图
   （事件的 `note` 会说明），扫其中的二维码即可。登录结束后删除该图片。
3. 扫码后若抖音要求短信二次验证，**只能在浏览器窗口里手动完成**，所以抖音登录建议不要加 `--headless`。
4. 成功后把 storage_state 写到 `<数据目录>/accounts/douyin/<账号>.json`。
5. 扫码等待时长 = 本次任务超时 − 120 秒（默认 `timeouts.login` 300 → 任务 270 → 扫码约 150 秒；
   余下时间留给 cookie 校验、抓二维码和轮询开销，免得被总超时直接打断）。超时返回 `failed` / `timeout`；
   如果整个过程都没拿到二维码图片，消息会提示去掉 `--headless`。需要更长时间就调大 `timeouts.login`。

装了可选依赖 `multipub[terminal-qr]`（opencv）时，二维码也会以字符画打印到 worker 日志（stderr）。

`mpub check douyin` 用无头浏览器打开上传页，没被跳到登录页即有效（重试 3 次；网络异常也会判为失效）。

## 发布流程与返回

视频：打开上传页 → 选择视频 → 等待进入发布页 → 等待发布表单出现（新版发布页要等视频传完才出表单）→
填标题/简介/话题 → 等待上传完成 → 商品 → 自主声明 → 合集 → 封面 → （可选）同步头条开关 → 定时 →
点击「发布」并等待跳到作品管理页。图文类似（「发布图文」）。等表单和等上传完成共用 `upload_timeout`。

- 第一次点击最终「发布」前会发出 `submit_started` 事件。之后的任何失败都**不会自动重试**（runner 层面）。
- 只有「发布」按钮唯一、可见、可用（没有 `disabled` / `aria-disabled`）时才点：按钮还不能点时继续等（不算点击、
  不发 `submit_started`），一直不能点就在 `publish_timeout` 后按「发布前超时」返回 `timeout`。
- **不会重复点击发布**（multipub 补丁，上游每 3.5 秒点一次）：第一次点击之后只在两种情况下再点——
  平台弹窗拦下了上一次点击（短信验证已完成、提示「请设置封面后再发布」并已选推荐封面），或者 20 秒内既没跳转也没观察到
  发布请求（说明上次点击没生效）。发布请求还没返回、已拿到成功回执、或已经点了 3 次时一律不再点。
- 点击前会在「发布」按钮中心做命中测试：上面盖着别的弹窗 / 遮罩时这一轮不点（force 点击会落在遮罩上，什么也发不出去），
  先尝试关掉通知弹窗或按 Esc；一直被挡就在 `publish_timeout` 后返回 `failed` / `timeout`（消息写明「没有点击，作品没有发出」）。
- 页面上只有「我知道了 / 知道了 / 我已知晓」的通知弹窗（平台公告等）会被自动关掉；不会点「同意」之类有含义的按钮。
- 发布接口明确返回失败（如内容违规）且 15 秒内没有弹出验证：立即返回 `failed` / `platform_error`（附平台原文），不再空等。
- 发布接口已返回成功回执（有作品 ID）但页面 10 秒内没有跳到作品管理页（改版、停在原页只提示成功）：按已提交处理。
- 发布接口返回 HTTP 5xx 等（请求可能已被处理）：不补点，超时后按 `unknown_outcome` 返回，消息里带上 HTTP 状态。
- 成功：`status = submitted`（立即发布，待审核）或 `scheduled`（定时）。
- 作品链接：点击发布后被动监听创作者中心的创建接口回执，拿到 `item_id` 时返回
  `https://www.douyin.com/video/<id>`（图文为 `/note/<id>`）。拿不到时 `url` 为空，消息提示去「作品管理」查看。
- 每次成功后会把刷新后的 cookie 写回账号文件。

### 定时发布

选「定时发布」、在「日期和时间」框里输入时间并回车之后，**点「发布」之前会核实**：「定时发布」处于选中状态
（看选项上的 `data-checked` / 内部 radio；判断不了时至少要出现「日期和时间」框），且框里的值就是要求的时间（精确到分钟，
只比较数字，`2026-10-23 08:05`、`2026/10/23 8:05` 都算对）。已经是定时（如页面恢复了草稿）时不再点选项。
核实不了就截图并返回 `failed` / `validation`（**作品没有发出**）：上游在点击被组件吞掉时会直接立即发布，结果却报成已定时。
此时可以直接重试；仍失败就去掉 `schedule` 到时间再发，或在网页上手动定时。

### 短信验证码

发布时如果弹出短信验证（点「发布」之后弹出，或者一进上传页 / 发布表单刚出来就弹出的身份验证，处理方式相同）：

1. 自动点「获取验证码」，并发出 `needs_input` 事件（`kind = sms_code`，`path = <数据目录>/sau/verify_code.txt`）。
2. **发布还在等待时**，在 `sms_timeout`（默认 10 分钟，以 `needs_input` 消息里的秒数为准）内运行
   `mpub verify-code douyin <验证码>`（或直接写入该文件）。也可以用 PowerShell 的 `echo 123456 > 文件`、记事本等写入：
   文件按 BOM 识别 UTF-8 / UTF-16（Windows PowerShell 5.1 默认写 UTF-16），没有 BOM 时按 UTF-8、再按系统编码（GBK）解码，
   只取其中的数字（全角数字也行）；读不出验证码时只记一条警告并继续等，改好文件即可。
   加了 `--headed` 时，**也可以直接在浏览器窗口里输入验证码并点「验证」**：弹窗关闭后 multipub 不再要验证码，继续发布。
   验证码只能在发布等待期间提交：每一轮验证开始都会删掉旧文件并重新发短信，所以先写验证码再重新发布是没用的
   （旧文件删不掉时——例如 Windows 上被别的程序占用——会忽略它原来的内容，等内容变化后再读）。
3. 超时（或提交 3 轮仍未通过）时：
   - 还没点过发布 → `needs_action` / `sms_code_required`，重新发布后按上面的步骤及时提交新验证码；
   - 已经点过发布（短信验证通常就在点击之后弹出）→ `failed` / `unknown_outcome`：作品可能已经创建，
     先去「作品管理」确认；发布记录会拦住不带 `--force` 的重发。

`verify_code.txt` 按数据目录全局共享：不要同时用两个抖音账号发布。

### 哪些验证可以在浏览器窗口里完成（`--headed`）

| 验证 | 能否在窗口里完成 | 说明 |
|---|---|---|
| 登录后的短信二次验证 | ✔（只能在窗口里） | 所以抖音登录建议不加 `--headless` |
| 发布时的短信验证 | ✔ | 在窗口里输入验证码并点「验证」，或用 `mpub verify-code douyin <验证码>`，两种都行；弹窗连续两次轮询（约 4 秒）都不在就当作已完成 |
| 发布时的人机验证（滑块等） | ✔ | 验证显示期间 multipub 不点「发布」、不把「需要验证」的接口失败当成平台拒绝，从验证出现起最多再等 `publish_timeout`（默认 240 秒）；完成后页面若自己继续提交，multipub 只等结果；没观察到发布请求时按原有规则补点（距上次点击 ≥20 秒，最多 3 次）。没完成就超时 → `unknown_outcome` |
| 无头模式（默认） | ✘ | 看不到窗口：短信验证只能用 `mpub verify-code`；人机验证出现后照旧很快报错（点发布前 `captcha`，点发布后 `unknown_outcome`），提示加 `--headed` 重发 |

### 错误映射

| 情况 | status / error_code |
|---|---|
| 没有账号文件、cookie 失效、中途被踢回登录页（点发布前；打开上传页时掉线会在几秒内报出） | `needs_action` / `login_required` |
| 页面停在人机验证（滑块等），点发布前 | `needs_action` / `captcha`（建议 `--headed` 重发并手动验证） |
| 短信验证码超时；或提交 3 轮验证码后弹窗仍在（验证码错误/过期），点发布前 | `needs_action` / `sms_code_required` |
| 点发布**之后**出现人机验证或短信验证（超时 / 验证失败） | `failed` / `unknown_outcome`（结果未知：先去后台确认；消息里附验证提示） |
| 自主声明没能确认设置上（选项文字与页面不一致、点击没生效等），点发布前 | `failed` / `validation`（拒绝发布，作品没有发出） |
| 标题读回和清单不一致（被网页截断等），点发布前 | `failed` / `validation`（拒绝发布，作品没有发出） |
| `sync_toutiao: false` 但同步开关开着且关不掉，点发布前 | `failed` / `validation`（拒绝发布） |
| 填了 `schedule` 但没能核实「定时发布」已选中、时间已填对，点发布前 | `failed` / `validation`（拒绝发布，作品没有发出） |
| 点发布后接口明确返回失败 | `failed` / `platform_error`（不可重试；作品应未创建，建议先去后台确认） |
| 点发布前某一步超时（含「发布」按钮一直被浮层挡住、没有点击） | `failed` / `timeout`（可重试） |
| 点发布后超时或页面异常 | `failed` / `unknown_outcome`（**先去后台确认，不要直接重发**） |
| 视频上传失败且重传 3 次仍失败 | `failed` / `platform_error` |
| 参数问题（SAU 校验） | `failed` / `validation` |

超时、出错时会把整页截图存到本次运行目录（`evidence`）。

## 已知风险

- 选择器来自上游 2026-06 至 2026-08 的修复（抖音创作者中心改版后），平台随时可能再改版；改版后表现为某一步超时并留下截图。
- 默认使用 patchright 自带的 Chromium；想降低风控可在 config.toml 设置 `browser.channel = "chrome"` 或 `browser.executable_path`。
- 重复提交风险已大幅降低但未消除：发布接口的路径未在真实账号上验证，监听不到时只剩「20 秒内无反应才补点、最多 3 次」两道限制。
- 上游 2026-05 曾认为「自主声明」是发布必选项，2026-07 起改为可不填（multipub 也默认不填）。如果抖音要求必须声明，
  表现为点击发布后不跳转直至超时（`unknown_outcome`，实际没有发出）；此时请填写 `declaration`。
- 抖音图文流程上游没有自主声明，`ai_generated: true` 不会自动标注。
- 第三方同步开关（头条/西瓜）的选择器较旧：`sync_toutiao: true` 可能无效（只告警）；`sync_toutiao: false`
  在找不到开关（或开关旁没有「头条/西瓜」字样）时也只告警，此时请发布前在网页上确认开关状态。

## 未在真实账号上验证

本仓库没有可用的抖音账号，以下全部**未在真实账号上验证**。置信度：高 = 上游近期针对新版页面修过且有实测说明；
中 = 上游代码但较旧或依赖哈希类名；低 = multipub 新增或推测。

| 项 | 选择器 / 接口 | 置信度 |
|---|---|---|
| 登录二维码 | `div#animate_qrcode_container img[src^="data:image"]` 等 4 个候选 | 高（上游 3ed6336，2026-06） |
| 登录完成判定 | URL 含 `creator.douyin.com/creator-micro` 且「扫码登录/手机号登录」不可见 | 高 |
| cookie 校验 | 打开 `creator-micro/content/upload`，无「手机号登录」「扫码登录」 | 高（51209a1） |
| 上传入口 | `input.upload-btn-input, div[class^='container'] input[accept]` | 中高 |
| 发布页 URL | `content/publish?enter_from=publish_page` / `content/post/video?enter_from=publish_page` | 中高 |
| 标题 / 简介 | `input[placeholder*="填写作品标题"]`、`div.zone-container[contenteditable="true"]` | 高（2026-06 DOM） |
| 上传完成 | `[class^="long-card"] div:has-text("重新上传")` | 中 |
| 自主声明 | `.semi-modal-content` 含「请选择声明类型」、`label.semi-radio .semi-radio-addon` | 高（ad49eba，2026-08） |
| 自主声明核实 | 选中后 `label.semi-radio` 带 `semi-radio-checked`（或内部 radio 已勾选）；点「确定」后弹窗隐藏；没有 radio 结构时看入口处是否显示所选文字（multipub 新增） | 中低 |
| 合集 | `[class*="select-collection-"]`、`.semi-select-option.collection-option` | 中高 |
| 封面弹窗 | `div.dy-creator-content-modal`、`.semi-upload:has(.semi-upload-drag-area-main-text) input.semi-upload-hidden-input` | 中高 |
| 同步头条/西瓜开关 | `[class^="info"] > [class^="first-part"] div div.semi-switch`，周围文字含「头条/西瓜」，状态看 `semi-switch-checked` | 低 |
| 发布表单出现 | 新版 `post/video` 页传完才渲染标题框 `input[placeholder*="填写作品标题"]`（上游 5e28092 实测约 40 秒） | 中高 |
| 定时 | `[class^='radio']:has-text('定时发布')`、`.semi-input[placeholder="日期和时间"]` | 中 |
| 定时核实 | 选项的 `data-checked` / `aria-checked` / 内部 radio / 类名含 checked（postsail 记录抖音是 `label.radio-*` + `data-checked`）；输入框 `input_value()` 的数字等于要求的时间（multipub 新增） | 低 |
| 短信弹窗关闭判定 | 短信输入框（同上偏宽的选择器）连续两轮轮询不可见（multipub 新增，`--headed` 手动验证用） | 中低 |
| 有头模式的人机验证 | 同「卡住原因判定」的文字 / 选择器；出现时不点「发布」（multipub 新增） | 低 |
| 发布按钮 / 成功 | 角色 button「发布」（exact）→ URL `creator-micro/content/manage**` | 高 |
| 短信验证 | `input[placeholder*="验证码"], input[type="tel"], …`、「获取验证码」、`div.uc-ui-verify_sms-verify_button` | 中（be77aae；输入框选择器偏宽） |
| 图文入口 / 上传 | 文本「发布图文」、`div[class^='container'] input[accept*='image']`、URL `**/content/post/image?**` | 中 |
| 图文成功 | URL `**/creator-micro/content/manage?enter_from=publish**` | 中 |
| 背景音乐 | `.semi-sidesheet-content`、`.card-container-tmocjc`、`.apply-btn-LUPP0D`（哈希类名） | 低 |
| 图文流程的短信验证 | 复用视频流程的选择器，并要求同时出现 `[class*="uc-ui-verify"]`、「获取验证码」「身份验证」「短信验证」之一（multipub 新增） | 低 |
| 作品 ID / 防重复点击 | `POST creator.douyin.com/web/api/media/aweme/create(_v2)/`：HTTP 2xx 且 `status_code == 0` 的 `item_id`；`status_code != 0` 视为拒绝（multipub 新增；postsail 的抖音文章流程使用同一接口与字段） | 中低 |
| 被拦下后允许再点 | 短信弹窗处理完、「请设置封面后再发布」+ 推荐封面（与上游循环行为一致） | 中 |
| 登录二维码兜底 | 定位不到二维码元素时上报整页截图（multipub 新增） | 中 |
| 作品链接格式 | `https://www.douyin.com/video/<id>`、`/note/<id>` | 中 |
| 卡住原因判定 | 文本「请完成安全验证」「拖动下方滑块」等、`#captcha_container`（multipub 新增，只影响错误分类） | 低 |
| 「发布」被挡住的判定 | 按钮中心 `elementFromPoint` 命中的元素属于类名含 modal/mask/overlay、`role=dialog` 或 `#captcha_container` 的层（且该层不包含按钮）（multipub 新增；判断错时表现为一直不点、报 `timeout`） | 低 |
| 通知弹窗 | `.semi-modal` 里的按钮「我知道了」「知道了」「我已知晓」（multipub 新增） | 低 |
| 标题读回 | `input[placeholder*="填写作品标题"]` 的 `input_value()` 等于要填的标题（multipub 新增） | 中 |
| 封面弹窗取消 | 弹窗里的「取消」按钮 / `.semi-modal-close` / Esc（multipub 新增） | 中低 |

## 本地仿真测试

`tests/sim/test_sim_douyin.py` 用真实 CLI（`mpub publish / login / check / verify-code`）+ 真 Chromium 跑一遍上面的流程，
页面和接口是 `tests/sim/douyin/` 里按上述选择器、文案和接口形状写的仿真页（异步上传进度、上传完才出表单或「发布」才可用、
创建接口回执、跳转、toast、短信 / 人机验证、新手引导、通知弹窗、关不掉的封面弹窗……），真实网络一律拦截。
没有 Chromium 时自动跳过。仿真页只证明流程面对这样的页面时行为正确，不能代替真实账号验证。
