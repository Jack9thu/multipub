# 小红书（xiaohongshu）

实现：`multipub/platforms/xiaohongshu.py` + vendored 的 social-auto-upload（`multipub/_sau`，上游 `0012d2c`，
改动见 [`src/multipub/_sau/PATCHES.md`](../../src/multipub/_sau/PATCHES.md)）。全程浏览器自动化（patchright），
页面是小红书创作服务平台 `creator.xiaohongshu.com`。

## 支持的内容类型

| kind | 支持 | 说明 |
|---|---|---|
| `video` | ✔ | 标题、正文、话题、封面、定时、内容类型声明（可选） |
| `note`（图文笔记） | ✔ | 1–18 张图、标题、正文、话题、定时、内容类型声明（可选） |
| `article` | ✘ | 自动跳过 |

## 清单字段

通用字段可写在顶层，也可在 `platforms.xiaohongshu` 下覆盖：

| 字段 | 用法 |
|---|---|
| `title` | 标题，≤20 字。视频必填；图文不填时用正文前 20 字。含 emoji 时网页可能按 2 个字计（校验给 warning）：填完会读回核对，被标题框截断就拒绝发布（`validation`，笔记没有发出） |
| `body` | 正文（描述），≤1000 字。正文里自己写的 `#xxx` / `@xxx` 只是普通文字（逐行输入，这样的行回车前先按 Esc 收起候选框，免得回车选中别的话题）；要挂话题请用 `tags` |
| `tags` | 话题，≤10 个。只选名字与标签相同的候选（没有时退而求其次选名字包含标签的）；6 秒内没出现候选、或约 3 秒内候选里没有对得上的，就跳过该标签（不影响发布） |
| `cover` | 视频封面。设置失败时关掉封面弹窗、用平台默认封面照常发布，结果消息里注明「自定义封面没能设置上」；图文以第一张图为封面，`cover` 会被忽略 |
| `schedule` | 定时发布（本地时间） |
| `ai_generated` | `true` 时尽力在「添加内容类型声明」里选择「笔记含AI合成内容」，选完核实（面板收起、页面上显示该声明）；没能确认选上只告警（写进结果消息），仍会发布。写了 `declaration` 时以 `declaration` 为准 |

平台私有参数（`platforms.xiaohongshu` 下）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `declaration` | 不写（跟随 `ai_generated`） | 「添加内容类型声明」要选的选项文字，必须与页面完全一致：`虚构演绎，仅供娱乐` / `笔记含AI合成内容` / `内容包含营销广告`（2026-10 调研到的网页端选项，未在真实账号上验证）。选完核实，**核实不了就拒绝发布**（`validation`，笔记没有发出）。`false` / `none` 表示不声明（`ai_generated` 的 AI 声明也不选）。「来源转载」请用 `repost_source`；「自主拍摄」要填地点和日期，不支持 |
| `collection` | 不写（不加入合集） | 发布页「内容设置 → 加入合集」里要选的已有合集，名称精确匹配。选完核对按钮上显示的就是这个合集名；找不到「加入合集」、列表里没有这个名字、没能确认选上，都只告警（写进结果消息），照常发布。不会新建合集 |
| `repost_source` | 无（不声明） | 填写后声明「来源转载」并填入媒体名称。上游会无条件声明转载（且名称为空），这里改为只有填写才声明。设置后会核实（点「确认」后输入框关闭、页面上显示出来源或「来源转载」），**核实不了就拒绝发布**（`validation`，笔记没有发出）：此时可去掉 `repost_source` 重发并在网页上手动声明 |

```yaml
platforms:
  xiaohongshu:
    title: 3分钟学会XXX🔥
    # declaration: 虚构演绎，仅供娱乐
    # repost_source: 某某日报
```

### config.toml 可调项（`[platforms.xiaohongshu]`）

| 键 | 默认 | 含义 |
|---|---|---|
| `upload_timeout` | 本次任务超时 − 600（最少 180） | 等待视频/图片上传完成（秒）。任务超时 = `mpub publish --timeout`，没给时为 `timeouts.publish_<kind>` |
| `publish_timeout` | 240 | 点击「发布」后等待跳到成功页；`--headed` 下出现人机验证时，从验证出现起再等这么久 |

环境变量 `SAU_XHS_CREATOR_BASE_URL` 可切换创作者中心域名（如 `https://creator.rednote.com`）。

## validate() 检查的限制

