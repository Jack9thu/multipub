<#
.SYNOPSIS
    multipub 一键安装：不需要管理员权限，可以重复运行。

.DESCRIPTION
    1. 检查 uv；没有时显示官方安装命令，征得同意后才运行。
    2. uv sync 安装依赖（失败时退回 uv venv + uv pip install -e .）。
    3. uv run mpub setup：安装浏览器内核、准备 biliup（会问你用本机已有的还是下载；已装好的会跳过）。
    4. uv run mpub doctor：环境自检。

.PARAMETER China
    国内网络：本次运行用国内镜像装依赖（PyPI：阿里云；Python 本体：npmmirror），
    不下载 Chromium（镜像上没有 patchright 需要的版本），改用 Windows 自带的 Edge；
    config.toml 不存在时生成一份（浏览器用 Edge，biliup 走 GitHub 镜像）。不改系统设置。

.PARAMETER SkipBrowser
    不下载 Chromium（已在 config.toml 里改用本机 Chrome / Edge 时使用）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -China
#>
[CmdletBinding()]
param(
    [switch]$China,
    [switch]$SkipBrowser
)

Set-StrictMode -Version Latest
# 不用 'Stop'：Windows PowerShell 5.1 在 stderr 被重定向时会把原生命令的 stderr 当成错误抛出，
# 而 uv / mpub 的进度信息都写在 stderr。原生命令一律检查 $LASTEXITCODE。
$ErrorActionPreference = 'Continue'

$UvInstallCommand = 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
$PypiMirror = 'https://mirrors.aliyun.com/pypi/simple/'
$PythonMirror = 'https://registry.npmmirror.com/-/binary/python-build-standalone'
$GithubMirror = 'https://ghfast.top/'
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Write-Hint([string]$Text) {
    Write-Host "    $Text" -ForegroundColor Yellow
}

function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    # 运行原生命令并返回退出码；输出直接显示（stdout 经 Out-Host，按 UTF-8 解码）
    Write-Host "    > $(Split-Path -Leaf $Exe) $($Arguments -join ' ')" -ForegroundColor DarkGray
    & $Exe @Arguments | Out-Host
    return $LASTEXITCODE
}

