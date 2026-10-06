"""Collabor Board tab - placeholder.

Reserved for team collaboration features. It is intentionally empty for now,
but follows the same page contract as the other tabs (``shutdown()`` and a
``list_counts_changed`` signal) so ``MainWindow`` can treat it like any other
page and the real content can drop in without touching the shell.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont, QPalette
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget


class CollaborBoardPage(QWidget):
    """Empty placeholder page for upcoming collaboration features."""

    list_counts_changed = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CollaborBoardPage")

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.addStretch(1)

        title = QLabel("Collabor Board")
        tf = QFont()
        tf.setBold(True)
        tf.setPointSizeF(max(12.0, tf.pointSizeF() * 1.4))
        title.setFont(tf)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(title)

        note = QLabel("Coming soon.")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        muted = self.palette().color(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text
        ).name()
        note.setStyleSheet(f"color: {muted};")
        root.addWidget(note)

        root.addStretch(2)

    # --- page contract -----------------------------------------------------

    def refresh(self) -> None:
        """Nothing to load yet; present so the shell can refresh every page."""

    def _refresh_table(self) -> None:
        """MainWindow initialises pages via _refresh_table(); keep the name."""
        self.refresh()

    def item_count(self) -> int:
        return 0

    def shutdown(self) -> None:
        """No timers or threads to stop yet."""