| 项 | 规则 | 级别 |
|---|---|---|
| 标题 | ≤20 字（上游会静默截断，这里直接报错）；视频必填 | error |
| 图文无标题 | 用正文前 20 字；标题和正文都为空时 | warning / error |
| 正文 | ≤1000 字 | error |
| 正文 + 话题 | 合计超过 1000 字 | warning |
| 话题 | ≤10 个 | error |
| 话题内容 | 含空格、`#`、逗号 | warning |
| 视频格式 | `.mp4 .mov .avi .mkv .m4v .webm .flv .wmv` | error |
| 图片 | 1–18 张，`.jpg .jpeg .png .webp .bmp` | error |
| 图片 BMP | 网页端可能不接受，建议转 JPG/PNG | warning |
| 定时 | 必须晚于现在 2 小时以上 | error |
| 定时 | 离 2 小时下限不足 15 分钟 / 超过 14 天 | warning |
| `ai_generated` / `declaration` + `repost_source` | 声明可能是单选，以后选的 AI / `declaration` 声明为准 | warning |
| `declaration` | 不是字符串或 `false`；写成 `来源转载` / `自主拍摄` / `内容来源声明` | error |
| `collection` | 不是字符串，或是空字符串 | error |
| `declaration` | 不在已知选项里（文字可能和页面不一致） | warning |
| `declaration` + `ai_generated` | 声明不是 AI 那一项（单选，以 `declaration` 为准）；或 `declaration: false` 关掉了 AI 声明 | warning |
| 未知参数 | 拼写错误等 | warning |

## 登录

```
mpub login xiaohongshu --account main
mpub login xiaohongshu --account main --headless   # 小红书无头登录可用：扫上报的二维码图片
```

1. 已有 cookie 且仍有效时直接返回。
2. 否则打开 `/login`，切到扫码面板，二维码复制到 `<数据目录>/qrcode/xiaohongshu-<账号>.png` 并上报 `qrcode` 事件；
   登录结束后删除。以「二维码图片可见」判断是否已在扫码面板；切换入口在 10 秒内轮流看：登录框里的切换图标（哈希类名
   `img.css-wemwzq`）、「扫码登录」文字、「APP扫一扫登录」，3 秒后再试登录框右上角的图标（类名改版后它通常还在）。
   二维码过期（「已过期 / 已失效 / 点击刷新」）时自动点刷新，新二维码重新上报（同一路径，以最新图片为准）。
   **切不过去或定位不到二维码时不会立刻失败**：改为上报整页截图（事件的 `note` 会说明，面板已经是扫码时扫其中的二维码即可），
   并照常等待登录完成——有头模式下可以直接在弹出的窗口里扫码，或改用手机号 + 验证码登录。
   无头模式下截图不会刷新，截图里没有二维码（停在手机号登录面板）或二维码过期了，就去掉 `--headless` 重试（`note` 里也会这样提示）。
3. 登录成功后再用无头浏览器校验一次 cookie，写入 `<数据目录>/accounts/xiaohongshu/<账号>.json`。
4. 扫码等待时长 = 本次任务超时 − 120 秒（默认约 150 秒；余下时间留给 cookie 校验和抓二维码）。需要更长时间就调大 `timeouts.login`。

`mpub check xiaohongshu` 用无头浏览器打开发布页，被跳到 `/login` 或出现登录框即判为失效。

## 发布流程与返回

视频：打开 `/publish/publish?from=homepage&target=video` → 等上传框渲染 → 上传 → 等待上传完成 → 标题/正文/话题 → 封面 →
（可选）内容类型声明 → 定时 → 点击「发布」/「定时发布」并等待 URL 变为 `/publish/success`（带不带查询串都认）。
图文：`target=image`，上传图片后等标题框出现，填完表、点发布之前再等页面上不再显示「上传中」，其余相同。

- 视频上传失败（预览区显示「上传失败」等）自动重传，最多 2 次；仍失败返回 `failed` / `platform_error`（可重试，笔记没有发出）。
  图文有图片显示「上传失败」时同样返回 `platform_error`，不会少一张图发出去。
- 流程中途被踢回登录页立即返回 `login_required`（不再等到上传截止时间）；填表时页面停在人机验证返回 `captcha`。
- 发布页恢复了上次没发出去的草稿时，正文框清空后重写（上游在这里卡 30 秒后报 internal）。

- 定时：打开「定时发布」开关（已经打开就不点，上游会把它点关）、把时间填进日期框并让输入框失焦以提交
  （不按回车：输入框若在表单里，回车可能触发隐式提交），**点发布之前核实**开关已打开（判断不了时至少要出现日期框）、
  框里的值就是要求的时间（精确到分钟）。核实不了就截图并返回 `failed` / `validation`（**笔记没有发出**）：
  上游在时间没填进去时可能按页面默认时间定时，结果却报成你要的时间。可以直接重试；仍失败就去掉 `schedule` 到时间再发。
- 第一次点击最终发布按钮前发出 `submit_started` 事件；之后不再自动重试（runner 层面）。
- 加 `--headed` 时，页面出现人机验证（滑块等）可以直接在窗口里完成：验证显示期间不点「发布」、不把接口的「需要验证」
  失败当成平台拒绝，从验证出现起最多再等 `publish_timeout`；没完成就超时（点过发布 → `unknown_outcome`）。
  无头模式下照旧很快按人机验证报错。
