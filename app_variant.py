"""Which build this is: the plain app, the PM's server, or a dev's client.

One codebase produces three builds. They differ only in a `variant.txt` dropped
into the app folder at build time - no code changes, no separate branches.

The variant exists to stop the builds treating each other's things as their own.
Two copies on one PC (the normal case while testing, and a PM who also wants the
plain app) would otherwise share:

* the single-instance mutex, so the second one would silently refuse to start;
* the QSettings tree, so they would fight over which mode the app runs in;

The database is already per-folder, since it lives beside the app.

**The unmarked build is untouched.** No `variant.txt` means the same settings
key, the same mutex and the same data as every previous release, so an existing
install keeps working and upgrades in place.
"""

from __future__ import annotations

import sys
from pathlib import Path

VARIANT_FILENAME = "variant.txt"

PLAIN = ""
SERVER = "server"
CLIENT = "client"

_LABELS = {
    SERVER: "Server (PM)",
    CLIENT: "Client",
}


def _app_dir() -> Path:
    """Where the app's files live - the same place the database uses."""
    return Path(__file__).resolve().parent


def _read_variant() -> str:
    """The build marker, or '' for the ordinary build.

    Read once at import: it is fixed at build time and cannot change while the
    app is running.
    """
    for folder in _dirs_to_search():
        try:
            raw = (folder / VARIANT_FILENAME).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        value = raw.strip().lower()
        if value in (SERVER, CLIENT):
            return value
    return PLAIN


def _dirs_to_search() -> list[Path]:
    """The app folder, plus the exe's folder when frozen.

    Under PyInstaller the modules live in `_internal` while the exe sits one
    level up, and it is not obvious to a person which of the two they should
    drop the marker into - so accept either.
    """
    dirs = [_app_dir()]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if exe_dir not in dirs:
            dirs.append(exe_dir)
    return dirs


VARIANT = _read_variant()

SETTINGS_ORG = "TaskManager"
SETTINGS_APP = {
    SERVER: "TaskManagerServer",
    CLIENT: "TaskManagerClient",
}.get(VARIANT, "TaskManager")

# The single-instance mutex. Distinct per variant so a PM can run the server
# build and a plain build side by side.
INSTANCE_NAME = SETTINGS_APP


def label() -> str:
    """Human name for this build, or '' for the ordinary one."""
    return _LABELS.get(VARIANT, "")


def short_label() -> str:
    """Just the build's word: 'Server', 'Client', or ''.

    Used where the role is stated separately - the main window titles itself
    with the signed-in account, so "Server (PM) - sebastian (PM)" would say PM
    twice.
    """
    return {SERVER: "Server", CLIENT: "Client"}.get(VARIANT, "")


def window_title(base: str = "Task Manager") -> str:
    """Title bar text - so two open windows are tellable apart at a glance."""
    suffix = label()
    return f"{base} — {suffix}" if suffix else base


def default_network_mode() -> str:
    """The mode this build starts in before anyone configures it.

    Only a default: whatever the user chooses in Settings wins from then on, so
    a client build can still be switched to standalone without a rebuild.
    """
    return VARIANT or "standalone"
