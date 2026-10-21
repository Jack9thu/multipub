# mpub 命令行约定（给脚本 / Claude Code）

以 `src/multipub/cli.py`、`models.py`、`platforms/base.py` 为准。

## 通用

- 运行：仓库根目录下 `uv run mpub <子命令> [参数] [--json] [--home <目录>]`。
- `--json` / `--home` 只能写在**子命令后面**（`mpub --json doctor` 是用法错误）。
- `--home`：数据目录，默认 `%LOCALAPPDATA%\multipub`（或环境变量 `MULTIPUB_HOME`）。
- `--json` 时：
  - **stdout** 只有一个 JSON 对象（`indent=2` 多行），在命令结束时输出。例外：`setup` 没有 JSON 输出，`new`、`verify-code` 输出一行纯文本路径，
    `login --interactive` 不输出 JSON。
  - **stderr**：进度。事件是单行 JSON（以 `{"event":` 开头），其余是普通中文提示行（如「→ 发布到 抖音/main ……」）。
- 平台名不分大小写，别名：`bilibili|bili|b站|b`、`douyin|dy|抖音`、`xiaohongshu|xhs|小红书|rednote`、`zhihu|zh|知乎`。
- `<post>`：`post.yaml` 的路径或其所在目录。
- argparse 用法错误：退出码 2，stderr 打印 usage，没有 JSON。Ctrl+C：退出码 130。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 全部成功（`ok` / `published` / `submitted` / `scheduled` / `draft` / `dry_run` / `skipped`） |
| 1 | 至少一个结果 `failed`；`doctor` 有核心检查项不通过；`setup` 有步骤失败 |
| 2 | 用法错误、清单错误（`error: manifest`）、校验错误（`error: validation`）、`publish` 没加 `--yes` 又不在交互终端、交互确认时回答了「否」、`new` 目标已存在、`verify-code` 平台不是抖音、`login --interactive` 用在不支持的平台 |
| 3 | 没有失败，但至少一个结果 `needs_action` |

## 命令

### doctor

```
mpub doctor [--json]
```

```json
{"ok": false, "checks": [{"name": "browser", "ok": false, "detail": "未安装 Chromium，运行: mpub setup（或 patchright install chromium）"}, ...]}
```

检查项名：`python`、`data_home`、`patchright`、`browser`、`bilibili.biliup`、`douyin.terminal_qr`、`douyin.stealth_js`、
`xiaohongshu.terminal_qr`、`xiaohongshu.stealth_js`、`zhihu.deps`，以及每个平台的 `<平台>.accounts`。
`ok` 不计 `*.accounts`（没登录不算环境问题）。退出码：`ok` 为 true 时 0，否则 1。不联网、不开浏览器。

### setup

```
mpub setup [--skip-browser]
```

安装 patchright Chromium（`browser.channel = "chromium"` 且没设 `executable_path` 时；`--skip-browser` 跳过）、下载 biliup。
已安装的会跳过，可重复运行。**会联网下载**。没有 JSON 输出（加了 `--json` 也只有 stderr 进度），看退出码：0 成功，1 有失败。

### login

```
mpub login <平台> [--account <名字>] [--headless] [--interactive] [--json]
```

- `--account` 默认 `default`。账号名只能是字母、数字、`_`、`-`。
- 默认**有头**（弹出浏览器窗口）；`--headless` 只输出二维码图片。B站不开浏览器（TV 端扫码，只有二维码图片）。
- 已有有效登录时直接返回 `ok`（「无需重新扫码」）。
- 总超时 `[timeouts] login`（默认 300 秒）；实际等扫码的时间比它短 45~90 秒。
- `--interactive`：仅 B站，在真实终端里运行 biliup 自带的交互式登录；需要 TTY，Claude Code 里不要用。没有 JSON，退出码是 biliup 的。

```json
{"ok": true, "result": {<Result>, "action": "login", "status": "ok", "message": "扫码登录成功（...）"}}
```

