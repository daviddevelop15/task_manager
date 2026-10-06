"""Small window helpers shared by editor dialogs and the tray icon."""

from __future__ import annotations

import ctypes
import sys

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer
from PyQt6.QtGui import QCursor, QGuiApplication, QScreen
from PyQt6.QtWidgets import QWidget


def show_existing_window(window: QWidget) -> None:
    """Show and focus an existing editor window instead of creating another."""
    state = window.windowState()
    if state & Qt.WindowState.WindowMinimized:
        window.setWindowState(state & ~Qt.WindowState.WindowMinimized)
    window.show()
    window.raise_()
    window.activateWindow()


def screen_under_cursor() -> QScreen | None:
    """The monitor the mouse is on - i.e. the one the person is working on.

    The tray icon lives on the taskbar of whichever monitor the person clicked,
    so the pointer is the only reliable answer to "which screen do you mean".
    """
    screen = QGuiApplication.screenAt(QCursor.pos())
    return screen if screen is not None else QGuiApplication.primaryScreen()


def placement_for(frame: QRect, source: QRect, target: QRect) -> QRect:
    """Where a window whose frame is `frame` on `source` should sit on `target`.

    Keeps the size, and keeps the window at the same relative spot - a window
    parked top-left stays top-left - then clamps so that a window which hung
    off the edge of a wide screen does not end up off the edge of a narrow one.
    """
    width = min(frame.width(), target.width())
    height = min(frame.height(), target.height())
    rel_x = (frame.x() - source.x()) / max(1, source.width())
    rel_y = (frame.y() - source.y()) / max(1, source.height())
    x = target.x() + int(rel_x * target.width())
    y = target.y() + int(rel_y * target.height())
    x = max(target.x(), min(x, target.x() + target.width() - width))
    y = max(target.y(), min(y, target.y() + target.height() - height))
    return QRect(x, y, width, height)


def move_to_screen(window: QWidget, screen: QScreen | None) -> None:
    """Move `window` onto `screen`, keeping its size and where it sat.

    A window remembers the monitor it was last closed on, which is wrong the
    moment the person walks over to another desk or unplugs a monitor: the
    window is then off-screen or on a display they are not looking at. This
    puts it where they are, at the same relative spot on the new screen, so a
    window parked top-left stays top-left.
    """
    if screen is None:
        return
    current = window.screen()
    # Compared by name: Qt hands out fresh QScreen wrappers, so identity is not
    # a safe test for "the window is already where it belongs".
    if current is not None and current.name() == screen.name():
        return

    target = screen.availableGeometry()
    source = current.availableGeometry() if current is not None else target
    # Geometry changes do not stick while a window is maximized - Windows puts
    # it straight back. Drop to normal, place it, then maximize on the new screen.
    was_maximized = window.isMaximized()
    if was_maximized:
        window.showNormal()

    frame = window.frameGeometry()
    place = placement_for(frame, source, target)
    if place.size() != frame.size():
        # The new screen is smaller; shrink by the frame difference, since
        # resize() sizes the client area and the title bar and borders are not
        # part of it.
        window.resize(
            place.width() - (frame.width() - window.width()),
            place.height() - (frame.height() - window.height()),
        )
    window.move(place.x(), place.y())

    if was_maximized:
        window.showMaximized()


def screen_of_foreground_window() -> QScreen | None:
    """The monitor showing whatever the person is working in right now.

    Unlike the pointer, this is a trustworthy answer to "where are you?" when a
    global hotkey fires: the key was pressed while another application had
    focus, so that application's window is on the screen being looked at. It is
    only good BEFORE this app takes focus - after that the answer is our own
    window, which is what we were trying to work out.
    """
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        class _Rect(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        rect = _Rect()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        # Native pixels, while Qt works in device-independent ones - so ask Qt
        # which screen owns the point rather than comparing rectangles.
        centre = QPoint(
            (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        )
        for screen in QGuiApplication.screens():
            if screen.geometry().contains(_to_qt_pixels(screen, centre)):
                return screen
    except Exception:            # noqa: BLE001 - a guess about focus, never fatal
        return None
    return None


def _to_qt_pixels(screen: QScreen, native: QPoint) -> QPoint:
    ratio = screen.devicePixelRatio() or 1.0
    if ratio == 1.0:
        return native
    return QPoint(int(native.x() / ratio), int(native.y() / ratio))


def bring_to_screen(window: QWidget, screen: QScreen | None = None) -> None:
    """Fetch `window` onto `screen` (default: the one the mouse is on) and focus it.

    Order is the whole point. Moving first and un-minimizing afterwards looks
    right and does the wrong thing: Windows ignores a move on a minimized
    window - it relocates the taskbar animation, not the window - and then
    restores the window to the monitor it came from. All the caller sees is the
    focus jumping to the other screen, which is the opposite of what was asked.

    So: un-minimize, let Qt actually apply that, then move, then focus. The
    move waits a pass of the event loop because the state change is not in
    effect the instant setWindowState returns.
    """
    target = screen if screen is not None else screen_under_cursor()
    # Clears minimized but deliberately keeps maximized: a maximized window
    # should arrive maximized, and move_to_screen knows how to carry that over.
    window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
    if not window.isVisible():
        window.show()

    def place() -> None:
        move_to_screen(window, target)
        raise_to_front(window)
        # Windows can undo the move a beat later when it finishes restoring the
        # window, so check where it actually landed and insist once.
        QTimer.singleShot(0, lambda: _insist_on_screen(window, target))

    QTimer.singleShot(0, place)


def _insist_on_screen(window: QWidget, target: QScreen | None) -> None:
    if target is None:
        return
    current = window.screen()
    if current is None or current.name() == target.name():
        return
    move_to_screen(window, target)
    window.raise_()


def raise_to_front(window: QWidget) -> None:
    """Un-minimize, show and focus a window, even from a background process."""
    state = window.windowState()
    if state & Qt.WindowState.WindowMinimized:
        window.setWindowState(state & ~Qt.WindowState.WindowMinimized)
    if not window.isVisible():
        window.show()
    window.raise_()
    window.activateWindow()
    _force_foreground(window)


def _force_foreground(window: QWidget) -> None:
    """Actually take focus on Windows rather than just flashing the taskbar.

    Windows only lets the process that owns the foreground window hand focus
    over. Attaching to that process's input queue for the length of the call
    makes us look like the same thread, which is the long-standing way to make
    SetForegroundWindow work for a legitimate user-initiated raise.
    """
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = int(window.winId())
        foreground = user32.GetForegroundWindow()
        ours = kernel32.GetCurrentThreadId()
        theirs = user32.GetWindowThreadProcessId(foreground, None)
        attached = bool(theirs and theirs != ours
                        and user32.AttachThreadInput(theirs, ours, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(theirs, ours, False)
    except Exception:            # noqa: BLE001 - focus is cosmetic, never fatal
        pass
