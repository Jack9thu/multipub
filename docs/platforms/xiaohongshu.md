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
| `title` | 标题，≤20 字。视频必填；图文不填时用正文前 20 字 |
| `body` | 正文（描述），≤1000 字 |
| `tags` | 话题，≤10 个。每个话题要在 6 秒内出现联想候选才会选中，否则跳过（不影响发布） |
| `cover` | 视频封面（失败时用视频首帧）；图文以第一张图为封面，`cover` 会被忽略 |
| `schedule` | 定时发布（本地时间） |
| `ai_generated` | `true` 时尽力在「添加内容类型声明」里选择「笔记含AI合成内容」，选完核实（面板收起、页面上显示该声明）；没能确认选上只告警（写进结果消息），仍会发布 |

平台私有参数（`platforms.xiaohongshu` 下）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `repost_source` | 无（不声明） | 填写后声明「来源转载」并填入媒体名称。上游会无条件声明转载（且名称为空），这里改为只有填写才声明。设置后会核实（点「确认」后输入框关闭、页面上显示出来源或「来源转载」），**核实不了就拒绝发布**（`validation`，笔记没有发出）：此时可去掉 `repost_source` 重发并在网页上手动声明 |

```yaml
platforms:
  xiaohongshu:
    title: 3分钟学会XXX🔥
    # repost_source: 某某日报
```

### config.toml 可调项（`[platforms.xiaohongshu]`）

| 键 | 默认 | 含义 |
|---|---|---|
| `upload_timeout` | 本次任务超时 − 600（最少 180） | 等待视频/图片上传完成（秒）。任务超时 = `mpub publish --timeout`，没给时为 `timeouts.publish_<kind>` |
| `publish_timeout` | 240 | 点击「发布」后等待跳到成功页 |

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
| `ai_generated` + `repost_source` | 声明可能是单选，以 AI 声明为准 | warning |
| 未知参数 | 拼写错误等 | warning |

## 登录

```
mpub login xiaohongshu --account main
mpub login xiaohongshu --account main --headless   # 小红书无头登录可用：扫上报的二维码图片
```

1. 已有 cookie 且仍有效时直接返回。
2. 否则打开 `/login`，切到扫码面板，二维码复制到 `<数据目录>/qrcode/xiaohongshu-<账号>.png` 并上报 `qrcode` 事件；
   登录结束后删除。
3. 登录成功后再用无头浏览器校验一次 cookie，写入 `<数据目录>/accounts/xiaohongshu/<账号>.json`。
4. 扫码等待时长 = 本次任务超时 − 120 秒（默认约 150 秒；余下时间留给 cookie 校验和抓二维码）。需要更长时间就调大 `timeouts.login`。

`mpub check xiaohongshu` 用无头浏览器打开发布页，被跳到 `/login` 或出现登录框即判为失效。

## 发布流程与返回

视频：打开 `/publish/publish?from=homepage&target=video` → 上传 → 等待上传完成 → 标题/正文/话题 → 封面 →
（可选）内容类型声明 → 定时 → 点击「发布」/「定时发布」并等待 URL 变为 `/publish/success?…`。
图文：`target=image`，上传图片后等标题框出现，其余相同。

- 第一次点击最终发布按钮前发出 `submit_started` 事件；之后不再自动重试（runner 层面）。
- 只有发布按钮唯一、可见、可用时才点；按钮还不能点时继续等（不算点击、不发 `submit_started`）。
  `button:has-text("发布")` 是子串匹配，命中多个按钮时改点文字正好是「发布」（定时为「定时发布」）的那个；
  仍不唯一就不点，`publish_timeout` 后返回 `timeout`（可重试，笔记没有发出）。
- **不会重复点击发布**（multipub 补丁，上游每 3.5 秒点一次）：点击后被动监听创建笔记接口
  `POST edith.xiaohongshu.com/web_api/sns/v2/note`；请求未返回或已成功时不再点。只有 20 秒内既没跳转也没观察到该请求
  （上次点击没生效）才补点一次，最多点 3 次。
