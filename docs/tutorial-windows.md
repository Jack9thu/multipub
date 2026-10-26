# multipub Windows 使用教程（从零开始）

这份教程面向**不写代码的创作者**：照着做，就能在 Windows 电脑上用一条命令把视频、图文、文章发到
B站、抖音、小红书、知乎，也可以交给 Claude Code 帮你操作。

> **先读这一段**
> - multipub 用浏览器自动化和平台网页接口发布，**会真实地发到你的账号上**。
> - 代码有完整的离线测试和本地仿真测试；真实账号上只验证过基本发布和合集（见 [README](../README.md) 开头），**其余功能没有逐项验证**。请先用**小号**试，确认没问题再用主号。
> - 各平台都有风控：控制发布频率（同一账号一天几条以内），不要批量刷内容，遵守平台规则。

目录

1. [准备工作](#1-准备工作)
2. [安装 multipub](#2-安装-multipub)
3. [浏览器和 biliup](#3-浏览器和-biliup)
4. [登录四个平台](#4-登录四个平台)
5. [第一次发布](#5-第一次发布)
6. [各平台注意事项速查](#6-各平台注意事项速查)
7. [发布中需要你处理的情况](#7-发布中需要你处理的情况)
8. [配合 Claude Code 使用](#8-配合-claude-code-使用)
9. [定时发布与自动发布](#9-定时发布与自动发布)
10. [常用配置 config.toml](#10-常用配置-configtoml)
11. [常见问题](#11-常见问题)
12. [升级、卸载与数据清理](#12-升级卸载与数据清理)

---

## 1. 准备工作

| 需要 | 说明 |
|---|---|
| Windows 10 / 11 | 64 位 |
| [Windows Terminal](https://aka.ms/terminal) | 推荐用它打开 PowerShell：中文、二维码显示都更正常（Win11 自带） |
| [Git](https://git-scm.com/download/win) | 下载仓库用；安装时一路默认即可 |
| Microsoft Edge 或 Google Chrome | Windows 自带 Edge；国内网络建议直接用它（见第 3 节） |
| [VC++ 2015-2022 运行库（x64）](https://aka.ms/vs/17/release/vc_redist.x64.exe) | 只有发 **B站视频** 需要；多数电脑已经装过，`mpub doctor` 会检查 |

不需要单独安装 Python：下一步的 uv 会自动准备。

> 打开 PowerShell：开始菜单搜索「终端」或「Windows Terminal」→ 默认就是 PowerShell。
> 下面所有 `PS>` 开头的命令都在这里输入（输入时不要带 `PS>`）。

## 2. 安装 multipub

### 2.1 下载仓库

选一个放程序的目录，比如 `D:\tools`：

```powershell
PS> cd D:\tools
PS> git clone https://github.com/Jack9thu/multipub
PS> cd multipub
```

之后所有 `uv run mpub ...` 命令都要在这个 `multipub` 目录里运行。

### 2.2 一键安装（推荐）

```powershell
PS> powershell -ExecutionPolicy Bypass -File scripts\install.ps1          # 普通网络
PS> powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -China   # 国内网络
```

脚本会做这些事（可以重复运行，已完成的会跳过，不需要管理员权限）：

1. 检查 **uv**（Python 项目管理工具）。没有的话，先显示官方安装命令，**问你同意后**才安装。
2. `uv sync`：准备 Python 3.12、建 `.venv`、装好依赖。
3. `uv run mpub setup`：准备浏览器内核和 biliup（见第 3 节；会问你 biliup 怎么找）。
4. `uv run mpub doctor`：环境自检。
5. 检查 VC++ 运行库，缺的话提示安装命令。

`-China` 额外做的事（只影响这一次运行，不改系统设置）：

- 依赖从阿里云 PyPI 镜像装，需要时 Python 本体从 npmmirror 下载。
- **不下载** Chromium（国内镜像没有它需要的版本），改用 Windows 自带的 Edge。
- 数据目录里还没有 `config.toml` 时，生成一份：浏览器用 Edge、biliup 下载走 GitHub 镜像。已有的不会改。

### 2.3 手动安装（可选）

```powershell
PS> powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # 安装 uv，然后重开终端
PS> uv sync
PS> uv run mpub setup
PS> uv run mpub doctor
```

### 2.4 数据放在哪里

登录信息、下载的工具、发布记录都在 **`%LOCALAPPDATA%\multipub`**（不在仓库里，避免误上传）：

```
%LOCALAPPDATA%\multipub\
├─ config.toml               可选配置（第 10 节）
├─ accounts\<平台>\<账号>.json  登录凭据：等同账号密码，不要发给别人
├─ tools\biliup\             biliup（下载的固定版本、你的选择 selected.json）
├─ qrcode\                   登录二维码图片
├─ runs\                     每次运行的日志和截图（出问题时看这里）
├─ sau\                      抖音/小红书的日志、抖音短信验证码文件
├─ pending\ history.jsonl     发布记录（查重用）
└─ locks\                    账号锁
```

在资源管理器地址栏输入 `%LOCALAPPDATA%\multipub` 回车就能打开。想换位置：设置环境变量 `MULTIPUB_HOME`。

## 3. 浏览器和 biliup

### 3.1 浏览器内核

抖音、小红书、知乎登录和 B站图文用浏览器自动化。两种选择：

- **patchright 自带的 Chromium**（默认）：`uv run mpub setup` 会下载（约 150 MB）。
- **本机的 Edge / Chrome**：不用下载，指纹也更像真人。在 `config.toml` 写：

  ```toml
  [browser]
  channel = "msedge"     # 或 "chrome"
  ```

  然后运行 `uv run mpub setup --skip-browser`。`-China` 安装已经帮你这样配好了。

### 3.2 biliup（B站视频上传工具）—— 你来选怎么找到它

B站视频是通过 [biliup](https://github.com/biliup/biliup) 上传的。**用哪个 biliup 由你决定**，三种方式：

| 方式 | 适合 | 命令 |
|---|---|---|
| ① 直接告诉路径 | 你知道 biliup.exe 在哪 | `uv run mpub biliup use "D:\tools\biliup\biliup.exe"` |
| ② 让软件在本机找 | 以前下载过，但不记得在哪（比如用过 social-auto-upload） | `uv run mpub biliup find`，再 `uv run mpub biliup use 1`（按编号）；或 `uv run mpub biliup use --auto` |
| ③ 下载固定版本 | 电脑上没有 | `uv run mpub biliup use --download`，然后 `uv run mpub setup` |

- 第一次运行 `uv run mpub setup` 时，如果你还没选过，它会**列出本机找到的 biliup 并问你**选哪种，不会擅自下载。
- `mpub biliup find` 会查：PATH、social-auto-upload 的工具目录（`%USERPROFILE%\.social-auto-upload\tools\biliup\`）、
  「下载」文件夹（包括里面 `biliup` 开头的解压目录）、当前目录、multipub 下载过的版本。
  每一个都会实际运行 `--version` 和 `--help`（不联网）检查是否兼容，并告诉你不兼容的原因。
- 随时查看现在用哪个：`uv run mpub biliup status`。
- 固定版本是 **v1.3.1**，压缩包的 sha256 已内置在代码里，镜像下载的文件不对会直接拒绝。
- 国内下载慢：在 `config.toml` 写 `github_mirror = "https://ghfast.top/"`（第三方加速，不可用就换一个）。

示例：

```text
PS> uv run mpub biliup find
当前使用：还没选 biliup 来源（……）
  [1] C:\Users\me\.social-auto-upload\tools\biliup\windows-x86_64\biliup.exe
      v1.3.1 · social-auto-upload · 兼容
  [2] D:\Downloads\biliupR-v0.1.9\biliup.exe
      v0.1.9 · 下载文件夹 · 不兼容：缺少参数 --copyright（版本可能太旧）
选择：mpub biliup use <编号或路径> / mpub biliup use --auto / mpub biliup use --download

PS> uv run mpub biliup use 1
已选择：C:\Users\me\.social-auto-upload\tools\biliup\windows-x86_64\biliup.exe（v1.3.1）
```

### 3.3 自检

```powershell
PS> uv run mpub doctor
```

每一项前面是 ✔ 或 ✘：

| 项 | ✘ 时怎么办 |
|---|---|
| `python` / `patchright` | 重新 `uv sync` |
| `browser` | `uv run mpub setup`；或改用 Edge/Chrome（3.1） |
| `bilibili.biliup` | 按提示选 biliup（3.2）；提示缺 VC++ 运行库就安装它 |
| `douyin.terminal_qr` 等 | 只是说明，不影响使用 |
| `<平台>.accounts` | 还没登录，下一节处理（这一项 ✘ 不算环境问题） |

## 4. 登录四个平台

每个平台每个账号登录一次，登录信息会保存下来，之后发布不用再登录（过期了会提示）。
`--account` 是你给这个账号起的名字（字母、数字、`_`、`-`），默认 `default`，下面都用 `main`。

```powershell
PS> uv run mpub login bilibili --account main
PS> uv run mpub login douyin --account main
PS> uv run mpub login xiaohongshu --account main
PS> uv run mpub login zhihu --account main
```

| 平台 | 会看到什么 | 注意 |
|---|---|---|
| **B站** | 不开浏览器，生成二维码图片（路径会打印出来，在 `%LOCALAPPDATA%\multipub\qrcode\bilibili-main.png`） | 打开图片，用 B站 App 扫码，手机上会显示「TV 端登录」确认。二维码 3 分钟过期会自动刷新 |
| **抖音** | 弹出浏览器窗口，显示登录二维码 | 用抖音 App 扫码。扫完可能要求**短信二次验证**，直接在弹出的窗口里输入；所以抖音登录**不要**加 `--headless` |
| **小红书** | 弹出浏览器窗口，显示二维码 | 用小红书 App 扫码。找不到二维码时窗口不会关，可以在窗口里手动切到扫码登录 |
| **知乎** | 弹出浏览器窗口 | 用知乎 App 扫码，或在窗口里用其他方式登录 |

- 不想弹窗口：加 `--headless`，只生成二维码图片（`%LOCALAPPDATA%\multipub\qrcode\<平台>-<账号>.png`）。
- 同一平台多个账号：换个 `--account` 名字再登录一次，发布时用清单里的 `account` 或 `--account` 选。
- 检查登录是否有效（抖音、小红书要开无头浏览器检查，每个账号可能要一两分钟）：

  ```powershell
  PS> uv run mpub check                    # 所有已登录账号
  PS> uv run mpub check douyin --account main
  PS> uv run mpub accounts                 # 列出已登录账号
  ```

- **以前用 biliup 登录过 B站**：把那份 `cookies.json` 复制成 `%LOCALAPPDATA%\multipub\accounts\bilibili\main.json`，
  再 `uv run mpub check bilibili --account main` 确认有效，就不用重新扫码（`mpub biliup find` 发现 biliup 旁边有
  `cookies.json` 时会提示）。
- B站备用登录：`uv run mpub login bilibili --account main --interactive`，在终端里运行 biliup 自带的交互式登录。

## 5. 第一次发布

### 5.1 准备素材和清单

每条内容一个文件夹，里面放素材和一个 `post.yaml`（发布清单）。用 `mpub new` 生成模板：

```powershell
PS> uv run mpub new posts\第1期 --kind video      # 视频
PS> uv run mpub new posts\周末徒步 --kind note     # 图文（多张图 + 文字）
PS> uv run mpub new posts\一篇文章 --kind article  # 长文（Markdown，目前只有知乎）
```

然后把素材放进去，用记事本或 VS Code 打开 `post.yaml` 改内容。

**视频清单示例**（`posts\第1期\post.yaml`）：

```yaml
kind: video
title: 第1期：三分钟讲清楚 XXX
body: |
  视频简介写在这里。
tags: [科技, 教程]
video: video.mp4            # 和 post.yaml 放在同一个文件夹
cover: cover.jpg            # 可选：封面
# schedule: "2026-11-01 20:00"   # 可选：定时发布（本地时间，加引号）
ai_generated: false         # 内容含 AI 生成时改成 true，会在支持的平台勾选 AI 声明
account: main

platforms:
  bilibili:
    tid: 231                # B站分区（必填），231 = 计算机技术
    copyright: 1            # 1 自制，2 转载（转载要写 source）
  douyin:
    title: 三分钟讲清楚 XXX   # 抖音标题最多 30 字，可以单独写
  xiaohongshu:
    title: 3分钟学会XXX🔥     # 小红书标题最多 20 字
```

**怎么读这份清单**：

- 顶层字段（`title`、`body`、`tags`……）是所有平台共用的；`platforms` 下面列出要发的平台，同名字段可以按平台覆盖。
- 不想发某个平台：在 `platforms` 里删掉它，或写成 `bilibili: false`。
- 某个平台不支持这种内容（比如知乎不支持视频）会自动跳过。

**图文**：`images:` 下列出图片（按顺序），B站发成图文动态、抖音发图文、小红书发笔记、知乎发想法
（`platforms.zhihu.as: article` 可改成知乎文章）。

**文章**：正文写在 `article.md`（Markdown），本地图片用相对路径引用，发布时自动上传到知乎图床；
`platforms.zhihu.draft: true` 只存草稿，你自己去知乎点发布。

**路径怎么写**：

- 最省事：素材和 `post.yaml` 放在同一个文件夹，写文件名或相对路径（`images/1.jpg`）。
- Windows 绝对路径用**单引号**或正斜杠：`video: 'D:\视频素材\第1期.mp4'` 或 `video: D:/视频素材/第1期.mp4`。
  双引号里的 `\` 是转义符，容易出错。
- 中文、空格都可以；命令行里带空格的路径要加引号：`uv run mpub publish "D:\视频 素材\第1期"`。

所有字段和每个平台的选项见 [docs/manifest.md](manifest.md)。

### 5.2 校验（不联网）

```powershell
PS> uv run mpub validate posts\第1期
```

✘ 开头的是错误，必须改；`!` 开头的是提醒（比如标题太长、标签格式），看情况处理。

### 5.3 预演（不联网、不发布）

```powershell
PS> uv run mpub publish posts\第1期 --dry-run
```

会列出每个平台「校验通过」或「跳过」（比如同一内容已经发过）。

### 5.4 正式发布

```powershell
PS> uv run mpub publish posts\第1期
即将发布「第1期：三分钟讲清楚 XXX」到 B站/main、抖音/main、小红书/main，确认? [y/N] y
→ 发布到 B站/main ……
  ✔ B站/main: 已提交  https://www.bilibili.com/video/BV1xxxxxxxxx
等待 30 秒后发布下一个平台……
→ 发布到 抖音/main ……
```

- 默认逐个平台发，中间间隔 30 秒（`--gap` 可改）。只发其中几个：`--to bilibili,douyin`。
- 想看浏览器在做什么：加 `--headed`。
- 结果状态：

| 显示 | 含义 |
|---|---|
| 已发布 | 平台已公开，有链接 |
| 已提交 | 平台已接收，可能在审核；有链接就给链接（小红书不返回链接，去笔记管理看） |
| 已定时 | 设置了定时发布 |
| 已存草稿 | 知乎 `draft: true` |
| 跳过 | 平台不支持这种内容，或同一内容已经发过 |
| 需要处理 | 登录过期、验证码、短信验证码等（第 7 节） |
| 失败 | 看后面的说明；**说「结果未知」的不要直接重发**，先去平台后台看 |

- 退出码：0 全部成功；1 有失败；2 清单/参数错误；3 需要你处理。
- 查看发布记录：`uv run mpub history`。

### 5.5 查重：同一内容不会被重复发

同一份内容（文字 + 素材文件）发到同一平台账号后，再运行会自动跳过。点了发布但结果不确定的也会跳过，
防止重复发出。确认没发出、真的要再发：加 `--force`（只重发某个平台：`--to douyin --force`）。

## 6. 各平台注意事项速查

| | B站 | 抖音 | 小红书 | 知乎 |
|---|---|---|---|---|
| 视频 | ✔ 标题 ≤80，**必须写 `tid` 分区**，标签 1–12 个 | ✔ 标题 ≤30 | ✔ 标题 ≤20，标签 ≤10 | ✘ 自动跳过 |
| 图文 | ✔ 动态：标题 ≤20、正文 ≤1000、图 1–9 张 | ✔ 标题 ≤20、正文 ≤1000、图 1–35 张 | ✔ 标题 ≤20、正文 ≤1000、图最多 18 张 | ✔ 想法：图 ≤9 张 |
| 文章 | ✘ | ✘ | ✘ | ✔ Markdown，标题 ≤100 |
| 定时 | 视频：2 小时~15 天后；动态：5 分钟~7 天后 | 2 小时后 | 2 小时后 | ✘ 不支持 |
| AI 声明 | `ai_generated: true` → 创作声明 | `ai_generated` 或 `declaration` | `ai_generated` 尽力勾选 | `ai_generated` 或 `statement` |

几条容易踩的坑：

- **B站动态《动态发布规范》**：第一次发图文动态，B站会弹出规范确认框。multipub **不会替你点同意**：
  加 `--headed` 重发一次，在弹出的窗口里亲手点「确认并发送」（会记住这个账号）；
  或者你读过规范后在清单写 `platforms.bilibili.confirm_rules: true`。
- **抖音、小红书的声明**：默认不做任何声明（上游工具原本会默认勾 AI 声明、默认「来源转载」，multipub 已去掉）。
  要声明时明确写出来；声明设置失败会拒绝发布，不会悄悄发出去。
- **标题里的 emoji**：抖音、小红书可能按 UTF-16 计算字数，一个 emoji 算两个字，超长时会拒绝发布。
- **知乎文章的图片**：Markdown 里的本地图片（包括 Obsidian 的 `![[图.png]]`）会自动上传；引用不到的图片会报错。

每个平台的完整说明在 [docs/platforms/](platforms/)。

## 7. 发布中需要你处理的情况

| 情况 | 怎么办 |
|---|---|
| **登录过期**（需要处理 · login_required） | `uv run mpub login <平台> --account <账号>`，再发一次（只发这个平台：`--to <平台>`） |
| **抖音短信验证码** | 终端会提示「需要输入」。收到短信后**另开一个终端**运行 `uv run mpub verify-code douyin 123456`，发布会自动继续（默认等 10 分钟）。用 `--headed` 时也可以直接在浏览器窗口里输入 |
| **人机验证 / 滑块**（captcha） | 加 `--headed` 重发，在弹出的窗口里手动完成，multipub 会等你 |
| **风控**（risk_control） | 知乎：运行 `uv run mpub login zhihu --account <账号>`，在弹出的窗口里完成验证；B站：过一会儿再试、降低频率 |
| **B站动态规范确认**（consent_required） | 见第 6 节 |
| **结果未知**（unknown_outcome） | 已经点了发布但没拿到结果。**不要直接重发**：去平台后台（作品管理/笔记管理/创作中心）确认有没有发出，确定没发出再加 `--force` 重发 |
| **账号正被另一个任务使用** | 同一账号同一时间只能跑一个任务，等前一个结束 |

发布途中按 **Ctrl+C** 会安全停止：浏览器、biliup 一起结束；如果已经点了发布，会记成「结果未知」，避免下次重复发。

出问题时，结果里的 `evidence` 列出了日志和截图路径（在 `%LOCALAPPDATA%\multipub\runs\` 下），截图能看到页面停在哪一步。

## 8. 配合 Claude Code 使用

仓库里带了一个 Claude Code skill（`.claude\skills\multipub`）：在 multipub 目录里打开 Claude Code，它会自动加载。

```powershell
PS> cd D:\tools\multipub
PS> claude
```

然后直接用中文说，比如：

- 「检查一下各平台的登录状态」
- 「帮我把 posts\第1期 发到 B站和抖音」
- 「新建一个图文：周末徒步，图片在 D:\照片\徒步，发小红书和知乎想法」
- 「看看最近的发布记录」

Claude 会这样做：

1. 先 `validate` 和 `publish --dry-run`，给你一张汇总表（平台、账号、标题、定时、声明、素材），**你明确同意后才发布**。
2. 需要登录时运行登录命令，并把二维码图片给你看。
3. 发抖音前提醒你可能会收到短信验证码；收到后直接告诉 Claude，它会运行 `verify-code`。
4. 需要选 biliup 时问你用哪种方式（第 3.2 节），不会自己下载。
5. 不会替你做 AI 声明、转载声明、B站规范确认；结果未知时不会自动重发。

**在任何目录都能用**：把 skill 复制到用户目录：

```powershell
PS> Copy-Item -Recurse .claude\skills\multipub "$env:USERPROFILE\.claude\skills\multipub"
```

再把复制出来的 `SKILL.md` 里的 `uv run mpub` 换成 `uv run --project "D:\tools\multipub" mpub`。仓库更新后记得重新复制。

## 9. 定时发布与自动发布

**平台定时**（推荐）：在清单里写 `schedule: "2026-11-01 20:00"`，multipub 会在平台上设置定时发布，
发布后状态是「已定时」，电脑可以关机。multipub 会核实定时开关和时间确实设置成功，设置不上就拒绝发布。

**用 Windows 任务计划程序到点再发**（平台不支持定时时，比如知乎）：

1. 开始菜单搜索「任务计划程序」→ 创建基本任务。
2. 触发器选时间；操作选「启动程序」：
   - 程序：`C:\Users\<你>\.local\bin\uv.exe`
   - 参数：`run mpub publish "D:\tools\multipub\posts\第1期" --yes --json`
   - 起始于：`D:\tools\multipub`
3. 注意：计划任务里没人能输入短信验证码；抖音如果要求验证，会在等待后返回 `sms_code_required`。

## 10. 常用配置 config.toml

文件位置：`%LOCALAPPDATA%\multipub\config.toml`（没有就新建）。用记事本编辑，保存为 UTF-8。

```toml
gap_seconds = 30                         # 平台之间间隔秒数
github_mirror = "https://ghfast.top/"    # 国内下载 biliup 用的 GitHub 镜像（可选）

[browser]
channel = "msedge"                       # chromium（默认，需 mpub setup 下载）/ msedge / chrome
# executable_path = 'C:\Program Files\Google\Chrome\Application\chrome.exe'

[timeouts]
publish_video = 3600                     # 单个平台发视频最长秒数（大视频调大）
publish_note = 1800

[platforms.bilibili]
# biliup_path = 'D:\tools\biliup\biliup.exe'   # 一般不用写：用 mpub biliup use 选择即可
```

- Windows 路径用**单引号**。
- 写错时所有命令都会提示「配置错误」并退出，按提示改正即可。
- 全部可配置项见 [docs/config.example.toml](config.example.toml)。

## 11. 常见问题

**安装时 uv 下载很慢 / 失败**
国内网络用 `scripts\install.ps1 -China`。

**Chromium 下载失败**
改用 Edge：`config.toml` 写 `[browser]` `channel = "msedge"`，然后 `uv run mpub setup --skip-browser`。

**biliup.exe 被杀毒软件拦截 / 弹出「Windows 已保护你的电脑」**
确认来源后在文件属性里勾选「解除锁定」，或把 `%LOCALAPPDATA%\multipub\tools` 加入杀毒软件白名单；
也可以改用你自己下载的 biliup（`mpub biliup use <路径>`）。

**提示缺少 VC++ 运行库（VCRUNTIME140.dll）**
`winget install Microsoft.VCRedist.2015+.x64`，或下载安装 https://aka.ms/vs/17/release/vc_redist.x64.exe 。

**终端里二维码显示不全 / 乱码**
直接打开打印出来的 PNG 图片扫码；用 Windows Terminal 显示更正常。

**`mpub` 提示「正被另一个 mpub 任务使用」**
同一账号已经有任务在跑（可能在后台）。等它结束；如果确定没有任务在跑，重开终端再试。

**发布到一半按了 Ctrl+C / 关了窗口**
浏览器和上传会一起停止。已经点过发布的会记成「结果未知」：先去平台后台确认，再决定是否 `--force` 重发。

**同一内容想再发一次**
默认会被查重跳过。确认要再发：`uv run mpub publish <清单> --to <平台> --force`。

**登录状态多久过期**
各平台不同，几天到几个月不等。`uv run mpub check` 可以随时检查；发布时过期会提示重新登录。

**出了问题怎么排查**
看结果里 `evidence` 的日志和截图；或 `uv run mpub publish <清单> --headed` 观察浏览器停在哪一步。
各平台的错误说明见 [docs/platforms/](platforms/)。

## 12. 升级、卸载与数据清理

**升级 multipub**：

```powershell
PS> cd D:\tools\multipub
PS> git pull
PS> uv sync
PS> uv run mpub doctor
```

**卸载**：

1. 删除仓库目录（例如 `D:\tools\multipub`）。
2. 删除数据目录 `%LOCALAPPDATA%\multipub`（**里面有登录凭据**，删除即全部退出登录）。
3. 可选：删除 patchright 的浏览器 `%LOCALAPPDATA%\ms-playwright`。
4. 可选：卸载 uv（删除 `%USERPROFILE%\.local\bin\uv.exe`）。

**只清理某个账号**：删除 `%LOCALAPPDATA%\multipub\accounts\<平台>\<账号>.json`。

**清理运行日志和截图**：删除 `%LOCALAPPDATA%\multipub\runs\` 里的旧文件夹（不影响登录和发布记录）。

---

更多：[README](../README.md) · [清单字段](manifest.md) · [Windows 细节](windows.md) · [各平台说明](platforms/)