function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($candidate in @(
            (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
            (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe'))) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

function Install-Uv {
    Write-Hint '没有找到 uv。官方安装命令（装到 %USERPROFILE%\.local\bin，不需要管理员权限）：'
    Write-Host ''
    Write-Host "    $UvInstallCommand"
    Write-Host ''
    $answer = Read-Host '现在运行上面的命令安装 uv？[y/N]'
    if ($answer.Trim().ToLower() -notin @('y', 'yes')) {
        Write-Hint '已取消。手动安装 uv 后重新运行本脚本即可（也可以 winget install --id=astral-sh.uv -e）。'
        return $null
    }
    powershell -NoProfile -ExecutionPolicy ByPass -Command 'irm https://astral.sh/uv/install.ps1 | iex' | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Hint "uv 安装失败（退出码 $LASTEXITCODE）。"
        return $null
    }
    # 安装程序会改用户 PATH，但当前会话还看不到，手动补上
    $bin = Join-Path $env:USERPROFILE '.local\bin'
    if (($env:Path -split ';') -notcontains $bin) { $env:Path = "$bin;$env:Path" }
    return Find-Uv
}

function Get-DataHome {
    if ($env:MULTIPUB_HOME) { return $env:MULTIPUB_HOME }
    return (Join-Path $env:LOCALAPPDATA 'multipub')
}

function Write-ChinaConfig {
    $dataHome = Get-DataHome
    $cfg = Join-Path $dataHome 'config.toml'
    if (Test-Path $cfg) {
        Write-Hint "已有 $cfg，不改动。国内网络请确认里面有 [browser] channel = ""msedge""（或 ""chrome""）和 github_mirror。"
        return
    }
    New-Item -ItemType Directory -Force -Path $dataHome | Out-Null
    $text = @"
# 由 scripts\install.ps1 -China 生成，可随时修改（说明见 docs\config.example.toml）
# biliup 从 GitHub 下载，国内走镜像
github_mirror = "$GithubMirror"

[browser]
# 国内下载不到 patchright 的 Chromium，改用 Windows 自带的 Edge；装了 Chrome 也可以改成 "chrome"
channel = "msedge"
"@
    # UTF-8 不带 BOM（Windows PowerShell 5.1 的 Set-Content -Encoding UTF8 会带 BOM）
    [IO.File]::WriteAllText($cfg, $text, (New-Object System.Text.UTF8Encoding $false))
    Write-Hint "已生成 $cfg：浏览器用 Edge，biliup 从 $GithubMirror 下载"
}

function Invoke-Main {
    # 返回退出码：0 全部成功；1 有步骤失败（见提示）
    # ---------------------------------------------------------------- 1. uv
    Write-Step '检查 uv'
    $uv = Find-Uv
    if (-not $uv) { $uv = Install-Uv }
    if (-not $uv) { return 1 }
    Invoke-Native $uv @('--version') | Out-Null

    # ---------------------------------------------------------------- 2. 依赖
    if ($China) {
        # 只影响本次运行；已经设置过的不覆盖
        if (-not $env:UV_DEFAULT_INDEX) { $env:UV_DEFAULT_INDEX = $PypiMirror }
        if (-not $env:UV_PYTHON_INSTALL_MIRROR) { $env:UV_PYTHON_INSTALL_MIRROR = $PythonMirror }
        Write-Hint "本次运行使用镜像：PyPI $env:UV_DEFAULT_INDEX；Python $env:UV_PYTHON_INSTALL_MIRROR"
    }
    Write-Step '安装依赖（uv sync）'
    $code = Invoke-Native $uv @('sync')
    if ($code -ne 0) {
        Write-Hint "uv sync 失败（退出码 $code），改用 uv venv + uv pip install -e ."
        if (-not $China) {
            Write-Hint '国内网络建议加 -China 重新运行（PyPI / Python 走国内镜像）。'
        }
        if (-not (Test-Path (Join-Path $RepoRoot '.venv\Scripts\python.exe'))) {
            $code = Invoke-Native $uv @('venv')
            if ($code -ne 0) { Write-Hint 'uv venv 失败'; return 1 }
        }
        $code = Invoke-Native $uv @('pip', 'install', '-e', '.')
        if ($code -ne 0) { Write-Hint '依赖安装失败，请检查网络后重试'; return 1 }
    }

    # ---------------------------------------------------------------- 3. mpub setup
    $result = 0
    if ($China) {
        # npmmirror 没有 patchright 1.58 需要的 Chromium 145，直连又要访问 Google 存储：国内改用系统自带的 Edge
        Write-ChinaConfig
        $SkipBrowser = $true
    }
    $setupArgs = @('run', 'mpub', 'setup')
    if ($SkipBrowser) { $setupArgs += '--skip-browser' }
    Write-Step '安装浏览器内核、准备 biliup（mpub setup，会问你 biliup 用哪个）'
    $code = Invoke-Native $uv $setupArgs
    if ($code -ne 0) {
        $result = 1
        Write-Hint "mpub setup 没有全部成功（退出码 $code），常见处理："
        Write-Hint '- Chromium 下载失败：镜像可能没有 patchright 需要的版本。可以改用本机 Chrome / Edge，'
        Write-Hint '  在 %LOCALAPPDATA%\multipub\config.toml 写入  [browser]  channel = "chrome"（或 "msedge"），'
        Write-Hint '  然后加 -SkipBrowser 重新运行本脚本。'
        Write-Hint '- biliup 下载失败：在 config.toml 设置  github_mirror = "https://ghfast.top/"  后重试；'
        Write-Hint '  或用本机已有的 biliup：uv run mpub biliup find，再 uv run mpub biliup use <编号或路径>。'
        Write-Hint '详见 docs\windows.md'
    }

    # ---------------------------------------------------------------- 4. mpub doctor
    Write-Step '环境自检（mpub doctor）'
    $code = Invoke-Native $uv @('run', 'mpub', 'doctor')
    if ($code -ne 0) {
        $result = 1
        Write-Hint '有检查项没通过（见上面的 ✘；「未登录」不算）。处理后可重复运行本脚本。'
    }

    $vcRuntime = Join-Path $env:SystemRoot 'System32\vcruntime140.dll'
    if (-not (Test-Path $vcRuntime)) {
        Write-Hint '没有找到 VC++ 运行库（vcruntime140.dll），B站视频上传（biliup.exe）需要它。安装：'
        Write-Hint '  winget install Microsoft.VCRedist.2015+.x64'
        Write-Hint '  或下载 https://aka.ms/vs/17/release/vc_redist.x64.exe'
    }

    Write-Step '下一步：逐个平台扫码登录'
    Write-Host '    uv run mpub login bilibili --account main'
    Write-Host '    uv run mpub login douyin --account main'
    Write-Host '    uv run mpub login xiaohongshu --account main'
    Write-Host '    uv run mpub login zhihu --account main'
    Write-Host '    uv run mpub check --all'
    return $result
}

if (-not (Test-Path (Join-Path $RepoRoot 'pyproject.toml'))) {
    Write-Host "找不到 $RepoRoot\pyproject.toml：请在 multipub 仓库里运行 scripts\install.ps1" -ForegroundColor Red
    exit 2
}

# mpub 的输出是 UTF-8；没有控制台（输出被重定向）时设置会失败，忽略即可
$savedEncoding = $null
try {
    $savedEncoding = [Console]::OutputEncoding
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
}
catch { }
Push-Location $RepoRoot
try {
    $exitCode = [int](@(Invoke-Main)[-1])
}
finally {
    Pop-Location
    if ($savedEncoding) { try { [Console]::OutputEncoding = $savedEncoding } catch { } }
}
exit $exitCode
