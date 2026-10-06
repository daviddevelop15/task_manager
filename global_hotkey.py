"""A hotkey that works while another application has focus (Windows).

Qt shortcuts only fire when the app has keyboard focus, which is exactly wrong
for the paste-a-card key: the text being pasted is copied from somewhere else -
a browser, a mail client - and having to find and click the Task Manager window
first defeats the point of a shortcut.

Windows answers this with RegisterHotKey, which reserves a combination process-
wide. The key is delivered as a WM_HOTKEY message on the thread's queue rather
than as a keystroke to a window, so it is picked up by a native event filter
instead of Qt's usual key handling.

A reserved combination is taken away from every other program on the machine
for as long as this app runs, so it is worth choosing one nobody else wants -
which is why a bare key like F2 is a poor global hotkey and Ctrl+Shift+F2 is
the default. Registration fails, rather than silently overriding, if something
else already holds the combination; the caller gets the reason to show.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PyQt6.QtCore import QAbstractNativeEventFilter, QObject, Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
# Without this, holding the key down repeats the action as fast as the key
# repeats - dozens of cards from one long press.
MOD_NOREPEAT = 0x4000

_ERROR_HOTKEY_ALREADY_REGISTERED = 1409

# Ids are per-thread and must differ per registration, since the app now holds
# more than one at a time - a combination, and optionally plain F2 as well.
_ID_BASE = 0xB001
_next_id = _ID_BASE


def _claim_id() -> int:
    global _next_id
    _next_id += 1
    return _next_id - 1


# Plain F2, offered as a second registration alongside the combination. A key
# with no modifier is a heavy thing to reserve: Windows hands it to one program
# exclusively, so while this app runs F2 stops reaching Explorer's rename,
# Excel's edit-cell and every editor that uses it. That is the deal, and it is
# why this is a separate switch rather than part of the main setting.
PLAIN_KEY = "F2"

# Win+F2. Windows keeps Win+letter for itself but leaves Win+F-key alone, and
# nothing else on a normal desktop wants it - so it is free, one-handed, and
# does not collide with a key any application uses internally.
DEFAULT_SEQUENCE = "Meta+F2"

# Offered in Settings as a list to pick from. The Windows key cannot always be
# typed into a shortcut field - the shell swallows some combinations before an
# application sees them - so choosing from a list is the route that always
# works. Every one of these was checked against Windows on this machine.
SUGGESTED = (
    "Meta+F2",
    "Ctrl+Shift+F2",
    "Ctrl+Shift+F9",
    "Ctrl+Shift+F12",
    "Ctrl+Alt+F12",
    "Alt+Shift+F2",
)


def display_name(sequence: str) -> str:
    """'Meta+F2' is what Qt calls it; 'Win+F2' is what the keyboard calls it."""
    return sequence.replace("Meta", "Win")


def available() -> bool:
    return sys.platform == "win32"


def to_win32(sequence: str) -> tuple[int, int] | None:
    """Turn 'Ctrl+Shift+F2' into the (modifiers, virtual-key) Windows wants.

    Returns None for anything that cannot be a global hotkey, so the caller can
    say so rather than registering something unexpected.
    """
    seq = QKeySequence(sequence)
    if seq.isEmpty():
        return None
    combo = seq[0]
    key = Qt.Key(combo.key())
    qt_mods = combo.keyboardModifiers()

    mods = 0
    if qt_mods & Qt.KeyboardModifier.ControlModifier:
        mods |= MOD_CONTROL
    if qt_mods & Qt.KeyboardModifier.ShiftModifier:
        mods |= MOD_SHIFT
    if qt_mods & Qt.KeyboardModifier.AltModifier:
        mods |= MOD_ALT
    if qt_mods & Qt.KeyboardModifier.MetaModifier:
        mods |= MOD_WIN

    vk = _virtual_key(key)
    if vk is None:
        return None
    return mods | MOD_NOREPEAT, vk


def _virtual_key(key: Qt.Key) -> int | None:
    value = int(key)
    # Letters and digits share their values with the Windows codes.
    if 0x30 <= value <= 0x39 or 0x41 <= value <= 0x5A:
        return value
    if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24:
        return 0x70 + (value - int(Qt.Key.Key_F1))
    return {
        Qt.Key.Key_Space: 0x20,
        Qt.Key.Key_Insert: 0x2D,
        Qt.Key.Key_Delete: 0x2E,
        Qt.Key.Key_Home: 0x24,
        Qt.Key.Key_End: 0x23,
        Qt.Key.Key_PageUp: 0x21,
        Qt.Key.Key_PageDown: 0x22,
    }.get(key)


class _MessageFilter(QAbstractNativeEventFilter):
    """Watches the thread's message queue for our WM_HOTKEY."""

    def __init__(self, owner: "GlobalHotkey") -> None:
        super().__init__()
        self._owner = owner

    def nativeEventFilter(self, event_type, message):  # type: ignore[override]
        if event_type != b"windows_generic_MSG":
            return False, 0
        msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
        if msg.message == WM_HOTKEY and int(msg.wParam) == self._owner.hotkey_id:
            self._owner.activated.emit()
            return True, 0
        return False, 0


