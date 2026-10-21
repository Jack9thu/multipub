# 知乎（zhihu）

实现：`multipub/platforms/zhihu.py`（适配器）、`zhihu_api.py`（Web 接口、图床上传、OSS 签名）、
`zhihu_html.py`（Markdown → 知乎 HTML）。

- **登录**：浏览器（patchright）打开登录页，人扫码或输手机号，拿到 `z_c0` cookie 后保存登录态。
- **发布**：**不开浏览器**，用登录文件里的 cookie 直接调知乎网页版自己用的接口（cookie + `x-xsrftoken`，
  不做 `x-zse-96` 签名）。接口和请求体参考了 zhihu_obsidian（0BSD）、KrisTHL181/zhihu-cli（MIT）、
  BAIGUANGMEI/zhihu-cli（Apache-2.0）的公开代码，multipub 里是独立实现。

> **以下全部未在真实账号上验证。** 第一次使用前请按文末「先用小号验证」清单走一遍。

## 支持的内容类型

| kind | 发成 | 说明 |
|---|---|---|
| `article` | 专栏文章 `https://zhuanlan.zhihu.com/p/<id>` | Markdown 正文、本地/远程图片、封面、话题、创作声明、专栏、目录；可只存草稿 |
| `note` | 想法 `https://www.zhihu.com/pin/<id>`（默认） | 纯文本 + 最多 9 张图 |
| `note` + `as: article` | 专栏文章 | 正文按 Markdown 渲染，`images` 依次放在文末 |
| `video` | ✘ | 自动跳过（v1 不支持，知乎视频没有可用的公开接口参考） |

## 清单字段

通用字段可写在顶层，也可在 `platforms.zhihu` 下覆盖：

| 字段 | 文章 | 想法 |
|---|---|---|
| `title` | 必填，≤100 字 | 可选（接口会带上，页面上不一定显示） |
| `body` / `body_file` | Markdown（规则见下文） | 纯文本，一行一段，空行去掉 |
| `images` | 正文里没引用到的图片依次追加在文末 | 最多 9 张 |
| `cover` | 文章题图 | 忽略（警告） |
| `tags` | 话题，尽力匹配前 3 个；匹配不到只警告 | 默认忽略；`tags_in_text: true` 时以 `#话题#` 文本附在正文末尾 |
| `ai_generated` | `true` 且没写 `statement` 时，创作声明为「包含 AI 辅助创作」 | 不生效（警告） |
| `schedule` | 不支持（报错） | 不支持（报错） |

平台私有参数（`platforms.zhihu` 下）：

| 参数 | 默认 | 适用 | 说明 |
|---|---|---|---|
| `as` | note → `pin`；article → `article` | note | `pin` 发想法；`article` 发文章。长文（kind: article）只能是 `article` |
| `draft` | `false` | 文章 | `true` 只存草稿、不发布，返回编辑页链接，由你手动点「发布」。想法没有草稿箱，设了会报错 |
| `statement` | 跟随 `ai_generated` | 文章 | 创作声明：`none` / `ai`（包含 AI 辅助创作）/ `spoiler`（包含剧透）/ `medical`（包含医疗建议）/ `fiction`（虚构创作）/ `finance`（包含理财内容） |
| `column` | 无 | 文章 | 发到自己的专栏：专栏 id（`c_1234567890`）或专栏链接 |
| `toc` | `false` | 文章 | 开启文章目录 |
| `tags_in_text` | `false` | 想法 | 把 `#话题#` 追加到正文末尾（是否会变成真正的话题未验证） |

```yaml
platforms:
  zhihu:
    draft: false
    statement: ai
    column: c_123456789
```

本地图片的相对路径按 **`body_file` 所在目录** 解析，找不到再按 `post.yaml` 所在目录；支持中文、空格（Markdown 里写
`![](<我的 图.png>)`）、`file:///` 和绝对路径。

### config.toml 可调项（`[platforms.zhihu]`）

| 键 | 默认 | 含义 |
|---|---|---|
| `write_interval` | `2.0` | 两次写请求（建草稿、登记图片、发布……）之间至少间隔的秒数；知乎风控对频率敏感，不建议调小，**最低 1 秒**（配得更小也按 1 秒） |
| `image_timeout` | `90` | 每张图片等待知乎处理完成的最长秒数 |

