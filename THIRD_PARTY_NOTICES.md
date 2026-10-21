# 第三方声明（Third-Party Notices）

multipub 本身以 MIT 许可证发布（见 [`LICENSE`](LICENSE)）。本文件列出仓库里包含的第三方代码、
运行时下载的第三方程序，以及只参考过、没有复制代码的项目。

## 1. 仓库里包含的第三方代码

### 1.1 social-auto-upload（vendored）

- 位置：[`src/multipub/_sau/`](src/multipub/_sau/)（抖音、小红书的浏览器发布流程）
- 上游：<https://github.com/dreammis/social-auto-upload>
- 固定提交：`0012d2c355f88f683cc38dde2a2db209e14091bc`（见 `src/multipub/_sau/__init__.py` 的 `UPSTREAM_COMMIT`）
- 许可证：MIT，原文随代码放在 [`src/multipub/_sau/LICENSE`](src/multipub/_sau/LICENSE)
- 改动：只取了需要的文件，所有代码改动都用 `# multipub patch:` 注释标出，并逐条记在
  [`src/multipub/_sau/PATCHES.md`](src/multipub/_sau/PATCHES.md)

```
MIT License

Copyright (c) 2023 dreammis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### 1.2 omnipost（一个函数）

- 位置：`src/multipub/_sau/uploader/xiaohongshu_uploader/main.py` 里的 `set_ai_content_declaration()`
  （小红书「笔记含AI合成内容」声明），以及选项文字常量 `XHS_AI_DECLARATION`
- 来源：<https://github.com/rehatRobot/omnipost> 提交 `23f30666e0fbbfc6373ed97394daea82acb0a0e5`，
  `uploader/xiaohongshu_uploader/main.py:420-492`；移植时改了兜底定位器（见 PATCHES.md 第 c 条）
- 许可证：MIT

```
MIT License

Copyright (c) 2021-2026 dreammis and social-auto-upload contributors
Copyright (c) 2026 contributors of this repository

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### 1.3 puppeteer-extra stealth evasions（随 social-auto-upload 一起 vendored）

- 位置：`src/multipub/_sau/utils/stealth.min.js`（原样，来自上游 SAU）
- 来源：由 <https://github.com/berstend/puppeteer-extra/tree/master/packages/extract-stealth-evasions> 生成，
  文件头注明 `License: MIT`（puppeteer-extra 项目，作者 berstend）

## 2. 运行时下载、不随仓库分发的程序

### 2.1 biliup（B站视频上传）

- 不在仓库里。`mpub setup` 从 GitHub Releases 下载固定版本 **v1.3.1** 到
  `<数据目录>\tools\biliup\v1.3.1\`（可在 config.toml 用 `biliup_version` / `biliup_path` 改），
  v1.3.1 的七个发布包 sha256 已内置校验，见 [`docs/platforms/bilibili.md`](docs/platforms/bilibili.md)
- 上游：<https://github.com/biliup/biliup>
- 许可证：MIT，`Copyright (c) 2019 ForgQi`（2026-10-21 核对
  <https://raw.githubusercontent.com/biliup/biliup/v1.3.1/LICENSE>）

### 2.2 浏览器内核

- `mpub setup` 调用 `patchright install chromium` 下载 Chrome for Testing（Chromium，BSD 风格许可证及其组件许可证），
  装在 patchright 的默认目录（Windows 为 `%LOCALAPPDATA%\ms-playwright`），不随仓库分发。
  也可以改用本机已安装的 Chrome / Edge（见 [`docs/windows.md`](docs/windows.md)）。

## 3. Python 依赖（通过 PyPI 安装，不随仓库分发）

| 包 | 许可证 |
|---|---|
| patchright | Apache-2.0 |
| loguru | MIT |
| httpx | BSD-3-Clause |
| PyYAML | MIT |
| mistune | BSD-3-Clause |
| Pillow | MIT-CMU |
| segno | BSD-3-Clause |
| filelock | MIT |
| opencv-python-headless、numpy（可选 extra `terminal-qr`） | Apache-2.0 / BSD-3-Clause |
| pytest（开发） / pytest-asyncio（开发） | MIT / Apache-2.0 |

## 4. 只参考过、没有复制代码的项目

以下项目的公开代码或文档用于了解平台接口、请求字段、页面结构和 HTML 写法；multipub 里对应的代码是独立实现：

| 项目 | 许可证 | 参考了什么 | multipub 位置 |
|---|---|---|---|
| [dongguaguaguagua/zhihu_obsidian](https://github.com/dongguaguaguagua/zhihu_obsidian) | 0BSD | 知乎文章接口、图片上传、Markdown → 知乎 HTML 规则 | `platforms/zhihu_api.py`、`platforms/zhihu_html.py` |
| [KrisTHL181/zhihu-cli](https://github.com/KrisTHL181/zhihu-cli) | MIT | 知乎写接口、想法图片字段 | `platforms/zhihu_api.py` |
| [BAIGUANGMEI/zhihu-cli](https://github.com/BAIGUANGMEI/zhihu-cli) | Apache-2.0 | 知乎发布接口请求体 | `platforms/zhihu_api.py` |
| [leaperone/MultiPost-Extension](https://github.com/leaperone/MultiPost-Extension) | Apache-2.0 | 知乎想法图片数量上限（注释） | `platforms/zhihu.py` |
| [Linwei-Chen/zhihu-article-publisher-skill](https://github.com/Linwei-Chen/zhihu-article-publisher-skill)、[wechatsync/Wechatsync](https://github.com/wechatsync/Wechatsync) | —（前者未附许可证）/ GPL-3.0 | 只核对了知乎表格、公式、脚注的 HTML 写法（事实性格式），未复制代码 | `platforms/zhihu_html.py` |
| [rehatRobot/omnipost](https://github.com/rehatRobot/omnipost) | MIT | B站创作声明 `--extra-fields` 用法（除 1.2 节移植的函数外） | `platforms/bili_biliup.py` |
| [zhao-yc/postsail](https://github.com/zhao-yc/postsail) | MIT | 抖音创建作品接口回执的监听方式 | `_sau/uploader/douyin_uploader/main.py`（multipub patch） |
| [ReaJason/xhs](https://github.com/ReaJason/xhs) | — | 小红书创建笔记接口路径 | `_sau/uploader/xiaohongshu_uploader/main.py`（multipub patch） |
| [biliup/biliup](https://github.com/biliup/biliup) 源码 | MIT | 命令行参数、登录文件格式、TV 扫码登录接口、令牌校验 | `platforms/bili_biliup.py`、`bili_cookies.py`、`bili_http.py` |
| B站网页端发布器脚本（t.bilibili.com） | — | 动态发布框选择器、WBI 签名、`create/dyn` 请求字段 | `platforms/bili_note.py`、`bili_http.py` |

multipub **没有**使用 GPL 许可的 bilibili-api。
