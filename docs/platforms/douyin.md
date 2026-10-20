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
| `declaration` | video | 不声明；`ai_generated: true` 时为 `内容由AI生成` | 「自主声明」选项文字，必须与页面上的单选项完全一致（如 `内容由AI生成`、`内容为个人观点或见解`）。填 `false` / `none` 表示不声明。声明设置失败会**拒绝发布**（不会在没有声明的情况下发出去） |
| `collection` | video | 无 | 加入已有合集（名称精确匹配）；找不到时跳过并告警 |
| `product_link` / `product_title` | video | 无 | 购物车商品链接与短标题（≤10 字），两项都填才生效 |
| `sync_toutiao` | video | `false` | `true` 时打开「同步到今日头条/西瓜视频」开关；`false` 时完全不碰该开关（保持页面默认） |
| `thumbnail_portrait` | video | 无 | 竖封面（3:4）。路径相对 post.yaml 所在目录 |
| `thumbnail_landscape` | video | 无 | 横封面（4:3）。同时填竖、横时只会应用竖封面 |
| `bgm` | note | 无 | 背景音乐名称，搜索后选第一条；搜不到跳过 |

`cover` 的用法：没有填 `thumbnail_*` 时，按图片宽高自动放进横封面（宽>高）或竖封面（其余）。
封面设置失败不会中断发布，平台会用推荐封面。

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
| `upload_timeout` | `timeouts.publish_<kind>` − 600（最少 180） | 等待视频/图片上传完成 |
| `publish_timeout` | 240 | 点击「发布」后等待跳转到作品管理页 |
| `sms_timeout` | 300 | 等待短信验证码 |

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
| 话题 | >5 个 | warning |
| 话题内容 | 含空格、`#`、逗号（会被拆开） | warning |
| 定时 | 必须晚于现在 2 小时以上（发布前在 worker 里会再查一次） | error |
| 定时 | 离 2 小时下限不足 15 分钟 / 超过 14 天 | warning |
| `sync_toutiao` | 必须是布尔值 | error |
| `declaration` | 必须是字符串或 `false` | error |
| 图文 + `ai_generated` | 图文流程不支持自动 AI 声明 | warning |
| 图文 + `cover` | 图文以第一张图为封面，cover 被忽略 | warning |
| 未知参数 | 拼写错误等 | warning |

长度按 Python 字符数计算，可能与平台的计数方式（如 emoji）不完全一致。

## 登录

```
mpub login douyin --account main          # 默认弹出浏览器窗口
mpub login douyin --account main --headless
```

1. 已有 cookie 且仍有效时直接返回「登录仍有效」。
2. 否则打开 `https://creator.douyin.com/`，抓取二维码图片，复制到 `<数据目录>/qrcode/douyin-<账号>.png`
   并上报 `qrcode` 事件（二维码失效会自动刷新并再次上报）。登录结束后删除该图片。
3. 扫码后若抖音要求短信二次验证，**只能在浏览器窗口里手动完成**，所以抖音登录建议不要加 `--headless`。
4. 成功后把 storage_state 写到 `<数据目录>/accounts/douyin/<账号>.json`。
5. 等待时长 = `timeouts.login` − 90 秒（默认 210 秒）。

装了可选依赖 `multipub[terminal-qr]`（opencv）时，二维码也会以字符画打印到 worker 日志（stderr）。

`mpub check douyin` 用无头浏览器打开上传页，没被跳到登录页即有效（重试 3 次；网络异常也会判为失效）。

## 发布流程与返回

视频：打开上传页 → 选择视频 → 等待进入发布页 → 填标题/简介/话题 → 等待上传完成 → 商品 → 自主声明 →
合集 → 封面 → （可选）同步头条 → 定时 → 循环点击「发布」直到跳到作品管理页。图文类似（「发布图文」）。

- 第一次点击最终「发布」前会发出 `submit_started` 事件。之后的任何失败都**不会自动重试**。
- 成功：`status = submitted`（立即发布，待审核）或 `scheduled`（定时）。
- 作品链接：点击发布后被动监听创作者中心的创建接口回执，拿到 `item_id` 时返回
  `https://www.douyin.com/video/<id>`（图文为 `/note/<id>`）。拿不到时 `url` 为空，消息提示去「作品管理」查看。
