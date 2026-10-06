"""Task Manager — application entry point."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PyQt6.QtWidgets import QApplication

import app_variant
from app_resources import app_icon
from main_window import MainWindow
import security_ui


class _SingleInstanceLock:
    """Windows named mutex that keeps the desktop app single-instance."""

    _ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str) -> None:
        self.already_running = False
        self._kernel32 = None
        self._handle = None
        if sys.platform != "win32":
            return

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        self._kernel32 = kernel32
        self._handle = kernel32.CreateMutexW(None, False, f"Local\\{name}")
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.already_running = ctypes.get_last_error() == self._ERROR_ALREADY_EXISTS

    def close(self) -> None:
        if self._kernel32 is not None and self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None

    def __del__(self) -> None:
        self.close()


def _set_taskbar_identity() -> None:
    """Give this build its own taskbar identity on Windows.

    Without it, every build of the app shares one taskbar button and one icon,
    because Windows groups by the executable name - and all three are called
    TaskManager.exe. With it, the server and client sit side by side, each
    showing its own icon.

    Must run before any window is created, or Windows has already decided.
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            f"TaskManager.{app_variant.SETTINGS_APP}"
        )
    except Exception:            # noqa: BLE001 - cosmetic; never block startup
        pass


def _sign_in_if_client() -> None:
    """In client mode, log in to the PM's server before the window opens.

    Cancelling is not fatal. The app opens against this PC's own data for the
    session so the person can still reach Settings > Network / Sharing - a
    machine that cannot log in must not be a machine that cannot start. The
    configured mode is left alone, so the next launch tries the server again.
    """
    import app_store

    if app_store.network_mode() != app_store.MODE_CLIENT:
        return
    import client_login_ui
    import sync_client

    result = client_login_ui.prompt_login()
    if result is not None:
        import busy_overlay

        store = sync_client.RemoteStore(result)
        # A network round trip with no window up yet: on a slow link this is
        # dead time between the login dialog closing and anything appearing.
        splash = busy_overlay.StartupSplash("Fetching the team directory…")
        splash.start()
        try:
            store.refresh_directory()  # names for the Creator column, up front
        finally:
            splash.finish()
        app_store.set_store(store)


def main() -> None:
    # Per-variant, so the PM can run the server build alongside a plain one and
    # a test client on the same PC without them locking each other out.
    instance_lock = _SingleInstanceLock(app_variant.INSTANCE_NAME)
    if instance_lock.already_running:
        instance_lock.close()
        return

    _set_taskbar_identity()
    app = QApplication(sys.argv)
    app.setWindowIcon(app_icon())
    app.setOrganizationName(MainWindow.SETTINGS_ORG)
    app.setApplicationName(MainWindow.SETTINGS_APP)

    # Unlock (or first-run set up) the encrypted database before any DB access.
    # A client is the exception: it stores nothing of its own, so it opens a
    # throwaway local cache silently and the only password anyone types is the
    # one for their account on the server, a moment later.
    import app_store

    if app_store.network_mode() == app_store.MODE_CLIENT:
        unlocked = security_ui.ensure_unlocked_for_client()
    else:
        unlocked = security_ui.ensure_unlocked()
    if not unlocked:
        instance_lock.close()
        return

    # Sign-in first, because it puts its own dialog on screen and nothing should
    # be floating over that.
    _sign_in_if_client()

    # Building the window is the slow, silent stretch: every board opened
    # against a database that can be tens of megabytes, with nothing on screen
    # yet. Without this the app shows nothing for several seconds and looks
    # like it failed to launch.
    import busy_overlay

    splash = busy_overlay.StartupSplash("Loading your boards…")
    splash.start()
    try:
        w = MainWindow()
        w.show()
    finally:
        splash.finish()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
