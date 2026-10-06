"""The Network / Sharing settings screen.

One dialog covers all three modes, because the choice between them is the first
thing on it:

* **This PC only** - the app as it has always been.
* **Team server (PM)** - this PC owns the data and serves it to the team.
* **Team client (developer)** - this PC keeps nothing and works against the PM's.

Changing mode needs a restart: the data seam is chosen once at startup, and
swapping a live store under open boards would be a much larger and more
fragile piece of machinery than a restart prompt.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

import app_store
import app_variant
import sync_server
from app_resources import app_icon

SETTINGS_ORG = app_variant.SETTINGS_ORG
SETTINGS_APP = app_variant.SETTINGS_APP

SERVER_HOST_KEY = "server_bind_host"
SERVER_PORT_KEY = "server_port"
SERVER_AUTOSTART_KEY = "server_autostart"


def _settings() -> QSettings:
    return QSettings(SETTINGS_ORG, SETTINGS_APP)


def server_host() -> str:
    val = _settings().value(SERVER_HOST_KEY, sync_server.DEFAULT_HOST)
    return val.strip() if isinstance(val, str) and val.strip() else sync_server.DEFAULT_HOST


def server_port() -> int:
    try:
        return int(_settings().value(SERVER_PORT_KEY, sync_server.DEFAULT_PORT))
    except (TypeError, ValueError):
        return sync_server.DEFAULT_PORT


def server_autostart() -> bool:
    return bool(_settings().value(SERVER_AUTOSTART_KEY, True, type=bool))


class NetworkSettingsDialog(QDialog):
    """Pick the mode and configure it."""

    def __init__(self, parent: Optional[QWidget] = None, window=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Network / Sharing")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setMinimumWidth(520)
        self._window = window          # MainWindow, when there is one
        self._start_mode = app_store.network_mode()

        root = QVBoxLayout(self)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("This PC runs as"))
        self._mode = QComboBox()
        self._mode.addItem("This PC only (no sharing)", app_store.MODE_STANDALONE)
        self._mode.addItem("Team server — project manager", app_store.MODE_SERVER)
        self._mode.addItem("Team client — developer", app_store.MODE_CLIENT)
        idx = self._mode.findData(self._start_mode)
        self._mode.setCurrentIndex(idx if idx >= 0 else 0)
        self._mode.currentIndexChanged.connect(self._sync_visibility)
        mode_row.addWidget(self._mode, 1)
        root.addLayout(mode_row)

        self._mode_note = QLabel("")
        self._mode_note.setWordWrap(True)
        root.addWidget(self._mode_note)

        # --- identity (standalone only) ---------------------------------
        self._identity_box = QGroupBox("Your name on records you create")
        ilay = QFormLayout(self._identity_box)
        self._display_name = QLineEdit(app_store.display_name())
        self._display_name.setPlaceholderText("e.g. Sam")
        ilay.addRow("Display name", self._display_name)
        note = QLabel(
            "Shown in the Creator column. In team mode your name comes from your "
            "account instead, so nobody can post as somebody else."
        )
        note.setWordWrap(True)
        ilay.addRow(note)
        root.addWidget(self._identity_box)

        # --- server ------------------------------------------------------
        self._server_box = QGroupBox("Server")
        slay = QFormLayout(self._server_box)
        self._host = QLineEdit(server_host())
        self._host.setPlaceholderText(sync_server.DEFAULT_HOST)
        self._host.setToolTip(
            "Which network adapters to accept connections on. 0.0.0.0 means all "
            "of them, which is what you want on a normal LAN."
        )
        slay.addRow("Listen on", self._host)
        self._port = QSpinBox()
        self._port.setRange(1024, 65535)
        self._port.setValue(server_port())
        slay.addRow("Port", self._port)

        self._service_state = QLabel("")
        self._service_state.setWordWrap(True)
        slay.addRow("Status", self._service_state)

        self._urls = QLabel("")
        self._urls.setWordWrap(True)
        self._urls.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        slay.addRow("Give the team", self._urls)

        btn_row = QHBoxLayout()
        self._start_btn = QPushButton("Start sharing")
        self._start_btn.clicked.connect(self._on_start)
        btn_row.addWidget(self._start_btn)
        self._stop_btn = QPushButton("Stop sharing")
        self._stop_btn.clicked.connect(self._on_stop)
        btn_row.addWidget(self._stop_btn)
        self._users_btn = QPushButton("Users…")
        self._users_btn.setToolTip("Add the people who may connect")
        self._users_btn.clicked.connect(self._on_users)
        btn_row.addWidget(self._users_btn)
        btn_row.addStretch(1)
        slay.addRow(btn_row)
        root.addWidget(self._server_box)

        # --- client ------------------------------------------------------
        self._client_box = QGroupBox("Client")
        clay = QVBoxLayout(self._client_box)
        self._client_state = QLabel("")
        self._client_state.setWordWrap(True)
        clay.addWidget(self._client_state)
        crow = QHBoxLayout()
        self._relogin_btn = QPushButton("Log in again…")
        self._relogin_btn.clicked.connect(self._on_relogin)
        crow.addWidget(self._relogin_btn)
        self._forget_btn = QPushButton("Forget saved password")
        self._forget_btn.clicked.connect(self._on_forget)
        crow.addWidget(self._forget_btn)
        crow.addStretch(1)
        clay.addLayout(crow)
        root.addWidget(self._client_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._sync_visibility()
        self._refresh_state()

    # --- state -------------------------------------------------------------

    @property
    def _server_obj(self):
        return getattr(self._window, "_sync_server", None)

    def _sync_visibility(self) -> None:
        mode = self._mode.currentData()
        self._identity_box.setVisible(mode == app_store.MODE_STANDALONE)
        self._server_box.setVisible(mode == app_store.MODE_SERVER)
        self._client_box.setVisible(mode == app_store.MODE_CLIENT)
        self._mode_note.setText({
            app_store.MODE_STANDALONE:
                "Everything stays on this PC, exactly as before.",
            app_store.MODE_SERVER:
                "This PC holds the team's data and serves the daily board and "
                "meetings to the developers. It has to be running for them to "
                "work.",
            app_store.MODE_CLIENT:
                "This PC stores nothing. The boards come from the PM's machine, "
                "so if a laptop is lost no team data goes with it.",
        }.get(mode, ""))
        self.adjustSize()

    def _refresh_state(self) -> None:
        srv = self._server_obj
        running = bool(srv is not None and srv.is_running)
        self._start_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        if running:
            sessions = srv.service.sessions.active()
            who = ", ".join(sorted({s.username for s in sessions})) or "nobody yet"
            self._service_state.setText(f"Sharing. Connected: {who}.")
            self._urls.setText("\n".join(srv.client_urls()))
        else:
            self._service_state.setText("Not sharing.")
            self._urls.setText(
                "\n".join(f"http://{a}:{self._port.value()}"
                          for a in sync_server.lan_addresses())
                + "\n(once you start sharing)"
            )

        store = app_store.get_store()
        if hasattr(store, "online"):
            pending = store.pending_writes
            state = "Connected" if store.online else "Not connected"
            extra = f" — {pending} change(s) waiting to send" if pending else ""
            self._client_state.setText(f"{state} as {store.author}.{extra}")
        else:
            self._client_state.setText("Not logged in yet.")

    # --- actions -----------------------------------------------------------

    def _on_start(self) -> None:
        if self._window is None:
            QMessageBox.information(self, "Sharing",
                                    "Save and restart to start the server.")
            return
        self._save_server_fields()
        try:
            self._window.start_sync_server()
        except OSError as exc:
            QMessageBox.warning(
                self, "Sharing",
                f"Could not listen on port {self._port.value()}.\n\n{exc}\n\n"
                "Another program may already be using it — try a different port.",
            )
        self._refresh_state()

    def _on_stop(self) -> None:
        if self._window is not None:
            self._window.stop_sync_server()
        self._refresh_state()

    def _on_users(self) -> None:
        from user_admin_ui import UsersDialog

        UsersDialog(self, server=self._server_obj).exec()
        self._refresh_state()

    def _on_relogin(self) -> None:
        if self._window is None:
            return
        if self._window.client_login():
            self._refresh_state()

    def _on_forget(self) -> None:
        import client_login_ui

        client_login_ui.forget_password()
        QMessageBox.information(self, "Password", "The saved password was removed.")

    def _save_server_fields(self) -> None:
        s = _settings()
        s.setValue(SERVER_HOST_KEY, self._host.text().strip() or sync_server.DEFAULT_HOST)
        s.setValue(SERVER_PORT_KEY, int(self._port.value()))

    def _on_save(self) -> None:
        mode = self._mode.currentData()
        self._save_server_fields()
        if mode == app_store.MODE_STANDALONE:
            app_store.set_display_name(self._display_name.text())
        app_store.set_network_mode(mode)
        if mode != self._start_mode:
            QMessageBox.information(
                self, "Restart needed",
                "Close and reopen Task Manager to switch mode.",
            )
        self.accept()