- 每次成功后会把刷新后的 cookie 写回账号文件。

### 短信验证码

发布时如果弹出短信验证：

1. 自动点「获取验证码」，并发出 `needs_input` 事件（`kind = sms_code`，`path = <数据目录>/sau/verify_code.txt`）。
2. 在 `sms_timeout`（默认 5 分钟）内运行 `mpub verify-code douyin <验证码>`（或直接写入该文件）。
3. 超时则返回 `needs_action` / `sms_code_required`。如果当时已经点过发布，消息会提醒先去后台确认再重发。

`verify_code.txt` 按数据目录全局共享：不要同时用两个抖音账号发布。

### 错误映射

| 情况 | status / error_code |
|---|---|
| 没有账号文件、cookie 失效、中途被踢回登录页（点发布前） | `needs_action` / `login_required` |
| 页面停在人机验证（滑块等） | `needs_action` / `captcha`（建议 `--headed` 重发并手动验证） |
| 短信验证码超时 | `needs_action` / `sms_code_required` |
| 点发布前某一步超时 | `failed` / `timeout`（可重试） |
| 点发布后超时或页面异常 | `failed` / `unknown_outcome`（**先去后台确认，不要直接重发**） |
| 视频上传失败且重传 3 次仍失败 | `failed` / `platform_error` |
| 参数问题（SAU 校验） | `failed` / `validation` |

超时、出错时会把整页截图存到本次运行目录（`evidence`）。

## 已知风险

- 选择器来自上游 2026-06 至 2026-08 的修复（抖音创作者中心改版后），平台随时可能再改版；改版后表现为某一步超时并留下截图。
- 默认使用 patchright 自带的 Chromium；想降低风控可在 config.toml 设置 `browser.channel = "chrome"` 或 `browser.executable_path`。
- 发布循环会在跳转前每隔几秒重复点击「发布」（上游行为）；抖音一般在第一次点击后禁用按钮，但理论上存在重复提交的可能。
- 抖音图文流程上游没有自主声明，`ai_generated: true` 不会自动标注。
- 第三方同步开关（头条/西瓜）的选择器较旧，`sync_toutiao: true` 可能无效。

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
| 合集 | `[class*="select-collection-"]`、`.semi-select-option.collection-option` | 中高 |
| 封面弹窗 | `div.dy-creator-content-modal`、`.semi-upload:has(.semi-upload-drag-area-main-text) input.semi-upload-hidden-input` | 中高 |
| 同步头条/西瓜开关 | `[class^="info"] > [class^="first-part"] div div.semi-switch` | 低 |
| 定时 | `[class^='radio']:has-text('定时发布')`、`.semi-input[placeholder="日期和时间"]` | 中 |
| 发布按钮 / 成功 | 角色 button「发布」（exact）→ URL `creator-micro/content/manage**` | 高 |
| 短信验证 | `input[placeholder*="验证码"], input[type="tel"], …`、「获取验证码」、`div.uc-ui-verify_sms-verify_button` | 中（be77aae；输入框选择器偏宽） |
| 图文入口 / 上传 | 文本「发布图文」、`div[class^='container'] input[accept*='image']`、URL `**/content/post/image?**` | 中 |
| 图文成功 | URL `**/creator-micro/content/manage?enter_from=publish**` | 中 |
| 背景音乐 | `.semi-sidesheet-content`、`.card-container-tmocjc`、`.apply-btn-LUPP0D`（哈希类名） | 低 |
| 图文流程的短信验证 | 复用视频流程的选择器，并要求同时出现 `[class*="uc-ui-verify"]`、「获取验证码」「身份验证」「短信验证」之一（multipub 新增） | 低 |
| 作品 ID | `POST creator.douyin.com/web/api/media/aweme/create(_v2)/` 响应 `status_code == 0` 的 `item_id`（multipub 新增） | 低 |
| 作品链接格式 | `https://www.douyin.com/video/<id>`、`/note/<id>` | 中 |
| 卡住原因判定 | 文本「请完成安全验证」「拖动下方滑块」等、`#captcha_container`（multipub 新增，只影响错误分类） | 低 |
