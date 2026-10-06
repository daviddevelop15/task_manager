"""Login for the developer's app: server address, username, password.

Two things shape this dialog:

* **The network call must not freeze the window.** An unreachable PC can take
  the full timeout to fail, so every request runs on a worker thread and the
  dialog stays responsive with the buttons disabled.
* **A refusal has to say which refusal it was.** "Login failed" is useless when
  the causes are a typo, an unregistered PC, a disabled account and a server
  that is simply switched off - each needs a different next step, so the
  server's message is shown as-is.

A remembered password is stored with Windows DPAPI, tied to this Windows
account on this PC - the same protection the app already offers for the
database key. Nothing is written in the clear.
"""

from __future__ import annotations

import base64
from typing import Callable, Optional

from PyQt6.QtCore import QSettings, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

import app_variant
import busy_overlay
import sync_client
from app_resources import app_icon

SETTINGS_ORG = app_variant.SETTINGS_ORG
SETTINGS_APP = app_variant.SETTINGS_APP

SERVER_KEY = "client_server_address"
USERNAME_KEY = "client_username"
PASSWORD_KEY = "client_password_protected"   # DPAPI blob, base64


def _settings() -> QSettings:
    return QSettings(SETTINGS_ORG, SETTINGS_APP)


def _remember_password(password: str) -> None:
    """Store the password protected by the Windows account, or not at all."""
    try:
        import win_dpapi

        blob = win_dpapi.protect(password.encode("utf-8"))
        _settings().setValue(PASSWORD_KEY, base64.b64encode(blob).decode("ascii"))
    except Exception:
        # No DPAPI (not Windows, or it refused) means no saved password. Never
        # fall back to writing it in the clear.
        _settings().remove(PASSWORD_KEY)


def _recall_password() -> str:
    raw = _settings().value(PASSWORD_KEY, "")
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        import win_dpapi

        return win_dpapi.unprotect(base64.b64decode(raw)).decode("utf-8")
    except Exception:
        return ""     # different user or machine; just ask again


def forget_password() -> None:
    _settings().remove(PASSWORD_KEY)