期间的 stderr 事件：`qrcode`（可能多次，二维码刷新）、`log`。

### check

```
mpub check [平台 ...] [--all] [--account <名字>] [--json]
```

不写平台（或 `--all`）= 全部四个平台。不写 `--account` = 该平台所有已登录账号；某平台一个账号都没有时，返回一条
`account: "-"`、`status: needs_action`、`error_code: login_required` 的结果。B站、知乎走 HTTP；抖音、小红书开无头浏览器（较慢）。

```json
{"ok": false, "results": [<Result action=check>, ...]}
```

### accounts

```
mpub accounts [--json]
```

```json
{"accounts": [{"platform": "douyin", "account": "main", "file": "C:\\Users\\me\\AppData\\Local\\multipub\\accounts\\douyin\\main.json", "updated_at": "2026-10-21T10:00:00"}]}
```

`file` 是登录凭据，不要读取或外传。

### new

```
mpub new <目录> --kind video|note|article [--force]
```

在目录里生成 `post.yaml`（`article` 还会生成 `article.md`，已存在则不动）。stdout 输出 `post.yaml` 的路径（纯文本，加 `--json` 也一样）。
`post.yaml` 已存在且没加 `--force`：stderr 提示，退出码 2。

### validate

```
mpub validate <post> [--to 平台,平台] [--account <名字>] [--json]
```

纯本地：不联网、不检查登录、不查重。

```json
{
  "ok": true,
  "kind": "video",
  "posts": [<PlatformPost>, ...],
  "skipped": [<Result status=skipped error_code=unsupported>, ...],
  "issues": [{"level": "warning", "field": "tags", "message": "...", "platform": "zhihu"}]
}
```

有 `level: error` 的 issue 时 `ok: false`、退出码 2。清单本身读不了（找不到文件、YAML 错误、`kind` 不对、未知平台、`body_file` 不存在、
`schedule` 格式错、没有目标平台）时输出 `{"ok": false, "error": "manifest", "message": "..."}`，退出码 2。

### publish

```
mpub publish <post> [--to 平台,平台] [--account <名字>] [--dry-run] [--yes|-y] [--headed]
             [--gap <秒>] [--timeout <秒>] [--force] [--json]
```

| 参数 | 说明 |
|---|---|
| `--to` | 替换清单里的平台列表（逗号分隔） |
| `--account` | 所有平台统一用这个账号（覆盖清单） |
| `--dry-run` | 只做与 validate 相同的本地校验，每个平台返回 `status: dry_run`；不查重、不检查登录 |
| `--yes` / `-y` | 不再询问确认。stdin 不是终端时**必须**加，否则 stderr 提示、退出码 2、没有 JSON |
| `--headed` | 显示浏览器窗口（默认按 config `browser.headless`，默认无头） |
| `--gap` | 平台之间等待秒数（默认 config `gap_seconds` = 30） |
| `--timeout` | 单个平台的总超时秒数（默认 `[timeouts] publish_<kind>`：视频 3600、图文/文章 900） |
| `--force` | 忽略查重，强制再发 |

流程：解析清单 → 不支持该类型的平台记为 `skipped` → 校验（有 error 则输出
`{"ok": false, "error": "validation", "issues": [...]}`、退出码 2）→ `--dry-run` 到此为止 → 查重（同一内容已
`published`/`submitted`/`scheduled` 或上次 `unknown_outcome` 的平台记为 `skipped`，`error_code: validation`，
消息「同一内容已于 … 发过…确认要再发请加 --force」）→ 逐个平台串行发布（每个平台一个 worker 子进程）→ 写 `history.jsonl`。

```json
{"ok": true, "run_id": "20261021-200000", "results": [<Result action=publish>, ...]}
```

期间的 stderr 事件：`log`、`submit_started`、`needs_input`（抖音短信）。

### history

```
mpub history [--limit N] [--json]      # 默认最近 30 条
```

