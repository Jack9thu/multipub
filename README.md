# multipub

一条命令把**视频 / 图文 / 文章**发布到 **B站、抖音、小红书、知乎**。命令行工具 `mpub`，Windows 优先，
用浏览器自动化（[patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python)）和平台网页版自己用的接口完成登录与发布，
并带有一个 [Claude Code skill](#配合-claude-code-使用)，可以让 Claude 帮你准备清单、检查登录、确认后发布。

> [!WARNING]
> **代码已实现并有离线测试，但尚未在真实账号上验证——请先用小号测试。**
> 各平台页面和接口随时会改；每个平台文档末尾都列了「未在真实账号上验证」的清单和置信度。

## 支持的平台与内容

| | 视频 `video` | 图文 `note` | 文章 `article` |
|---|---|---|---|
| **B站** | ✔ 视频投稿（[biliup](https://github.com/biliup/biliup) 上传） | ✔ 动态（opus，浏览器发布） | ✘ |
| **抖音** | ✔ | ✔ 图文 | ✘ |
| **小红书** | ✔ | ✔ 图文笔记 | ✘ |
| **知乎** | ✘ | ✔ 想法（可改发成文章） | ✔ 专栏文章（Markdown、图片、话题、草稿） |

✘ 的组合会自动跳过，不算失败。每个平台支持的字段、限制、登录方式、结果和已知风险：
[B站](docs/platforms/bilibili.md) · [抖音](docs/platforms/douyin.md) · [小红书](docs/platforms/xiaohongshu.md) · [知乎](docs/platforms/zhihu.md)。

一条内容 = 一个目录 + 一个 `post.yaml`（[字段参考](docs/manifest.md)）：

```yaml
kind: video
title: 第12期：三分钟讲清楚 XXX
body: 视频简介
tags: [科技, 教程]
video: video.mp4
cover: cover.jpg
account: main
platforms:
  bilibili: {tid: 231}
  douyin: {title: 三分钟讲清楚 XXX}
  xiaohongshu: {title: 3分钟学会XXX}
```

## 安全设计

- **不会重复发布**：点「发布」/调用发布接口前会记录 `submit_started`；之后的任何失败都不会自动重试。
  结果不确定时返回 `unknown_outcome`，提示你先去平台后台确认。同一内容发过之后再发会被查重跳过（`--force` 才会再发）。
- **先校验再发布**：`mpub validate` / `mpub publish --dry-run` 只做本地检查（标题长度、图片数量、定时窗口……），不联网。
- **不替你做声明**：AI 声明、转载声明、平台规范确认都只有你明确写了才会勾选（见下文对 social-auto-upload 的修改）。
- **每个平台一个子进程**：某个平台卡死时连同浏览器一起结束，不影响其他平台；超时、出错都会留下日志和截图。
- **登录凭据只放在本机数据目录**（默认 `%LOCALAPPDATA%\multipub`），不在仓库里。

## 为什么基于上游 social-auto-upload，而不是 omnipost

抖音、小红书的浏览器流程来自 [social-auto-upload](https://github.com/dreammis/social-auto-upload)（SAU，MIT）
上游提交 `0012d2c`（2026-09-03），vendored 在 `src/multipub/_sau/`。没有选择它的分支 omnipost / PostSail，原因：

- **omnipost 是旧快照**：整个仓库只有一个压扁的提交，代码对应 SAU 2026-05-30 的 `99a392a`，
  缺少之后约 36 个针对抖音 / 小红书 / B站的上游修复（抖音创作者中心改版后的扫码登录与发布页选择器、短信二次验证、小红书话题与声明等）。
- **omnipost 的知乎很简陋**：只能发纯文本文章，没有图片、Markdown、话题，不检查是否发布成功，而且命令行调用不到（只接在 Web 界面里）。
- **omnipost 的 `requirements.txt` 已损坏**：编码错误，还有 PyPI 上不存在、容易被抢注（typosquat）的包名。
- **PostSail** 在 omnipost 之上增加了文章发布，但它自己的文档把知乎、B站、小红书标为未在真实账号上验收。

multipub 的知乎（接口发布）和 B站（biliup 投稿 + 动态）是自己实现的；只从 omnipost 移植了小红书 AI 内容声明这一个函数。

### multipub 对 SAU 的修改

所有改动在源码里以 `# multipub patch:` 标出，清单见 [`src/multipub/_sau/PATCHES.md`](src/multipub/_sau/PATCHES.md)。主要是：

- **不再默认 AI 声明**：上游给所有抖音视频默认勾选「内容由AI生成」；现在只有 `ai_generated: true` 或写了 `declaration` 才声明。
- **不再强制同步头条/西瓜**：上游会打开抖音的第三方同步开关；现在默认不碰，`sync_toutiao: true` 才打开，`false` 会把开着的开关关掉。
- **不再默认「来源转载」**：上游给小红书笔记无条件声明转载（且媒体名为空）；现在只有写了 `repost_source` 才声明。
- **所有等待循环都有截止时间**：页面改版时不会无限卡住；超时会截图，并区分「点发布之前」（可重试）与「点发布之后」（结果未知）。
- **发布按钮受保护**：上游每 3.5 秒点一次「发布」直到跳转；现在第一次点击后只在平台弹窗拦下或 20 秒无反应时补点，最多 3 次，
  并监听创建接口的回执；平台明确拒绝时立即报错。
- 另外：浏览器遵循配置（可用本机 Chrome）、日志不再写 stdout、抖音短信验证码通过 `mpub verify-code` 提交、二维码图片可在无头模式下扫。

## Windows 快速开始

需要 Windows 10/11、[Git](https://git-scm.com/download/win)。推荐在 [Windows Terminal](https://aka.ms/terminal) 的 PowerShell 里操作。
更多细节（中文路径、国内镜像、用本机 Chrome、杀毒软件、计划任务）见 [docs/windows.md](docs/windows.md)。

1. 安装 [uv](https://docs.astral.sh/uv/)（会顺便管理 Python，不需要单独装 Python）：

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   装完重新打开终端，`uv --version` 能输出版本即可。

2. 下载代码并安装依赖：

   ```powershell
   git clone https://github.com/Jack9thu/multipub
   cd multipub
   uv sync
   ```

   `uv sync` 会按 `.python-version` 准备 Python 3.12、建 `.venv`、装好依赖（含测试用的 dev 组）。
   也可以 `uv venv` 然后 `uv pip install -e .`。或者直接运行 `scripts\install.ps1` 完成第 2、3 步（国内网络加 `-China`：依赖走国内镜像，浏览器改用系统自带的 Edge，详见 [docs/windows.md](docs/windows.md)）。

3. 安装浏览器内核、下载 biliup，并自检：

   ```powershell
   uv run mpub setup
   uv run mpub doctor
   ```

   国内网络下载慢：见 [docs/windows.md](docs/windows.md) 的镜像设置，或改用本机 Chrome / Edge（不用下载内核）。

4. 逐个平台扫码登录（`main` 是你给这个账号起的名字，`post.yaml` 里的 `account` 要和它一致）：

   ```powershell
   uv run mpub login bilibili --account main
   uv run mpub login douyin --account main
   uv run mpub login xiaohongshu --account main
   uv run mpub login zhihu --account main
   uv run mpub check --all
   ```

   抖音、小红书、知乎会弹出浏览器窗口扫码；B站用手机 App 扫终端里打印路径的二维码图片（TV 端登录，不开浏览器）。
   二维码图片都在 `%LOCALAPPDATA%\multipub\qrcode\`。

5. 准备一条内容并发布：

   ```powershell
   uv run mpub new posts/demo --kind video      # 生成 posts\demo\post.yaml，按注释修改，放入 video.mp4 等
   uv run mpub publish posts/demo --dry-run     # 只做本地校验
   uv run mpub publish posts/demo               # 真正发布，会再问一次确认
   ```

## 常用命令

所有命令都在仓库根目录用 `uv run mpub ...` 运行（或先激活 `.venv`）。`--json` / `--home` 要写在子命令后面。

| 命令 | 作用 |
|---|---|
| `mpub doctor` | 环境自检：Python、patchright、浏览器、biliup、各平台已登录账号 |
| `mpub setup [--skip-browser]` | 安装 patchright Chromium、下载 biliup（只有它会下载） |
| `mpub login <平台> --account <名字> [--headless]` | 扫码登录；默认显示浏览器窗口 |
| `mpub login bilibili --account <名字> --interactive` | 在真实终端里跑 biliup 自带的交互式登录（B站备用方案） |
| `mpub check [平台…] [--account <名字>]` | 检查登录是否有效；不写平台 = 全部（`--all` 同义） |
| `mpub accounts` | 列出已登录账号 |
| `mpub new <目录> --kind video\|note\|article [--force]` | 生成 `post.yaml` 模板 |
| `mpub validate <post>` | 本地校验清单 |
| `mpub publish <post> --dry-run` | 本地校验，按发布结果的格式输出 |
| `mpub publish <post> [--to 平台,平台] [--account 名字] [--yes] [--headed] [--gap 秒] [--timeout 秒] [--force]` | 发布 |
| `mpub history [--limit N]` | 查看发布记录 |
| `mpub verify-code douyin <验证码>` | 抖音发布中途要求短信验证时，提交验证码 |

平台名可以写 `bilibili` / `douyin` / `xiaohongshu` / `zhihu`，也可以写 `b站`、`抖音`、`xhs`、`知乎` 等别名。
`<post>` 是 `post.yaml` 的路径或它所在目录。加 `--json` 时 stdout 只输出一个 JSON 对象，进度信息写到 stderr
（格式见 [cli-contract.md](.claude/skills/multipub/references/cli-contract.md)）。

## 退出码

| 退出码 | 含义 |
|---|---|
| `0` | 全部成功（包括已发布、已提交审核、已定时、草稿、校验通过、跳过） |
| `1` | 至少一个平台失败 |
| `2` | 用法或清单错误、校验不通过、没加 `--yes` 又不在交互终端、取消确认 |
| `3` | 需要人处理：登录失效、验证码、短信验证码、风控验证、平台规范确认 |

同时有失败和需要处理时返回 `1`。

## 数据目录

默认 `%LOCALAPPDATA%\multipub`（Linux/macOS：`~/.local/share/multipub`），可用环境变量 `MULTIPUB_HOME` 或 `--home` 改：

```
%LOCALAPPDATA%\multipub\
├─ config.toml              可选配置，见 docs/config.example.toml
├─ accounts\<平台>\<账号>.json   登录凭据（cookie / token，等同账号密码，不要发给别人）
├─ tools\biliup\v1.3.1\     biliup.exe 及其安装记录（install.json、sha256）
├─ qrcode\                  登录二维码图片（<平台>-<账号>.png，登录结束后删除）
├─ runs\<时间>\<平台>\       每次运行的日志和截图（结果里的 evidence 指向这里）
├─ sau\                     抖音/小红书流程的日志、抖音短信验证码文件 verify_code.txt
├─ locks\                   账号锁（同一账号同一时间只跑一个任务）
└─ history.jsonl            发布记录（查重、对账用）
```

patchright 的 Chromium 装在它自己的目录（Windows：`%LOCALAPPDATA%\ms-playwright`）。

## 风险提示

- **风控与封号**：自动化发布可能触发平台风控（验证码、限流、降权，严重时封号）。multipub 尽量模拟正常操作，但无法保证不被识别。
- **先用小号测试**：本项目没有在真实账号上验证过。请先用不重要的小号把登录、发布、草稿、定时各走一遍。
- **控制频率**：同一账号一天发几条以内；多个平台之间默认间隔 30 秒（`--gap` / `gap_seconds`）；不要多账号共用一个 IP 高频发布。
- **遵守平台规则**：内容需符合各平台社区规范；AI 生成内容请设 `ai_generated: true`；转载请如实声明来源。
- **B站接口的法律风险**：B站已通过律师函关停多个第三方接口库。multipub 的 B站图文默认走浏览器，HTTP 模式仅作实验，详见 [bilibili.md](docs/platforms/bilibili.md)。
- **结果未知时先去后台确认**：看到 `unknown_outcome` 或 `submit_started: true` 的失败，先到平台后台看是否已发出，不要直接重发。

## 配合 Claude Code 使用

仓库自带项目级 skill：[`.claude/skills/multipub/`](.claude/skills/multipub/SKILL.md)。在仓库目录里打开 Claude Code，
直接说「把 posts/ep12 发到 B站和抖音」「检查一下各平台登录」「帮我写一个小红书图文的 post.yaml」即可。Claude 会：

1. 用 `mpub new` / `mpub validate` 准备和检查清单；
2. **先 `--dry-run`**，把平台、账号、标题、定时、声明等汇总给你，**等你明确确认后**才真正发布；
3. 登录失效时帮你跑 `mpub login` 并把二维码图片给你看，抖音要短信验证码时向你要验证码；
4. 结果不确定时提醒你先去平台后台确认，**不会自动重发**。

想在任何目录都能用，把 `.claude\skills\multipub` 复制到 `%USERPROFILE%\.claude\skills\multipub`，
并在 SKILL.md 里把命令改成带仓库路径的形式（见 SKILL.md 末尾「全局安装」）。

## 开发

```powershell
uv sync                          # 含 dev 组（pytest）
uv run pytest -q                 # 全部离线测试，不需要浏览器和账号
```

pip 用户：`pip install -e ".[dev]"`。可选 extra `terminal-qr`（opencv）用于在日志里打印字符画二维码。

## 许可证

[MIT](LICENSE)。包含的第三方代码（social-auto-upload 等）与运行时下载的程序（biliup、Chromium）的许可证见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本项目与各平台官方无关。