## validate() 检查的限制

| 项 | 规则 | 级别 |
|---|---|---|
| kind | `video` 不支持 | error |
| `as` | 只能 `pin` / `article`；长文不能 `as: pin` | error |
| 定时 | 设置了 `schedule` | error |
| 文章标题 | 必填，≤100 字 | error |
| 文章正文里的本地图片 | 文件必须存在；不支持 `data:` 内嵌图 | error |
| 正文里的原始 HTML `<img>` | 不会上传到知乎图床（`*.zhimg.com` 的除外），可能显示不出来，请改用 `![](路径)` | warning |
| 图片格式 | `.jpg .jpeg .png .gif .webp .bmp`；webp/bmp 自动转 JPEG | error |
| GIF | 动图只保留第一帧（转成 JPEG） | warning |
| 创作声明 | 只能是上表 6 个值；与 `ai_generated` 冲突时以 `statement` 为准 | error / warning |
| 专栏 | 写了 `column` 但为空 | error |
| 话题 | 超过 3 个只取前 3 个；文章没有话题 | warning |
| `draft` + 声明/专栏 | 只存草稿时声明和专栏不会生效 | warning |
| 想法图片 | ≤9 张 | error |
| 想法 `draft: true` | 想法没有草稿箱 | error |
| 想法正文 | 超过 1000 字（上限未知） | warning |
| 想法 cover / tags / ai_generated | 不生效 | warning |
| 布尔参数 | `draft` `toc` `tags_in_text` 必须是 true/false | error |
| 未知参数 | 拼写错误等 | warning |

## Markdown → 知乎 HTML

用 mistune 3 解析成语法树后自己渲染（`zhihu_html.render_article`）：

| Markdown | 知乎 HTML |
|---|---|
| 开头的 YAML front matter（`---` … `---`） | 丢弃 |
| 开头唯一的 `# 一级标题`（或与 `title` 相同） | 丢弃（标题是单独字段） |
| 其余标题 | 正文里**最浅的一级** → `<h2>`，次一级 → `<h3>`，更深 → `<p><b>…</b></p>`（知乎只有两级标题） |
| 段落里的单个换行 | 拆成多个 `<p>`（知乎一行一段） |
| `**粗**` / `*斜*` / `~~删~~` / `` `代码` `` | `<b>` / `<i>` / `<del>` / `<code>` |
| 代码块 | `<pre lang="python">…</pre>` |
| 表格 | `<table data-draft-node="block" data-draft-type="table" data-size="normal" data-row-style="normal"><tbody>`，表头行放进 tbody（保留 `<th>`） |
| `$公式$` / `$$公式$$` | `<img eeimg="1|2" src="//www.zhihu.com/equation?tex=…">` |
| 脚注 `[^1]` | `<sup data-text data-url data-draft-type="reference" data-numero>`，脚注列表去掉（知乎自动生成「参考」） |
| 图片 | 先传到知乎图床，再写成带 `data-rawwidth / data-original-src / data-watermark-src …` 的 `<img>`；`![]()` 空地址丢掉 |
| 链接 | `<a href>`（`javascript:` 等改成 `#`） |
| 原始 HTML | 原样保留，HTML 注释去掉 |

## 图片

1. 读本地文件，或**下载**正文里的远程图片（http/https，≤20MB，不带知乎 cookie）。已在 `*.zhimg.com` 的图片直接引用。
   远程图片没有用 `zhuanlan.zhihu.com/api/uploaded_images`（服务端抓取）接口，统一走下面的上传流程，少一个未验证接口。
2. JPEG / PNG 原样上传；WebP、BMP、GIF（第一帧）等用 Pillow 转成 JPEG。
3. `POST https://api.zhihu.com/images {"image_hash": md5, "source": "article"|"pin"}`：
   `state=1` 知乎已有，跳过上传；`state=2` 用返回的临时凭证 `PUT https://zhihu-pics-upload.zhimg.com/<object_key>`，
   阿里云 OSS v1 签名（bucket 是 `zhihu-pics`），签名头包含 `x-oss-date`、`x-oss-security-token`、`x-oss-user-agent`。
