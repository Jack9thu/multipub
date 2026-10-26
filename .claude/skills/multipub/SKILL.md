---
name: multipub
description: 用 multipub（mpub 命令行）把视频、图文、文章发布到 B站、抖音、小红书、知乎。当用户想发布/定时发布视频、图文笔记、动态、想法或知乎文章，检查或登录这些平台的账号，准备或检查 post.yaml 发布清单，或询问发布结果、发布记录时使用。也负责「作品做好之后、发布之前」的准备工作：给一个作品目录或一组成品（视频、小红书图文、知乎文章），核对或补齐各平台的标题、简介、标签、封面、声明、合集，检查知乎 Markdown 渲染和小红书等纯文本正文里残留的 Markdown 符号，核对知乎话题，做到万事俱备、只差用户说「发」——用户说「发布前的准备」「检查标题封面简介」「检查 md 渲染」时也用。Use when the user wants to publish or schedule videos / image posts / articles to Bilibili, Douyin, Xiaohongshu (RedNote) or Zhihu, check or log in platform accounts, or prepare a post.yaml manifest.
---

# multipub：多平台发布

multipub 是本仓库里的命令行工具 `mpub`。它用浏览器自动化和平台网页接口发布内容，**会真实地发到用户的账号上**，
而且大部分功能只有离线测试（真实账号上走通过的部分见仓库 README 开头的说明）。所以：先校验、先给用户看、用户明确同意后才发；结果不确定时绝不重发。

参考资料（需要时再读）：
- [references/cli-contract.md](references/cli-contract.md)：所有命令、参数、JSON 输出、事件、退出码、错误码及处理方式
- [references/troubleshooting.md](references/troubleshooting.md)：常见问题
- 仓库里的 `docs/manifest.md`（post.yaml 全部字段）、`docs/platforms/<平台>.md`（各平台细节）
- [references/prepare.md](references/prepare.md)：**发布前准备**（作品做好之后、发布之前）。用户要做准备工作、或给了作品目录 / 一组成品时，先读它：
  读现有产物 → 定一句话主旨 → 各平台标题、简介、标签、封面对照表（含小红书图文、知乎文章）→ 声明 → 封面 → 正文检查与知乎话题核对 → 合集 →
  validate / dry-run → 确认卡片。**用户只要准备时，到确认卡片就停，不发布。**
- [references/content-check.md](references/content-check.md)：**发布前的正文渲染检查**（知乎 Markdown 逐处核对、小红书等纯文本平台的残留符号、知乎话题），脚本 `scripts/content_check.py`。

## 怎么运行

- 在**仓库根目录**运行 `uv run mpub <子命令> ... --json`。`--json`、`--home` 要写在子命令**后面**。
- 加 `--json` 时 stdout 只有最后一个 JSON 对象（多行缩进）；stderr 是进度：以 `{"event":` 开头的单行 JSON 事件，加普通中文提示行。
- 平台名：`bilibili` `douyin` `xiaohongshu` `zhihu`（也认 `b站` `抖音` `xhs` `知乎` 等别名）。
- 退出码：0 成功；1 有平台失败；2 用法/清单/校验错误；3 需要人处理。以 JSON 里每个平台的 `status` / `error_code` 为准。

## 铁律

1. **发布前必须 dry-run 并取得明确确认**：先 `uv run mpub publish <post> --dry-run --json`，把汇总给用户看，
   用户明确说「发」「确认」之类之后，才运行 `uv run mpub publish <post> --yes --json`。不确定就再问。
2. **不加 `--force`**，除非用户明确要求再发一次同样的内容（查重跳过时，告诉用户已经发过，问要不要强制再发）。
   `--force` **一定和 `--to <平台>` 一起用**，只写要重发的那个平台：`--force` 会让本次所有目标平台都忽略查重，
   不加 `--to` 会把已经发出去的平台再发一遍。
