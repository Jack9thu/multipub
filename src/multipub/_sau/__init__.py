"""vendored 的 social-auto-upload（SAU）子集：抖音 / 小红书的浏览器发布流程。

上游: https://github.com/dreammis/social-auto-upload （MIT，见同目录 LICENSE）
固定版本: UPSTREAM_COMMIT。所有改动都标了 "# multipub patch:" 并记录在 PATCHES.md。

用法（只在 worker 子进程里）：先 ``multipub._sau.conf.configure(settings, headless)``，
再导入 ``multipub._sau.uploader.douyin_uploader.main`` 等模块。
本文件不导入任何重依赖。
"""

UPSTREAM_REPO = "https://github.com/dreammis/social-auto-upload"
UPSTREAM_COMMIT = "0012d2c355f88f683cc38dde2a2db209e14091bc"