- 接口明确返回失败（`success: false`）且 15 秒内页面没有进展：返回 `failed` / `platform_error`（附平台 `msg`）。
- 成功：`status = submitted` 或 `scheduled`。**不返回笔记链接**（分享链接需要 xsec_token）；
  如果监听到创建接口回执，`item_id` 为笔记 ID（24 位十六进制），消息里也会写上。
- `ai_generated: true` 但没能确认选上 AI 声明时，结果消息会附「注意：未能确认已选择…」，请到笔记管理里检查并补充。
- 成功后把刷新后的 cookie 写回账号文件。

### 错误映射

| 情况 | status / error_code |
|---|---|
| 没有账号文件、cookie 失效、中途跳回登录页（点发布前） | `needs_action` / `login_required` |
| 页面停在人机验证，点发布前 | `needs_action` / `captcha` |
| 点发布前某一步超时（含发布按钮一直不可点 / 不唯一） | `failed` / `timeout`（可重试） |
| 点发布后超时、页面异常，或停在人机验证 | `failed` / `unknown_outcome`（先去后台确认，不要直接重发；发布记录会拦住不带 `--force` 的重发） |
| 填了 `repost_source` 但没能确认设置上「来源转载」，点发布前 | `failed` / `validation`（拒绝发布，笔记没有发出） |
| 点发布后接口明确返回失败 | `failed` / `platform_error`（不可重试；笔记应未创建，建议先去后台确认） |
| 参数问题（SAU 校验） | `failed` / `validation` |

超时、出错时整页截图保存到本次运行目录。

## 已知风险

- 小红书登录框切换按钮用的是哈希类名 `img.css-wemwzq`，改版后会失效（表现为登录超时）。
- 发布按钮选择器 `button:has-text("发布")` 是子串匹配；命中多个按钮时改用文字完全一致的那个，仍不唯一则不点，超时返回 `timeout`。
- 话题依赖联想接口，网络慢时会被跳过。
- 小红书发布页可能恢复上一次没发出去的草稿内容（postsail 为此专门做了检测）；上游没有处理，
  若之前有失败的发布，建议先在网页上清掉草稿。
- 重复提交风险已大幅降低但未消除：创建笔记接口路径未在真实账号上验证，监听不到时只剩「20 秒内无反应才补点、最多 3 次」。
- AI 内容声明来自 omnipost（基于 2026-05 的页面），未经上游验证；没能确认选上时**只告警仍发布**，
  对需要合规标注的内容请发布后人工确认。核实依据（面板收起后页面上显示「笔记含AI合成内容」）是推测的页面行为，
  页面显示方式不同时会多出一条不必要的告警。
- 「来源转载」的核实同样基于推测（输入框关闭 + 页面显示来源名称或「来源转载」）；页面显示方式不同会被误判为失败而拒绝发布。
- 默认使用 patchright 自带 Chromium；可在 config.toml 设置 `browser.channel` / `browser.executable_path` 使用本机 Chrome。

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
| 封面 | `div.upload-cover` → 「上传封面」→ `div.upload-wrapper input[type=file]` → `div.d-modal-footer`「确定」 | 中高（3590c52，2026-08） |
| 定时 | `.custom-switch-card`「定时发布」`.d-switch`、`.d-datepicker-input-filter input.d-text` | 中 |
| 发布 / 成功 | `button:has-text("发布")` / `button:has-text("定时发布")` → URL `**/publish/success?**` | 高 |
| 来源转载声明 | 「添加内容类型声明」→「来源转载」→ placeholder「请输入媒体名称」→「确认」 | 中（3590c52） |
| AI 内容声明 | 「添加内容类型声明」→「笔记含AI合成内容」（omnipost / postsail 同一文字） | 低 |
| 声明核实 | 转载：「请输入媒体名称」输入框关闭 + 输入区以外可见来源名称或「来源转载」；AI：点过的选项随面板收起 + 可见「笔记含AI合成内容」（multipub 新增） | 低 |
| 创建笔记接口 / 笔记 ID | `POST *.xiaohongshu.com/web_api/sns/v2/note`，`success: true` 时 `data.id`（参考开源 xhs 客户端 ReaJason/xhs，multipub 新增） | 中低 |
| 卡住原因判定 | 文本「请完成验证」、`.red-captcha` 等（multipub 新增，只影响错误分类） | 低 |