4. 轮询 `GET https://api.zhihu.com/images/<id>` 直到 `status=success`（每 1.5 秒，最多 `image_timeout` 秒）。
5. 同一次任务里相同的图片只上传一次。

## 登录

```
mpub login zhihu --account main               # 默认弹出浏览器窗口：扫码或手机号登录
mpub login zhihu --account main --headless    # 无头：扫上报的二维码图片
mpub check zhihu --account main
```

1. 已有登录文件且 `GET /api/v4/me` 正常 → 直接返回「已登录」（要换号先删除登录文件）。
2. 否则打开 `https://www.zhihu.com/signin`。登录页自己请求二维码接口（`/api/v3/account/api/login/qrcode`）时，
   拦截返回的 `link` 重新生成二维码 PNG（`<数据目录>/qrcode/zhihu-<账号>.png`）并上报 `qrcode` 事件；
   8 秒内没拦到就截取二维码区域（或整页）作为替代。二维码刷新会再次上报。
3. 等到 `z_c0` cookie 出现（最长 `timeouts.login` − 45 秒，默认 255 秒），再打开一次首页补齐 `_xsrf` / `d_c0`，
   保存 `storage_state` 到 `<数据目录>/accounts/zhihu/<账号>.json`，然后用接口校验一次。
   登录浏览器的 User-Agent 记到 `accounts/zhihu/<账号>.ua.txt`（无头模式的 `HeadlessChrome` 改回 `Chrome`），
   之后发布请求沿用它，让登录和发布看起来是同一个浏览器；没有这个文件就用内置的 Windows Chrome UA。
4. **风控验证**：发布或检查时遇到 40352，验证页地址会记到 `accounts/zhihu/<账号>.verify.txt`；
   下次 `mpub login zhihu`（有头）会**带着现有登录态**打开验证页，你完成验证、页面离开验证页后自动保存。

登录文件也可以手动放：`storage_state` 格式、浏览器插件导出的 cookie 列表，或 `{"z_c0": "...", "_xsrf": "..."}` 都能读
（UTF-8，可带 BOM）。

`mpub check` 调 `GET /api/v4/me?include=is_bind_phone`，返回用户名、是否绑定手机、`z_c0` 剩余天数。

## 发布流程与返回

**文章**：`GET /me` → 上传正文图片、追加图片、封面 → `POST zhuanlan/api/articles/drafts` →
`PATCH zhuanlan/api/articles/<id>/draft`（标题、正文、目录）→ 封面另一次 PATCH（`titleImage`）→
话题（`GET zhuanlan/api/autocomplete/topics` + `POST zhuanlan/api/articles/<id>/topics`）→
`draft: true` 到此为止 → `submit_started` → `POST www/api/v4/content/publish`（`action: article`，
`pc_business_params` 是用 `json.dumps` 生成的 JSON **字符串**，`traceId` = `<毫秒>,<uuid4>`）→
`GET www/api/v4/articles/<id>` 确认。

**想法**：`GET /me` → 上传图片（`source: pin`）→ `POST www/api/v4/content/drafts {"action":"pin"}` →
`submit_started` → `POST www/api/v4/content/publish`（`action: pin`，`hybrid.html` + `media.medias`）→
`GET www/api/v4/pins/<id>` 确认。

所有写请求带 `x-xsrftoken`（`_xsrf` cookie）、`x-requested-with: fetch`、对应的 `Origin` / `Referer`，
彼此至少间隔 `write_interval` 秒。图片上传失败不会留下草稿（先传图再建草稿）。

| 结果 | status | url | 说明 |
|---|---|---|---|
| 文章发布成功且能查到 | `published` | `https://zhuanlan.zhihu.com/p/<id>` | 话题/封面没加上会在 message 里提示 |
| 文章发布成功但暂时查不到 | `submitted` | 同上 | 可能在审核中 |
| 只存草稿 | `draft` | `https://zhuanlan.zhihu.com/p/<id>/edit` | 声明 / 专栏需在编辑页发布时自己选 |
| 知乎明确拒绝发布文章（`code≠0`，或 HTTP 4xx 且不是登录失效 / 风控） | `failed` / `platform_error` | 编辑页链接 | 内容已在草稿箱，可修改后手动发布 |
| 想法发布成功 | `published` / `submitted` | `https://www.zhihu.com/pin/<id>` | 没拿到 id 时 `submitted` 且无链接 |
| 想法被拒绝 | `failed` / `platform_error` | — | |

