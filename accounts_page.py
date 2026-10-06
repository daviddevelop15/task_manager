"""Accounts (Acc) tab: read-only table; double-click row opens edit dialog with Save."""

from __future__ import annotations

import sqlite3

from PyQt6.QtCore import QEvent, QObject, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QFontInfo
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import database as db
from app_store import PermissionDenied, get_store as _store
from PyQt6 import sip
from app_resources import app_icon
from clipboard_utils import COPY_ICON_BTN_QSS, apply_copy_button_flash, orange_copy_icon
from task_helpers import fields_match_search, normalize_search
from constants import (
    ACC_COL_CHECK,
    ACC_COL_MAIL,
    ACC_COL_NUM,
    ACC_COL_OTHER,
    ACC_COL_PWD,
    ACC_COL_URL,
    ROLE_ACC_ID,
)
from delegates import CheckBoxHeader, DecorationFreeTableWidget
from window_utils import show_existing_window


def _other_table_display(other: str) -> str:
    """Full text for the table cell; keeps line breaks (unlike a single-line preview)."""
    return other.replace("\r\n", "\n")


def _pwd_display_mask(pwd_plain: str) -> str:
    if not pwd_plain:
        return ""
    return "•" * min(12, max(6, len(pwd_plain)))


def _copy_button() -> QPushButton:
    btn = QPushButton()
    ico = orange_copy_icon()
    if not ico.isNull():
        btn.setIcon(ico)
        btn.setIconSize(QSize(18, 18))
    else:
        btn.setText("⧉")
    btn.setFixedSize(30, 28)
    btn.setObjectName("copyIconBtn")
    btn.setStyleSheet(COPY_ICON_BTN_QSS)
    return btn