class GlobalHotkey(QObject):
    """One system-wide hotkey. `activated` fires wherever focus happens to be."""

    activated = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._filter: _MessageFilter | None = None
        self._sequence = ""
        self.hotkey_id = _claim_id()

    @property
    def sequence(self) -> str:
        """The combination currently reserved, or '' if none is."""
        return self._sequence

    def apply(self, sequence: str) -> str:
        """Reserve `sequence` (empty turns the hotkey off). '' means success.

        Any other return is a sentence explaining why it is not in effect, for
        the caller to put in front of the person - a hotkey that silently did
        not register is a feature that silently does not exist.
        """
        self.stop()
        sequence = (sequence or "").strip()
        if not sequence:
            return ""
        if not available():
            return "Global hotkeys are only supported on Windows."

        parsed = to_win32(sequence)
        if parsed is None:
            return (f"{sequence} cannot be used as a global hotkey. "
                    "Use a letter, digit or function key with Ctrl, Alt or Shift.")
        mods, vk = parsed

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.RegisterHotKey.argtypes = (
            wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT,
        )
        user32.RegisterHotKey.restype = wintypes.BOOL
        # hwnd=None sends WM_HOTKEY to this thread's queue, which is what the
        # native event filter reads; no window of our own has to be involved.
        if not user32.RegisterHotKey(None, self.hotkey_id, mods, vk):
            err = ctypes.get_last_error()
            if err == _ERROR_HOTKEY_ALREADY_REGISTERED:
                # Usually another build of this app: the server, client and
                # plain builds run side by side and each wants the same default.
                # Their settings are separate, so the fix is a different
                # combination here, not a change to the one that got there first.
                return (f"{sequence} is already taken - most likely by another build "
                        "of Task Manager that is already running. Choose a different "
                        "combination in Settings > App settings.")
            return f"Windows refused to register {sequence} (error {err})."

        from PyQt6.QtCore import QCoreApplication

        app = QCoreApplication.instance()
        if app is None:                       # no event loop: nothing to filter
            self._unregister()
            return "The application is not running an event loop."
        self._filter = _MessageFilter(self)
        app.installNativeEventFilter(self._filter)
        self._sequence = sequence
        return ""

    def stop(self) -> None:
        """Give the combination back to the rest of the system."""
        if self._filter is not None:
            from PyQt6.QtCore import QCoreApplication

            app = QCoreApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self._filter)
            self._filter = None
        if self._sequence:
            self._unregister()
        self._sequence = ""

    def _unregister(self) -> None:
        if not available():
            return
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
            user32.UnregisterHotKey(None, self.hotkey_id)
        except OSError:
            pass
