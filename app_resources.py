"""Application icons and static assets.

The server and client builds get a badged version of the icon so the two are
tellable apart in the taskbar and Alt-Tab, where the window title is not always
visible. The badge is drawn over the base icon at run time rather than shipped
as separate files, so there is still one icon to maintain and the builds cannot
drift apart.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QIcon, QImage, QPixmap

import app_variant

# Each build gets its own gradient. Warm for the server, cool for the client, so
# the two are told apart by temperature and not only by hue - which keeps them
# distinguishable to someone who cannot separate red from green.
_GRADIENTS = {
    app_variant.SERVER: (QColor("#ff8a33"), QColor("#d62828")),   # orange -> red
    app_variant.CLIENT: (QColor("#2196f3"), QColor("#2ecc71")),   # blue -> green
}


def _base_icon_path() -> Path | None:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "resources" / "app_icon.png")
        candidates.append(Path(sys.executable).resolve().parent / "resources" / "app_icon.png")
    else:
        candidates.append(Path(__file__).resolve().parent / "resources" / "app_icon.png")
    for p in candidates:
        if p.is_file():
            return p
    return None


def _recoloured(source: QImage, top: QColor, bottom: QColor) -> QImage:
    """Re-tint the artwork with a top-to-bottom gradient.

    Each pixel keeps its own brightness and transparency and takes its colour
    from the gradient, so the rounded corners and the dark checklist survive -
    only the hue changes. Tinting by compositing a colour over the top would
    have flattened the checklist into the background instead.
    """
    img = source.convertToFormat(QImage.Format.Format_ARGB32)
    w, h = img.width(), img.height()
    # The gradient is sampled per row, so this is height lookups rather than one
    # per pixel.
    ramp = []
    for y in range(h):
        t = y / max(1, h - 1)
        ramp.append((
            top.red() + (bottom.red() - top.red()) * t,
            top.green() + (bottom.green() - top.green()) * t,
            top.blue() + (bottom.blue() - top.blue()) * t,
        ))
    for y in range(h):
        gr, gg, gb = ramp[y]
        for x in range(w):
            px = img.pixel(x, y)
            a = (px >> 24) & 0xFF
            if not a:
                continue
            r, g, b = (px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF
            # Rec. 601 luma: how light this pixel is, regardless of its hue.
            lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
            img.setPixel(x, y, (a << 24)
                         | (min(255, int(gr * lum)) << 16)
                         | (min(255, int(gg * lum)) << 8)
                         | min(255, int(gb * lum)))
    return img


@lru_cache(maxsize=4)
def app_icon(variant: str | None = None) -> QIcon:
    """The app icon, recoloured per build: warm for server, cool for client."""
    path = _base_icon_path()
    if path is None:
        return QIcon()
    which = app_variant.VARIANT if variant is None else variant
    gradient = _GRADIENTS.get(which)
    if gradient is None:
        return QIcon(str(path))
    base = QImage(str(path))
    if base.isNull():
        return QIcon(str(path))
    top, bottom = gradient
    # Recolour once at full size, then scale - the per-pixel pass is the
    # expensive part and its result is identical at every size.
    tinted = QPixmap.fromImage(_recoloured(base, top, bottom))
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(tinted.scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
    return icon
