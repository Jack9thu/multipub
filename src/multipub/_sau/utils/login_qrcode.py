# -*- coding: utf-8 -*-
from datetime import datetime
import base64
from pathlib import Path
import sys

import segno

# multipub patch: cv2/numpy 是可选依赖（extra "terminal-qr"），没装就跳过终端二维码
try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - 取决于环境
    cv2 = None
    np = None


def terminal_qr_available() -> bool:
    return cv2 is not None and np is not None


def build_login_qrcode_path(account_file: str, suffix: str = "login_qrcode") -> Path:
    account_path = Path(account_file)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return account_path.with_name(f"{account_path.stem}_{suffix}_{timestamp}.png")


def save_data_url_image(data_url: str, output_path: Path) -> Path:
    if not data_url.startswith("data:image/"):
        raise ValueError("二维码地址不是 data:image 格式")

    header, encoded = data_url.split(",", 1)
    if ";base64" not in header:
        raise ValueError("二维码图片不是 base64 编码")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(base64.b64decode(encoded))
    return output_path


def remove_qrcode_file(qrcode_path: Path | None) -> bool:
    if qrcode_path and qrcode_path.exists():
        qrcode_path.unlink()
        return True
    return False


def decode_qrcode_from_path(qrcode_path: Path) -> str | None:
    if not terminal_qr_available():  # multipub patch: 没装 opencv 时静默跳过
        return None
    # Windows 下 cv2.imread 对中文路径不稳定，优先走 numpy+imdecode
    image = None
    try:
        data = np.fromfile(str(qrcode_path), dtype=np.uint8)
        if data.size > 0:
            image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        image = None
    if image is None:
        image = cv2.imread(str(qrcode_path))
    if image is None:
        return None

    detector = cv2.QRCodeDetector()
    qrcode_content, _, _ = detector.detectAndDecode(image)
    return qrcode_content or None


# multipub patch: 以下打印全部改到 stderr（worker 的 stdout 是 JSON 事件通道），且不再 reconfigure stdout
def _print_ascii_qrcode(qrcode, out) -> None:
    border = 1
    rows = list(qrcode.matrix)
    empty_line = "  " * (len(rows[0]) + border * 2)
    print(empty_line, file=out)
    for row in rows:
        line = ["  "] * border
        line.extend("##" if cell else "  " for cell in row)
        line.extend(["  "] * border)
        print("".join(line), file=out)
    print(empty_line, file=out)


def print_terminal_qrcode(
    qrcode_content: str,
    qrcode_path: Path,
    app_name: str,
    compact: bool = True,
    border: int = 0,
) -> None:
    out = sys.stderr
    print(file=out)
    print(f"请使用{app_name}扫描下方二维码登录：", file=out)
    qrcode = segno.make(qrcode_content, error="L", boost_error=False)
    try:
        qrcode.terminal(out=out, compact=compact, border=border)
    except (UnicodeEncodeError, OSError):
        print("当前终端不支持 Unicode 二维码字符，已切换为 ASCII 打印：", file=out)
        _print_ascii_qrcode(qrcode, out)
    print(f"也可以打开 {qrcode_path} 扫码", file=out)
    print(file=out, flush=True)