3. **结果里 `submit_started: true` 或 `error_code: unknown_outcome` 的平台绝不重试**：告诉用户先去平台后台确认是否已发出
   （B站 member.bilibili.com / t.bilibili.com，抖音 creator.douyin.com「作品管理」，小红书 creator.xiaohongshu.com「笔记管理」，
   知乎创作中心），由用户决定下一步。
4. 不替用户做声明或同意：AI 声明、转载声明、B站《动态发布规范》确认，只有用户明确表示后才写进清单或配置。
5. 不读取、不打印、不外传 `accounts\` 下的登录文件（等同账号密码）。
6. 正在运行的 `mpub publish` 不要中途杀掉（可能导致结果未知）。万一被结束了，已点过发布的平台在发布记录里是
   `unknown_outcome`，查重会拦住原样重发；按铁律 3 处理。

## 流程

### 1. 环境（第一次用或出错时）

```bash
uv run mpub doctor --json
```

`ok: false` 时看 `checks` 里 `ok: false` 的项：缺浏览器/biliup → `uv run mpub setup --json`。
**biliup 的来源由用户决定**：`setup` 结果里 `bilibili.biliup` 带 `needs_choice: true` 时，把 `candidates`（路径、版本、是否兼容）
列给用户，问他：①直接告诉你 biliup 路径 ②用本机找到的（选编号，或让软件自动挑兼容的）③下载固定版本 v1.3.1；
然后对应运行 `uv run mpub biliup use "<路径>"` / `uv run mpub biliup use <编号>`（或 `--auto`）/ `uv run mpub setup --biliup download --json`。
不要替用户选，也不要直接下载。之后可用 `uv run mpub biliup status --json` 确认。它要下载几百 MB，
**一定用 Bash 的 `run_in_background` 运行，`timeout: 1800000`**（前台默认 2 分钟会被杀掉，留下半截下载），先告诉用户；
已改用本机 Chrome/Edge 时加 `--skip-browser` 只下载 biliup。国内网络见 troubleshooting。
`bilibili.biliup` 提示缺少 VC++ 运行库时，请用户安装 https://aka.ms/vs/17/release/vc_redist.x64.exe 。
`*.accounts` 项只表示没登录，不影响 `ok`。

### 2. 准备清单

- 新建：`uv run mpub new posts/<名字> --kind video|note|article`（输出新文件路径；article 还会生成 `article.md`），
  然后按用户的内容用编辑工具修改 `post.yaml`，把媒体文件放进同一目录（相对路径按 post.yaml 所在目录解析）。
- 字段与平台限制见 `docs/manifest.md`。要点：`platforms` 下列出目标平台；B站视频必须有 `platforms.bilibili.tid`；
  抖音视频标题 ≤30、小红书标题 ≤20、B站动态/抖音图文标题 ≤20；知乎不支持视频和定时。
- 校验：`uv run mpub validate posts/<名字> --json`，直到 `ok: true`。`issues` 里 `level: error` 必须改，`warning` 转告用户。
  `skipped` 是该平台不支持这个内容类型，会自动跳过。`field: account` 的 warning 表示这个账号名还没有登录文件
  （常见原因：登录时用了 `--account main`，清单却没写 `account`，默认是 `default`），按提示改清单或去登录。
- 写清单时：标题、正文里带 `#话题` 要给整个值加引号（`body: "今天去徒步 #徒步"`），`tags` 直接写 `[徒步, 周末]`；
  不加引号的 `#xxx` 会被 YAML 当成注释，validate 会报「会被 YAML 当成注释而丢掉」。Windows 路径用单引号。
- 合集 / 专栏（合集要用户先手动建好）：B站视频 `platforms.bilibili.collection: 合集名`（投稿成功后立即加入，审核中也行）、
  抖音 `platforms.douyin.collection: 合集名`、小红书 `platforms.xiaohongshu.collection: 合集名`、知乎 `platforms.zhihu.column: c_…`。选不上合集不拦发布，结果 `message` 里会注明，要转告用户。
- 内容标注（2026-05 起发布时必选）：B站 `creation_statement`、抖音 `declaration`、小红书 `declaration`、知乎 `statement`，
  各平台写法对照见 [references/prepare.md](references/prepare.md) 第 4 步。属于铁律 4，用户确认后才写。
