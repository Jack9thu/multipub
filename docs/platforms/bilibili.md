# B站（bilibili）

实现：`multipub/platforms/bilibili.py`（适配器）+ 自有辅助模块：

| 模块 | 作用 |
|---|---|
| `bili_biliup.py` | biliup 命令行的下载/校验/调用、上传参数拼接、输出解析 |
| `bili_cookies.py` | 登录文件（biliup 格式）的读取、转换为浏览器 cookie、原子写入 |
| `bili_http.py` | WBI / APP 签名、nav、TV 扫码登录、图文动态的 HTTP 接口（自研，未用 GPL 的 bilibili-api） |
| `bili_note.py` | 图文动态的浏览器发布（t.bilibili.com 动态发布框） |

视频由 [biliup](https://github.com/biliup/biliup)（MIT）上传并投稿；图文是 **B站动态（opus）**，默认用浏览器在
`https://t.bilibili.com/` 的发布框里发，可选实验性的 HTTP 接口模式。

## 支持的内容类型

| kind | 支持 | 说明 |
|---|---|---|
| `video` | ✔ | 标题、简介、标签、分区、封面、自制/转载、定时、粉丝动态文案、创作声明 |
| `note`（图文动态） | ✔ | 1–9 张图、标题（选填 ≤20 字）、正文（≤1000 字，含 `#标签#`）、定时（5 分钟~7 天）、AI 声明、图片排版 |
| `article` | ✘ | 自动跳过（专栏编辑器 read-editor 未接入） |

## 清单字段

通用字段（`title` / `body` / `tags` / `video` / `cover` / `images` / `schedule` / `ai_generated` / `account`）
可写在顶层，也可在 `platforms.bilibili` 下覆盖。下表是 `platforms.bilibili` 下的平台私有参数
（写错名字会在 validate 时给出 warning）：

### 视频（kind: video）

| 参数 | 默认 | 说明 → biliup 参数 |
|---|---|---|
| `tid` | **必填** | 分区 id → `--tid`。例：231 计算机技术、21 日常、160 生活、95 数码、201 科学科普、138 搞笑、36 知识。2025 年 B站改版分区后部分旧 id 可能失效，以创作中心为准 |
| `tid_v2` | 不传 | 新版分区 id → `--tid-v2`（biliup v1.3.1 新增；不设置时不提交该字段） |
| `copyright` | `1` | 1 自制 / 2 转载 → `--copyright` |
| `source` | 无 | 转载来源，`copyright: 2` 时必填 → `--source` |
| `dynamic` | 无 | 投稿同时发的粉丝动态文案 → `--dynamic` |
| `creation_statement` | `ai_generated: true` 时为 `1`，否则不传 | 创作声明 id：-1 无 / 1 AI / 2 虚构演绎 / 3 营销 / 4 个人观点，经 `--extra-fields '{"creation_statement":{"id":N}}'` 提交（用法来自 OmniPost，**未验证**） |
| `no_reprint` | `false` | `true` → `--no-reprint=1`（禁止转载） |
| `is_only_self` | `false` | `true` → `--is-only-self=1`（仅自己可见） |
| `selection_reply` / `close_reply` / `close_danmu` | `false` | 精选评论 / 关闭评论 / 关闭弹幕 → `--up-selection-reply` 等（biliup 注明仅 app 投稿接口有效，默认就是 app） |
| `extra_fields` | 无 | 映射，原样 JSON 合并进投稿参数 → `--extra-fields`（高级用法） |
| `line` | 自动探测 | 上传线路：`bldsa cnbldsa andsa atdsa bda2 cnbd anbd atbd tx cntx antx attx txa alia estx akbd` → `--line` |
| `limit` | 3（biliup 默认） | 单文件并发分片数 → `--limit` |
| `submit` | `app`（biliup 默认） | 投稿接口：`app` / `web` / `b-cut-android` → `--submit` |

其他映射：`title` → `--title`；`body` → `--desc`（**为空时用标题**，B站要求简介非空）；`tags` → `--tag a,b`；
`cover` → `--cover`；`schedule` → `--dtime <本地时间转成的 epoch 秒>`。

```yaml
platforms:
  bilibili:
    tid: 231
    copyright: 1
    # source: https://...        # copyright: 2 时必填
    # dynamic: 新视频上线啦
    # creation_statement: 1      # 或者顶层 ai_generated: true
```

### 图文（kind: note）

| 参数 | 默认 | 说明 |
|---|---|---|
| `mode` | `browser` | `browser`：浏览器在 t.bilibili.com 发布框里发（推荐）；`http`：直接调 Web 接口（实验性，见下） |
| `pic_mode` | 0 | 0 九宫格（图在正文下方）/ 1 大封面（图在正文上方） |
| `tags_in_text` | `true` | 把 `tags` 以 `#标签#`（B站话题写法）追加到正文末尾；正文里已经有的不重复加 |
| `confirm_rules` | 不写 | `true`：本条动态允许 multipub 代为确认《动态发布规范》弹窗（见下文「动态发布规范」）；`false`：本条不代为确认（优先于 config.toml 和确认记录）；不写则看 config.toml / 确认记录 |

`cover` 对动态无效（warning 后忽略）。

### config.toml（`[platforms.bilibili]` 与全局项）

| 键 | 默认 | 含义 |
|---|---|---|
| `biliup_version` | `v1.3.1` | 使用的 biliup 版本（安装目录 `<数据目录>/tools/biliup/<版本>/`） |
| `biliup_path` | 空 | 指定已有的 biliup 可执行文件，优先于自动安装；`mpub setup` 不会动它 |
| `github_mirror`（全局） | 空 | 下载 biliup 时拼在 GitHub URL 前，例如 `https://ghfast.top/` |
| `[timeouts] publish_video` | 3600 | 单次视频发布总超时；biliup 会在此值 − 60 秒时被杀掉 |
| `confirm_dynamic_rules` | `false` | `true`：阅读过 [《动态发布规范》](https://t.bilibili.com/h5/dynamic/specification) 后，允许 multipub 在浏览器模式里代为确认该弹窗（对所有账号生效） |

## biliup 的安装与校验（`mpub setup` / `mpub doctor`）

- 只有 `mpub setup` 会下载；已安装的版本**永不自动更新**，换版本需要改 `biliup_version` 再 `mpub setup`。
- 下载地址：`https://github.com/biliup/biliup/releases/download/<版本>/<文件>`（可加镜像前缀）。按系统选文件：

  | 系统 | 文件 |
  |---|---|
  | Windows x64（ARM64 也用它，系统转译） | `biliupR-v1.3.1-x86_64-windows.zip` |
  | Linux x86_64 / aarch64 / armv7 | `biliupR-v1.3.1-{x86_64,aarch64,arm}-linux.tar.xz` |
  | macOS Intel / Apple Silicon | `biliupR-v1.3.1-{x86_64,aarch64}-macos.tar.xz` |

- **sha256**：v1.3.1 七个发布包的 sha256 已内置（2026-10-20 两次独立下载核对；这七个文件名在 `releases/download/v1.3.1/` 下都存在，没有 Windows ARM64 / 32 位包），不一致直接删除、拒绝安装。
  其他版本是 **trust-on-first-use**：首次下载时记录，不做比对。记录写在二进制旁边：
  `install.json`（版本、URL、压缩包 sha256、二进制 sha256、`verified: pinned|tofu`）和 `<压缩包名>.sha256`。
  请自行与 GitHub 发布页核对 TOFU 的值。

  | 文件 | sha256 |
  |---|---|
  | `biliupR-v1.3.1-x86_64-windows.zip` | `b177488823204e32db1fdb2a81c6bfcc4ffc58b2d47b14831ead4f83adc6401f` |
  | `biliupR-v1.3.1-x86_64-linux.tar.xz` | `e55cdefc66606fb326c738be45e5740b575c21e9ad9ed7c12f83897c8dd683d3` |
  | `biliupR-v1.3.1-x86_64-linux-musl.tar.xz` | `5ca2146a97061a478eb537569dea7fb84dc8bbcba77c32da6fa7d46409d8ae45` |
  | `biliupR-v1.3.1-aarch64-linux.tar.xz` | `b2526e8d7ea2d4689e77226b3fcbbff9c3506a6aa7e5d00baeddd87977ce72c3` |
  | `biliupR-v1.3.1-arm-linux.tar.xz` | `b027127a71f9d03c57a6543ffc8f521b5d78239a39fa90e4e0e84a852d7194d9` |
  | `biliupR-v1.3.1-x86_64-macos.tar.xz` | `bd46266cdf8b7fdd47a4c9a933f7e39c66edc1f898acb99de8fa66fc1457163d` |
  | `biliupR-v1.3.1-aarch64-macos.tar.xz` | `443b5e07f4b6eead71a6fad5a0b103e6959e23a10baafe343b5a40f36a97d594` |

- 只从压缩包里解出 `biliup` / `biliup.exe` 本身（按文件名匹配，忽略包内路径），先写临时文件再原子替换。
- `mpub doctor`：报告安装路径、版本、压缩包 sha256；若二进制与安装时记录的 sha256 不一致则报错。
  使用 `biliup_path` 时会运行一次 `biliup --version`（纯本地；注意不带参数的 biliup 会启动 Web 服务，我们从不这样调用）。

## validate() 检查的限制

| 项 | 规则 | 级别 |
|---|---|---|
| 视频标题 | 必填，≤80 字 | error |
| 视频简介 | ≤2000 字；为空时用标题代替 | error / warning |
| 视频标签 | 至少 1 个、最多 12 个，每个 ≤20 字，不能含逗号（biliup `--tag` 用逗号分隔） | error |
| `tid` | 必填，1–65535 的整数（也接受数字字符串） | error |
| `copyright` / `source` | 只能 1 或 2；2 必须有 `source` | error |
| `creation_statement` | -1/1/2/3/4 | error |
| `ai_generated: true` 且未写 `creation_statement` | 提示将以 id=1 提交（未验证） | warning |
| `line` / `submit` / `limit` | 见上表取值；`limit` 1–64 | error |
| 视频格式 | `.mp4 .flv .avi .wmv .mov .webm .mpeg .mpg .mkv .m4v .ts .rm .rmvb .3gp` 以外 | warning |
| 封面 | 只能 jpg/png；>5MB | error / warning |
| 视频定时 | 晚于现在 ≥2 小时、≤15 天；不足 2.5 小时给 warning（上传耗时会吃掉余量） | error / warning |
| 动态标题 | ≤20 字 | error |
| 动态正文 | 正文 + 追加的 `#标签#` ≤1000 字（超过时网页会弹「转为专栏」） | error |
| 动态图片 | ≤9 张（至少 1 张由通用检查保证）；jpg/jpeg/png/gif；单张 ≤20MB | error |
| 动态定时 | 晚于现在 ≥5 分钟、≤7 天（发布器日期控件的范围）；不足 10 分钟给 warning | error / warning |
| `mode` / `pic_mode` | `browser`/`http`；0/1 | error |
| `confirm_rules` / `tags_in_text` | 必须是 true/false | error |
| 未知的 `platforms.bilibili.*` 参数 | 列出支持的参数 | warning |

`publish()` 开始时会再跑一遍 validate（定时窗口可能在等待期间过期）。

## 登录

```
mpub login bilibili --account main
```

- **方式：TV 端扫码**（`passport.bilibili.com/x/passport-tv-login/qrcode/{auth_code,poll}`，云视听小电视 appkey），
  与 biliup 自己的「扫码登录」是同一套接口。二维码 PNG 写到 `<数据目录>/qrcode/bilibili-<账号>.png`，
  通过 `qrcode` 事件交给 CLI / Claude Code 展示；手机 B站 App 扫码后会显示「TV 端登录」确认。
  二维码 3 分钟过期，过期自动刷新（最多 3 次）；整体等待上限为 `[timeouts] login` − 45 秒（默认 255 秒）。
  不需要终端、不需要浏览器，所以能在 Claude Code 里直接跑（`interactive_login = False`）。
- **为什么不用浏览器登录**：biliup 上传前会用 `token_info.access_token` 调 `oauth2/info` 校验并按需刷新，
  `LoginInfo` 里 `token_info` 是必填字段，投稿（默认 APP 接口 `x/vu/app/add`）也用 `access_key` 签名。网页登录只有 cookie、
  没有 token，biliup 读不了。biliup 自己的「网页Cookie登录」其实也是拿 SESSDATA + bili_jct 去确认一张 TV 二维码
  （`x/passport-tv-login/h5/qrcode/confirm`）换 TV token——既然最终都要 TV token，直接让用户扫 TV 码最简单。
  另外按 v1.3.1 源码，biliup 的扫码登录轮询遇到 86090（已扫未确认）会直接报错退出；我们的实现会继续等待确认。
- 结果写成 biliup 的 `cookies.json` 格式：`<数据目录>/accounts/bilibili/<账号>.json`
  （`cookie_info` / `sso` / `token_info` / `platform: "BiliTV"`），UTF-8 **无 BOM**（biliup 的 serde_json 不认 BOM）、
  原子替换、POSIX 下权限 600。视频（biliup `-u`）和图文（内存里转成浏览器 cookie）共用这一份。
- 已有有效登录（nav 已登录 **且** biliup 令牌校验通过）时 `login` 直接返回「无需重新扫码」；要换号先删除该文件。
  只要其中一项不通过就重新扫码，避免「发布说要登录、登录说已登录」的死循环。
- **备选：biliup 交互式登录**。需要真实终端（Windows Terminal 等），在账号目录里运行：

  ```
  "<biliup 路径>" -u "<数据目录>\accounts\bilibili\main.json" login
  ```

  选「扫码登录」；终端二维码显示不全时打开同目录的 `qrcode.png`。适配器里有 `run_interactive_login()` 封装
  （检查 TTY、cwd 设为账号目录），但目前 CLI 只在 `interactive_login = True` 时调用它，见下方「需要的核心改动」。
- **绝不从浏览器回写登录文件**：图文的浏览器模式只在内存里把 cookie 转成 `storage_state`
  （`http_only`/`secure` 的 0/1 转 bool，域 `.bilibili.com`，`sameSite: Lax`），不调用 `context.storage_state(path=...)`，
  否则会丢掉 `token_info`。

### 续期

- `mpub check` **不**跑 `biliup renew`（renew 会重写整个文件，check 应该是只读的）。
- 上传视频时 biliup 自己会先校验 token，需要时刷新并重写文件，不用我们管。
- 发图文前先调 nav；如果网页登录态失效但文件里有 token 且装了 biliup，会自动 `biliup -u <文件> renew` 一次再试。

## check

读取登录文件（`utf-8-sig`），`GET https://api.bilibili.com/x/web-interface/nav` 带 cookie：

- 文件不存在 / 无法解析 / 缺 `SESSDATA` 或 `bili_jct` → `needs_action`（`login_required`），提示 `mpub login bilibili --account <名字>`
- 缺 `token_info`（不是扫码登录生成的）→ `login_required`（biliup 无法上传视频）
- `data.isLogin` 为 false → `login_required`；HTTP 412 → `needs_action`（`risk_control`）
- 再做一次与 biliup 上传前完全相同的**只读**令牌校验：`GET passport.bilibili.com/x/passport-login/oauth2/info`
  （`access_key` + appkey 签名；`platform: BiliTV` 用 TV appkey，`Android`/缺省用 Android appkey）。
  不通过 → `login_required`；`data.refresh` 为 true 时提示「快到期，下次上传时 biliup 会自动续期」
- 正常 → `ok`，消息含昵称、UID、令牌状态、cookie 剩余天数（<15 天时提示）

## 发布流程与返回

### 视频

1. 再次 validate；确认登录文件有 `token_info`、找到 biliup（否则 `internal`：先 `mpub setup`）。
   再做一次与 biliup 相同的只读令牌校验（`oauth2/info`）：令牌失效 → 启动 biliup **之前**就返回 `login_required`；
   网络错误 / 风控只记警告，交给 biliup 自己校验。
2. 拼命令：`biliup --rust-log info -u <登录文件> upload --title=… --desc=… --tid=… --tag=… --copyright=… [...] -- <视频>`。
   全局参数写在子命令前；字符串参数都用 `--opt=value` 形式，避免以 `-` 开头的标题被当成参数；`--` 之后才是视频路径。
   设置 `NO_COLOR=1`，输出里残留的 ANSI 码也会被剥掉。
3. **启动 biliup 前调用 `mark_submit_started()`**（上传和投稿是一条命令）。之后的任何失败都不会自动重试。
4. 逐行读取 stdout+stderr：全部写入 worker 日志；`user:`、`Upload completed`、`投稿成功`、错误行低频转发；
   60 秒没有输出时发一条「上传中……已用时 N 分钟」。超时（`publish_video` − 60 秒）杀进程。
5. 解析输出：投稿响应 `ResponseData { code: 0, data: Some(Object {"aid": Number(…), "bvid": String("BV…")}) … }`
   里取 BV 号 / aid。

| 情况 | 状态 | URL / 说明 |
|---|---|---|
| 投稿成功（看到 code 0 的响应或「投稿成功」） | `submitted`（有定时则 `scheduled`） | `https://www.bilibili.com/video/<BV>`（取不到 BV 时用 `av<aid>`）；审核通过前页面可能显示审核中 |
| biliup 退出码 0 但没解析到结果 | `submitted` | 无链接，提示去创作中心确认 |
| 投稿接口明确拒绝（`ResponseData { code: 非0 … }`） | `failed` / `platform_error` | 带 B站的 code 和 message |
| 投稿接口返回服务端错误码 -500/-502/-503/-504 | `failed` / `unknown_outcome` | 稿件可能已建好，先去创作中心确认 |
| 视频已传完（看到 `Upload completed` / `All files uploaded successfully`）后失败或超时 | `failed` / `unknown_outcome` | 先去创作中心确认，不要重发 |
| 上传阶段失败 / 超时（还没传完） | `failed` / `platform_error` 或 `timeout` | 尚未投稿；因为已标记 submit_started，重发需要 `--force` |
| 输出里是登录错误（`credential validation failed`、`账号未登录` 等）且没传完 | `needs_action` / `login_required` | 重新登录 |

> 取舍：按约定在启动 biliup 前就标记 submit_started，所以「上传阶段就失败」也会被查重记录为结果未知，
> 再发需要 `--force`。错误消息里会写明「尚未投稿」。

### 图文（动态）

正文 = `body` + 末尾一行 `#标签1# #标签2#`（`tags_in_text: false` 关闭）。先 `GET finger/spi`（只读）补
`buvid3`/`buvid4`（biliup 的 cookie 里没有，缺了风控分更高；拿不到不影响继续），再 nav 确认登录。

**浏览器模式（默认）**：patchright 打开 `https://t.bilibili.com/`（`--headed` 可看到窗口）：

1. 等 `.bili-dyn-publishing`；被重定向到 passport 或看到登录按钮 → `login_required`。
   接着检查《动态发布规范》的同意状态（见下文），无头且未同意时在这一步就停下。
2. 标题填 `input.bili-dyn-publishing__title__input` 并回读校验；正文点击 `.bili-dyn-publishing__input [contenteditable]`
   后逐行 `insert_text`（行间按 Enter），回读首行校验。
3. 点图片工具 `.bili-dyn-publishing__tools__item.pic`，点 `.bili-pics-uploader__add` 触发文件选择框
   （上传控件是脚本动态创建的 `<input type=file>`，不在 DOM 里），`set_files`。
   以监听到的 `upload_bfs` 成功响应数为准等待全部传完（60 秒 + 每张 30 秒）；出现 `.bili-pics-uploader-item-error` 立即失败。
4. 定时 / AI 声明 / 图片排版：发布器的日期时间选择器、级联菜单是自绘组件，没有稳定的 DOM 可点，
   改为在页面主 world 里找到发布器的 Vue 2 组件（元素上的 `__vue__`）直接设置
   `tools.timingSetting`（`active`、`datetime` 毫秒）、`creationOption.value = 1`、`picModeOption.value`。
   定时设置后检查 `.bili-dyn-publishing__datetime` 已显示，否则失败（不会悄悄变成立即发布）；
   账号没有 AI 声明选项时只告警。点击前再次确认定时仍晚于现在 5.5 分钟（否则发布器会弹「直接发布？」）。
5. 等「发布」按钮 `.bili-dyn-publishing__action.launcher` 可用，**调用 `mark_submit_started()` 后点击**。
6. 监听 `/x/dynamic/feed/create/submit_check` 与 `/x/dynamic/feed/create/dyn` 的响应（60 秒）：
   create/dyn `code 0` → `data.dyn_id_str`；submit_check 非 0 → 明确失败（发布请求未发出）；
   出现极验验证码 → `needs_action`（`captcha`）。失败时截图 `bilibili-note-error.png`，成功后截图 `bilibili-note-done.png`。

#### 动态发布规范（确认弹窗）

发布器脚本里，**立即发布**前会检查 cookie `dy_spec_agreed`（`.bilibili.com`，网页端同意后写入，有效期一年）：
不是 `1` 就弹出 `.bili-dyn-specification-popup`（内嵌 `https://t.bilibili.com/h5/dynamic/specification`），
点「确认并发送」才会发出请求，点「取消」则不发。multipub 每次都用新的浏览器上下文（cookie 来自 biliup 登录文件，
不回写），所以**每次都会遇到这个弹窗**。这相当于替你同意一份平台规范，multipub 不会擅自代点：

- 已同意：config.toml `[platforms.bilibili] confirm_dynamic_rules = true`，或清单 `platforms.bilibili.confirm_rules: true`，
  或之前在有头模式里亲手点过确认（记录在 `<数据目录>/accounts/bilibili/<账号>.dy_spec_agreed`，删除即撤销）。
  此时浏览器上下文直接带上 `dy_spec_agreed=1`，不再弹窗；万一仍弹出，则代为点击「确认并发送」并写日志。
- 未同意、无头（默认）：打开发布框、**传图之前**就返回 `needs_action`（`error_code: validation`），提示上面两种做法；
  此时没有标记 submit_started，什么都没发。
- 未同意、`--headed`：照常填写并点击发布，弹窗出现后等你在窗口里点（最多 180 秒）；你确认并发出后写入上面的记录文件。
  你点了取消（弹窗关闭 8 秒内没有发布请求）或超时 → `failed`，消息写明「动态没有发出」。
- 定时发布走发布器的另一条分支，不会弹这个框；HTTP 模式没有这个弹窗。
- 清单里写 `confirm_rules: false` 时，本条忽略 config.toml 和确认记录，按「未同意」处理。

**HTTP 模式（`mode: http`，实验性）**：

1. nav 取 `mid` 和 WBI 密钥（`wbi_img.img_url` / `sub_url` 文件名）。
2. 每张图 `POST /x/dynamic/feed/draw/upload_bfs`（multipart：`file_up`、`biz=dyn`、`category=daily`、`csrf=bili_jct`）。
3. `POST /x/dynamic/feed/create/submit_check?csrf=…`（与网页端一致的预检查）。
4. **`mark_submit_started()`**，然后 `POST /x/dynamic/feed/create/dyn?platform=web&csrf=…&x-bili-device-req-json=…&dm_img_*…&w_dyn_req.upload_id=…&w_dyn_req.meta=…&wts=…&w_rid=…`，
   JSON：`{"dyn_req": {"content": {"contents": [{"raw_text": 正文, "type": 1, "biz_id": ""}], "title": 标题},
   "pics": [{img_src, img_width, img_height, img_size, ai_gen_pic}], "scene": 2, "upload_id": "<mid>_<秒>_<0-9999>",
   "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
   "option": {"aigc": 1|2, "pic_mode": 0|1, "timer_pub_time": 秒（定时才有）}}}`。
   与网页端一致：`img_size` 是本地文件大小 / 1024（KB，浮点），`ai_gen_pic` 取 upload_bfs 的返回值。
5. URL 参数照网页端 create/dyn 的中间件链拼：METADATA 的
   `x-bili-device-req-json={"platform":"web","device":"pc","spmid":"333.1365","mobi_app":"web_cn"}`（submit_check 也带，不含 mobi_app）；
   RISK_USER_LOG 的 `dm_img_list=[]`、`dm_img_inter`（无鼠标轨迹）和固定的 `dm_img_str`/`dm_cover_img_str`；
   WBI 中间件的 `bodyPayload`：把 body 里的 `upload_id`、`meta` 以 `w_dyn_req.` 前缀拷进 URL 一起签名。
6. WBI 签名自己实现（照网页端 `um()`）：`img_key+sub_key` 按固定 64 位表重排取前 32 位为 mixin_key；参数加 `wts` 后按键排序，
   跳过空值，**只对字符串**去掉 `!'()*`，按 encodeURIComponent 规则编码（空格是 `%20`），`w_rid = md5(query + mixin_key)`。
   网页端对对象参数直接 `encodeURIComponent(obj)`，所以 `w_dyn_req.meta` 按 `"[object Object]"` 参与签名，
   URL 里发送的却是 JSON（请求库 `JSON.stringify`）——这里照做。测试用 WBI 文档的示例向量（mixin_key `ea1db124…`、
   w_rid `8f6f2b5b…`）和一个独立的直白实现交叉校验。
7. 网页端还会带 `b_wet`（WASM 生成的环境令牌）和 `x-bili-locale-json`，这里无法提供，
   所以可能被风控。HTTP 412 或 `code -352` → `needs_action`（`risk_control`），提示改用浏览器模式。

| 情况 | 状态 | URL / 说明 |
|---|---|---|
| 成功 | `published`（有定时则 `scheduled`） | `https://www.bilibili.com/opus/<dyn_id_str>`；定时的在发布前打不开 |
| `code 4126021` | `failed` / `platform_error` | 账号没绑定手机，不能发动态 |
| `code -101` / `-111` | `needs_action` / `login_required` | 重新登录 |
| 412 / `-352` | `needs_action` / `risk_control` | 浏览器模式里出现时，请先到动态页确认再用 `--headed` 重试 |
| 发布请求网络错误 / 非 JSON / 服务端错误码 -500/-502/-503/-504 / 60 秒无结果 / 成功但没有 id | `failed` / `unknown_outcome` | 先到动态页确认，不要直接重发 |
| 传图失败、submit_check 不通过 | `failed` / `platform_error` | 发布请求没有发出 |
| 浏览器模式未同意《动态发布规范》（无头） | `needs_action` / `validation` | 发布前就停下，什么都没发；见「动态发布规范」 |

## 已知风险

- **法律/封禁风险**：B站已通过律师函关停 bilibili-API-collect（2026-01）和 bilibili-api（2026-07），
  函件点名「绕过反爬风控与签名鉴权」。HTTP 模式自己实现了 WBI 签名并带固定的 `dm_img_*` 值，属于这一类，
  所以默认用浏览器模式。TV 扫码登录使用公开的云视听小电视 appkey（与 biliup 相同）。
- 无头 Chromium 可能更容易触发风控；需要时加 `--headed`，或在 config.toml 的 `[browser]` 里设 `channel = "chrome"`。
- biliup 的 `--dtime` 注释写「距离提交大于 4 小时」，而创作中心是 2 小时；介于 2~4 小时的定时可能被拒（会得到明确的 `platform_error`）。
- 视频上传后投稿前若网络中断，结果未知；B站对重复投稿有限制，务必先去创作中心确认。
- biliup 会把上传断点（`biliup_checkpoint_*.json`）放在系统本地数据目录（Windows 为 `%LOCALAPPDATA%`），同一账号同一文件失败后用 `--force` 重发会跳过已传完的部分。
- 动态正文里的 `@某人` 会作为纯文本发送（不转成 @ 提及）；`#标签#` 在网页发布器里输入时可能弹出话题联想框。
- 账号需要绑定手机才能发动态；新账号可能还有转正答题等限制。

## 未在真实账号上验证

本仓库没有 B站账号，以下全部**未在真实账号上验证**。置信度：高 = 源码/官方脚本/文档多方一致；
中 = 来自线上脚本的类名或单一来源；低 = 推测或 multipub 新增的做法。

| 项 | 选择器 / 接口 / 做法 | 置信度 |
|---|---|---|
| biliup v1.3.1 命令行参数 | `-u`、`--rust-log`、`upload --title --desc --tid --tid-v2 --tag --cover --dtime --copyright --source --dynamic --no-reprint --is-only-self --up-*-reply --up-close-danmu --extra-fields --line --limit --submit`、`renew`、`login`（对照 v1.3.1 源码 `cli.rs` / `Studio`） | 高 |
| biliup 发布包文件名与 sha256 | 见上表（实际下载核对） | 高 |
| 投稿成功输出 | tracing 日志 `ResponseData { code: 0, data: Some(Object {"aid": Number(..), "bvid": String("BV..")}) … }`、`APP接口投稿成功`（据源码 `info!("{:?}", ret)` 推断格式，未实际运行） | 中 |
| 上传完成标记 | `Upload completed:`、`All files uploaded successfully, checkpoint removed` | 中高 |
| 登录错误标记 | `credential validation failed`、`credential refresh failed`、`open cookies file`、`账号未登录` | 中 |
| TV 扫码登录 | `x/passport-tv-login/qrcode/auth_code` 与 `poll`，appkey `4409e2ce8ffd12b8`；签名用文档示例向量校验过；poll 码 0/86038/86039/86090 | 高 |
| 扫码结果可被 biliup 直接使用 | 写入字段与 biliup `LoginInfo` 一致（`cookie_info`、`sso`、`token_info{access_token,expires_in,mid,refresh_token}`、`platform: BiliTV`） | 中高 |
| 登录检查 | `GET /x/web-interface/nav` → `data.isLogin`、`uname`、`mid`、`wbi_img` | 高 |
| biliup 令牌校验 | `GET passport.bilibili.com/x/passport-login/oauth2/info`（参数与签名照 biliup `validate_tokens`）→ `data.mid`、`refresh` | 中高 |
| buvid | `GET /x/frontend/finger/spi` → `data.b_3` / `b_4` | 高 |
| 视频创作声明 | `--extra-fields '{"creation_statement":{"id":1}}'`（来自 OmniPost，B站侧字段名未确认） | 低 |
| 动态发布框 | `.bili-dyn-publishing`、`input.bili-dyn-publishing__title__input`、`.bili-dyn-publishing__input [contenteditable]` | 中 |
| 图片上传 | `.bili-dyn-publishing__tools__item.pic`、`.bili-pics-uploader__add` + 文件选择框、`.bili-pics-uploader-item-error(__msg)` | 中 |
| 发布按钮 | `.bili-dyn-publishing__action.launcher`（禁用时带 `disabled` 类） | 中 |
| 定时 / AI 声明 / 排版 | 主 world 里改 Vue 组件状态：`tools.timingSetting{visible,active,data.datetime}`、`creationOption{list,value}`、`picModeOption.value` | 中低 |
| 发布结果 | 监听 `/x/dynamic/feed/create/dyn` 响应 `data.dyn_id_str`；`/create/submit_check` 预检查 | 中高 |
| 验证码检测 | `.geetest_panel`、`.geetest_box`、`iframe[src*='geetest']`、`.bili-mini-mask` | 低 |
| 动态发布规范弹窗 | cookie `dy_spec_agreed=1` 时不弹（发布器 `_checkSpecification` 源码）；弹窗 `.bili-dyn-specification-popup`、按钮 `.bili-dyn-specification-popup__btn` 文本「确认并发送」；定时发布不弹 | 高（逻辑）/ 中（选择器） |
| HTTP 模式附加参数 | `x-bili-device-req-json`（spmid 取自页面 `<meta name="spm_prefix" content="333.1365">`）、`w_dyn_req.upload_id` / `w_dyn_req.meta` 及其签名方式（来自发布器中间件源码） | 中 |
| 登录入口检测 | `.header-login-entry`、`.bili-header .login-btn` | 低 |
| HTTP 模式各接口字段 | `upload_bfs`（`file_up`/`biz`/`category`/`csrf`）、`create/dyn` 的 `dyn_req` 结构、错误码 4126021 / -101 / -352 | 高（字段）/ 低（能否通过风控） |
| 动态链接格式 | `https://www.bilibili.com/opus/<dyn_id_str>` | 高 |

## 需要的核心改动（未做，待合入）

- `models.ErrorCode` 增加 `CONSENT_REQUIRED = "consent_required"`（需要用户本人同意平台规则/协议）。目前「动态发布规范」未同意时
  用 `needs_action` + `validation` 代替，避免被误读成登录过期或验证码。
- `mpub login bilibili --interactive`：在 CLI 里提供调用 `run_interactive_login()` 的入口（biliup 交互式登录兜底）。
- 把 worker 的 `timeout` 放进 `JobContext`，让 biliup 的内部超时跟随 `mpub publish --timeout`（目前只跟随 `[timeouts] publish_video`；
  `--timeout` 更短时由 worker 取消协程，biliup 进程同样会被杀掉）。