```json
{"history": [{"run_id": "...", "fingerprint": "...", "source": "D:\\posts\\ep12\\post.yaml", "title": "...", <Result 字段>...}]}
```

### verify-code

```
mpub verify-code douyin <验证码>
```

把验证码写进 `<数据目录>\sau\verify_code.txt`（正在等待短信验证的抖音发布会读取它），stdout 输出该文件路径（纯文本）。
平台不是抖音：退出码 2。

## 数据结构

### Result

```json
{
  "platform": "douyin",
  "account": "main",
  "action": "publish",
  "status": "submitted",
  "kind": "video",
  "message": "已提交，审核通过后公开",
  "url": "https://www.douyin.com/video/7xxxxxxxxxxxxxxxxxx",
  "item_id": "7xxxxxxxxxxxxxxxxxx",
  "error_code": null,
  "retryable": false,
  "submit_started": true,
  "evidence": ["C:\\...\\runs\\20261021-200000\\douyin\\douyin-publish.log"],
  "started_at": "2026-10-21T20:00:00+08:00",
  "finished_at": "2026-10-21T20:06:12+08:00",
  "ok": true
}
```

- `ok`：`status` 属于 `ok/published/submitted/scheduled/draft/dry_run/skipped`。
- `submit_started`：已经点过「发布」或调用过发布接口。为 true 时 `retryable` 一定是 false，**不要自动重试**。
- `retryable`：失败发生在发布动作之前，原样重发一般安全（仍需用户同意）。
- `evidence`：worker 日志、截图、平台原始响应等文件路径（在 `<数据目录>\runs\<run_id>\<平台>\`）。
- `url`：作品链接；失败时也可能是验证页 / 草稿编辑页链接。小红书不返回链接（`item_id` 是笔记 ID）。

### status

| status | 含义 |
|---|---|
| `ok` | login / check 成功 |
| `published` | 已公开，有作品链接（知乎、B站动态） |
| `submitted` | 平台已接收，可能在审核，链接可能没有 |
| `scheduled` | 已设置定时发布 |
| `draft` | 只存了草稿（知乎 `draft: true`） |
| `dry_run` | 只做了本地校验 |
| `skipped` | 平台不支持该内容类型，或同一内容已发过 |
| `needs_action` | 需要人处理（见错误码） |
| `failed` | 失败 |

### PlatformPost（validate 的 `posts[]`）

```json
{"platform": "bilibili", "account": "main", "kind": "video", "title": "...", "body": "...", "tags": ["科技"],
 "video": "D:\\posts\\ep12\\video.mp4", "cover": null, "images": [], "schedule": "2026-10-25 20:00",
 "ai_generated": false, "options": {"tid": 231, "copyright": 1}, "source": "D:\\posts\\ep12\\post.yaml"}
