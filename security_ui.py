"""Startup unlock flow and Security settings UI for the encrypted database.

Call :func:`ensure_unlocked` once at startup (after the QApplication exists,
before building the main window). It runs the first-run setup wizard, or the
unlock dialog for a password-protected vault, or unlocks silently for a
Windows-account (DPAPI) vault, then puts the database into encrypted in-memory
mode via :func:`database.unlock`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

import app_variant
import database as db
import secure_store
from app_resources import app_icon

_MIN_PASSWORD_LEN = 6


def _vault_path() -> Path:
    return Path(__file__).resolve().parent / "task_manager.vault"


# --- first-run setup --------------------------------------------------------


class _SetupDialog(QDialog):
    """First-run wizard: choose Windows-account or password protection."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # Name the build. Each variant keeps its own database, so during a LAN
        # setup you can be looking at two of these at once and must know which
        # password you are choosing.
        self.setWindowTitle(app_variant.window_title("Protect your data"))
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.mode: Optional[str] = None
        self.password: Optional[str] = None

        lay = QVBoxLayout(self)
        which = app_variant.label()
        lay.addWidget(QLabel(
            (f"This is the {which} app, and it keeps its own separate database.\n"
             if which else "")
            + "Task Manager can encrypt your database and backups.\n"
            "Choose how to unlock it each time you open the app:"
        ))

        self._rb_dpapi = QRadioButton("No password — use my Windows account")
        self._rb_dpapi.setChecked(True)
        lay.addWidget(self._rb_dpapi)
        hint1 = QLabel("Unlocks automatically while you are signed in to Windows.")
        hint1.setStyleSheet("color: gray;")
        hint1.setContentsMargins(22, 0, 0, 6)
        lay.addWidget(hint1)

        self._rb_pw = QRadioButton("Protect with a password")
        lay.addWidget(self._rb_pw)
        hint2 = QLabel("You will enter this password each time the app starts.")
        hint2.setStyleSheet("color: gray;")
        hint2.setContentsMargins(22, 0, 0, 6)
        lay.addWidget(hint2)

        self._pw1 = QLineEdit()
        self._pw1.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw1.setPlaceholderText("Password")
        self._pw2 = QLineEdit()
        self._pw2.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw2.setPlaceholderText("Confirm password")
        lay.addWidget(self._pw1)
        lay.addWidget(self._pw2)

        self._rb_pw.toggled.connect(self._sync)
        self._sync()

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _sync(self) -> None:
        on = self._rb_pw.isChecked()
        self._pw1.setEnabled(on)
        self._pw2.setEnabled(on)
        if on:
            self._pw1.setFocus()

    def _accept(self) -> None:
        if self._rb_pw.isChecked():
            p1, p2 = self._pw1.text(), self._pw2.text()
            if len(p1) < _MIN_PASSWORD_LEN:
                QMessageBox.warning(
                    self, "Password too short",
                    f"Use at least {_MIN_PASSWORD_LEN} characters.",
                )
                return
            if p1 != p2:
                QMessageBox.warning(self, "Mismatch", "The passwords do not match.")
                return
            self.mode = secure_store.MODE_PASSWORD
            self.password = p1
        else:
            self.mode = secure_store.MODE_DPAPI
        self.accept()


# --- unlock (password mode) -------------------------------------------------


class _UnlockDialog(QDialog):
    """Ask for the password on startup for a password-protected vault."""

    def __init__(self, vault_path: Path, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(app_variant.window_title("Unlock Task Manager"))
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self._vault = vault_path
        self.dek: Optional[bytes] = None

        lay = QVBoxLayout(self)
        which = app_variant.label()
        lay.addWidget(QLabel(
            f"Enter the password for the {which} app's data:" if which
            else "Enter your password to unlock your data:"
        ))
        self._pw = QLineEdit()
        self._pw.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw.returnPressed.connect(self._try)
        lay.addWidget(self._pw)
        self._err = QLabel("")
        self._err.setStyleSheet("color: #d9534f;")
        lay.addWidget(self._err)

        row = QHBoxLayout()
        rec = QPushButton("Use recovery code…")
        rec.clicked.connect(self._recovery)
        row.addWidget(rec)
        row.addStretch()
        lay.addLayout(row)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._try)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._pw.setFocus()

    def _try(self) -> None:
        try:
            self.dek = secure_store.unlock(self._vault, self._pw.text())
        except secure_store.UnlockError:
            self._err.setText("Wrong password. Try again.")
            self._pw.selectAll()
            self._pw.setFocus()
            return
        self.accept()

    def _recovery(self) -> None:
        dek = _prompt_recovery(self._vault, self)
        if dek is not None:
            self.dek = dek
            self.accept()


def _prompt_recovery(vault_path: Path, parent: QWidget) -> Optional[bytes]:
    if not secure_store.has_recovery(vault_path):
        QMessageBox.warning(
            parent, "No recovery code", "No recovery code was set for this data."
        )
        return None
    code, ok = QInputDialog.getText(
        parent, "Recovery code", "Enter your recovery code:"
    )
    if not ok:
        return None
    try:
        return secure_store.unlock_with_recovery(vault_path, code)
    except secure_store.UnlockError:
        QMessageBox.warning(parent, "Invalid", "That recovery code did not work.")
        return None


