"""Write a build's .ico, recoloured for the server or client variant.

The window icon is set at run time, but Windows takes the *taskbar* icon from
the icon compiled into the .exe - so all three builds shared one icon there no
matter what the running app did. build.ps1 calls this to produce a per-variant
.ico and hands it to PyInstaller.

Usage:  py -3 make_variant_icon.py <server|client> <output.ico>

Writes a multi-size ICO directly rather than relying on Qt's ICO writer, which
is not guaranteed to be built into every PyQt wheel. An ICO is a small header
plus one PNG per size, so this needs nothing beyond what the app already has.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

# The icon work needs a QGuiApplication for image handling but no display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt   # noqa: E402
from PyQt6.QtGui import QGuiApplication, QImage               # noqa: E402

import app_resources                                          # noqa: E402
import app_variant                                            # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256)

_APP = None      # the QGuiApplication, kept alive while the icon is built


def png_bytes(image: QImage) -> bytes:
    # The QByteArray must outlive the QBuffer that writes into it - passing a
    # temporary lets Python collect it mid-write and takes the process with it.
    store = QByteArray()
    buf = QBuffer(store)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    buf.close()
    return bytes(store)


def write_ico(images: list[QImage], out: Path) -> None:
    """ICONDIR + one ICONDIRENTRY per image, then the PNG payloads."""
    payloads = [png_bytes(img) for img in images]
    header = struct.pack("<HHH", 0, 1, len(images))     # reserved, type=icon, count
    offset = len(header) + 16 * len(images)
    entries = b""
    for img, data in zip(images, payloads):
        w = 0 if img.width() >= 256 else img.width()    # 0 means 256 in an ICO
        h = 0 if img.height() >= 256 else img.height()
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    out.write_bytes(header + entries + b"".join(payloads))


def main() -> int:
    variant = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    if variant not in (app_variant.SERVER, app_variant.CLIENT):
        print(f"usage: {Path(__file__).name} <server|client> <output.ico>")
        return 2
    out = Path(sys.argv[2]).resolve()

    global _APP
    _APP = QGuiApplication([])           # held for the process's lifetime
    src = app_resources._base_icon_path()
    if src is None:
        print("resources/app_icon.png not found")
        return 1
    base = QImage(str(src))
    if base.isNull():
        print(f"could not read {src}")
        return 1

    top, bottom = app_resources._GRADIENTS[variant]
    tinted = app_resources._recoloured(base, top, bottom)
    images = [
        tinted.scaled(s, s,
                      Qt.AspectRatioMode.KeepAspectRatio,
                      Qt.TransformationMode.SmoothTransformation)
        for s in SIZES
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    write_ico(images, out)
    print(f"wrote {out}  ({out.stat().st_size / 1024:.0f} KB, "
          f"{len(SIZES)} sizes, {variant})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
