"""System tray icon: fetch the window onto the monitor you are sitting at.

The window remembers where it was last closed, which is the wrong answer on a
multi-monitor desk: open the app after moving to another screen and the window
is on the monitor that is no longer in front of you - or on one that is
unplugged, where nothing is visible at all.

The obvious implementation - "move the window to the screen the mouse is on" -
does not work, and it is worth writing down why. On Windows the notification
area exists only on the PRIMARY monitor's taskbar; secondary taskbars carry
window buttons and a clock, but no tray icons. So the pointer is on the primary
screen at the moment of every single tray click, whichever monitor the person
is actually working at. When the window is already on the primary, "move it to
the mouse's screen" is a no-op, and the only thing left that has any effect is
the call that takes focus - which drags the person's attention to the wrong
monitor and moves nothing. That is the bug this module is written around.

So a click means "the window is not where I am - send it away from where it
is": the mouse's screen if the window is somewhere else, and otherwise the next
monitor along. With two screens that always reads as "bring it to the other
one", with no guess about where the person is sitting. Which monitor was chosen
and where the window ended up are written to diag.log, because none of this is
observable after the fact.
"""

from __future__ import annotations

import app_variant
import database as db
import window_utils
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QAction, QCursor, QGuiApplication, QScreen
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

from app_resources import app_icon


def pick_target_name(
    screen_names: list[str], window_on: str | None, cursor_on: str | None
) -> str | None:
    """Which monitor a tray click should send the window to, by name.

    Split out from the widget because it is the whole decision and it cannot be
    tested through the UI - it needs two monitors to mean anything, and the
    machine this was written on has one.
    """
    if cursor_on is None or window_on is None or len(screen_names) < 2:
        return cursor_on
    if window_on != cursor_on:
        return cursor_on             # the window is elsewhere: bring it over
    # Same screen as the pointer, which on Windows is always the primary - it
    # says nothing about where the person is. Move it along rather than doing
    # nothing, which is what made a click look broken.
    if window_on not in screen_names:
        return cursor_on
    return screen_names[(screen_names.index(window_on) + 1) % len(screen_names)]


class TrayIcon(QSystemTrayIcon):
    """Tray entry for the main window: fetch it, hide it, quit."""

    def __init__(self, window: QWidget) -> None:
        super().__init__(app_icon(), window)
        self._window = window
        self.setToolTip(app_variant.window_title("Task Manager"))

        # Parented to the window so the menu lives exactly as long as the tray
        # icon does; a menu that outlives its owner crashes on Windows.
        menu = QMenu(window)
        act_fetch = QAction("&Bring window to my screen", menu)
        act_fetch.triggered.connect(self.fetch_to_my_screen)
        menu.addAction(act_fetch)
        # Named monitors as well, because the click's guess can only ever be a
        # guess. Rebuilt each time it opens: monitors get plugged and unplugged.
        self._screens_menu = menu.addMenu("Move to &monitor")
        self._screens_menu.aboutToShow.connect(self._rebuild_screens_menu)
        menu.addSeparator()
        act_hide = QAction("&Hide to tray", menu)
        act_hide.triggered.connect(self._hide_window)
        menu.addAction(act_hide)
        menu.addSeparator()
        act_quit = QAction("&Quit", menu)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_quit)
        self.setContextMenu(menu)

        self.activated.connect(self._on_activated)

    @staticmethod
    def is_available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    # ---------------------------------------------------------------- clicks

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Context:
            return                       # right-click belongs to the menu
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            # Show it where it already is, for when the window is on this
            # screen and simply buried under something else.
            window_utils.raise_to_front(self._window)
            return
        self.fetch_to_my_screen()

    def fetch_to_my_screen(self) -> None:
        """Send the window to the monitor the person is most likely sitting at."""
        self._move_to(self._target_screen(), why="click")

    def _target_screen(self) -> QScreen | None:
        screens = QGuiApplication.screens()
        here = window_utils.screen_under_cursor()
        on = self._window.screen()
        want = pick_target_name(
            [s.name() for s in screens],
            on.name() if on is not None else None,
            here.name() if here is not None else None,
        )
        for screen in screens:
            if screen.name() == want:
                return screen
        return here

    def _move_to(self, screen: QScreen | None, why: str) -> None:
        before = self._window.screen()
        db.log_diagnostic(
            f"tray {why}: cursor={QCursor.pos().x()},{QCursor.pos().y()} "
            f"screens={[s.name() for s in QGuiApplication.screens()]} "
            f"window_on={before.name() if before else None} "
            f"target={screen.name() if screen else None}"
        )
        window_utils.bring_to_screen(self._window, screen)
        QTimer.singleShot(400, self._log_landing)

    def _log_landing(self) -> None:
        got = self._window.screen()
        db.log_diagnostic(
            f"tray settled: window_on={got.name() if got else None} "
            f"geometry={self._window.frameGeometry().getRect()} "
            f"visible={self._window.isVisible()} maximized={self._window.isMaximized()}"
        )

    # ----------------------------------------------------------------- menu

    def _rebuild_screens_menu(self) -> None:
        self._screens_menu.clear()
        on = self._window.screen()
        for i, screen in enumerate(QGuiApplication.screens(), start=1):
            geo = screen.geometry()
            mark = "  •" if on is not None and on.name() == screen.name() else ""
            act = QAction(
                f"Monitor &{i} — {screen.name()} ({geo.width()}×{geo.height()}){mark}",
                self._screens_menu,
            )
            act.triggered.connect(
                lambda _checked=False, s=screen: self._move_to(s, why="menu")
            )
            self._screens_menu.addAction(act)

    def _hide_window(self) -> None:
        self._window.hide()

    def _quit(self) -> None:
        # close() so the window's own closeEvent still saves settings and shuts
        # the encrypted database down cleanly - quitting outright skips it.
        if self._window.close():
            QApplication.quit()