class _Call(QThread):
    """Runs one blocking network call off the GUI thread."""

    done = pyqtSignal(object)
    failed = pyqtSignal(object)

    def __init__(self, fn: Callable, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:                       # pragma: no cover - thread body
        try:
            self.done.emit(self._fn())
        except Exception as exc:                 # noqa: BLE001 - reported to the user
            self.failed.emit(exc)


def _describe(exc: Exception) -> str:
    """Turn an exception into something worth reading."""
    if isinstance(exc, sync_client.OfflineError):
        return (
            "Could not reach the server.\n\n"
            "Check that the PM's Task Manager is running and sharing, that the "
            "address is right, and that both PCs are on the same network."
        )
    if isinstance(exc, sync_client.RemoteError):
        return exc.message
    if isinstance(exc, ValueError):
        return str(exc)
    return f"Unexpected error: {exc}"


class LoginDialog(QDialog):
    """Collects the server address and credentials, and logs in."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect to the team server")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setMinimumWidth(420)
        self.result_login: Optional[sync_client.LoginResult] = None
        self._worker: Optional[_Call] = None
        self._overlay = busy_overlay.BusyOverlay(self)

        s = _settings()
        root = QVBoxLayout(self)
        intro = QLabel(
            "The daily board and meetings live on the project manager's PC. "
            "Log in with the username and password the PM gave you."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        form = QFormLayout()
        self._server = QLineEdit(str(s.value(SERVER_KEY, "") or ""))
        self._server.setPlaceholderText("192.168.1.10  (or 192.168.1.10:8765)")
        form.addRow("Server", self._server)
        self._username = QLineEdit(str(s.value(USERNAME_KEY, "") or ""))
        self._username.setPlaceholderText("your username")
        form.addRow("Username", self._username)
        self._password = QLineEdit(_recall_password())
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password", self._password)
        root.addLayout(form)

        self._remember = QCheckBox("Remember my password on this PC")
        self._remember.setChecked(bool(self._password.text()))
        self._remember.setToolTip(
            "Stored using your Windows account, so it cannot be read from "
            "another account or another PC."
        )
        root.addWidget(self._remember)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        row = QHBoxLayout()
        self._test_btn = QPushButton("Test connection")
        self._test_btn.clicked.connect(self._on_test)
        row.addWidget(self._test_btn)
        self._forgot_btn = QPushButton("Forgot password…")
        self._forgot_btn.setToolTip("Use a recovery code from the PM to set a new password")
        self._forgot_btn.clicked.connect(self._on_forgot)
        row.addWidget(self._forgot_btn)
        row.addStretch(1)
        root.addLayout(row)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Log in")
        self._buttons.accepted.connect(self._on_login)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)

        for w in (self._server, self._username, self._password):
            w.returnPressed.connect(self._on_login)
        (self._password if self._username.text() else self._username).setFocus()

    # --- helpers -----------------------------------------------------------

    def _busy(self, on: bool, message: str = "") -> None:
        for w in (self._buttons, self._test_btn, self._forgot_btn,
                  self._server, self._username, self._password):
            w.setEnabled(not on)
        self._status.setText(message)
        self.setCursor(Qt.CursorShape.WaitCursor if on
                       else Qt.CursorShape.ArrowCursor)
        # The work already runs on a worker thread here, so the spinner turns
        # properly - and a login against an unreachable server sits for the
        # full timeout, which is exactly when greyed-out fields alone are not
        # enough to say the app is still trying.
        if on:
            self._overlay.start(message)
        else:
            self._overlay.stop()

    def _run(self, fn: Callable, on_done: Callable, busy_text: str) -> None:
        self._busy(True, busy_text)
        worker = _Call(fn, self)
        self._worker = worker              # keep a reference or Qt collects it

        def finish(result=None, exc=None):
            self._busy(False)
            worker.deleteLater()
            self._worker = None
            if exc is not None:
                self._status.setText("")
                QMessageBox.warning(self, "Connection", _describe(exc))
            else:
                on_done(result)

        worker.done.connect(lambda r: finish(result=r))
        worker.failed.connect(lambda e: finish(exc=e))
        worker.start()

    # --- actions -----------------------------------------------------------

    def _on_test(self) -> None:
        server = self._server.text().strip()
        if not server:
            QMessageBox.information(self, "Connection", "Enter the server address.")
            return

        def done(info: dict) -> None:
            if not info.get("configured"):
                self._status.setText(
                    "Reached the server, but it has no accounts yet — ask the PM "
                    "to add you."
                )
            else:
                self._status.setText("Server reached. Log in with your username and password.")

        self._run(lambda: sync_client.ping(server), done, "Contacting the server…")

    def _on_login(self) -> None:
        server = self._server.text().strip()
        username = self._username.text().strip()
        password = self._password.text()
        if not server or not username or not password:
            QMessageBox.information(
                self, "Log in", "Server, username and password are all required."
            )
            return

        def done(result: sync_client.LoginResult) -> None:
            s = _settings()
            s.setValue(SERVER_KEY, server)
            s.setValue(USERNAME_KEY, username)
            if self._remember.isChecked():
                _remember_password(password)
            else:
                forget_password()
            self.result_login = result
            self.accept()

        self._run(lambda: sync_client.login(server, username, password), done,
                  "Logging in…")

    def _on_forgot(self) -> None:
        dlg = RecoveryDialog(self, server=self._server.text().strip(),
                             username=self._username.text().strip())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._password.setText(dlg.new_password)
            self._status.setText("Password set. Log in with it now.")


class RecoveryDialog(QDialog):
    """Redeem a one-time code from the PM and choose a new password.

    The PM cannot set a password for anyone - they hand over a code, and the
    person sets their own here.
    """

    def __init__(self, parent: Optional[QWidget] = None, server: str = "",
                 username: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Use a recovery code")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setMinimumWidth(420)
        self.new_password = ""
        self._worker: Optional[_Call] = None

        root = QVBoxLayout(self)
        intro = QLabel(
            "Ask the PM to issue a recovery code for your account, then enter it "
            "here with the password you want to use from now on. The code works "
            "once, expires, and only from your own PC."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        form = QFormLayout()
        self._server = QLineEdit(server)
        self._server.setPlaceholderText("192.168.1.10")
        form.addRow("Server", self._server)
        self._username = QLineEdit(username)
        form.addRow("Username", self._username)
        self._code = QLineEdit()
        self._code.setPlaceholderText("ABCD-EFGH-JKMN")
        form.addRow("Recovery code", self._code)
        self._pw1 = QLineEdit()
        self._pw1.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("New password", self._pw1)
        self._pw2 = QLineEdit()
        self._pw2.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Repeat it", self._pw2)
        root.addLayout(form)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Set password")
        self._buttons.accepted.connect(self._on_ok)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)

    def _on_ok(self) -> None:
        server = self._server.text().strip()
        username = self._username.text().strip()
        code = self._code.text().strip()
        pw = self._pw1.text()
        if not server or not username or not code or not pw:
            QMessageBox.information(self, "Recovery", "Every field is required.")
            return
        if pw != self._pw2.text():
            QMessageBox.information(self, "Recovery", "The two passwords do not match.")
            return

        self._buttons.setEnabled(False)
        worker = _Call(lambda: sync_client.recover(server, username, code, pw), self)
        self._worker = worker

        def ok(_result) -> None:
            self._buttons.setEnabled(True)
            self.new_password = pw
            self.accept()

        def failed(exc) -> None:
            self._buttons.setEnabled(True)
            QMessageBox.warning(self, "Recovery", _describe(exc))

        worker.done.connect(ok)
        worker.failed.connect(failed)
        worker.start()


def prompt_login(parent: Optional[QWidget] = None) -> Optional[sync_client.LoginResult]:
    """Show the dialog until the user logs in or gives up."""
    dlg = LoginDialog(parent)
    if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_login is not None:
        return dlg.result_login
    return None
