# multipub

**中文** | [English](README.en.md)

一条命令把**视频 / 图文 / 文章**发布到 **B站、抖音、小红书、知乎**。命令行工具 `mpub`，Windows 优先，
用浏览器自动化（[patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python)）和平台网页版自己用的接口完成登录与发布，
并带有一个 [Claude Code skill](#配合-claude-code-使用)，可以让 Claude 帮你准备清单、检查登录、确认后发布。

> **第一次用？看 [Windows 使用教程](docs/tutorial-windows.md)**：从安装、登录到第一次发布、定时发布、常见问题，一步一步来。

> [!WARNING]
> **大部分功能只有离线测试，没有逐项在真实账号上验证——请先用小号测试。**
> 各平台页面和接口随时会改；每个平台文档末尾都列了「未在真实账号上验证」的清单和置信度。

> [!NOTE]
> **真实账号实测（2026-10-25）**：四个平台的基本发布都已成功（B站视频、抖音视频、小红书图文笔记、知乎专栏文章各一条，都带内容标注）。
> 抖音 `collection`、小红书 `collection`、知乎 `column`（发布时加入合集 / 专栏）也在同一天的真实发布里用过：抖音、小红书的日志显示已选上合集，知乎文章经专栏接口核实已收录。

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

## 实现原理

两种做法：

- **浏览器自动化**：用 patchright 打开一个真实的 Chrome（或 Chromium / Edge），装上登录时保存的登录状态，
  像人一样在创作者网页上上传、填写、点击、点「发布」，每一步再核对页面是否变成预期的样子。平台的签名、加密由网页自己算好；
  代价是慢，而且页面改版（按钮改名、换位置）就要跟着改定位。
- **直接调接口**：不开浏览器，用登录文件里的 cookie / 令牌直接把请求发给平台服务器（就是网页背后发出的那个请求）。
  快、不依赖页面长什么样；代价是接口一改、或开始要求请求签名就会失效。

| 平台 | 简单发布 | 收录到合集 |
|---|---|---|
| **B站** | **视频：调接口**——调用 [biliup](https://github.com/biliup/biliup) 命令行，biliup 直接调 B站的投稿接口上传、提交；**动态：浏览器自动化**（t.bilibili.com 发布框） | **调接口、发布后补加**——biliup 的 `season add`，再到合集列表核对；审核中的稿件加不进 |
| **抖音** | **浏览器自动化**——创作者中心发布页：上传、标题与描述、话题、封面、声明、点发布，并监听创建接口的回执 | **浏览器自动化、发布时顺带选上**——发布表单的「添加合集」下拉，按名称精确匹配点选并核对 |
| **小红书** | **浏览器自动化**——创作服务平台发布页，流程同抖音 | **浏览器自动化、发布时顺带选上**——「内容设置 → 加入合集」，用页面脚本点选并核对按钮上的合集名 |
| **知乎** | **调接口**（只有扫码登录时开浏览器）——知乎网页版自己用的接口：图片传图床 → 建草稿 → 正文、话题、声明 → 发布 | **调接口、发布时一并完成**——发布请求里带上专栏 id（`column`） |

四个平台都**不能新建合集**（合集 / 专栏要先在网页或 App 里手动建）；没选上合集时只在结果里提示，照常发布。

## 合集 / 专栏

| 平台 | 清单里怎么写 | 说明 |
|---|---|---|
| B站 | multipub 本身不处理；发完用 `biliup season add <section_id> --vid <BV>` 补加 | 审核中的稿件加不进合集，过审后再加 |
| 抖音 | `platforms.douyin.collection: 合集名` | 名称精确匹配；只对视频 |
| 小红书 | `platforms.xiaohongshu.collection: 合集名` | 名称精确匹配；视频、图文都可以 |
| 知乎 | `platforms.zhihu.column: c_1234567890`（或专栏链接） | 只对文章；`draft: true` 时不生效 |

已经发出去的作品：抖音到创作者中心「内容管理 → 作品合集 → 编辑合集 → 添加作品」；知乎在专栏页文章下方「设置 → 取消收录」/ 专栏页「收录内容」；
小红书只能在 App 里加（网页端看不到合集内容）。

## 安全设计

- **不会重复发布**：点「发布」/调用发布接口前会记录 `submit_started`（同时先在发布记录里记一笔「结果待定」，
  mpub 中途被强行结束也不丢）；之后的任何失败都不会自动重试。结果不确定时返回 `unknown_outcome`，提示你先去平台后台确认。
  同一内容发过之后再发会被查重跳过，两个 mpub 同时发同一条也只发一次；确实要再发用 `--to <平台> --force`。
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
- **小红书任意内容类型声明**：`platforms.xiaohongshu.declaration: 虚构演绎，仅供娱乐`（或 `内容包含营销广告`、`笔记含AI合成内容`；`false` 不声明）。
  上游只会选 AI 合成和来源转载；2026-05 起发布时要求做内容标注。选完核实，**核实不了就拒绝发布**（笔记不会发出）。
- **小红书发布时加入合集**：`platforms.xiaohongshu.collection: 合集名`，操作发布页「内容设置 → 加入合集」（页面结构为真实发布页实测）；选不上只告警，照常发布。

## Windows 快速开始

需要 Windows 10/11、[Git](https://git-scm.com/download/win)；发 B站视频还需要 [VC++ 2015-2022 运行库（x64）](https://aka.ms/vs/17/release/vc_redist.x64.exe)（多数电脑已有，`mpub doctor` 会检查）。推荐在 [Windows Terminal](https://aka.ms/terminal) 的 PowerShell 里操作。
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

3. 安装浏览器内核、准备 biliup，并自检：

   ```powershell
   uv run mpub setup
   uv run mpub doctor
   ```

   B站视频上传用的 biliup **由你选怎么来**：`setup` 会列出本机已有的 biliup，问你是直接给路径、用本机找到的，
   还是下载经过验证的 v1.3.1。也可以随时用 `mpub biliup find` / `mpub biliup use <路径或编号>` /
   `mpub biliup use --auto` / `mpub biliup use --download` 修改，`mpub biliup status` 查看（详见 [docs/platforms/bilibili.md](docs/platforms/bilibili.md)）。

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
| `mpub setup [--skip-browser] [--biliup auto\|download\|<路径>]` | 安装 patchright Chromium、准备 biliup（只有它会下载） |
| `mpub biliup find` / `status` / `use <路径或编号>` / `use --auto` / `use --download` | 选择 B站视频上传用哪个 biliup |
| `mpub login <平台> --account <名字> [--headless]` | 扫码登录；默认显示浏览器窗口 |
| `mpub login bilibili --account <名字> --interactive` | 在真实终端里跑 biliup 自带的交互式登录（B站备用方案） |
| `mpub check [平台…] [--account <名字>]` | 检查登录是否有效；不写平台 = 全部（`--all` 同义） |
| `mpub accounts` | 列出已登录账号 |
| `mpub new <目录> --kind video\|note\|article [--force]` | 生成 `post.yaml` 模板 |
| `mpub validate <post>` | 本地校验清单 |
| `mpub publish <post> --dry-run` | 本地校验，按发布结果的格式输出（会提示哪些平台已经发过、真发时会跳过） |
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
| `130` | 被 Ctrl+C 中断（已点过发布的平台记为结果未知） |

同时有失败和需要处理时返回 `1`。

## 数据目录

默认 `%LOCALAPPDATA%\multipub`（Linux/macOS：`~/.local/share/multipub`），可用环境变量 `MULTIPUB_HOME` 或 `--home` 改：

```
%LOCALAPPDATA%\multipub\
├─ config.toml              可选配置，见 docs/config.example.toml
├─ accounts\<平台>\<账号>.json   登录凭据（cookie / token，等同账号密码，不要发给别人）
├─ tools\biliup\v1.3.1\     下载的 biliup.exe 及其安装记录（install.json、sha256）
├─ tools\biliup\selected.json  你选择的 biliup 来源（mpub biliup use 写入）
├─ qrcode\                  登录二维码图片（<平台>-<账号>.png，登录结束后删除）
├─ runs\<时间>\<平台>\       每次运行的日志和截图（结果里的 evidence 指向这里）
├─ sau\                     抖音/小红书流程的日志、抖音短信验证码文件 verify_code.txt
├─ locks\                   账号锁（同一账号同一时间只跑一个任务）
├─ pending\                 已点发布、还没有最终结果的记录（发布途中 mpub 被结束时留下，用于查重）
└─ history.jsonl            发布记录（查重、对账用）
```

patchright 的 Chromium 装在它自己的目录（Windows：`%LOCALAPPDATA%\ms-playwright`）。

## 风险提示

- **风控与封号**：自动化发布可能触发平台风控（验证码、限流、降权，严重时封号）。multipub 尽量模拟正常操作，但无法保证不被识别。
- **先用小号测试**：除了上面 NOTE 里列出的基本发布，其余功能没有逐项在真实账号上验证。请先用不重要的小号把登录、发布、草稿、定时各走一遍。
- **控制频率**：同一账号一天发几条以内；多个平台之间默认间隔 30 秒（`--gap` / `gap_seconds`）；不要多账号共用一个 IP 高频发布。
- **遵守平台规则**：内容需符合各平台社区规范；AI 生成内容请设 `ai_generated: true`；转载请如实声明来源。
- **B站接口的法律风险**：B站已通过律师函关停多个第三方接口库。multipub 的 B站图文默认走浏览器，HTTP 模式仅作实验，详见 [bilibili.md](docs/platforms/bilibili.md)。
- **结果未知时先去后台确认**：看到 `unknown_outcome` 或 `submit_started: true` 的失败，先到平台后台看是否已发出，不要直接重发。
- **知乎接口可能开始要求签名**：2026-10-25 实测，发布本身成功，但发布后的核对请求被拒（HTTP 403「请求参数异常，请升级客户端后重试」）。
  以后如果发布也报同类错误，说明知乎开始要求 `x-zse-96` 签名，接口发布这条路会失效。
- **知乎图片处理超时**：图片停在上传图片这一步超时（点发布之前，不会发出内容）时，可在 `config.toml` 的 `[platforms.zhihu]` 调大 `image_timeout`（秒），
  并用 `--timeout` 放宽整次任务的上限。旧版曾经单张图要等 6～10 分钟以上，原因是传完图床后没有通知知乎（已修复，正常几秒内完成）。
- **小红书主站会拦无头浏览器**：用无头浏览器打开 www.xiaohongshu.com 会被判「IP 存在风险」（发布用的创作服务平台没有这个问题），需要访问主站时用有头模式。

## 配合 Claude Code 使用

仓库自带项目级 skill：[`.claude/skills/multipub/`](.claude/skills/multipub/SKILL.md)。在仓库目录里打开 Claude Code，
直接说「把 posts/ep12 发到 B站和抖音」「检查一下各平台登录」「帮我写一个小红书图文的 post.yaml」即可。Claude 会：

1. 用 `mpub new` / `mpub validate` 准备和检查清单；
2. **先 `--dry-run`**，把平台、账号、标题、定时、声明等汇总给你，**等你明确确认后**才真正发布；
3. 登录失效时帮你跑 `mpub login` 并把二维码图片给你看，抖音要短信验证码时向你要验证码；
4. 结果不确定时提醒你先去平台后台确认，**不会自动重发**。

也可以只做**发布前准备**：说「帮我做 <作品目录> 的发布前准备」，Claude 会核对或补齐各平台的标题、简介、标签、封面、声明、合集，
检查知乎文章的 Markdown 渲染（如中文标点紧贴 `**` 导致加粗失效）、小红书等纯文本正文里残留的 Markdown 符号、知乎话题有没有同名的，
做到确认卡片为止，不发布（见 [prepare.md](.claude/skills/multipub/references/prepare.md)、[content-check.md](.claude/skills/multipub/references/content-check.md)）。

想在任何目录都能用，把 `.claude\skills\multipub` 复制到 `%USERPROFILE%\.claude\skills\multipub`，
并在 SKILL.md 里把命令改成带仓库路径的形式（见 SKILL.md 末尾「全局安装」）。
自己的写作规范、作品目录约定、封面工具、合集表等可以补进全局那一份，哪些适合补、补在哪，见 SKILL.md「本机适配」。

## 开发

```powershell
uv sync                          # 含 dev 组（pytest）
uv run pytest -q                 # 全部离线测试，不需要浏览器和账号
```

`tests/sim/` 下的浏览器模拟测试默认跳过；设置 `MULTIPUB_SIM_CHROMIUM` 指向一个 Chromium 内核浏览器即可运行，本机 Chrome 也可以：

```powershell
$env:MULTIPUB_SIM_CHROMIUM = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
uv run pytest -q tests/sim/test_sim_xhs.py -k "not sigterm"   # sigterm 那个测试读 /proc，只能在 Linux 上跑
```

pip 用户：`pip install -e ".[dev]"`。可选 extra `terminal-qr`（opencv）用于在日志里打印字符画二维码。

## 许可证

[MIT](LICENSE)。包含的第三方代码（social-auto-upload 等）与运行时下载的程序（biliup、Chromium）的许可证见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本项目与各平台官方无关。
