# multipub 常见问题

先看结果的 `message`，再看 `evidence` 里的文件：`<平台>-<动作>.log` 是 worker 日志，`*.png` 是出错时的整页截图
（可以用 Read 直接看图判断页面停在哪里）。都在 `<数据目录>\runs\<run_id>\<平台>\`（登录在 `runs\<时间>-login\`，检查在 `runs\<时间>-check\`）。
抖音/小红书流程的详细日志另在 `<数据目录>\sau\logs\`。

## 安装与环境

| 现象 | 处理 |
|---|---|
| `uv: command not found` | 安装 uv：`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"`，重开终端；或运行 `scripts\install.ps1` |
| `doctor` 里 `browser` ✘ | `uv run mpub setup`。下载慢/失败：改用本机浏览器，在 `<数据目录>\config.toml` 写 `[browser]` `channel = "chrome"`（或 `"msedge"`），再 `uv run mpub setup --skip-browser` |
| Chromium 镜像 404 | patchright 1.58.2 需要 Chrome for Testing 145.0.7632.6，npmmirror（2026-10）没有这个版本。去掉 `PLAYWRIGHT_DOWNLOAD_HOST` 直连，或改用本机 Chrome / Edge（同上） |
| `doctor` 里 `bilibili.biliup` ✘ / 发布报「未找到 biliup」 | `uv run mpub setup`；GitHub 慢时 config 里设 `github_mirror = "https://ghfast.top/"`；被杀毒软件隔离见 `docs/windows.md` |
| biliup「sha256 不匹配」 | 下载被镜像或网络篡改/截断，已拒绝安装。换镜像或直连后重试 |
| doctor 报 biliup 二进制与记录不一致 | 删除 `<数据目录>\tools\biliup\<版本>` 整个目录后 `uv run mpub setup` |
| PowerShell 里捕获 `--json` 输出中文乱码 | 先 `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8` |
| `mpub: error: unrecognized arguments: --json` | `--json` 要写在子命令后面：`mpub check --json` |

## 登录

| 现象 | 处理 |
|---|---|
| 没看到 `qrcode` 事件 / 没弹窗口 | 抖音/小红书/知乎默认会弹浏览器窗口，二维码在窗口里；也会写到 `<数据目录>\qrcode\<平台>-<账号>.png`。抖音定位不到二维码时上报的是整页截图（`note` 会说明） |
| B站登录没有浏览器 | 正常：B站用 TV 端扫码，只有 PNG。打开图片用 B站 App 扫，手机上确认「TV 端登录」。二维码 3 分钟过期自动刷新（最多 3 次） |
| 登录超时 `timeout` | 默认约 3.5~4 分钟内要扫完。重新运行 `mpub login`；需要更久就调大 config `[timeouts] login` |
| 抖音扫码后要短信验证 | 只能在弹出的浏览器窗口里完成，所以抖音登录不要加 `--headless` |
| 登录说已登录，发布却说要登录 | B站会同时校验网页登录和 biliup 令牌，任一失效都会要求重新扫码。其他平台：删除 `<数据目录>\accounts\<平台>\<账号>.json` 后重新登录 |
| 想换账号 | 用新的 `--account` 名字登录；或删除旧的登录文件再登录 |
| 抖音 `check` 说失效但其实能用 | 上游提示无头浏览器偶尔被误判；设置环境变量 `DOUYIN_COOKIE_AUTH_HEADLESS=false` 再 check；网络异常也会被判为失效 |
| 小红书登录一直超时 | 登录页切换按钮依赖哈希类名，改版后会失效；用有头模式看窗口里的情况，必要时手动点到扫码页 |
| 知乎 `risk_control`（40352） | `uv run mpub login zhihu --account <账号>`（不要加 `--headless`）会带着现有登录态打开验证页，完成验证后自动保存，再发布 |

## 校验

