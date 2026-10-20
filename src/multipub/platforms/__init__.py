"""平台注册表。适配器模块按需导入，主进程不会提前加载 patchright 等重依赖。"""

from __future__ import annotations

import importlib
import os

from .base import PlatformAdapter

_REGISTRY: dict[str, str] = {
    "bilibili": "multipub.platforms.bilibili:BilibiliAdapter",
    "douyin": "multipub.platforms.douyin:DouyinAdapter",
    "xiaohongshu": "multipub.platforms.xiaohongshu:XiaohongshuAdapter",
    "zhihu": "multipub.platforms.zhihu:ZhihuAdapter",
}

DISPLAY_NAMES = {
    "bilibili": "B站",
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "zhihu": "知乎",
}

if os.environ.get("MULTIPUB_ENABLE_FAKE") == "1":  # 仅测试用
    _REGISTRY["fake"] = "multipub.platforms.fake:FakeAdapter"
    DISPLAY_NAMES["fake"] = "Fake"


def available() -> list[str]:
    return list(_REGISTRY)


def load_adapter(name: str) -> type[PlatformAdapter]:
    try:
        target = _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"未注册的平台: {name}") from exc
    module_name, _, attr = target.partition(":")
    return getattr(importlib.import_module(module_name), attr)