### 错误映射

| 情况 | status / error_code |
|---|---|
| 没有登录文件、没有 `z_c0`、`z_c0` 过期、HTTP 401、`ERR_TICKET_NOT_EXIST`、被跳到登录页 | `needs_action` / `login_required` |
| HTTP 403 + `error.code=40352`（验证页地址写进 message）、`zh-zse-ck` 挑战、HTTP 429 | `needs_action` / `risk_control` |
| 发布前网络错误、5xx、图片处理超时 | `failed`（可重试） |
| 发布请求已发出后超时 / 断网 / 5xx / 返回无法识别 | `failed` / `unknown_outcome`（**先去知乎确认，不要直接重发**） |
| 建草稿之后出错 | 错误信息末尾带「草稿已保存：<编辑页链接>」（结果未知时是「文章编辑页：<链接>」） |
| 话题搜索 / 添加被风控（40352、429）或接口报错 | 只在 message 里警告，**继续发布**（登录失效除外：直接中止） |
| 正文引用的图片找不到、远程图片下载失败（HTTP 非 200）或超过 20MB、文件不是图片 | `failed` / `validation` |

发布接口只调用一次，绝不自动重试。发布结果不是干净的成功时（被拒、结果未知、没拿到 id），发布接口的原始响应会存成
`<数据目录>/runs/<本次运行>/zhihu/zhihu-publish-response.txt`，路径在结果的 `evidence` 里（响应里没有 cookie）。
发布全程不开浏览器，所以没有截图；登录失败/超时会截图。

## 已知风险

- **接口随时可能改**：知乎若开始在写接口上强制 `x-zse-96` 签名，发布会失败（多半表现为 403 或 `code≠0`）。
  知乎对缺签名的典型回复是 `10003 请求参数异常，请升级客户端后重试`，multipub 会在错误信息里提示这一点。
  备选方案：移植 KrisTHL181/zhihu-cli（MIT）的 `zse.py` 签名，或改成打开编辑页点「发布」的浏览器兜底（尚未实现）。
- **风控**：知乎对请求频率、新设备、异地 IP 敏感。一个账号一天发几篇以内、不要多账号共用一个 IP；出现 40352 按提示过验证。
- **账号要求**：没绑定手机号的账号通常不能发布（`mpub check` 会提示）。
- **审核**：「发布成功」不代表已公开，可能在审核中（返回 `submitted`）。
- **AI 内容**：知乎创作声明有「包含 AI 辅助创作」；AI 生成的内容请设 `ai_generated: true` 或 `statement: ai`。
- **登录文件是明文 cookie**，等同账号密码，不要提交到仓库或发给别人。
- 发布时不会把服务器刷新的 cookie 写回登录文件；登录过期后重新 `mpub login` 即可。

## 未在真实账号上验证

本仓库没有知乎账号，以下全部**未在真实账号上验证**。置信度：高 = 多个 2026 年仍在维护的项目一致；
中 = 单一来源或各项目写法不一；低 = 来源自己也标注不稳定，或 multipub 推测。