| 现象 | 处理 |
|---|---|
| `tid` 必填 | B站视频在 `platforms.bilibili.tid` 写分区 id（231 计算机技术、21 日常、160 生活、95 数码、201 科学科普、138 搞笑、36 知识） |
| 标题超长 | 在 `platforms.<平台>.title` 单独写短标题：抖音视频 ≤30、小红书 ≤20、抖音图文 ≤20、B站动态 ≤20、B站视频 ≤80、知乎文章 ≤100 |
| 定时报错 | 本地时间 `"YYYY-MM-DD HH:MM"` 加引号。B站视频 2 小时~15 天；B站动态 5 分钟~7 天；抖音、小红书 2 小时以上；知乎不支持（去掉 `schedule` 或 `--to` 排除知乎） |
| 图片格式不支持 | B站动态 jpg/png/gif；抖音/小红书 jpg/png/webp/bmp（不支持 GIF）；知乎 jpg/png/gif/webp/bmp |
| 路径找不到 | 相对路径按 post.yaml 所在目录；Windows 路径在 YAML 里用单引号或正斜杠；知乎正文图片相对 `body_file` 所在目录 |
| `未知参数，会被忽略` | 平台私有参数拼错了，对照 `docs/manifest.md` |
| dry-run 通过，真发时被跳过「同一内容已于…发过」 | dry-run 不查重。确实要再发一次需要用户明确同意后加 `--force` |

## 发布

| 现象 | 处理 |
|---|---|
| `非交互环境需要加 --yes` | Claude Code 里必须加 `--yes`（在用户确认之后） |
| `正被另一个任务使用` | 同一平台同一账号已有任务在跑（可能是另一个终端）。等它结束 |
| 抖音要短信验证码 | 看到 `needs_input` 后问用户要验证码，`uv run mpub verify-code douyin <验证码>`。默认等 5 分钟（config `[platforms.douyin] sms_timeout`）。不要同时用两个抖音账号发布（验证码文件共用） |
| 抖音「自主声明设置失败」（`validation`） | `declaration` 必须与页面上的选项文字完全一致，或设为 `false` 不声明 |
| 抖音/小红书某一步 `timeout` | 平台可能改版了：看截图停在哪一步；加 `--headed` 重试观察。上传大文件：调大 config `[timeouts] publish_video`（上传等待 = 它 − 600 秒），并加同样的 `--timeout` |
| 小红书发布页出现上次没发出去的内容 | 小红书可能恢复草稿；先在网页上清掉草稿再发 |
| 小红书没有作品链接 | 正常：小红书不返回可分享链接，`item_id` 是笔记 ID，到「笔记管理」查看 |
| 抖音 `submitted` 但没有链接 | 没监听到创建接口回执，到「作品管理」查看 |
| B站动态 `consent_required` | 见 SKILL.md：让用户选择在清单写 `confirm_rules: true`、在 config 写 `confirm_dynamic_rules = true`，或 `--headed` 亲手确认 |
| B站动态 `captcha` | 出现极验验证码：`--headed` 重发，用户在窗口里完成 |
| B站 `code 4126021` | 账号没绑定手机，不能发动态 |
| B站视频「上传超过 N 秒，已终止，尚未投稿」 | 调大 `[timeouts] publish_video` / `--timeout`；可换上传线路 `platforms.bilibili.line`。结果带 `submit_started: true`，重发前和用户确认 |
| B站定时 2~4 小时被拒 | biliup 注释写「大于 4 小时」，创作中心是 2 小时；改成 4 小时以后 |
| 知乎 `10003 请求参数异常，请升级客户端后重试` | 知乎可能开始要求 `x-zse-96` 签名，接口发布暂时不可用；用 `draft: true` 存草稿后在网页手动发布，并报告问题 |
| 知乎文章 `failed` 但消息里有「草稿已保存」 | 内容在草稿箱，`url` 是编辑页，可修改后手动发布，不要重复建 |
| 知乎话题没加上 | 话题接口不稳定，只警告继续发布；到文章编辑页手动补 |
| `unknown_outcome` | 先去平台后台确认（地址见 cli-contract.md），**不要重发** |

## 报告问题时附上

`mpub doctor --json` 输出、结果 JSON（去掉账号信息）、`evidence` 里的日志和截图。不要附 `accounts\` 下的任何文件。