- 标题、简介、标签、封面怎么写和怎么检查，见 [references/prepare.md](references/prepare.md) 第 2、3、5 步。

### 3. 检查登录

用 validate 输出的 `posts[].platform` / `posts[].account`：

```bash
uv run mpub check douyin xiaohongshu --account main --json
```

`status: ok` 为有效；`needs_action` + `login_required` 就去登录（见下）。抖音/小红书的检查要开无头浏览器，每个账号可能要一两分钟。

### 4. dry-run 与确认

**先查正文渲染**（validate / dry-run 不查这个）：`uv run python .claude/skills/multipub/scripts/content_check.py <post.yaml 或其目录> --topics`。
有错误就按 [references/content-check.md](references/content-check.md) 逐处做最小修复，重跑到没有错误；知乎文章查 Markdown 渲染，
其他平台的正文按纯文本查（不渲染 Markdown，符号会原样显示）；`--topics` 联网只读核对知乎话题有没有同名的。

```bash
uv run mpub validate posts/<名字> --json            # 取每个平台实际要发的字段
uv run mpub publish posts/<名字> --dry-run --json    # 发布前的最终本地校验
```

两者都不联网。dry-run 会读本地发布记录：`status: skipped` 且消息「同一内容已于…」的平台真正发布时会被查重跳过，
汇总时单独列出（已发过 / 点过发布但结果未知），问用户是否要对这个平台强制再发（`--to <平台> --force`）。
给用户一张汇总表，每个平台一行：平台 / 账号 / 内容类型（发成什么：B站视频或动态、抖音视频或图文、小红书笔记、知乎想法或文章）/
标题 / 定时（`schedule`，没有就写「立即」）/ 声明（`ai_generated`；抖音 `declaration`、B站 `creation_statement` 与 `copyright`/`source`、
小红书 `declaration` / `repost_source`、知乎 `statement`、知乎 `draft`）/ 合集（`collection` / `column`，没有就写「不加入」）/ 标签 / 媒体文件。再列出跳过的平台和所有 warning。
然后问：「确认发布到以上平台吗？」只有明确的肯定回答才继续。

### 5. 发布

```bash
uv run mpub publish posts/<名字> --yes --json
```

- 目标里有**抖音**时，发布前先告诉用户：「抖音可能会给绑定手机发短信验证码，收到后直接发给我」。
- **用 Bash 的 `run_in_background` 运行**，超时给足：视频 `timeout: 7200000`（2 小时，工具上限），图文/文章 `1800000`。
  每个平台之间默认等 30 秒；单个平台视频默认最多 1 小时。几个大视频加起来可能超过 2 小时时，用 `--to <平台>` 分几次发。
- **发布期间不要结束这一轮对话去等后台任务**（后台任务只有结束时才会通知你，中途的验证码请求会错过）：
  用 Monitor 工具（或循环读取后台输出、每次间隔几十秒）盯住输出，直到出现 `"event": "needs_input"` 或命令结束。留意 stderr 事件：
  - `{"event": "needs_input", "kind": "sms_code", ...}`（抖音短信验证）：**让发布继续在后台跑**，请用户报出抖音账号绑定手机收到的验证码，
    然后运行 `uv run mpub verify-code douyin <验证码>`，再继续盯输出。默认等 10 分钟；验证码错了可能会再来一次 `needs_input`。
    用户用 `--headed` 时也可以直接在弹出的浏览器窗口里输入验证码。
  - `{"event": "submit_started"}`：已经点了发布，此后这个平台无论结果如何都不能自动重发。
  - `{"event": "log", ...}`：进度，可以简要转告用户。
- 结束后读 stdout 的 JSON：`{"ok", "run_id", "results": [...], "issues": [...]}`。被中断（Ctrl+C、终止）时退出码 130，
  JSON 是 `{"ok": false, "error": "interrupted", "run_id", "results"}`：没开始的平台 `message` 是「已中断，这个平台还没开始发布」。

### 6. 汇报结果

