"""SAU 日志。

multipub patch: 上游在 import 时 ``logger.remove()`` 全部 sink、把带颜色的日志写到 stdout、
reconfigure stdout/stderr，并在 BASE_DIR/logs 下为 12 个平台各建一个文件 sink。
worker 的 stdout 是 JSON 事件通道，所以这里改为：

- import 时没有任何副作用，只创建 bind 过的 logger；
- ``setup(log_dir)``（由 conf.configure() 调用）安装 sink：stderr（无颜色）+ log_dir 下的文件；
- 不碰 sys.stdout，不开 diagnose（避免把变量值、cookie 写进日志）。
"""

import sys
from pathlib import Path

from loguru import logger

LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level}: {message}"
# 只保留 multipub 用到的业务 logger
BUSINESS_NAMES = ("douyin", "xiaohongshu", "sau")

_handler_ids: list[int] = []


def create_logger(log_name: str):
    """multipub patch: 只 bind，不在 import 时添加文件 sink。"""
    return logger.bind(business_name=log_name)


def remove_sinks() -> None:
    """移除 setup() 安装的 sink。"""
    for handler_id in _handler_ids:
        try:
            logger.remove(handler_id)
        except ValueError:
            pass
    _handler_ids.clear()


def setup(log_dir: Path | None) -> None:
    """安装（或重装）SAU 的日志 sink；可重复调用。"""
    remove_sinks()
    try:
        logger.remove(0)  # loguru 默认 sink（stderr、可能带颜色），换成下面的无颜色 sink
    except ValueError:
        pass
    _handler_ids.append(logger.add(sys.stderr, colorize=False, format=LOG_FORMAT, level="INFO"))
    if log_dir is None:
        return
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    for name in BUSINESS_NAMES:
        _handler_ids.append(logger.add(
            log_dir / f"{name}.log",
            filter=lambda record, _name=name: record["extra"].get("business_name") == _name,
            level="INFO", format=LOG_FORMAT, rotation="10 MB", retention="10 days",
            encoding="utf-8", colorize=False, backtrace=False, diagnose=False, delay=True,
        ))


douyin_logger = create_logger("douyin")
xiaohongshu_logger = create_logger("xiaohongshu")
sau_logger = create_logger("sau")