```

已合并顶层字段和 `platforms.<平台>` 覆盖；路径已解析成绝对路径；`options` 是平台私有参数。

## 事件（`--json` 时在 stderr，每行一个 JSON）

所有事件都有 `event`、`platform`、`account`。

| event | 其他字段 | 含义 / 怎么做 |
|---|---|---|
| `log` | `level`（`info`/`warning`/`debug`）、`message` | 进度。`debug` 是 worker 的非 JSON 输出，可忽略 |
| `qrcode` | `path`、`note` | 登录二维码图片已生成或刷新（`<数据目录>\qrcode\<平台>-<账号>.png`）。展示给用户扫码；`note` 里有提示（如抖音没定位到二维码时这是整页截图） |
| `needs_input` | `kind`（目前只有 `sms_code`）、`message`、`path` | 抖音发布需要短信验证码：向用户要验证码，运行 `mpub verify-code douyin <验证码>`（或写入 `path`）；默认等 300 秒 |
| `submit_started` | — | 即将/已经点击发布。此后该平台的任何失败都不能自动重试 |

不加 `--json` 时这些事件以中文提示行显示在 stderr。

## 错误码与处理

`error_code` 出现在 `needs_action`、`failed`、`skipped` 的结果里。

| error_code | 常见 status | 含义 | 怎么办 |
|---|---|---|---|
| `login_required` | needs_action | 没有登录文件、登录失效、中途被踢回登录页 | `mpub login <平台> --account <账号>`，成功后只重发该平台（`--to`） |
| `sms_code_required` | needs_action | 抖音短信验证码没在时限内提交，或 3 轮后仍未通过 | 发布进行中看到 `needs_input` 时就要验证码；已结束：`submit_started: false` 可重发，`true` 先去后台确认 |
| `captcha` | needs_action | 页面停在人机验证（滑块、极验） | `submit_started: false`：加 `--headed` 重发，让用户在窗口里过验证；否则先去后台确认 |
| `risk_control` | needs_action | 风控拦截：知乎 40352 / 429、B站 412 / -352 | 知乎：`mpub login zhihu --account <账号>`（有头）打开验证页完成验证；B站：别用 `mode: http`，稍后 `--headed` 重试；降低频率 |
| `consent_required` | needs_action | B站动态需要确认《动态发布规范》，无头模式下发布前就停了（什么都没发） | 让用户选：阅读规范后在清单写 `platforms.bilibili.confirm_rules: true` 或 config `[platforms.bilibili] confirm_dynamic_rules = true`；或 `--headed` 重发并亲手点确认 |
| `unknown_outcome` | failed | 已点发布，但超时 / 断网 / 进程异常，结果未知 | **绝不重试**。请用户去平台后台确认；确认没发出且用户要求时才 `--force` |
| `platform_error` | failed | 平台明确拒绝（内容违规、参数不对、账号限制等），或上传失败 | 看 `message`（附平台原文）。`submit_started: true` 时先去后台确认；修改内容后由用户决定是否重发 |
| `timeout` | failed | 发布动作之前某一步超时（页面改版、网络慢、上传太久） | 可在用户同意后重发；大文件调大 config `[timeouts] publish_video`（抖音/小红书的上传等待由它推算）并配合 `--timeout`；可加 `--headed` 观察 |
| `validation` | failed / skipped | `failed`：发布前校验失败、抖音自主声明文字与页面不符、正文图片找不到。`skipped`：查重命中（同一内容已发过） | `failed`：改清单后重新 validate；`skipped`：告诉用户已经发过，用户明确要求才 `--force` |
| `unsupported` | skipped | 平台不支持该内容类型 | 无需处理（知乎不支持视频；B站/抖音/小红书不支持文章） |
| `internal` | failed | 环境问题或程序错误：缺 biliup、worker 崩溃、账号被另一个任务占用 | 看 `message` 和 `evidence` 里的日志：缺 biliup → `mpub setup`；「正被另一个任务使用」→ 等它结束；其余报告问题 |

## 平台后台（确认是否已发出）

| 平台 | 地址 |
|---|---|
| B站 | 视频：<https://member.bilibili.com/>（创作中心 → 稿件管理）；动态：<https://t.bilibili.com/> |
| 抖音 | <https://creator.douyin.com/creator-micro/content/manage>（作品管理） |
| 小红书 | <https://creator.xiaohongshu.com/>（笔记管理） |
| 知乎 | <https://www.zhihu.com/creator>（创作中心 → 内容管理；草稿在草稿箱） |

## 超时默认值

| 动作 | 默认（秒） | 改法 |
|---|---|---|
| login | 300 | config `[timeouts] login` |
| check | 180 | config `[timeouts] check` |
| publish 视频 | 3600 / 平台 | `--timeout` 或 config `[timeouts] publish_video` |
| publish 图文 / 文章 | 900 / 平台 | `--timeout` 或 config `[timeouts] publish_note` / `publish_article` |
| 平台之间间隔 | 30 | `--gap` 或 config `gap_seconds` |
