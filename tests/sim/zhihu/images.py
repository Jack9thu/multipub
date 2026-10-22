"""仿真用的图片字节（真能被 Pillow / 浏览器解码）。"""

from __future__ import annotations

import io


def _save(size: tuple[int, int], color: tuple[int, int, int], fmt: str, **kw) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt, **kw)
    return buf.getvalue()


def png(w: int = 64, h: int = 48, color: tuple[int, int, int] = (0, 132, 255)) -> bytes:
    return _save((w, h), color, "PNG")


def jpeg(w: int = 64, h: int = 48, color: tuple[int, int, int] = (200, 80, 20)) -> bytes:
    return _save((w, h), color, "JPEG", quality=90)


def webp(w: int = 64, h: int = 48, color: tuple[int, int, int] = (20, 160, 90)) -> bytes:
    return _save((w, h), color, "WEBP")