- 只有发布按钮唯一、可见、可用、且没被弹窗 / 遮罩挡住（先做一次不派发的可点性检查）时才点；按钮还不能点时继续等
  （不算点击、不发 `submit_started`，被挡住时按一次 Esc），一直不能点就在 `publish_timeout` 后返回 `timeout`（笔记没有发出）。
  `button:has-text("发布")` 是子串匹配，命中多个按钮时改点文字正好是「发布」（定时为「定时发布」）的那个；
  仍不唯一就不点，`publish_timeout` 后返回 `timeout`（可重试，笔记没有发出）。
- **不会重复点击发布**（multipub 补丁，上游每 3.5 秒点一次）：点击后被动监听创建笔记接口
  `POST edith.xiaohongshu.com/web_api/sns/v2/note`；请求未返回或已成功时不再点。只有 20 秒内既没跳转也没观察到该请求
  （上次点击没生效）才补点一次，最多点 3 次。
- 接口明确返回失败（`success: false`）且 15 秒内页面没有进展：返回 `failed` / `platform_error`（附平台 `msg`）。
- 接口返回成功（带笔记 ID）但页面一直不跳成功页：10 秒后按成功处理（不再点击）。接口返回 HTTP 4xx/5xx 时状态码写进超时报错。
- 成功：`status = submitted` 或 `scheduled`。**不返回笔记链接**（分享链接需要 xsec_token）；
  如果监听到创建接口回执，`item_id` 为笔记 ID（24 位十六进制），消息里也会写上。
- `ai_generated: true` 但没能确认选上 AI 声明时，结果消息会附「注意：未能确认已选择…」，请到笔记管理里检查并补充。
- 成功后把刷新后的 cookie 写回账号文件。

### 错误映射

| 情况 | status / error_code |
|---|---|
| 没有账号文件、cookie 失效、中途跳回登录页（点发布前） | `needs_action` / `login_required` |
| 页面停在人机验证，点发布前 | `needs_action` / `captcha` |
| 视频上传重传 2 次仍失败、有图片上传失败 | `failed` / `platform_error`（可重试，笔记没有发出） |
| 标题被网页标题框截断（emoji 按 2 个字计） | `failed` / `validation`（笔记没有发出，缩短标题后重发） |
| 点发布前某一步超时（含发布按钮一直不可点 / 不唯一） | `failed` / `timeout`（可重试） |
| 点发布后超时、页面异常，或停在人机验证 | `failed` / `unknown_outcome`（先去后台确认，不要直接重发；发布记录会拦住不带 `--force` 的重发） |
| 填了 `repost_source` 但没能确认设置上「来源转载」，点发布前 | `failed` / `validation`（拒绝发布，笔记没有发出） |
| 填了 `declaration` 但没能确认选上该项，点发布前 | `failed` / `validation`（拒绝发布，笔记没有发出） |
| 填了 `schedule` 但没能核实「定时发布」开关已打开、时间已填对，点发布前 | `failed` / `validation`（拒绝发布，笔记没有发出） |
| 点发布后接口明确返回失败 | `failed` / `platform_error`（不可重试；笔记应未创建，建议先去后台确认） |
| 参数问题（SAU 校验） | `failed` / `validation` |

超时、出错时整页截图保存到本次运行目录。

## 已知风险

- 小红书登录框切换按钮用的是哈希类名 `img.css-wemwzq`，改版后会失效。以前表现为 `mpub login` 10–30 秒后直接报
  「登录失败: …Timeout…」（`internal`）并关掉窗口，根本等不到扫码；现在会再试「扫码登录」文字，仍不行就上报整页截图（`qrcode` 事件的 `note` 写着
  「没定位到二维码元素」）并继续等待——请在弹出的窗口里手动登录，无头模式请去掉 `--headless`。
- 发布按钮选择器 `button:has-text("发布")` 是子串匹配；命中多个按钮时改用文字完全一致的那个，仍不唯一则不点，超时返回 `timeout`。
- 话题依赖联想接口，网络慢时会被跳过。
- 图文点发布前以「页面上可见的『上传中 / 上传失败』字样」判断图片是否传完 / 传失败（multipub 新增，推测的页面行为）：
  页面上若有常驻的同样字样（如帮助链接），图文会一直等到上传超时或被判为上传失败（不会发出去），请反馈页面截图。
- 小红书发布页可能恢复上一次没发出去的草稿内容（postsail 为此专门做了检测）：正文框会清空后重写、标题会覆盖；
  其他草稿内容（如已选的话题、声明）不会清，若之前有失败的发布，建议先在网页上清掉草稿。