def _show_recovery_code(parent: QWidget, code: str) -> None:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle("Save your recovery code")
    box.setText(
        "Write down this recovery code and keep it somewhere safe.\n"
        "It is the ONLY way to recover your data if you forget your password:\n\n"
        f"        {code}\n"
    )
    copy_btn = box.addButton("Copy to clipboard", QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Ok)
    box.exec()
    if box.clickedButton() is copy_btn:
        QApplication.clipboard().setText(code)


# --- public entry point -----------------------------------------------------


CLIENT_CACHE_DIRNAME = "client_cache"


def ensure_unlocked_for_client(parent: Optional[QWidget] = None) -> bool:
    """Open the client's local database without asking for anything.

    A client keeps no data of its own - the boards, notes, accounts and resumes
    all live on the PM's server - so its local database holds nothing worth a
    password, and asking for one before the login that actually matters is two
    passwords for one sign-in.

    It gets its own folder and a vault bound to this Windows account, created
    silently on first run. That keeps the file encrypted at rest without a
    prompt, and leaves any database from a previous standalone install exactly
    where it was, still protected by its own password.
    """
    cache = Path(db._base_dir()) / CLIENT_CACHE_DIRNAME
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        QMessageBox.critical(
            parent, "Cannot start",
            f"Could not create the local cache folder:\n{cache}\n\n{exc}",
        )
        return False

    db._base_dir = lambda: cache          # everything local now lives here
    vpath = cache / "task_manager.vault"
    try:
        if secure_store.vault_exists(vpath):
            dek = secure_store.unlock(vpath)
        else:
            dek = secure_store.create_vault(vpath, secure_store.MODE_DPAPI, None)
    except Exception:            # noqa: BLE001
        # DPAPI refused (a different Windows account, or not Windows at all), or
        # the file is damaged. Deliberately broad: this is a throwaway cache
        # holding nothing, so anything that goes wrong should mean "start a
        # fresh one", never "block the app" or "ask for a password".
        try:
            for stale in (vpath, cache / db.ENC_FILENAME):
                if stale.exists():
                    stale.unlink()
            dek = secure_store.create_vault(vpath, secure_store.MODE_DPAPI, None)
        except (secure_store.VaultError, OSError) as exc:
            QMessageBox.critical(
                parent, "Cannot start",
                f"Could not prepare the local cache:\n{exc}",
            )
            return False
    db.unlock(dek)
    return True


def ensure_unlocked(parent: Optional[QWidget] = None) -> bool:
    """Run setup / unlock and put the DB into encrypted mode. False = cancelled."""
    vpath = _vault_path()
    if not secure_store.vault_exists(vpath):
        return _run_setup(vpath, parent)

    mode = secure_store.vault_mode(vpath)
    if mode == secure_store.MODE_DPAPI:
        try:
            dek = secure_store.unlock(vpath)
        except secure_store.UnlockError:
            # DPAPI could not unwrap (e.g. moved to a different Windows user).
            dek = _prompt_recovery(vpath, parent or QWidget())
            if dek is None:
                QMessageBox.critical(
                    parent, "Cannot unlock",
                    "This data is locked to a different Windows account and no "
                    "recovery code worked, so it cannot be opened here.",
                )
                return False
            db.unlock(dek)
            secure_store.switch_to_dpapi(vpath, dek)  # re-bind to this account
            return True
        db.unlock(dek)
        return True

    dlg = _UnlockDialog(vpath, parent)
    if dlg.exec() != QDialog.DialogCode.Accepted or dlg.dek is None:
        return False
    db.unlock(dlg.dek)
    return True


def _run_setup(vpath: Path, parent: Optional[QWidget]) -> bool:
    dlg = _SetupDialog(parent)
    if dlg.exec() != QDialog.DialogCode.Accepted or dlg.mode is None:
        return False
    dek = secure_store.create_vault(vpath, dlg.mode, dlg.password)
    migrate = db.plaintext_db_exists()
    db.unlock(dek, migrate_plaintext=migrate)
    if migrate:
        try:
            archived = db.archive_plaintext_db()
        except OSError:
            archived = None  # data is already safely encrypted; leave the old file
        if archived is not None:
            QMessageBox.information(
                parent, "Data migrated",
                "Your existing data was imported into the encrypted database.\n\n"
                f"The old unencrypted file was renamed to:\n{archived.name}\n\n"
                "You can delete it once you have confirmed everything looks right.",
            )
    if dlg.mode == secure_store.MODE_PASSWORD:
        code = secure_store.generate_recovery_code()
        secure_store.set_recovery(vpath, dek, code)
        _show_recovery_code(parent or dlg, code)
    return True


# --- Security settings dialog ----------------------------------------------


