"""Table delegates and small Qt widget tweaks."""

from __future__ import annotations

from PyQt6.QtCore import QRect, Qt, pyqtSignal
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (
    QComboBox,
    QHeaderView,
    QPlainTextEdit,
    QStyle,
    QStyleOptionButton,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QTableWidget,
    QWidget,
)


class DecorationFreeTableWidget(QTableWidget):
    """Clears per-cell selection decoration so rows do not get left accent bars on each column."""

    def initViewItemOption(self, option: QStyleOptionViewItem) -> None:  # type: ignore[override]
        super().initViewItemOption(option)
        option.showDecorationSelected = False


class CheckBoxHeader(QHeaderView):
    """Horizontal header with a tri-state 'select all' checkbox in one column.

    Clicking the checkbox emits :attr:`toggled` with the new bool state; the page
    connects it to check/uncheck every row. Call :meth:`set_check_state` to keep
    the header in sync with the rows (checked / unchecked / partially checked).
    """

    toggled = pyqtSignal(bool)

    def __init__(self, check_column: int = 0, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._check_column = check_column
        self._check_state = Qt.CheckState.Unchecked
        self.setSectionsClickable(True)
        self.setHighlightSections(True)

    def set_check_state(self, state: Qt.CheckState) -> None:
        if state != self._check_state:
            self._check_state = state
            self.updateSection(self._check_column)

    def _indicator_rect(self, rect: QRect) -> QRect:
        size = self.style().pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth)
        x = rect.x() + (rect.width() - size) // 2
        y = rect.y() + (rect.height() - size) // 2
        return QRect(x, y, size, size)

    def paintSection(self, painter, rect, logicalIndex):  # type: ignore[no-untyped-def]
        super().paintSection(painter, rect, logicalIndex)
        if logicalIndex != self._check_column:
            return
        opt = QStyleOptionButton()
        opt.rect = self._indicator_rect(rect)
        opt.state = QStyle.StateFlag.State_Enabled
        if self._check_state == Qt.CheckState.Checked:
            opt.state |= QStyle.StateFlag.State_On
        elif self._check_state == Qt.CheckState.PartiallyChecked:
            opt.state |= QStyle.StateFlag.State_NoChange
        else:
            opt.state |= QStyle.StateFlag.State_Off
        self.style().drawPrimitive(
            QStyle.PrimitiveElement.PE_IndicatorCheckBox, opt, painter
        )

    def mousePressEvent(self, event):  # type: ignore[no-untyped-def]
        if self.logicalIndexAt(event.pos()) == self._check_column:
            new_checked = self._check_state != Qt.CheckState.Checked
            self.set_check_state(
                Qt.CheckState.Checked if new_checked else Qt.CheckState.Unchecked
            )
            self.toggled.emit(new_checked)
            return
        super().mousePressEvent(event)


class StatusComboBox(QComboBox):
    """Mouse wheel does not change the current index (avoids accidental changes while scrolling)."""

    def wheelEvent(self, event: QWheelEvent) -> None:  # type: ignore[override]
        event.ignore()


class DescriptionDelegate(QStyledItemDelegate):
    """Multiline description editor (QPlainTextEdit) in the table."""

    def __init__(self, table: QTableWidget, parent: QWidget | None = None) -> None:
        super().__init__(table)
        self._table = table

    def createEditor(self, parent: QWidget, option, index):  # type: ignore[no-untyped-def]
        ed = QPlainTextEdit(parent)
        ed.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        ed.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        ed.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        ed.setTabChangesFocus(True)
        ed.setMinimumHeight(96)
        return ed

    def setEditorData(self, editor: QWidget, index) -> None:  # type: ignore[no-untyped-def]
        if isinstance(editor, QPlainTextEdit):
            text = index.data(Qt.ItemDataRole.EditRole)
            if text is None:
                text = index.data(Qt.ItemDataRole.DisplayRole)
            editor.setPlainText(str(text) if text is not None else "")
        else:
            super().setEditorData(editor, index)

    def setModelData(self, editor: QWidget, model, index) -> None:  # type: ignore[no-untyped-def]
        if isinstance(editor, QPlainTextEdit):
            model.setData(index, editor.toPlainText(), Qt.ItemDataRole.EditRole)
            self._table.resizeRowToContents(index.row())
        else:
            super().setModelData(editor, model, index)

    def updateEditorGeometry(self, editor: QWidget, option, index) -> None:  # type: ignore[no-untyped-def]
        if not isinstance(editor, QPlainTextEdit):
            super().updateEditorGeometry(editor, option, index)
            return
        w = max(120, option.rect.width())
        doc = editor.document()
        doc.setTextWidth(w)
        h = int(max(96, min(300, doc.size().height() + 24)))
        editor.setGeometry(option.rect.x(), option.rect.y(), w, h)
        self._table.setRowHeight(index.row(), max(h + 4, self._table.rowHeight(index.row())))