| 项 | 接口 / 选择器 | 置信度 |
|---|---|---|
| 登录检查 | `GET https://www.zhihu.com/api/v4/me`（`include=is_bind_phone` 字段） | 高（字段：中） |
| CSRF | 请求头 `x-xsrftoken` = cookie `_xsrf`；写接口不需要 `x-zse-96` | 高（两个独立客户端） |
| 创建文章草稿 | `POST https://zhuanlan.zhihu.com/api/articles/drafts` → `id` | 高 |
| 写草稿正文 | `PATCH https://zhuanlan.zhihu.com/api/articles/<id>/draft` | 高 |
| 封面 | 同一 PATCH，`titleImage` = 图片 `original_src`，`isTitleImageFullScreen: false` | 中 |
| 话题 | `GET zhuanlan/api/autocomplete/topics` + `POST zhuanlan/api/articles/<id>/topics`（zhihu_obsidian 自己标注「不可用」） | 低 |
| 发布 | `POST https://www.zhihu.com/api/v4/content/publish`，`action: article / pin`，请求体字段合并自三个项目 | 中高 |
| `pc_business_params` 的具体键 | 含 `title` / `content`、`commercial_report_info` 等 | 中 |
| 专栏 | `data.column = {"column": ["c_…"]}`，并去掉 `pc_business_params.column` | 低 |
| 创作声明 | `creationStatement.disclaimer_type`；无声明时 `disclaimer_status` 用 `close`（另一项目写 `closed`） | 中 |
| 发布结果 | `data.result` 是 JSON 字符串，id 在 `publish.id` 或 `id` | 中高 |
| 发布后确认 | `GET www/api/v4/articles/<id>`、`GET www/api/v4/pins/<id>` | 中 |
| 想法草稿 | `POST https://www.zhihu.com/api/v4/content/drafts {"action":"pin"}` → `data.content_id` | 高 |
| 想法图片 | `media.medias[].image{url, originalUrl, watermark, watermarkUrl, width, height}` + `hybrid.html` 里的 `<img>` | 中高 |
| 想法上限 | 9 张图（来自 MultiPost 注释）、正文 1000 字（multipub 猜测，只警告） | 低 |
| `#话题#` 文本是否变成话题 | `tags_in_text` | 低 |
| 图片登记 / 上传 / 轮询 | `POST api.zhihu.com/images` → OSS `PUT zhihu-pics-upload.zhimg.com/<key>`（bucket `zhihu-pics`）→ `GET api.zhihu.com/images/<id>` | 高 |
| 文章里 `<img>` 地址 | 用 `original_src` + 扩展名（同 zhihu_obsidian）；想法里直接用 `src`（同 KrisTHL181） | 中 |
| GIF | 不走分片上传，转成静态 JPEG | —（有意简化） |
| 风控 | 403 + `error.code=40352` + `error.redirect` | 中高 |
| 登录页二维码 | 拦截 `/api/v3/account/api/login/qrcode` 返回的 `link`；截图兜底选择器 `.Qrcode-img`、`.Qrcode-content img`、`img[alt*='二维码']` | 中（接口）/ 低（选择器） |
| 登录完成判定 | 出现 `z_c0`；带旧登录态过验证时以离开 `/account/unhuman` 为准 | 高 / 低 |
| Markdown 方言 | 标题、表格、代码、公式、脚注的 HTML 写法（zhihu_obsidian、Linwei-Chen、Wechatsync 一致） | 中高 |
| 删除线 `<del>`、`<ol start>` | 知乎是否保留未知 | 低 |

## 先用小号验证

正式使用前，用一个不重要的、已绑定手机的账号按顺序确认：

1. `mpub login zhihu --account test`：二维码 PNG 是否能扫、登录后 `mpub check` 是否显示用户名和「绑定手机」状态。
2. 文章 `draft: true`：草稿能在编辑页打开，正文格式（标题层级、表格、代码、公式、脚注、图片）正常，封面和话题已加上。
3. 文章直接发布：`content/publish` 不带 `x-zse-96` 能否成功；返回的 id 与链接是否正确。若失败，用浏览器手动发布一次，
   抓包（HAR）对比 `pc_business_params` 的键与本仓库的差异。
4. 话题、专栏（`column`）是否真的挂上了。
5. 创作声明：`statement: ai` 后文章页是否显示「包含 AI 辅助创作」；无声明时 `close` 还是 `closed` 才对。
6. 想法：带 9 张图能否发布、第 10 张是否被拒；`tags_in_text: true` 的 `#话题#` 是否变成可点击的话题。
7. 服务端是否校验 `textLength`（把它改错一次看看）。
8. 连续发两篇看是否触发 40352，以及 `mpub login` 打开验证页、验证后能否继续发布。
9. （v2）视频：知乎视频的上传接口和「上传完成」信号需要抓包确认，v1 不支持。
