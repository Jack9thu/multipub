# 在 Windows 上使用

multipub 以 Windows 10/11 为主要平台。下面的命令都在 **PowerShell** 里、仓库根目录下运行。

## 终端：用 Windows Terminal

- 推荐 [Windows Terminal](https://aka.ms/terminal)（Win11 自带）：中文、emoji、`✔ ✘` 显示正常，
  `mpub login bilibili --interactive` 的字符画二维码也显示得完整。
- 如果用老的「命令提示符 / PowerShell」黑窗口（conhost）：**不要在发布过程中用鼠标点选窗口里的文字**。
  「快速编辑模式」下选中文字会暂停程序输出，发布进程会卡住直到你按 Esc / 回车。
- `mpub` 自己把输出设为 UTF-8，在终端里直接看没有乱码。如果要在 PowerShell 里**捕获** `--json` 输出（赋给变量、管道给
  `ConvertFrom-Json`），先把控制台编码改成 UTF-8，否则中文会被按 GBK 解码成乱码：

  ```powershell
  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  $r = uv run mpub check --json | Out-String | ConvertFrom-Json
  $r.results | Format-Table platform, account, status, message
  ```

## 安装

```powershell
# 1. uv（官方安装命令；或者 winget install --id=astral-sh.uv -e）
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# 重开终端后：
git clone https://github.com/Jack9thu/multipub
cd multipub
uv sync                      # 或: uv venv; uv pip install -e .
uv run mpub setup
uv run mpub doctor
```

也可以用脚本一步完成（不需要管理员权限，可重复运行）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1           # 普通网络
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -China    # 国内网络
```

`-China` 做的事（只影响这一次运行）：依赖从阿里云 PyPI 镜像装，需要时 Python 本体从 npmmirror 下载；
不下载 Chromium（镜像上没有 patchright 需要的版本），改用 Windows 自带的 Edge；
数据目录里还没有 `config.toml` 时生成一份（`[browser] channel = "msedge"`、`github_mirror = "https://ghfast.top/"`），
已有的不会改。`ghfast.top` 是第三方 GitHub 加速，不可用时换成别的镜像或留空直连。

uv 默认装在 `%USERPROFILE%\.local\bin\uv.exe`。想省掉每次的 `uv run`，可以激活虚拟环境：
`.\.venv\Scripts\Activate.ps1`（提示「禁止运行脚本」时先运行一次
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`），之后直接用 `mpub ...`。

## B站视频：需要 VC++ 运行库

B站视频用 biliup 上传，Windows 版 `biliup.exe` 依赖 **Microsoft Visual C++ 2015-2022 运行库（x64）**。
很多电脑已经装过；刚重装的系统可能没有。缺少时 `mpub doctor` / `mpub setup` 的 `bilibili.biliup` 一项会显示 ✘
并提示「缺少 Microsoft Visual C++ 运行库（VCRUNTIME140.dll）」，发布前也会先检查，不会在没装好的情况下开始投稿。安装：

```powershell
winget install Microsoft.VCRedist.2015+.x64
# 或下载安装 https://aka.ms/vs/17/release/vc_redist.x64.exe
```

## 中文路径

- 仓库、素材、数据目录都可以在中文路径下，路径里有空格也没问题；命令行里带空格的路径加引号：
  `uv run mpub publish "D:\视频 素材\第12期"`。
- `post.yaml` 里的 Windows 路径用**单引号**或**正斜杠**：`video: 'D:\视频素材\第12期.mp4'`、`video: D:/视频素材/第12期.mp4`。
  双引号里的 `\` 是转义符，要写 `"D:\\视频素材\\第12期.mp4"`。最省事的是把素材和 `post.yaml` 放在同一个目录、写相对路径。
- 文本文件按 UTF-8 读取（记事本「另存为 UTF-8」即可，带 BOM 也行）；`config.toml` 另外也认 PowerShell 5.1
  `Out-File` 写出的 UTF-16。`config.toml` 写错时所有命令都会报「配置错误」并退出（退出码 2），按提示改正即可。
- 如果 Windows 用户名是中文、浏览器启动出现奇怪的问题，可以把数据目录和浏览器内核目录都换到纯英文路径再试：

  ```powershell
  setx MULTIPUB_HOME "D:\multipub-data"            # 之后新开的终端生效
  setx PLAYWRIGHT_BROWSERS_PATH "D:\ms-playwright" # 换了这个要重新 uv run mpub setup
  ```

## 国内网络：Chromium 镜像

`mpub setup` 调用 `patchright install chromium` 下载浏览器内核（约 150MB+）。下载慢时，可以在**当前终端**设置镜像再运行：

```powershell
$env:PLAYWRIGHT_DOWNLOAD_HOST = "https://npmmirror.com/mirrors/playwright"
uv run mpub setup
```

注意：patchright 1.58.2 下载的是 Chrome for Testing `145.0.7632.6`（路径 `builds/cft/145.0.7632.6/...`）。
2026-10-21 核对时 npmmirror 的 `builds/cft/` 目录只从 148 开始，**这个版本在镜像上是 404**，
所以镜像可能不起作用。下载失败时最简单的办法是**不下载内核，改用本机的 Chrome 或 Edge**（下一节），然后：

```powershell
uv run mpub setup --skip-browser    # 只下载 biliup
uv run mpub doctor
```

## 国内网络：biliup 走 GitHub 镜像

B站视频由 biliup 上传，`mpub setup` 从 GitHub Releases 下载 `biliupR-v1.3.1-x86_64-windows.zip`。
GitHub 访问困难时，在 `%LOCALAPPDATA%\multipub\config.toml` 里设置镜像前缀（拼在 GitHub 地址前面）：

```toml
github_mirror = "https://ghfast.top/"
```

v1.3.1 的压缩包 sha256 已内置在代码里，镜像返回的文件不对会直接拒绝安装，所以用第三方镜像也是安全的。
也可以自己下载 biliup，再用 `biliup_path` 指过去（见 [config.example.toml](config.example.toml)）：

```toml
[platforms.bilibili]
biliup_path = 'D:\tools\biliup\biliup.exe'
```

## 使用本机 Chrome / Edge

patchright 自带的 Chromium 之外，也可以用本机已安装的浏览器：不用下载内核，指纹更像真人，部分平台风控更宽松。
在 `%LOCALAPPDATA%\multipub\config.toml` 里：

```toml
[browser]
channel = "chrome"        # 本机 Google Chrome
# channel = "msedge"      # 或 Edge（Windows 10/11 都自带）
```

找不到浏览器时直接写可执行文件路径（优先于 `channel`；TOML 里用单引号，反斜杠不用转义）：

```toml
[browser]
executable_path = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
# executable_path = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
```

multipub 每次都会以独立的临时配置启动浏览器，不会读写你平时用的 Chrome 资料，你自己的 Chrome 开着也没关系。
设置了 `channel = "chrome"` / `"msedge"` 或 `executable_path` 后，`mpub setup` 会跳过 Chromium 下载，`mpub doctor` 也不再检查它。

## 杀毒软件 / SmartScreen 拦截 biliup.exe

biliup.exe 是没有代码签名的开源程序，有时会被 Windows 安全中心或第三方杀毒软件误报、隔离：

- **症状**：`mpub doctor` 显示 biliup 未安装或校验不一致；发布 B站视频时报「未找到 biliup」；或者 biliup 刚启动就退出。
- **处理**：
  1. 打开「Windows 安全中心 → 病毒和威胁防护 → 保护历史记录」，找到被隔离的 biliup.exe，选择「还原」/「允许」。
  2. 给 biliup 目录加排除项（需要时）：`%LOCALAPPDATA%\multipub\tools\biliup`。
  3. 运行 `uv run mpub doctor`：它会比对 biliup.exe 的 sha256 和安装时的记录（`tools\biliup\v1.3.1\install.json`，
     里面也有压缩包的 sha256，可与 [bilibili.md](platforms/bilibili.md) 里的表对照）。
     文件被删了就重新 `uv run mpub setup`；doctor 报「不一致」时删除整个 `tools\biliup\v1.3.1` 目录再 `mpub setup`
     （已安装的版本 setup 不会覆盖）。
- `mpub setup` 用程序下载，文件不带「来自网络」标记，一般不会弹 SmartScreen。如果你是用浏览器手动下载的 biliup
  （配合 `biliup_path`），运行前可能弹出「Windows 已保护你的电脑」：确认来源后在文件属性里勾选「解除锁定」，或运行
  `Unblock-File 'D:\tools\biliup\biliup.exe'`。
- 公司电脑的应用程序控制策略禁止运行未签名程序时，B站视频无法发布（B站图文、其他平台不受影响）。

## 登录二维码

- 抖音、小红书、知乎：`mpub login <平台> --account main` 默认弹出浏览器窗口，直接用 App 扫窗口里的二维码。
- B站：不开浏览器（TV 端扫码登录），终端会打印二维码图片路径，用看图软件打开它扫码：

  ```powershell
  Invoke-Item "$env:LOCALAPPDATA\multipub\qrcode\bilibili-main.png"
  ```

  二维码 3 分钟过期后会自动刷新（同一个文件），以最新的为准。也可以在 Windows Terminal 里用
  `uv run mpub login bilibili --account main --interactive` 跑 biliup 自带的终端二维码登录。

## 用「任务计划程序」定时发布

平台自带的定时（`schedule` 字段）是首选：内容提前传上去，到点由平台发布。知乎不支持定时，
或者你希望到点才上传时，可以用 Windows 任务计划程序在指定时间运行 `mpub publish`。

1. 先手动跑一次 `uv run mpub publish posts\ep12 --dry-run`，确认清单没问题、`uv run mpub check` 登录有效。
2. 在仓库里写一个包装脚本 `publish-ep12.cmd`（路径按实际修改；路径里有中文时保留 `chcp 65001` 那行并以 UTF-8 保存）：

   ```bat
   @echo off
   chcp 65001 >nul
   cd /d "%USERPROFILE%\multipub"
   "%USERPROFILE%\.local\bin\uv.exe" run mpub publish posts\ep12 --yes --json ^
     1> "%LOCALAPPDATA%\multipub\runs\ep12-result.json" ^
     2> "%LOCALAPPDATA%\multipub\runs\ep12-progress.log"
   ```

   计划任务里没有人能回答确认提示，所以必须加 `--yes`；结果 JSON 和进度日志分别写到两个文件。
3. 在 PowerShell 里注册一次性任务：

   ```powershell
   $action  = New-ScheduledTaskAction -Execute "$env:USERPROFILE\multipub\publish-ep12.cmd"
   $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date "2026-10-25 20:00")
   Register-ScheduledTask -TaskName "multipub-ep12" -Action $action -Trigger $trigger `
     -Description "multipub: 发布 posts\ep12"
   ```

注意事项：

- 任务要以**你自己的账号**运行：登录凭据在你的 `%LOCALAPPDATA%\multipub` 里。默认「只在用户登录时运行」即可，
  这样需要显示浏览器窗口（`--headed`、B站动态等你确认规范）时也能正常弹出。
- 电脑在那个时间要开机、不能睡眠（任务属性 → 条件 →「唤醒计算机运行此任务」可选）。
- 退出码 `3` 表示需要人处理（登录失效、验证码……），`1` 表示失败；事后看 `ep12-result.json`，
  或 `uv run mpub history`。抖音如果中途要短信验证码，计划任务里没人能输入，最多等约 10 分钟（`sms_timeout`，且不超过任务剩余时间）后返回 `sms_code_required`。
- 用完删除：`Unregister-ScheduledTask -TaskName "multipub-ep12" -Confirm:$false`。