class AccEditDialog(QDialog):
    """Edit one account; use Save to persist. Closing the window saves any unsaved edits."""

    saved = pyqtSignal(int)
    closed_for_id = pyqtSignal(int)

    def __init__(self, acc_id: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._acc_id = acc_id
        self._loading = False
        self._snapshot: tuple[str, str, str, str] = ("", "", "", "")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )
        self.setModal(False)
        self.setMinimumSize(440, 360)
        self.resize(520, 420)
        self.setWindowIcon(app_icon())

        root = QVBoxLayout(self)
        self._url = QLineEdit()
        self._mail = QLineEdit()
        self._pwd = QLineEdit()
        self._pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self._other = QPlainTextEdit()
        self._other.setTabChangesFocus(True)
        self._other.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._other.setMinimumHeight(120)

        _base = self.font()
        _fi = QFontInfo(_base)
        _pt = _fi.pointSizeF()
        if _pt <= 0:
            _pt = float(_fi.pointSize() or 10)
        _body_font = QFont(_base)
        _body_font.setPointSizeF(_pt * 1.05)
        self._other.setFont(_body_font)

        root.addWidget(QLabel("URL"))
        root.addWidget(self._url)
        root.addWidget(QLabel("Mail"))
        root.addWidget(self._mail)
        root.addWidget(QLabel("Password"))
        root.addWidget(self._pwd)
        root.addWidget(QLabel("Other"))
        root.addWidget(self._other, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.setMinimumWidth(100)
        save_btn.clicked.connect(self._on_save_clicked)
        btn_row.addWidget(save_btn)
        root.addLayout(btn_row)

        self._url.textChanged.connect(self._on_field_changed)
        self._mail.textChanged.connect(self._on_field_changed)
        self._pwd.textChanged.connect(self._on_field_changed)
        self._other.textChanged.connect(self._on_field_changed)

        self._load_from_db()
        self.setWindowTitle(self._window_title())

    def reject(self) -> None:  # type: ignore[override]
        super().reject()

    def _window_title(self) -> str:
        u = self._url.text().strip()
        return f"Acc — {u}" if u else "Acc — (edit account)"

    def _load_from_db(self) -> None:
        row = _store().get_acc(self._acc_id)
        self._loading = True
        self._url.blockSignals(True)
        self._mail.blockSignals(True)
        self._pwd.blockSignals(True)
        self._other.blockSignals(True)
        try:
            if row is None:
                self._url.setText("")
                self._mail.setText("")
                self._pwd.setText("")
                self._other.setPlainText("")
                self._snapshot = ("", "", "", "")
                self.setWindowTitle("Acc — (missing)")
                return
            self._url.setText(row.url)
            self._mail.setText(row.mail)
            self._pwd.setText(row.pwd)
            self._other.setPlainText(row.other)
            self._snapshot = (row.url, row.mail, row.pwd, row.other)
        finally:
            self._url.blockSignals(False)
            self._mail.blockSignals(False)
            self._pwd.blockSignals(False)
            self._other.blockSignals(False)
            self._loading = False
        self.setWindowTitle(self._window_title())

    def reload_from_db(self) -> None:
        self._load_from_db()

    def _on_field_changed(self) -> None:
        if self._loading:
            return
        self.setWindowTitle(self._window_title())

    def _on_save_clicked(self) -> None:
        if self._flush_save():
            top = self.window()
            if isinstance(top, QMainWindow):
                top.statusBar().showMessage("Account saved.", 3000)

    def _flush_save(self) -> bool:
        if self._loading:
            return False
        if _store().get_acc(self._acc_id) is None:
            return False
        url = self._url.text()
        mail = self._mail.text()
        pwd = self._pwd.text()
        other = self._other.toPlainText()
        prev = self._snapshot
        if (url, mail, pwd, other) == prev:
            return True
        pwd_prev = prev[2]
        pwd_arg: str | None
        if pwd != pwd_prev:
            pwd_arg = pwd
        else:
            pwd_arg = None
        try:
            _store().update_acc(self._acc_id, url, mail, pwd_arg, other)
        except (PermissionDenied, sqlite3.Error, OSError, ValueError, RuntimeError) as e:
            QMessageBox.warning(self, "Save failed", str(e))
            return False
        self._snapshot = (url, mail, pwd, other)
        self.saved.emit(self._acc_id)
        return True

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._flush_save()
        self.closed_for_id.emit(self._acc_id)
        super().closeEvent(event)


class AccountsPage(QWidget):
    """Account list (read-only cells); double-click a row to edit in a dialog."""

    list_counts_changed = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading_table = False
        self._editors: dict[int, AccEditDialog] = {}
        self._refresh_table_timer = QTimer(self)
        self._refresh_table_timer.setSingleShot(True)
        self._refresh_table_timer.setInterval(50)
        self._refresh_table_timer.timeout.connect(self._refresh_table)

        lay = QVBoxLayout(self)
        search_row = QHBoxLayout()
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText(
            "Search — matches URL, mail, password, other, or id (partial or whole text)…"
        )
        self._search_input.setClearButtonEnabled(True)
        # Search only on Enter or the Search button — live filtering on every
        # keystroke was a performance problem as data grew. Clearing the box
        # (the X button or deleting the text) restores all rows.
        self._search_input.returnPressed.connect(self._schedule_refresh_table)
        self._search_input.textChanged.connect(
            lambda text: self._schedule_refresh_table() if not text.strip() else None
        )
        search_row.addWidget(self._search_input)
        self._search_btn = QPushButton("Search")
        self._search_btn.clicked.connect(self._schedule_refresh_table)
        search_row.addWidget(self._search_btn)
        lay.addLayout(search_row)

        toolbar = QHBoxLayout()
        new_btn = QPushButton("New account")
        new_btn.setToolTip("Add a row and open the editor")
        new_btn.clicked.connect(self._on_new_acc)
        del_btn = QPushButton("Delete checked")
        del_btn.setToolTip("Remove rows whose first-column checkbox is checked")
        del_btn.clicked.connect(self._on_delete_selected)
        toolbar.addWidget(new_btn)
        toolbar.addWidget(del_btn)
        toolbar.addStretch()
        lay.addLayout(toolbar)

        self._table = DecorationFreeTableWidget(0, 6)
        self._table.setObjectName("accountsTable")
        self._table.setStyleSheet(
            "QTableWidget#accountsTable::item:selected { background-color: #5C9CE6; color: #111111; }"
            "QTableWidget#accountsTable::item:selected:!active { background-color: #A5D6FA; color: #111111; }"
        )
        self._header = CheckBoxHeader(ACC_COL_CHECK, self._table)
        self._table.setHorizontalHeader(self._header)
        self._header.toggled.connect(self._on_select_all)
        self._table.setHorizontalHeaderLabels(["", "#", "URL", "Mail", "Password", "Other"])
        self._table.verticalHeader().setVisible(False)
        # Fixed one-line row height regardless of the (possibly multi-line)
        # "Other" content — rows never grow to fit the description.
        self._table.verticalHeader().setDefaultSectionSize(30)
        self._table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setWordWrap(False)
        self._table.itemDoubleClicked.connect(self._on_checkbox_item_double_clicked)
        self._table.itemChanged.connect(self._on_check_item_changed)
        hdr = self._table.horizontalHeader()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(ACC_COL_CHECK, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(ACC_COL_CHECK, 32)
        hdr.setSectionResizeMode(ACC_COL_NUM, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(ACC_COL_URL, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(ACC_COL_MAIL, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(ACC_COL_PWD, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(ACC_COL_OTHER, QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(ACC_COL_NUM, 44)
        self._table.setColumnWidth(ACC_COL_URL, 200)
        self._table.setColumnWidth(ACC_COL_MAIL, 180)
        self._table.setColumnWidth(ACC_COL_PWD, 140)
        self._table.setColumnWidth(ACC_COL_OTHER, 220)
        lay.addWidget(self._table, stretch=1)

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:  # type: ignore[override]
        if not isinstance(obj, QWidget) or event is None:
            return False
        acc_id: int | None = None
        w: QWidget | None = obj
        while w is not None:
            sid = w.property("acc_id")
            if isinstance(sid, int):
                acc_id = sid
                break
            w = w.parentWidget()
        if acc_id is None:
            return False
        row = self._row_for_acc_id(acc_id)
        if row < 0:
            return False
        et = event.type()
        if et == QEvent.Type.MouseButtonDblClick:
            QTimer.singleShot(0, lambda aid=acc_id: self._deferred_open_editor(aid))
            return False
        if et in (QEvent.Type.MouseButtonPress, QEvent.Type.FocusIn):
            self._table.selectRow(row)
        return False

    def _deferred_open_editor(self, acc_id: int) -> None:
        if self._loading_table:
            return
        self._open_editor(acc_id)

    def _row_for_acc_id(self, acc_id: int) -> int:
        for r in range(self._table.rowCount()):
            ck = self._table.item(r, ACC_COL_CHECK)
            if ck is not None and ck.data(ROLE_ACC_ID) == acc_id:
                return r
        return -1

    def _status_message(self, text: str) -> None:
        top = self.window()
        if isinstance(top, QMainWindow):
            top.statusBar().showMessage(text, 5000)

    def _copy_text(self, text: str, copy_btn: QPushButton | None, ok_msg: str) -> None:
        QApplication.clipboard().setText(text)
        self._status_message(ok_msg)
        if copy_btn is not None:
            apply_copy_button_flash(copy_btn)

    def _make_display_cell(
        self,
        acc_id: int,
        display_text: str,
        copy_text: str,
        *,
        copy_tip: str,
        ok_msg: str,
        show_copy: bool = True
    ) -> QWidget:
        w = QWidget()
        w.setProperty("acc_id", acc_id)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(4)
        lab = QLabel(display_text)
        lab.setTextFormat(Qt.TextFormat.PlainText)
        lab.setWordWrap(False)  # keep every cell to a single line
        lab.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if display_text.strip():
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        else:
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        
        lay.addWidget(lab, stretch=1)
        if show_copy:
            copy_btn = _copy_button()
            copy_btn.setToolTip(copy_tip)
            copy_btn.clicked.connect(
                lambda _c=False, t=copy_text, btn=copy_btn: self._copy_text(t, btn, ok_msg)
            )
            
            lay.addWidget(copy_btn, stretch=0, alignment=Qt.AlignmentFlag.AlignTop)
            copy_btn.installEventFilter(self)

        for cw in (w, lab):
            cw.installEventFilter(self)
        return w

    def _make_index_cell(self, acc_id: int, display: str) -> QWidget:
        return self._make_display_cell(
            acc_id,
            display,
            display,
            copy_tip="Copy row number",
            ok_msg="Copied.",
            show_copy=False
        )

    def _checkbox_acc_item(self, acc_id: int) -> QTableWidgetItem:
        it = QTableWidgetItem()
        it.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsSelectable
        )
        it.setCheckState(Qt.CheckState.Unchecked)
        it.setData(ROLE_ACC_ID, acc_id)
        return it

    def _acc_matches_search(self, row: db.AccRow, needle: str) -> bool:
        return fields_match_search(
            needle, row.url, row.mail, row.pwd, row.other, str(row.id)
        )

    def _refresh_table(self) -> None:
        self._loading_table = True
        self._table.blockSignals(True)
        needle = normalize_search(self._search_input.text())
        rows = [a for a in _store().list_accs() if self._acc_matches_search(a, needle)]
        self._table.setRowCount(len(rows))
        for r, a in enumerate(rows):
            self._table.setItem(r, ACC_COL_CHECK, self._checkbox_acc_item(a.id))
            self._table.setCellWidget(
                r,
                ACC_COL_NUM,
                self._make_index_cell(a.id, str(r + 1)),
            )
            url_disp = (a.url or "").strip().upper()
            self._table.setCellWidget(
                r,
                ACC_COL_URL,
                self._make_display_cell(
                    a.id,
                    url_disp,
                    a.url,
                    copy_tip="Copy URL",
                    ok_msg="URL copied to clipboard.",
                ),
            )
            mail_disp = (a.mail or "").strip()
            self._table.setCellWidget(
                r,
                ACC_COL_MAIL,
                self._make_display_cell(
                    a.id,
                    mail_disp,
                    a.mail,
                    copy_tip="Copy mail",
                    ok_msg="Mail copied to clipboard.",
                ),
            )
            self._table.setCellWidget(
                r,
                ACC_COL_PWD,
                self._make_display_cell(
                    a.id,
                    _pwd_display_mask(a.pwd),
                    a.pwd,
                    copy_tip="Copy password",
                    ok_msg="Password copied to clipboard.",
                ),
            )
            self._table.setCellWidget(
                r,
                ACC_COL_OTHER,
                self._make_display_cell(
                    a.id,
                    " ".join((a.other or "").split()),  # collapse to one line for the cell
                    a.other,  # copy still yields the full multi-line value
                    copy_tip="Copy other",
                    ok_msg="Copied to clipboard.",
                ),
            )
        self._table.blockSignals(False)
        self._loading_table = False
        self._update_select_all_header()
        self.list_counts_changed.emit(len(rows))

    def _on_select_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._table.blockSignals(True)
        for r in range(self._table.rowCount()):
            it = self._table.item(r, ACC_COL_CHECK)
            if it is not None:
                it.setCheckState(state)
        self._table.blockSignals(False)

    def _on_check_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading_table or item.column() != ACC_COL_CHECK:
            return
        self._update_select_all_header()

    def _update_select_all_header(self) -> None:
        total = self._table.rowCount()
        checked = sum(
            1
            for r in range(total)
            if (it := self._table.item(r, ACC_COL_CHECK)) is not None
            and it.checkState() == Qt.CheckState.Checked
        )
        if total == 0 or checked == 0:
            state = Qt.CheckState.Unchecked
        elif checked == total:
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked
        self._header.set_check_state(state)

    def _acc_id_for_row(self, row: int) -> int | None:
        it = self._table.item(row, ACC_COL_CHECK)
        if it is None:
            return None
        sid = it.data(ROLE_ACC_ID)
        return sid if isinstance(sid, int) else None

    def _on_checkbox_item_double_clicked(self, item: QTableWidgetItem) -> None:
        if self._loading_table or item.column() != ACC_COL_CHECK:
            return
        aid = item.data(ROLE_ACC_ID)
        if isinstance(aid, int):
            QTimer.singleShot(0, lambda a=aid: self._deferred_open_editor(a))

    def _open_editor(self, acc_id: int) -> None:
        if self._loading_table:
            return
        w = self._editors.get(acc_id)
        if w is not None:
            if sip.isdeleted(w):
                self._editors.pop(acc_id, None)
            else:
                try:
                    w.reload_from_db()
                except RuntimeError:
                    self._editors.pop(acc_id, None)
                else:
                    show_existing_window(w)
                    return
        w = AccEditDialog(acc_id, self)
        w.saved.connect(self._on_editor_saved)
        w.closed_for_id.connect(self._forget_editor)
        w.destroyed.connect(lambda *args, aid=acc_id: self._editors.pop(aid, None))
        self._editors[acc_id] = w
        w.show()

    def _forget_editor(self, acc_id: int) -> None:
        self._editors.pop(acc_id, None)

    def _schedule_refresh_table(self) -> None:
        self._refresh_table_timer.start()

    def _on_editor_saved(self, _acc_id: int) -> None:
        self._schedule_refresh_table()

    def _on_new_acc(self) -> None:
        self._refresh_table_timer.stop()
        aid = _store().add_acc("", "", "", "")
        self._refresh_table()
        QTimer.singleShot(0, lambda a=aid: self._deferred_open_editor(a))

    def _on_delete_selected(self) -> None:
        checked_ids: list[int] = []
        for r in range(self._table.rowCount()):
            ck = self._table.item(r, ACC_COL_CHECK)
            if ck is None or ck.checkState() != Qt.CheckState.Checked:
                continue
            aid = ck.data(ROLE_ACC_ID)
            if isinstance(aid, int):
                checked_ids.append(aid)
        checked_ids = list(dict.fromkeys(checked_ids))
        if not checked_ids:
            QMessageBox.information(
                self,
                "Delete",
                "Check one or more rows in the first column, then click Delete checked.",
            )
            return
        n = len(checked_ids)
        reply = QMessageBox.question(
            self,
            "Delete accounts" if n > 1 else "Delete account",
            f"Remove {n} account(s) from the database?" if n > 1 else "Remove this account from the database?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        refused = 0
        for aid in checked_ids:
            ed = self._editors.pop(aid, None)
            if ed is not None:
                ed.close()
            try:
                _store().delete_acc(aid)
            except PermissionDenied:
                refused += 1
        if refused:
            QMessageBox.warning(
                self, "Accounts not deleted",
                f"{refused} account(s) belong to somebody else and were left alone.",
            )
        self._refresh_table()

    def shutdown(self) -> None:
        self._refresh_table_timer.stop()
        for w in list(self._editors.values()):
            w.close()
        self._editors.clear()