每个平台一行：平台 / 账号 / 状态（中文）/ 链接 / 说明。

| status | 告诉用户 |
|---|---|
| `published` | 已发布，给出 `url` |
| `submitted` | 平台已接收，可能在审核；有 `url` 就给，没有就说去哪个后台看（小红书不返回链接，`item_id` 是笔记 ID） |
| `scheduled` | 已设置定时 |
| `draft` | 只存了草稿（知乎 `draft: true`），给出编辑页 `url`，提醒用户自己点发布 |
| `skipped` | 跳过：平台不支持该类型（`unsupported`），或查重命中（`validation`，消息「同一内容已于…」：已发过，或上次点过发布但结果未知——后者请用户先去后台确认） |
| `needs_action` | 按下节处理 |
| `failed` | 说明 `message`；按 `submit_started` / `error_code` 判断能否重试 |

`evidence` 里有日志和截图路径，失败时可以用 Read 查看截图帮助判断。也可以 `uv run mpub history --json` 查记录。

## 需要人处理（needs_action）与失败

完整的错误码表见 [cli-contract.md](references/cli-contract.md#错误码与处理)。重发前先确认这个平台的结果 `submit_started` 为 `false`，
并且只重发出问题的平台：`--to <平台>`；重发也要用户同意（内容没变时一句确认即可）。

- **`login_required`**：运行 `uv run mpub login <平台> --account <账号> --json`（同样用 `run_in_background`，`timeout: 600000`）。
  - 默认会在用户电脑上弹出浏览器窗口（抖音、小红书、知乎），用户可以直接扫窗口里的二维码。
  - 盯着 stderr 的 `{"event": "qrcode", "path": "...png", "note": "..."}`：用 Read 打开这个 PNG 确认是二维码，
    把路径告诉用户，并可在 Windows 上帮用户打开它：`powershell.exe -NoProfile -Command "Start-Process -FilePath '<path>'"`。
    二维码在数据目录的 `qrcode\<平台>-<账号>.png`，过期会自动刷新（再来一个 qrcode 事件，同一路径），以最新为准。
  - **B站不开浏览器**（TV 端扫码），只能扫这个 PNG：一定要帮用户打开或展示它；手机上会出现「TV 端登录」确认。
  - 抖音扫码后可能要求短信二次验证，只能在弹出的浏览器窗口里完成，所以抖音登录不要加 `--headless`。
  - 结束后 JSON `{"ok", "result"}`，`result.status: ok` 即成功；然后重发该平台。
- **`sms_code_required`**（抖音）：发布中途等短信验证码超时或验证码错误。`submit_started: false` 时可以重新发布，
  这次一看到 `needs_input` 就向用户要验证码（或加 `--headed`，让用户直接在弹出的窗口里输入）；`true` 时先去抖音后台确认。
- **`captcha`**：平台弹了人机验证。把 `message`（以及 `url`，如果有）转告用户。`submit_started: false` 时，加 `--headed` 重发，
  请用户在弹出的窗口里手动完成验证（点发布之后出现的验证，multipub 也会在窗口里等用户完成）；否则先去后台确认。
- **`risk_control`**：被风控。把 `message` 和 `url`（验证页地址）转告用户。知乎：验证要在带着 multipub 登录态的浏览器里完成，
  所以运行 `uv run mpub login zhihu --account <账号>`（会弹窗并直接打开验证页，完成后自动保存），然后再发；
  在用户自己的浏览器里打开那个地址不一定有效。B站：HTTP 模式（`mode: http`）被拦就去掉它改用默认浏览器模式；
  浏览器模式被拦请用户先到 t.bilibili.com 确认，过一会儿再用 `--headed` 重试。都建议降低频率。
- **`consent_required`**（B站动态《动态发布规范》）：发布前就停下了，什么都没发。把选项告诉用户，由用户选：
  1. 用户阅读 <https://t.bilibili.com/h5/dynamic/specification> 并表示同意后，在清单 `platforms.bilibili` 下写 `confirm_rules: true`（只对这一条），
     或在数据目录 `config.toml` 的 `[platforms.bilibili]` 写 `confirm_dynamic_rules = true`（以后所有账号都代为确认）；
  2. 或者加 `--headed` 重发，由用户在弹出的窗口里亲手点「确认并发送」（会记住这个账号，以后不再问）。
  不要替用户选，也不要为了绕开弹窗改用定时或 HTTP 模式。
- **`unknown_outcome`**：已点发布但没拿到确定结果。**不要重试**，请用户去平台后台确认；确认没发出、用户要求再发时才用
  `--to <该平台> --force`（不要漏了 `--to`）。
- **`timeout`** / **`platform_error`** / **`internal`** / **`validation`**：看 `message`。`submit_started: false` 且 `retryable: true` 可以在用户同意后重发；
  缺 biliup → `uv run mpub setup`；缺 VC++ 运行库 → 请用户安装（见 doctor 提示）；「正被另一个任务使用」→ 等前一个任务结束；
  `validation` → 改清单后重新 validate。定时/声明没能在页面上设置成功也是 `validation`，此时作品没有发出，可以安全重发。

## 其他常用

- 列出已登录账号：`uv run mpub accounts --json`
- 发布记录：`uv run mpub history --limit 20 --json`
- 只发其中几个平台 / 统一换账号：`--to bilibili,douyin`、`--account test`
- 显示浏览器窗口发布（排查问题、手动过验证）：`--headed`

## 本机适配（可选）

本 skill 只写通用的部分。各人的环境不同，下面这些适合在自己的**全局 skill**（复制到用户目录的那一份，见下节）里补充，
补充的文件放进 `references/` 或 `scripts/`，并在 SKILL.md 末尾记一段「本机安装说明」，列出本机加了哪些文件、哪些段落，
仓库更新、重新复制 SKILL.md 后照着补回去（本机加的文件不会被覆盖）。

| 可以适配的 | 写什么 | 用在哪一步 |
|---|---|---|
| 写作规范 | 自己的封面 / 标题 / 简介规范、各题材的形态和风格建议（文档路径） | prepare.md「依据」、第 2–3 步 |
| 作品目录约定 | 一个作品目录里哪个文件是文案、清单放在哪、成片 / 时间轴 / 封面 / 图卡输出到哪 | prepare.md 第 1 步 |
| 封面工具 | 用什么画封面（AI 画图 skill、代码画图、设计软件），用什么脚本切各平台尺寸和出检查图，已知会裁字的地方 | prepare.md 第 5 步 |
| 合集 / 专栏表 | 各系列在四个平台的合集名、知乎专栏 id | prepare.md 第 7 步、第 6 步汇报 |
| 账号 | 各平台用的账号名（如 `main`）、登录有效期 | 第 3 步 |
| 署名来源 | 背景音乐、素材放在哪、许可说明在哪 | prepare.md 第 3 步 |
| 声明习惯 | 常用的内容标注（仍要每次请用户确认，铁律 4） | prepare.md 第 4 步 |
| 从一个视频出发 | 只给视频路径时：转写（whisper 等，注意 GPU / 解释器路径）、抽帧、推章节，再接 prepare.md | prepare.md 开头 |
| B站分区表 | 从创作中心刷新的 tid 列表缓存 | prepare.md 第 3 步 |
| 命令写法 | 全局使用时把 `uv run mpub`、`uv run python .claude/...` 换成带仓库路径的写法 | 全篇 |

## 全局安装（在任何目录使用）

把本目录复制到用户级 skills 目录：

```powershell
Copy-Item -Recurse .claude\skills\multipub "$env:USERPROFILE\.claude\skills\multipub"
```

然后在复制出来的 SKILL.md 里，把所有 `uv run mpub` 换成带仓库路径的写法（`--project` 只指定环境，不改变当前目录，
所以 post.yaml 的相对路径仍按当前目录解析），例如仓库在 `C:\Users\me\multipub`：

```bash
uv run --project "C:\Users\me\multipub" mpub check --json
```

仓库更新后记得重新复制。