class SecuritySettingsDialog(QDialog):
    """Change the unlock mode, password, or recovery code while running."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Security")
        self.setModal(True)
        self._vault = _vault_path()
        self._lay = QVBoxLayout(self)
        self._status = QLabel("")
        self._lay.addWidget(self._status)

        self._btn_switch = QPushButton()
        self._btn_switch.clicked.connect(self._switch_mode)
        self._lay.addWidget(self._btn_switch)

        self._btn_change_pw = QPushButton("Change password…")
        self._btn_change_pw.clicked.connect(self._change_password)
        self._lay.addWidget(self._btn_change_pw)

        self._btn_recovery = QPushButton("Show new recovery code…")
        self._btn_recovery.clicked.connect(self._new_recovery)
        self._lay.addWidget(self._btn_recovery)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        self._lay.addWidget(bb)
        self._refresh()

    def _dek(self) -> Optional[bytes]:
        dek = db.current_dek()
        if dek is None:
            QMessageBox.warning(self, "Locked", "The database is not unlocked.")
        return dek

    def _refresh(self) -> None:
        mode = secure_store.vault_mode(self._vault)
        if mode == secure_store.MODE_PASSWORD:
            self._status.setText("Unlock mode: Password")
            self._btn_switch.setText("Switch to Windows account (no password)")
            self._btn_change_pw.setEnabled(True)
        else:
            self._status.setText("Unlock mode: Windows account (no password)")
            self._btn_switch.setText("Switch to a password…")
            self._btn_change_pw.setEnabled(False)

    def _switch_mode(self) -> None:
        dek = self._dek()
        if dek is None:
            return
        mode = secure_store.vault_mode(self._vault)
        if mode == secure_store.MODE_PASSWORD:
            secure_store.switch_to_dpapi(self._vault, dek)
            QMessageBox.information(
                self, "Done", "The app will now unlock with your Windows account."
            )
        else:
            pw = _ask_new_password(self)
            if pw is None:
                return
            secure_store.switch_to_password(self._vault, dek, pw)
            code = secure_store.generate_recovery_code()
            secure_store.set_recovery(self._vault, dek, code)
            _show_recovery_code(self, code)
        self._refresh()

    def _change_password(self) -> None:
        dek = self._dek()
        if dek is None:
            return
        old, ok = QInputDialog.getText(
            self, "Current password", "Enter your current password:",
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return
        try:
            secure_store.unlock(self._vault, old)
        except secure_store.UnlockError:
            QMessageBox.warning(self, "Wrong password", "The current password is incorrect.")
            return
        pw = _ask_new_password(self)
        if pw is None:
            return
        secure_store.change_password(self._vault, dek, pw)
        QMessageBox.information(self, "Done", "Your password has been changed.")

    def _new_recovery(self) -> None:
        dek = self._dek()
        if dek is None:
            return
        code = secure_store.generate_recovery_code()
        secure_store.set_recovery(self._vault, dek, code)
        _show_recovery_code(self, code)


def ask_passphrase(
    parent: QWidget, title: str = "Password", prompt: str = "Enter a password:",
    confirm: bool = True, min_len: int = _MIN_PASSWORD_LEN,
) -> Optional[str]:
    """Prompt for a passphrase (optionally with confirmation). None = cancelled."""
    while True:
        p1, ok = QInputDialog.getText(parent, title, prompt, QLineEdit.EchoMode.Password)
        if not ok:
            return None
        if len(p1) < min_len:
            QMessageBox.warning(
                parent, "Password too short", f"Use at least {min_len} characters."
            )
            continue
        if not confirm:
            return p1
        p2, ok = QInputDialog.getText(
            parent, title, "Re-enter the password:", QLineEdit.EchoMode.Password
        )
        if not ok:
            return None
        if p1 != p2:
            QMessageBox.warning(parent, "Mismatch", "The passwords do not match.")
            continue
        return p1


def ask_passphrase_once(
    parent: QWidget, title: str = "Password", prompt: str = "Enter the password:"
) -> Optional[str]:
    """Prompt for an existing passphrase without confirmation. None = cancelled."""
    p, ok = QInputDialog.getText(parent, title, prompt, QLineEdit.EchoMode.Password)
    return p if ok else None


def confirm_vault_password(parent: QWidget) -> bool:
    """Verify the user by re-entering their unlock password before a destructive
    action. Returns True if confirmed; for a Windows-account (DPAPI) vault there
    is no password, so it confirms automatically."""
    vpath = _vault_path()
    if secure_store.vault_mode(vpath) != secure_store.MODE_PASSWORD:
        return True
    pw = ask_passphrase_once(
        parent, "Confirm password", "Enter your password to confirm:"
    )
    if pw is None:
        return False
    try:
        secure_store.unlock(vpath, pw)
    except secure_store.UnlockError:
        QMessageBox.warning(parent, "Wrong password", "The password is incorrect.")
        return False
    return True


def _ask_new_password(parent: QWidget) -> Optional[str]:
    return ask_passphrase(parent, "New password", "Enter a new password:")
