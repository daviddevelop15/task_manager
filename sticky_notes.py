"""Sticky note editor window and list tab."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QSize, Qt, QTimer, QTime, pyqtSignal
from PyQt6.QtGui import QFont, QFontInfo, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
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
from delegates import CheckBoxHeader, DecorationFreeTableWidget
from clipboard_utils import COPY_ICON_BTN_QSS, apply_copy_button_flash, orange_copy_icon
from constants import ROLE_STICKY_ID, STICKY_COL_CHECK, STICKY_COL_NUM, STICKY_COL_PREVIEW, STICKY_COL_TITLE
from task_helpers import fields_match_search, normalize_search
from window_utils import show_existing_window


def sticky_body_preview(body: str, max_len: int = 96) -> str:
    line = " ".join(body.replace("\r\n", "\n").split())
    if len(line) <= max_len:
        return line
    return line[: max_len - 1].rstrip() + "…"


class StickyEditorWindow(QWidget):
    """Separate window to edit one sticky note; changes persist automatically (debounced)."""

    saved = pyqtSignal(int)
    closed_for_id = pyqtSignal(int)

    def __init__(self, sticky_id: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sticky_id = sticky_id
        self._loading = False
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )
        self.setMinimumSize(420, 320)
        self.resize(520, 400)
        self.setWindowIcon(app_icon())

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Title"))
        self._title = QLineEdit()
        self._body = QPlainTextEdit()
        self._body.setTabChangesFocus(True)
        _base = self.font()
        _fi = QFontInfo(_base)
        _pt = _fi.pointSizeF()
        if _pt <= 0:
            _pt = float(_fi.pointSize() or 10)
        _title_font = QFont(_base)
        _title_font.setPointSizeF(_pt * 1.5)
        self._title.setFont(_title_font)
        _body_font = QFont(_base)
        _body_font.setPointSizeF(_pt * 1.2)
        self._body.setFont(_body_font)
        root.addWidget(self._title)
        root.addWidget(QLabel("Body"))
        root.addWidget(self._body, stretch=1)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._flush_save)

        self._title.textChanged.connect(self._on_field_changed)
        self._body.textChanged.connect(self._on_field_changed)

        esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        esc_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        esc_shortcut.activated.connect(self.close)

        self._load_from_db()

    def _load_from_db(self) -> None:
        row = _store().get_sticky(self._sticky_id)
        self._loading = True
        self._title.blockSignals(True)
        self._body.blockSignals(True)
        try:
            if row is None:
                self._title.setText("")
                self._body.setPlainText("")
                self.setWindowTitle("Sticky note (missing)")
                return
            self._title.setText(row.title)
            self._body.setPlainText(row.body)
            self._update_window_title(row.title)
        finally:
            self._title.blockSignals(False)
            self._body.blockSignals(False)
            self._loading = False

    def _update_window_title(self, title: str) -> None:
        t = title.strip() or "Untitled"
        self.setWindowTitle(f"Sticky — {t}")

    def reload_from_db(self) -> None:
        self._load_from_db()

    def _on_field_changed(self) -> None:
        if self._loading:
            return
        self._save_timer.start()

    def _flush_save(self) -> None:
        if self._loading:
            return
        if _store().get_sticky(self._sticky_id) is None:
            return
        title = self._title.text()
        body = self._body.toPlainText()
        try:
            _store().update_sticky(self._sticky_id, title, body)
        except PermissionDenied as exc:
            # Only reachable in a race - the list shows nobody else's notes -
            # but this is an autosave, so failing silently would lose the edit
            # without the person ever knowing.
            self._save_timer.stop()
            QMessageBox.warning(self, "Note not saved", str(exc))
            return
        self._update_window_title(title)
        self.saved.emit(self._sticky_id)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._save_timer.stop()
        self._flush_save()
        self.closed_for_id.emit(self._sticky_id)
        super().closeEvent(event)


class StickyNotesPage(QWidget):
    """List of sticky notes; double-click a row cell to open an editor window."""

    list_counts_changed = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._editors: dict[int, StickyEditorWindow] = {}
        self._loading_table = False

        lay = QVBoxLayout(self)
        search_row = QHBoxLayout()
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText(
            "Search — matches title, body, or id (partial or whole text)…"
        )
        self._search_input.setClearButtonEnabled(True)
        # Search only on Enter or the Search button — live filtering on every
        # keystroke was a performance problem as data grew. Clearing the box
        # (the X button or deleting the text) restores all rows.
        self._search_input.returnPressed.connect(self._refresh_table)
        self._search_input.textChanged.connect(
            lambda text: self._refresh_table() if not text.strip() else None
        )
        search_row.addWidget(self._search_input)
        self._search_btn = QPushButton("Search")
        self._search_btn.clicked.connect(self._refresh_table)
        search_row.addWidget(self._search_btn)
        lay.addLayout(search_row)

        toolbar = QHBoxLayout()
        new_btn = QPushButton("New sticky")
        new_btn.setToolTip("Create a note and open it in a new window")
        new_btn.clicked.connect(self._on_new_sticky)
        del_btn = QPushButton("Delete checked")
        del_btn.setToolTip("Remove every note whose checkbox is checked in the first column")
        del_btn.clicked.connect(self._on_delete_selected)
        toolbar.addWidget(new_btn)
        toolbar.addWidget(del_btn)
        toolbar.addStretch()
        lay.addLayout(toolbar)

        self._table = DecorationFreeTableWidget(0, 4)
        self._table.setObjectName("stickyNotesTable")
        self._table.setStyleSheet(
            "QTableWidget#stickyNotesTable::item:selected { background-color: #5C9CE6; color: #111111; }"
            "QTableWidget#stickyNotesTable::item:selected:!active { background-color: #A5D6FA; color: #111111; }"
        )
        self._header = CheckBoxHeader(STICKY_COL_CHECK, self._table)
        self._table.setHorizontalHeader(self._header)
        self._header.toggled.connect(self._on_select_all)
        self._table.setHorizontalHeaderLabels(["", "#", "Title", "Body preview"])
        self._table.verticalHeader().setVisible(False)
        # Fixed one-line row height, matching the Acc board: rows never grow to
        # fit a multi-line body preview, so every note is the same height and the
        # list stays scannable.
        self._table.verticalHeader().setDefaultSectionSize(30)
        self._table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setWordWrap(False)
        self._table.itemChanged.connect(self._on_check_item_changed)
        hdr = self._table.horizontalHeader()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(STICKY_COL_CHECK, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(STICKY_COL_CHECK, 32)
        hdr.setSectionResizeMode(STICKY_COL_NUM, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(STICKY_COL_TITLE, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(STICKY_COL_PREVIEW, QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(STICKY_COL_NUM, 44)
        self._table.setColumnWidth(STICKY_COL_TITLE, 180)
        self._table.setColumnWidth(STICKY_COL_PREVIEW, 320)
        self._table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        lay.addWidget(self._table, stretch=1)

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:  # type: ignore[override]
        if not isinstance(obj, QWidget) or event is None:
            return False
        sticky_id: int | None = None
        w: QWidget | None = obj
        while w is not None:
            sid = w.property("sticky_id")
            if isinstance(sid, int):
                sticky_id = sid
                break
            w = w.parentWidget()
        if sticky_id is None:
            return False
        row = self._row_for_sticky_id(sticky_id)
        if row < 0:
            return False
        et = event.type()
        if et == QEvent.Type.MouseButtonDblClick:
            QTimer.singleShot(0, lambda sid=sticky_id: self._deferred_open_editor(sid))
            return False
        if et in (QEvent.Type.MouseButtonPress, QEvent.Type.FocusIn):
            self._table.selectRow(row)
        return False

    def _row_for_sticky_id(self, sticky_id: int) -> int:
        for r in range(self._table.rowCount()):
            ck = self._table.item(r, STICKY_COL_CHECK)
            if ck is not None and ck.data(ROLE_STICKY_ID) == sticky_id:
                return r
        return -1

    def _make_sticky_body_cell_widget(self, sticky_id: int, preview: str, full_body: str) -> QWidget:
        w = QWidget()
        w.setObjectName("stickyBodyCell")
        w.setProperty("sticky_id", sticky_id)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(4)
        lab = QLabel(preview)
        # One line, clipped — the row is a fixed height, so wrapping would only
        # hide the overflow behind the row edge. The preview is already a single
        # collapsed line; the full body is a click on the copy button away.
        lab.setWordWrap(False)
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        copy_btn = QPushButton()
        ico = orange_copy_icon()
        if not ico.isNull():
            copy_btn.setIcon(ico)
            copy_btn.setIconSize(QSize(18, 18))
        else:
            copy_btn.setText("⧉")
        copy_btn.setFixedSize(30, 28)
        copy_btn.setObjectName("copyIconBtn")
        copy_btn.setStyleSheet(COPY_ICON_BTN_QSS)
        copy_btn.setToolTip("Copy full body to clipboard")
        copy_btn.clicked.connect(
            lambda _c=False, b=full_body, btn=copy_btn: self._copy_sticky_body_to_clipboard(b, btn)
        )
        lay.addWidget(lab, stretch=1)
        lay.addWidget(copy_btn, stretch=0, alignment=Qt.AlignmentFlag.AlignVCenter)
        for cw in (w, lab, copy_btn):
            cw.installEventFilter(self)
        return w

    def _copy_sticky_body_to_clipboard(self, body: str, copy_btn: QPushButton | None = None) -> None:
        QApplication.clipboard().setText(body)
        top = self.window()
        if isinstance(top, QMainWindow):
            top.statusBar().showMessage("Sticky body copied to clipboard.", 5000)
        if copy_btn is not None:
            apply_copy_button_flash(copy_btn)

    def _checkbox_sticky_item(self, sticky_id: int) -> QTableWidgetItem:
        it = QTableWidgetItem()
        it.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsSelectable
        )
        it.setCheckState(Qt.CheckState.Unchecked)
        it.setData(ROLE_STICKY_ID, sticky_id)
        return it

    def _readonly_sticky_cell(self, text: str, sticky_id: int) -> QTableWidgetItem:
        it = QTableWidgetItem(text)
        it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
        it.setData(ROLE_STICKY_ID, sticky_id)
        return it

    def _sticky_matches_search(self, row: db.StickyRow, needle: str) -> bool:
        return fields_match_search(needle, row.title, row.body, str(row.id))

    def _refresh_table(self) -> None:
        self._loading_table = True
        self._table.blockSignals(True)
        needle = normalize_search(self._search_input.text())
        rows = [s for s in _store().list_stickies() if self._sticky_matches_search(s, needle)]
        self._table.setRowCount(len(rows))
        for r, s in enumerate(rows):
            self._table.setItem(r, STICKY_COL_CHECK, self._checkbox_sticky_item(s.id))
            self._table.setItem(
                r, STICKY_COL_NUM, self._readonly_sticky_cell(str(r + 1), s.id)
            )
            self._table.setItem(r, STICKY_COL_TITLE, self._readonly_sticky_cell(s.title or "—", s.id))
            self._table.setCellWidget(
                r,
                STICKY_COL_PREVIEW,
                self._make_sticky_body_cell_widget(
                    s.id, sticky_body_preview(s.body), s.body
                ),
            )
            # No resizeRowToContents: rows stay at the fixed one-line height set
            # on the vertical header, the same as the Acc board.
        self._table.blockSignals(False)
        self._loading_table = False
        self._update_select_all_header()
        self.list_counts_changed.emit(len(rows))

    def _on_select_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._table.blockSignals(True)
        for r in range(self._table.rowCount()):
            it = self._table.item(r, STICKY_COL_CHECK)
            if it is not None:
                it.setCheckState(state)
        self._table.blockSignals(False)

    def _on_check_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading_table or item.column() != STICKY_COL_CHECK:
            return
        self._update_select_all_header()

    def _update_select_all_header(self) -> None:
        total = self._table.rowCount()
        checked = sum(
            1
            for r in range(total)
            if (it := self._table.item(r, STICKY_COL_CHECK)) is not None
            and it.checkState() == Qt.CheckState.Checked
        )
        if total == 0 or checked == 0:
            state = Qt.CheckState.Unchecked
        elif checked == total:
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked
        self._header.set_check_state(state)

    def _sticky_id_for_row(self, row: int) -> int | None:
        it = self._table.item(row, STICKY_COL_CHECK)
        if it is None:
            it = self._table.item(row, STICKY_COL_NUM)
        if it is None:
            return None
        sid = it.data(ROLE_STICKY_ID)
        return sid if isinstance(sid, int) else None

    def _deferred_open_editor(self, sticky_id: int) -> None:
        if self._loading_table:
            return
        self._open_editor(sticky_id)

    def _on_cell_double_clicked(self, row: int, _col: int) -> None:
        if self._loading_table or row < 0:
            return
        sid = self._sticky_id_for_row(row)
        if sid is not None:
            QTimer.singleShot(0, lambda s=sid: self._deferred_open_editor(s))

    def _open_editor(self, sticky_id: int) -> None:
        w = self._editors.get(sticky_id)
        if w is not None:
            if sip.isdeleted(w):
                self._editors.pop(sticky_id, None)
            else:
                try:
                    w.reload_from_db()
                except RuntimeError:
                    self._editors.pop(sticky_id, None)
                else:
                    show_existing_window(w)
                    return
        w = StickyEditorWindow(sticky_id, self)
        w.saved.connect(self._on_editor_saved)
        w.closed_for_id.connect(self._forget_editor)
        w.destroyed.connect(lambda *args, sid=sticky_id: self._editors.pop(sid, None))
        self._editors[sticky_id] = w
        w.show()

    def _forget_editor(self, sticky_id: int) -> None:
        self._editors.pop(sticky_id, None)

    def _on_editor_saved(self, _sticky_id: int) -> None:
        self._refresh_table()

    def _on_new_sticky(self) -> None:
        sid = _store().add_sticky("Untitled", "")
        self._refresh_table()
        self._open_editor(sid)

    def _on_delete_selected(self) -> None:
        checked_ids: list[int] = []
        for r in range(self._table.rowCount()):
            ck = self._table.item(r, STICKY_COL_CHECK)
            if ck is None or ck.checkState() != Qt.CheckState.Checked:
                continue
            sid = ck.data(ROLE_STICKY_ID)
            if isinstance(sid, int):
                checked_ids.append(sid)
        checked_ids = list(dict.fromkeys(checked_ids))
        if not checked_ids:
            QMessageBox.information(
                self,
                "Delete",
                "Check one or more notes in the first column, then click Delete checked.",
            )
            return
        n = len(checked_ids)
        reply = QMessageBox.question(
            self,
            "Delete stickies" if n > 1 else "Delete sticky",
            f"Remove {n} note(s) from the database?" if n > 1 else "Remove this note from the database?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        refused = 0
        for sid in checked_ids:
            ed = self._editors.pop(sid, None)
            if ed is not None and not sip.isdeleted(ed):
                ed.close()
            try:
                _store().delete_sticky(sid)
            except PermissionDenied:
                refused += 1
        if refused:
            QMessageBox.warning(
                self, "Notes not deleted",
                f"{refused} note(s) belong to somebody else and were left alone.",
            )
        self._refresh_table()

    def shutdown(self) -> None:
        for w in list(self._editors.values()):
            if not sip.isdeleted(w):
                w.close()
        self._editors.clear()