- 重复提交风险已大幅降低但未消除：创建笔记接口路径未在真实账号上验证，监听不到时只剩「20 秒内无反应才补点、最多 3 次」。
- AI 内容声明来自 omnipost（基于 2026-05 的页面），未经上游验证；没能确认选上时**只告警仍发布**，
  对需要合规标注的内容请发布后人工确认。核实依据（面板收起后页面上显示「笔记含AI合成内容」）是推测的页面行为，
  页面显示方式不同时会多出一条不必要的告警。
- 「来源转载」的核实同样基于推测（输入框关闭 + 页面显示来源名称或「来源转载」）；页面显示方式不同会被误判为失败而拒绝发布。
- 默认使用 patchright 自带 Chromium；可在 config.toml 设置 `browser.channel` / `browser.executable_path` 使用本机 Chrome。

## 本地仿真测试

`tests/sim/test_sim_xhs.py` 用真实 CLI + 真 Chromium 跑本地仿真页面（`tests/sim/xhs/`，按上面这些选择器、文字、接口搭建，
真实网络一律拦截）：视频 / 图文 / 定时 / 声明 / 封面 / 话题 / 草稿 / 上传失败重传 / 被拒 / 网关错误 / 卡住 / 人机验证 /
中途掉线、查重与 `--force`、SIGTERM 中断，以及扫码登录（默认扫码面板、切换图标、类名改版、切不过去、二维码过期刷新）和 `mpub check`。
仿真页面的行为是按上游代码和其他开源实现推测的，**不能代替真实账号验证**；没有 Chromium 时自动跳过。

## 未在真实账号上验证

本仓库没有可用的小红书账号，以下全部**未在真实账号上验证**。置信度：高 = 上游近期修过；中 = 上游代码但较旧或依赖哈希类名；
低 = 来自其他 fork 或 multipub 新增。

| 项 | 选择器 / 接口 | 置信度 |
|---|---|---|
| 登录框 / 扫码面板 | `div[class*='login-box']`、`img.css-wemwzq`（哈希类名）、「扫一扫」 | 中 |
| 登录二维码 | `.login-box-container` 内「APP扫一扫登录」后的 `img` | 中 |
| cookie 校验 | `/publish/publish?from=homepage&target=video` 是否跳 `/login` | 高 |
| 视频上传 | `div[class^='upload-content'] input[class='upload-input']`、`preview-new` 内「上传成功」等文字 | 中高（fc531e9） |
| 图片上传 | `input[type="file"][accept*="image"]`，标题框出现即完成 | 中 |
| 标题 / 正文 | `input[placeholder*="填写标题"]`、`p[data-placeholder*="输入正文描述"]` | 高 |
| 话题 | `#creator-editor-topic-container .item` | 高（1b36b8b） |
| 封面 | `div.upload-cover` → `div.d-modal` →「上传封面」（没有页签时直接找弹窗里的图片上传框，postsail 的版本）→ `div.d-modal-footer`「确定」；失败时 Esc /「取消」关弹窗 | 中高（3590c52，2026-08） |
| 定时 | `.custom-switch-card`「定时发布」`.d-switch`、`.d-datepicker-input-filter input.d-text`（兜底 `input[placeholder*="日期和时间"]`） | 中 |
| 定时核实 | 开关的 `aria-checked` / `data-checked` / 内部 checkbox / 类名含 checked、active；日期框 `input_value()` 的数字等于要求的时间；失焦（`blur`）后日期选择器会提交输入（multipub 新增） | 低 |
| 登录兜底 | 「扫码登录」文字切换、整页截图上报（multipub 新增） | 中 |
| 有头模式的人机验证 | 同「卡住原因判定」；出现时不点「发布」（multipub 新增） | 低 |
| 发布 / 成功 | `button:has-text("发布")` / `button:has-text("定时发布")` → URL 含 `/publish/success` | 高 |
| 来源转载声明 | 「添加内容类型声明」→「来源转载」→ placeholder「请输入媒体名称」→「确认」 | 中（3590c52） |
| AI 内容声明 | 「添加内容类型声明」→「笔记含AI合成内容」（omnipost / postsail 同一文字） | 低 |
| 声明核实 | 转载：「请输入媒体名称」输入框关闭 + 输入区以外可见来源名称或「来源转载」；AI：点过的选项随面板收起 + 可见「笔记含AI合成内容」（multipub 新增） | 低 |
| 创建笔记接口 / 笔记 ID | `POST *.xiaohongshu.com/web_api/sns/v2/note`，`success: true` 时 `data.id`（参考开源 xhs 客户端 ReaJason/xhs，multipub 新增） | 中低 |
| 卡住原因判定 | 文本「请完成验证」、`.red-captcha` 等（multipub 新增，只影响错误分类） | 低 |
