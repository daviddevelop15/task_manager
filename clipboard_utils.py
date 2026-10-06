"""Copy-to-clipboard helpers and shared copy-button styling."""

from __future__ import annotations

from PyQt6.QtCore import QTimer, QSize
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QPushButton, QStyle

_ORANGE = QColor("#FF9800")

COPY_ICON_BTN_QSS = (
    "QPushButton#copyIconBtn { background-color: transparent; color: #FF9800; "
    "border: 1px solid transparent; border-radius: 4px; padding: 2px; }"
    "QPushButton#copyIconBtn:hover { color: #FFB74D; border: 1px solid #FF9800; "
    "background-color: rgba(255, 152, 0, 0.12); }"
    "QPushButton#copyIconBtn:pressed { color: #F57C00; background-color: rgba(255, 152, 0, 0.2); }"
)


def apply_copy_button_flash(btn: QPushButton) -> None:
    """Brief green highlight after a successful copy (icon or text button)."""
    orig = btn.styleSheet()
    btn.setStyleSheet(
        orig
        + "QPushButton#copyIconBtn { border: 2px solid #2E7D32; background-color: #C8E6C9; }"
    )

    def _restore() -> None:
        try:
            btn.setStyleSheet(orig)
        except RuntimeError:
            pass

    QTimer.singleShot(1800, _restore)


def copy_clipboard_icon() -> QIcon:
    """Best-effort copy icon (theme / style); may be null — use text fallback on the button."""
    sty = QApplication.style()
    pm = getattr(QStyle.StandardPixmap, "SP_DialogCopyButton", None)
    if pm is not None:
        ico = sty.standardIcon(pm)
        if not ico.isNull():
            return ico
    ico = QIcon.fromTheme("edit-copy")
    if not ico.isNull():
        return ico
    return QIcon()


def orange_copy_icon(size: QSize | None = None) -> QIcon:
    """Theme copy icon re-colored orange (stylesheets do not tint QIcon pixmaps)."""
    if size is None:
        size = QSize(18, 18)
    base = copy_clipboard_icon()
    if base.isNull():
        return QIcon()
    pm = base.pixmap(size, QIcon.Mode.Normal, QIcon.State.Off)
    if pm.isNull():
        return QIcon()
    tinted = QPixmap(pm.size())
    tinted.fill(QColor(0, 0, 0, 0))
    painter = QPainter(tinted)
    painter.drawPixmap(0, 0, pm)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(tinted.rect(), _ORANGE)
    painter.end()
    return QIcon(tinted)
