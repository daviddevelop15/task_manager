"""The PM's Users screen: who may connect, from where.

The one rule this UI exists to make unavoidable: **the PM never learns anyone
else's password.** So there is no password field for other people's accounts.
A new account is created without a password and handed a one-time recovery
code, which the person redeems to choose their own. The only password box in
here is on the very first account - the PM's own, when the server has no users
at all and somebody has to be able to log in.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

import sync_server
import sync_users
from app_resources import app_icon

_HEADERS = ("Username", "Name", "Role", "Allowed IPs", "Enabled", "Last seen")


class UsersDialog(QDialog):
    """List, add, edit and disable accounts; issue recovery codes."""

    def __init__(self, parent: Optional[QWidget] = None, server=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Users")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.resize(760, 420)
        self._server = server        # SyncServer, to log people out when disabled

        root = QVBoxLayout(self)
        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

        self._table = QTableWidget(0, len(_HEADERS))
        self._table.setHorizontalHeaderLabels(list(_HEADERS))
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.doubleClicked.connect(lambda *_: self._on_edit())
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self._table, 1)

        row = QHBoxLayout()
        self._add_btn = QPushButton("Add…")
        self._add_btn.clicked.connect(self._on_add)
        row.addWidget(self._add_btn)
        self._edit_btn = QPushButton("Edit…")
        self._edit_btn.clicked.connect(self._on_edit)
        row.addWidget(self._edit_btn)
        self._code_btn = QPushButton("Issue recovery code…")
        self._code_btn.setToolTip(
            "Give this person a one-time code so they can set their own password"
        )
        self._code_btn.clicked.connect(self._on_recovery)
        row.addWidget(self._code_btn)
        self._del_btn = QPushButton("Remove")
        self._del_btn.clicked.connect(self._on_delete)
        row.addWidget(self._del_btn)
        row.addStretch(1)
        root.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._reload()

    # --- data --------------------------------------------------------------

    def _reload(self) -> None:
        users = sync_users.list_users()
        self._table.setRowCount(len(users))
        for r, u in enumerate(users):
            cells = (
                u.username,
                u.display_name,
                "PM" if u.is_pm else "Developer",
                u.allowed_ips or "any (IP check off)",
                "yes" if u.enabled else "no",
                u.last_seen_at or "never",
            )
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, u.id)
                if c == 3 and not u.allowed_ips:
                    item.setToolTip(
                        "This account can be used from any PC. Add the person's "
                        "IP address to restrict it."
                    )
                self._table.setItem(r, c, item)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        if not users:
            self._hint.setText(
                "No accounts yet. Add your own PM account first — you will "
                "choose its password here, and every account after it gets a "
                "recovery code instead, so nobody's password passes through you."
            )
        else:
            self._hint.setText(
                "Add an account per person, with the IP address of the PC they "
                "work from. They set their own password using a recovery code."
            )
        for b in (self._edit_btn, self._code_btn, self._del_btn):
            b.setEnabled(bool(users))

    def _selected_user(self):
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        row = rows[0].row() if rows else self._table.currentRow()
        if row < 0:
            return None
        item = self._table.item(row, 0)
        if item is None:
            return None
        return sync_users.get_user(int(item.data(Qt.ItemDataRole.UserRole)))

    # --- actions -----------------------------------------------------------

    def _on_add(self) -> None:
        first = not sync_users.has_any_users()
        dlg = UserEditDialog(self, user=None, allow_password=first)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            user = sync_users.create_user(
                username=dlg.username,
                password=dlg.password,
                role=dlg.role,
                display_name=dlg.display_name,
                allowed_ips=dlg.allowed_ips,
                enabled=dlg.enabled,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Users", str(exc))
            return
        self._reload()
        if not dlg.password:
            QMessageBox.information(
                self, "Users",
                f"{user.username} was added without a password.\n\n"
                "Issue a recovery code for them now so they can set one.",
            )
            self._show_recovery(user)

    def _on_edit(self) -> None:
        user = self._selected_user()
        if user is None:
            return
        dlg = UserEditDialog(self, user=user)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            sync_users.update_user(
                user.id,
                display_name=dlg.display_name,
                role=dlg.role,
                allowed_ips=dlg.allowed_ips,
                enabled=dlg.enabled,
                username=dlg.username,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Users", str(exc))
            return
        # A disabled account must lose its open sessions immediately, or the
        # person keeps working until their token happens to expire.
        if not dlg.enabled:
            self._drop_sessions(user.id)
        self._reload()

    def _on_delete(self) -> None:
        user = self._selected_user()
        if user is None:
            return
        if QMessageBox.question(
            self, "Remove user",
            f"Remove {user.username}?\n\n"
            "The daily rows and meetings they created stay on the board, still "
            "showing their name.",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            sync_users.delete_user(user.id)
        except ValueError as exc:
            QMessageBox.warning(self, "Users", str(exc))
            return
        self._drop_sessions(user.id)
        self._reload()

    def _on_recovery(self) -> None:
        user = self._selected_user()
        if user is not None:
            self._show_recovery(user)

    def _show_recovery(self, user) -> None:
        code = sync_users.issue_recovery_code(user.id)
        self._reload()
        dlg = _RecoveryCodeDialog(self, user.username, code)
        dlg.exec()

    def _drop_sessions(self, user_id: int) -> None:
        srv = self._server
        if srv is not None and getattr(srv, "is_running", False):
            srv.service.sessions.drop_user(user_id)


class UserEditDialog(QDialog):
    """Add or edit one account."""

    def __init__(self, parent: Optional[QWidget] = None, user=None,
                 allow_password: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit user" if user else "Add user")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setMinimumWidth(460)
        self._user = user
        self._allow_password = allow_password
        self.username = ""
        self.display_name = ""
        self.role = sync_users.ROLE_DEV
        self.allowed_ips = ""
        self.enabled = True
        self.password = ""

        root = QVBoxLayout(self)
        form = QFormLayout()
        self._username = QLineEdit(user.username if user else "")
        form.addRow("Username", self._username)
        self._display = QLineEdit(user.display_name if user else "")
        self._display.setPlaceholderText("Shown in the Creator column")
        form.addRow("Name", self._display)
        self._role = QComboBox()
        self._role.addItem("Developer", sync_users.ROLE_DEV)
        self._role.addItem("Project manager", sync_users.ROLE_PM)
        idx = self._role.findData(user.role if user else sync_users.ROLE_DEV)
        self._role.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow("Role", self._role)
        self._ips = QLineEdit(user.allowed_ips if user else "")
        self._ips.setPlaceholderText("192.168.1.20   (comma-separate several)")
        form.addRow("Allowed IPs", self._ips)
        root.addLayout(form)

        here = ", ".join(sync_server.lan_addresses())
        hint = QLabel(
            f"This PC is {here}. Leave Allowed IPs empty to let the account be "
            f"used from anywhere — that turns the IP check off for them."
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        if allow_password:
            pw_form = QFormLayout()
            self._pw1 = QLineEdit()
            self._pw1.setEchoMode(QLineEdit.EchoMode.Password)
            pw_form.addRow("Password", self._pw1)
            self._pw2 = QLineEdit()
            self._pw2.setEchoMode(QLineEdit.EchoMode.Password)
            pw_form.addRow("Repeat it", self._pw2)
            root.addLayout(pw_form)
            pw_note = QLabel(
                "This is your own account, so you choose its password here. "
                "Everyone else sets their own with a recovery code."
            )
            pw_note.setWordWrap(True)
            root.addWidget(pw_note)
        else:
            self._pw1 = self._pw2 = None
            if user is None:
                note = QLabel(
                    "No password is set here. After adding the account you will "
                    "get a one-time code to pass on, and they choose their own "
                    "password with it."
                )
                note.setWordWrap(True)
                root.addWidget(note)

        self._enabled = QCheckBox("Account is enabled")
        self._enabled.setChecked(bool(user.enabled) if user else True)
        root.addWidget(self._enabled)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _on_ok(self) -> None:
        username = self._username.text().strip()
        if not username:
            QMessageBox.information(self, "User", "A username is required.")
            return
        if self._pw1 is not None:
            if not self._pw1.text():
                QMessageBox.information(self, "User", "Choose a password.")
                return
            if self._pw1.text() != self._pw2.text():
                QMessageBox.information(self, "User", "The two passwords do not match.")
                return
            self.password = self._pw1.text()
        self.username = username
        self.display_name = self._display.text().strip()
        self.role = self._role.currentData()
        self.allowed_ips = self._ips.text().strip()
        self.enabled = self._enabled.isChecked()
        self.accept()


class _RecoveryCodeDialog(QDialog):
    """Shows a code once, with a copy button.

    Shown once by necessity: only its hash is stored, so it cannot be looked up
    later - if it is lost, the PM issues another.
    """

    def __init__(self, parent: Optional[QWidget], username: str, code: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("Recovery code")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setMinimumWidth(420)
        self._code = code

        root = QVBoxLayout(self)
        root.addWidget(QLabel(f"One-time code for <b>{username}</b>:"))
        box = QLineEdit(code)
        box.setReadOnly(True)
        box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = box.font()
        font.setPointSize(max(12, font.pointSize() + 4))
        font.setBold(True)
        box.setFont(font)
        root.addWidget(box)

        note = QLabel(
            f"Valid for {sync_users.RECOVERY_TTL_HOURS} hours and usable once, "
            "from their own PC only. It is not stored anywhere you can read it "
            "again — if it gets lost, issue another."
        )
        note.setWordWrap(True)
        root.addWidget(note)

        row = QHBoxLayout()
        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(self._code))
        row.addWidget(copy_btn)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        row.addWidget(buttons)
        root.addLayout(row)
