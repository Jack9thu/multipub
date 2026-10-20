from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ["MULTIPUB_ENABLE_FAKE"] = "1"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("MULTIPUB_HOME", str(h))
    return h


@pytest.fixture
def media(tmp_path: Path) -> Path:
    """生成一组假的媒体文件（真实 JPEG/PNG，内容是纯色图）。"""
    from PIL import Image

    d = tmp_path / "post"
    (d / "images").mkdir(parents=True)
    for i, size in enumerate([(1080, 1440), (1080, 1080), (800, 600)], start=1):
        Image.new("RGB", size, (200, 100 + i * 30, 50)).save(d / "images" / f"{i}.jpg", "JPEG")
    Image.new("RGB", (1280, 720), (10, 20, 30)).save(d / "cover.jpg", "JPEG")
    (d / "video.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1024)
    return d


def write_post(d: Path, text: str) -> Path:
    p = d / "post.yaml"
    p.write_text(text, encoding="utf-8")
    return p
