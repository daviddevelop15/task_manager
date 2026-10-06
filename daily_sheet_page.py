"""Daily board as an Excel-style multi-sheet grid.

Replaces the old Kanban board. Each column of the board is now a *sheet* (a tab
at the bottom, Excel style); the active sheet shows its cards as fixed-height
rows in a table with the columns Status, Site, Company, Role, Link, Description.
Cells are read-only — selecting a row reveals a details panel on the right, and
double-clicking a row (or the panel's Edit button) opens the existing card
editor. Search, status filter and the date filters are unchanged.

Heavy logic (the card editor, body parsers, status palette, date helpers) is
reused from :mod:`daily_board_page`; only the view layer is new.
"""

from __future__ import annotations

import re
from typing import Optional

from PyQt6 import sip
from PyQt6.QtCore import QDate, QDateTime, QEvent, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPalette, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabBar,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import QSettings

import ai_assistant
import busy_overlay
import database as db
from app_store import PermissionDenied, author_label, get_store as _store
from task_helpers import fields_match_search, normalize_search
from window_utils import show_existing_window
from daily_board_page import (
    DailyCardEditDialog,
    _DAILY_STATUS_COLORS,
    _DAILY_STATUS_FILTER_ALL,
    _DAILY_STATUS_NONE,
    _DAILY_STATUS_ORDER,
    _SETTINGS_APP,
    _SETTINGS_ORG,
    _business_today,
    _card_to_html_document,
    _card_to_markdown,
    _configure_board_datetime_editor,
    _daily_glyph_icon,
    _daily_status_color,
    _daily_status_swatch,
    _day_window_bounds,
    _default_period_range,
    _excel_description_from_text,
    _excel_field_from_text,
    _excel_role_from_title,
    _excel_row_for_card,
    _first_line_title,
    _job_site_from_title,
    _plain_text_from_daily_body,
    _safe_filename,
    _site_name_from_url,
)

# How long a sheet switch may take before it is worth a spinner. Below this it
# reads as instant and an overlay would only flicker; above it, the window is
# visibly stuck and needs to say why.
_SPINNER_DELAY_MS = 180

_ROW_HEIGHT = 26
# "Creator" leads every row: each row is a job somebody applied to, so the
# applier is the first thing to see once the board is shared across a team.
_BASE_FIELDS = ("Creator", "Created", "Status", "Site", "Company", "Role", "Link",
                "Description")
_COL_CHECK = 0  # the per-row checkbox is always column 0

# Creator-filter combo data. Real creators are stored under their own name, so
# the two sentinels use a shape a display name can never take.
_CREATOR_FILTER_ALL = "\x00all"
_CREATOR_UNATTRIBUTED = "\x00none"
_CREATOR_UNATTRIBUTED_LABEL = "Unattributed"


def _creator_key(card: db.DailyCardRow) -> str:
    """Filter key for a card's creator; rows predating authorship are grouped."""
    return (card.created_by or "").strip() or _CREATOR_UNATTRIBUTED


def _creator_label(card: db.DailyCardRow) -> str:
    """Who applied to this job, as shown in the grid.

    The column stores an account id, so it has to be resolved to a name - that
    indirection is what lets somebody rename themselves without losing the rows
    they created.
    """
    return author_label((card.created_by or "").strip()) or "—"


def _site_from_title(title: str) -> str:
    """Job-site prefix before the first ' - ' in the title, else ''."""
    m = re.match(r"^(.+?)\s[-–—]\s+.+$", (title or "").strip())
    return m.group(1).strip() if m else ""


def _card_field_values(card: db.DailyCardRow) -> tuple[str, str, str, str, str, str, str]:
    """(created, status, site, company, role, link, description) for the grid;
    the last six are parsed the same way the Excel export builds a row."""
    text = _plain_text_from_daily_body(card.body)
    company = _excel_field_from_text(text, "Company")
    role = _excel_role_from_title(card.title)
    link = _excel_field_from_text(text, "URL") or _excel_field_from_text(text, "Company Link")
    site = _site_name_from_url(link) or _site_from_title(card.title)
    description = _excel_description_from_text(text)
    created = (card.created_at or "")[:16]  # 'YYYY-MM-DD HH:MM'
    return (created, card.status or "", site, company, role, link, description)


class DailyBoardPage(QWidget):
    """Excel-style multi-sheet view of the daily board (one sheet per column)."""

    board_counts_changed = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DailyBoardPage")
        self._day = _business_today()
        self._loading = False
        self._populating = False
        self._editors: dict[int, DailyCardEditDialog] = {}
        self._lists: list[db.DailyListRow] = []
        # Body-less card summaries grouped by sheet: enough for tab counts, the
        # Creator filter and status/date grouping, but a fraction of the payload
        # of the full board over the network. The bodies of the sheet actually
        # on screen are fetched separately into ``_active_cards``.
        self._cards_by_list: dict[int, list[db.DailyCardRow]] = {}
        self._active_cards: list[db.DailyCardRow] = []
        self._active_list_id: Optional[int] = None
        self._checked: set[int] = set()  # checked card ids on the active sheet
        # Cross-sheet search state: when active, a temporary "Search results"
        # sheet aggregates matching rows from the chosen sheets, with a leading
        # "Sheet" column.
        self._search_active = False
        # Set only while a search the user asked for is running; see
        # _report_progress for why the background paths must leave it alone.
        self._busy_session = None
        self._sheet_switch_ms = 0        # how long the last sheet switch took
        self._search_scope: set[int] = set()
        self._search_needle_committed = ""
        self._search_entries: list[tuple[db.DailyCardRow, str]] = []
        self._has_sheet_col = False

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(50)
        self._refresh_timer.timeout.connect(self._rebuild)

        root = QVBoxLayout(self)

        # --- Search + status filter (unchanged behaviour) ---
        search_row = QHBoxLayout()
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText(
            "Search — matches sheet title, card title, body, status, date, or id…"
        )
        self._search_input.setClearButtonEnabled(True)
        self._search_input.returnPressed.connect(self._on_search_changed)
        self._search_input.textChanged.connect(
            lambda text: self._on_search_changed() if not text.strip() else None
        )
        search_row.addWidget(self._search_input)
        self._search_btn = QPushButton("Search")
        self._search_btn.clicked.connect(self._on_search_changed)
        search_row.addWidget(self._search_btn)
        search_row.addWidget(QLabel("Status"))
        self._status_filter_combo = QComboBox()
        self._status_filter_combo.setToolTip("Show only cards with the selected status")
        self._status_filter_combo.addItem("All statuses", _DAILY_STATUS_FILTER_ALL)
        self._status_filter_combo.addItem("No status", _DAILY_STATUS_NONE)
        for name in _DAILY_STATUS_ORDER:
            self._status_filter_combo.addItem(
                _daily_status_swatch(_DAILY_STATUS_COLORS[name]), name, name
            )
        self._status_filter_combo.currentIndexChanged.connect(self._on_status_filter_changed)
        search_row.addWidget(self._status_filter_combo)
        search_row.addWidget(QLabel("Creator"))
        self._creator_filter_combo = QComboBox()
        self._creator_filter_combo.setToolTip("Show only jobs applied to by the selected person")
        self._creator_filter_combo.setMinimumWidth(130)
        self._creator_filter_combo.addItem("All creators", _CREATOR_FILTER_ALL)
        self._creator_filter_combo.currentIndexChanged.connect(self._on_creator_filter_changed)
        search_row.addWidget(self._creator_filter_combo)
        self._job_stats_btn = QPushButton("Job stats")
        self._job_stats_btn.setToolTip(
            "Total job bids by job site across every sheet, from the first "
            "day to the last (ignores the current filters)"
        )
        self._job_stats_btn.clicked.connect(self._show_job_bid_stats)
        search_row.addWidget(self._job_stats_btn)
        root.addLayout(search_row)

        # --- Date range filter ---
        period_row = QHBoxLayout()
        self._period_filter_check = QCheckBox("Filter by &date range")
        self._period_filter_check.toggled.connect(self._on_period_filter_toggled)
        period_row.addWidget(self._period_filter_check)
        self._period_from_label = QLabel("From")
        period_row.addWidget(self._period_from_label)
        self._period_from = QDateTimeEdit()
        _configure_board_datetime_editor(self._period_from)
        self._period_from.dateTimeChanged.connect(self._on_period_range_changed)
        period_row.addWidget(self._period_from)
        self._period_to_label = QLabel("To")
        period_row.addWidget(self._period_to_label)
        self._period_to = QDateTimeEdit()
        _configure_board_datetime_editor(self._period_to)
        self._period_to.dateTimeChanged.connect(self._on_period_range_changed)
        period_row.addWidget(self._period_to)
        period_row.addStretch()
        root.addLayout(period_row)

        # --- Board day picker ---
        day_row = QHBoxLayout()
        self._day_filter_check = QCheckBox("Filter by &board day")
        self._day_filter_check.setToolTip(
            "Show only cards created on this day. Every sheet stays on the tab "
            "bar either way — this filters rows, not sheets."
        )
        self._day_filter_check.toggled.connect(self._on_day_filter_toggled)
        day_row.addWidget(self._day_filter_check)
        self._day_label = QLabel("Day")
        day_row.addWidget(self._day_label)
        self._day_edit = QDateEdit()
        self._day_edit.setCalendarPopup(True)
        self._day_edit.setDisplayFormat("yyyy-MM-dd")
        self._day_edit.setMinimumWidth(140)
        self._day_edit.setDate(self._day)
        self._day_edit.dateChanged.connect(self._on_board_day_picker_changed)
        day_row.addWidget(self._day_edit)
        today_btn = QPushButton("Today")
        today_btn.clicked.connect(self._on_board_today)
        day_row.addWidget(today_btn)
        self._prev_btn = QPushButton("◀")
        self._prev_btn.setFixedWidth(36)
        self._prev_btn.clicked.connect(self._prev_day)
        day_row.addWidget(self._prev_btn)
        self._next_btn = QPushButton("▶")
        self._next_btn.setFixedWidth(36)
        self._next_btn.clicked.connect(self._next_day)
        day_row.addWidget(self._next_btn)
        day_row.addStretch()
        root.addLayout(day_row)

        # --- Sheet title + per-sheet add buttons ---
        title_row = QHBoxLayout()
        self._sheet_title = QLabel("—")
        tf = QFont()
        tf.setBold(True)
        tf.setPointSize(12)
        self._sheet_title.setFont(tf)
        title_row.addWidget(self._sheet_title)
        title_row.addStretch()
        pal = QApplication.palette()
        tbcolor = pal.color(QPalette.ColorRole.ButtonText)

        def _tool(kind: str, tooltip: str, slot) -> QToolButton:
            b = QToolButton()
            b.setIcon(_daily_glyph_icon(kind, tbcolor))
            b.setIconSize(QSize(20, 20))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            b.setAutoRaise(True)
            b.setToolTip(tooltip)
            b.clicked.connect(slot)
            return b

        self._add_btn = _tool("add", "Add a new blank card to this sheet", lambda: self._add_card())
        title_row.addWidget(self._add_btn)
        self._paste_btn = _tool(
            "paste", "Create a card from the clipboard text (F2)", lambda: self._paste_card()
        )
        title_row.addWidget(self._paste_btn)
        # F2 pastes from the clipboard whenever the Daily board tab is showing,
        # regardless of which sub-widget has focus. WindowShortcut (not
        # WidgetWithChildren) is what makes it reliable: the latter only fires
        # when focus happens to be inside the board, so it missed when focus sat
        # on the tab bar, a sheet button, or nothing in particular. Qt disables
        # the shortcut automatically while the page is hidden (another tab), so
        # it never fires off the Daily board.
        self._paste_shortcut = QShortcut(QKeySequence(Qt.Key.Key_F2), self)
        self._paste_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._paste_shortcut.activated.connect(self._paste_card)
        self._ai_paste_btn = _tool(
            "ai_paste", "Create a card and let AI parse the clipboard job posting",
            lambda: self._ai_paste_card(),
        )
        title_row.addWidget(self._ai_paste_btn)
        self._sheet_menu_btn = QToolButton()
        self._sheet_menu_btn.setText("⋮")
        self._sheet_menu_btn.setToolTip("Sheet actions")
        self._sheet_menu_btn.clicked.connect(self._show_sheet_menu_at_button)
        title_row.addWidget(self._sheet_menu_btn)
        root.addLayout(title_row)

        # --- Table + details panel ---
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._table = QTableWidget(0, 0)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setWordWrap(False)
        self._table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(_ROW_HEIGHT)
        self._table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_row_context_menu)
        self._table.itemSelectionChanged.connect(self._on_row_selection_changed)
        self._table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.installEventFilter(self)  # Esc closes the details panel
        # Click a column header to sort by that column; clicking again toggles
        # ascending/descending. Applies to both a normal sheet and Search results.
        self._table.setSortingEnabled(True)
        self._table.horizontalHeader().setSortIndicatorShown(True)
        self._table.horizontalHeader().setSectionsClickable(True)
        self._configure_columns()
        self._splitter.addWidget(self._table)

        self._detail_panel = self._build_detail_panel()
        self._detail_panel.setVisible(False)
        self._detail_panel.installEventFilter(self)
        self._splitter.addWidget(self._detail_panel)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        root.addWidget(self._splitter, stretch=1)

        # --- Bottom: New Sheet button + Excel-style sheet tabs ---
        tabs_row = QHBoxLayout()
        self._new_sheet_btn = QPushButton("+ Sheet")
        self._new_sheet_btn.setToolTip("Create a new sheet (column) for this day")
        self._new_sheet_btn.clicked.connect(self._new_sheet)
        tabs_row.addWidget(self._new_sheet_btn)
        self._tabbar = QTabBar()
        self._tabbar.setShape(QTabBar.Shape.RoundedSouth)
        self._tabbar.setExpanding(False)
        self._tabbar.setDrawBase(False)
        self._tabbar.setMovable(True)  # drag tabs to reorder sheets (Excel-style)
        # The selected sheet tab is highlighted with the accent colour so the
        # active sheet is obvious among the bottom ripple of tabs.
        self._tabbar.setStyleSheet(
            "QTabBar::tab {"
            "  padding: 4px 12px; margin-right: 2px;"
            "  border: 1px solid palette(mid); border-top: none;"
            "  border-bottom-left-radius: 4px; border-bottom-right-radius: 4px;"
            "  background: palette(button); color: palette(button-text); }"
            "QTabBar::tab:selected {"
            "  background: palette(highlight); color: palette(highlighted-text);"
            "  font-weight: bold; }"
            "QTabBar::tab:hover:!selected { background: palette(midlight); }"
        )
        self._tabbar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tabbar.customContextMenuRequested.connect(self._on_tab_context_menu)
        self._tabbar.currentChanged.connect(self._on_tab_changed)
        self._tabbar.tabMoved.connect(self._on_tab_moved)
        tabs_row.addWidget(self._tabbar, stretch=1)
        root.addLayout(tabs_row)

        self._restore_filter_settings()
        self._sync_filter_control_states()
        self.refresh_board()

    # ---------------------------------------------------------------- panel

    def _build_detail_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("dailyDetailPanel")
        panel.setFrameShape(QFrame.Shape.StyledPanel)
        panel.setMinimumWidth(300)
        outer = QVBoxLayout(panel)

        self._detail_title = QLabel("")
        dtf = QFont()
        dtf.setBold(True)
        dtf.setPointSize(12)
        self._detail_title.setFont(dtf)
        self._detail_title.setWordWrap(True)
        outer.addWidget(self._detail_title)

        pal = QApplication.palette()
        icon_color = pal.color(QPalette.ColorRole.ButtonText)

        def _icon_btn(kind: str, tooltip: str, slot=None, color: QColor = icon_color) -> QToolButton:
            b = QToolButton()
            b.setIcon(_daily_glyph_icon(kind, color))
            b.setIconSize(QSize(20, 20))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            b.setAutoRaise(True)
            b.setToolTip(tooltip)
            if slot is not None:
                b.clicked.connect(slot)
            return b

        btn_row = QHBoxLayout()
        btn_row.addWidget(_icon_btn("edit", "Edit card", self._edit_selected))
        btn_row.addWidget(_icon_btn("copy", "Copy all job details", self._copy_selected_details))
        btn_row.addWidget(_icon_btn("move", "Move to another sheet", self._move_selected_via_menu))
        export_btn = QToolButton()
        export_btn.setIcon(_daily_glyph_icon("export", icon_color))
        export_btn.setIconSize(QSize(20, 20))
        export_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        export_btn.setAutoRaise(True)
        export_btn.setToolTip("Export card")
        export_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        export_menu = QMenu(export_btn)
        export_menu.addAction("Markdown (.md)…", lambda: self._export_selected("md"))
        export_menu.addAction("HTML (.html)…", lambda: self._export_selected("html"))
        export_btn.setMenu(export_menu)
        btn_row.addWidget(export_btn)
        btn_row.addWidget(_icon_btn("trash", "Delete card", self._delete_selected, QColor("#C62828")))
        btn_row.addStretch()
        btn_row.addWidget(_icon_btn("close", "Close details panel", self._hide_detail_panel))
        outer.addLayout(btn_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        self._detail_form = QVBoxLayout(body)
        self._detail_form.setContentsMargins(0, 4, 0, 0)
        self._detail_field_labels: dict[str, QLabel] = {}
        for name in ("Creator", "Created", "Status", "Site", "Company", "Role", "Location",
                     "Salary", "Employment type", "Contact", "Link", "Description"):
            cap = QLabel(name)
            capf = QFont()
            capf.setBold(True)
            cap.setFont(capf)
            self._detail_form.addWidget(cap)
            val = QLabel("")
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            val.setOpenExternalLinks(True)
            self._detail_field_labels[name] = val
            self._detail_form.addWidget(val)
        self._detail_form.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll, stretch=1)
        return panel

    def _hide_detail_panel(self) -> None:
        self._table.clearSelection()
        self._table.setCurrentCell(-1, -1)  # also drop the current cell
        self._detail_panel.setVisible(False)

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            if not self._detail_panel.isHidden():
                self._hide_detail_panel()
                return True
        return super().eventFilter(obj, event)

    # ---------------------------------------------------------------- columns

    def _configure_columns(self) -> None:
        headers = ["", "Sheet", *_BASE_FIELDS] if self._has_sheet_col else ["", *_BASE_FIELDS]
        self._table.setColumnCount(len(headers))
        self._table.setHorizontalHeaderLabels(headers)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(_COL_CHECK, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(_COL_CHECK, 28)
        base = 2 if self._has_sheet_col else 1
        if self._has_sheet_col:
            hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
            self._table.setColumnWidth(1, 120)
        for i, w in enumerate((120, 120, 90, 110, 150, 170, 190)):  # Creator..Link
            hh.setSectionResizeMode(base + i, QHeaderView.ResizeMode.Interactive)
            self._table.setColumnWidth(base + i, w)
        hh.setSectionResizeMode(base + 7, QHeaderView.ResizeMode.Stretch)  # Description

    def _field_base(self) -> int:
        return 2 if self._has_sheet_col else 1

    # ---------------------------------------------------------------- filters

    def _search_needle(self) -> str:
        return normalize_search(self._search_input.text())

    def _card_matches_search(self, card: db.DailyCardRow, needle: str) -> bool:
        return fields_match_search(
            needle,
            card.title,
            _plain_text_from_daily_body(card.body),
            card.status,
            card.created_at,
            str(card.id),
        )

    def _on_search_changed(self) -> None:
        needle = self._search_needle()
        if not needle:
            # Clearing the box leaves the search view.
            if self._search_active:
                self._search_active = False
                self._has_sheet_col = False
            self.refresh_board()
            return
        if not self._lists:
            self._lists = _store().list_daily_lists_ordered()
        if not self._lists:
            return
        preselect = self._search_scope or {lst.id for lst in self._lists}
        dlg = _SheetSelectDialog(self._lists, preselect, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dlg.selected_ids()
        if not chosen:
            QMessageBox.information(self, "Search", "Select at least one sheet to search.")
            return
        self._search_scope = chosen
        self._search_needle_committed = needle
        self._search_active = True
        # Searching pulls every chosen sheet's cards in full - one round trip
        # per sheet on a client - so this is the slowest thing the board does
        # and the one place a person is most likely to think it has hung.
        overlay = busy_overlay.for_window(self)
        overlay.start(f"Searching {len(chosen)} sheet{'s' if len(chosen) != 1 else ''}…")
        self._busy_session = overlay
        try:
            self.refresh_now()      # not refresh_board: that would rebuild later
        finally:
            self._busy_session = None
            overlay.stop()

    def _selected_status_filter(self) -> Optional[str]:
        data = self._status_filter_combo.currentData()
        if not isinstance(data, str) or data == _DAILY_STATUS_FILTER_ALL:
            return None
        return data

    def _on_status_filter_changed(self) -> None:
        self.refresh_board()

    def _selected_creator_filter(self) -> Optional[str]:
        data = self._creator_filter_combo.currentData()
        if not isinstance(data, str) or data == _CREATOR_FILTER_ALL:
            return None
        return data

    def _on_creator_filter_changed(self) -> None:
        self._save_filter_settings()
        self.refresh_board()

    def _sync_creator_options(self, cards: list[db.DailyCardRow]) -> None:
        """Offer every creator present on the day, keeping the current choice.

        A creator the user has selected stays in the list even when the day has
        no rows from them, so switching days doesn't silently reset the filter.
        """
        keys = {_creator_key(c) for c in cards}
        current = self._creator_filter_combo.currentData()
        if isinstance(current, str) and current != _CREATOR_FILTER_ALL:
            keys.add(current)
        # Keys are account ids; the list is sorted and shown by name.
        named = sorted((k for k in keys if k != _CREATOR_UNATTRIBUTED),
                       key=lambda k: author_label(k).lower())
        wanted = [(_CREATOR_FILTER_ALL, "All creators")]
        wanted += [(k, author_label(k) or k) for k in named]
        if _CREATOR_UNATTRIBUTED in keys:
            wanted.append((_CREATOR_UNATTRIBUTED, _CREATOR_UNATTRIBUTED_LABEL))
        combo = self._creator_filter_combo
        have = [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())]
        if have == wanted:
            return
        combo.blockSignals(True)
        try:
            combo.clear()
            for data, text in wanted:
                combo.addItem(text, data)
            idx = combo.findData(current) if isinstance(current, str) else -1
            combo.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            combo.blockSignals(False)

    def current_day_str(self) -> str:
        return self._day.toString("yyyy-MM-dd")

    def _day_str(self) -> str:
        return self.current_day_str()

    def _active_card_date_bounds(self) -> Optional[tuple[str, str]]:
        fmt = "yyyy-MM-dd HH:mm:ss"
        if self._period_filter_check.isChecked():
            lo = self._period_from.dateTime().toString(fmt)
            hi = self._period_to.dateTime().toString(fmt)
            return (lo, hi) if lo <= hi else (hi, lo)
        if self._day_filter_check.isChecked():
            return _day_window_bounds(self._day)
        return None

    @staticmethod
    def _card_in_bounds(card: db.DailyCardRow, bounds: tuple[str, str]) -> bool:
        lo, hi = bounds
        ts = (card.created_at or "").strip()[:19]
        return bool(ts) and lo <= ts <= hi

    def _clamp_day_to_period(self, d: QDate) -> QDate:
        lo, hi = self._period_from.dateTime().date(), self._period_to.dateTime().date()
        if lo > hi:
            lo, hi = hi, lo
        if d < lo:
            return lo
        if d > hi:
            return hi
        return d

    def _set_board_day(self, d: QDate, refresh: bool = True) -> None:
        if self._period_filter_check.isChecked():
            d = self._clamp_day_to_period(d)
        self._day = d
        self._day_edit.blockSignals(True)
        self._day_edit.setDate(d)
        self._day_edit.blockSignals(False)
        if refresh:
            self.refresh_board()

    def _restore_filter_settings(self) -> None:
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        for w in (
            self._period_filter_check, self._day_filter_check,
            self._period_from, self._period_to, self._day_edit,
        ):
            w.blockSignals(True)
        try:
            period_on = settings.value("daily_tab_period_enabled", False, type=bool)
            day_on = settings.value("daily_tab_day_enabled", True, type=bool)
            if period_on and day_on:
                day_on = False
            self._period_filter_check.setChecked(period_on)
            self._day_filter_check.setChecked(day_on)

            fs = settings.value("daily_tab_period_from", "")
            ts = settings.value("daily_tab_period_to", "")
            loaded = False
            if isinstance(fs, str) and isinstance(ts, str) and fs and ts:
                qf = QDateTime.fromString(fs, "yyyy-MM-dd HH:mm:ss")
                qt = QDateTime.fromString(ts, "yyyy-MM-dd HH:mm:ss")
                if qf.isValid() and qt.isValid():
                    self._period_from.setDateTime(qf)
                    self._period_to.setDateTime(qt)
                    loaded = True
            if not loaded:
                df, dt = _default_period_range()
                self._period_from.setDateTime(df)
                self._period_to.setDateTime(dt)

            ds = settings.value("daily_tab_board_day", "")
            if isinstance(ds, str) and ds:
                qd = QDate.fromString(ds, "yyyy-MM-dd")
                if qd.isValid():
                    self._day = qd
            self._day_edit.setDate(self._day)

            status_sel = settings.value("daily_tab_status_filter", _DAILY_STATUS_FILTER_ALL)
            self._status_filter_combo.blockSignals(True)
            try:
                if isinstance(status_sel, str):
                    idx = self._status_filter_combo.findData(status_sel)
                    self._status_filter_combo.setCurrentIndex(idx if idx >= 0 else 0)
            finally:
                self._status_filter_combo.blockSignals(False)

            # The creator list is only known once cards load, so remember the
            # choice here and let _sync_creator_options() re-offer it.
            creator_sel = settings.value("daily_tab_creator_filter", _CREATOR_FILTER_ALL)
            if isinstance(creator_sel, str) and creator_sel != _CREATOR_FILTER_ALL:
                self._creator_filter_combo.blockSignals(True)
                try:
                    label = (_CREATOR_UNATTRIBUTED_LABEL
                             if creator_sel == _CREATOR_UNATTRIBUTED
                             else (author_label(creator_sel) or creator_sel))
                    self._creator_filter_combo.addItem(label, creator_sel)
                    self._creator_filter_combo.setCurrentIndex(
                        self._creator_filter_combo.count() - 1
                    )
                finally:
                    self._creator_filter_combo.blockSignals(False)
        finally:
            for w in (
                self._period_from, self._period_to, self._period_filter_check,
                self._day_filter_check, self._day_edit,
            ):
                w.blockSignals(False)

    def _save_filter_settings(self) -> None:
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        settings.setValue("daily_tab_period_enabled", self._period_filter_check.isChecked())
        settings.setValue("daily_tab_day_enabled", self._day_filter_check.isChecked())
        settings.setValue(
            "daily_tab_period_from",
            self._period_from.dateTime().toString("yyyy-MM-dd HH:mm:ss"),
        )
        settings.setValue(
            "daily_tab_period_to",
            self._period_to.dateTime().toString("yyyy-MM-dd HH:mm:ss"),
        )
        settings.setValue("daily_tab_board_day", self.current_day_str())
        status_data = self._status_filter_combo.currentData()
        settings.setValue(
            "daily_tab_status_filter",
            status_data if isinstance(status_data, str) else _DAILY_STATUS_FILTER_ALL,
        )
        creator_data = self._creator_filter_combo.currentData()
        settings.setValue(
            "daily_tab_creator_filter",
            creator_data if isinstance(creator_data, str) else _CREATOR_FILTER_ALL,
        )

    def _sync_filter_control_states(self) -> None:
        period_on = self._period_filter_check.isChecked()
        day_on = self._day_filter_check.isChecked()
        for w in (self._period_from, self._period_to, self._period_from_label, self._period_to_label):
            w.setEnabled(period_on)
        for w in (self._day_edit, self._day_label):
            w.setEnabled(day_on)

    def _on_period_filter_toggled(self, on: bool) -> None:
        if on:
            self._day_filter_check.blockSignals(True)
            self._day_filter_check.setChecked(False)
            self._day_filter_check.blockSignals(False)
            df, dt = _default_period_range()
            if self._period_from.dateTime() > self._period_to.dateTime():
                self._period_from.setDateTime(df)
                self._period_to.setDateTime(dt)
            self._set_board_day(self._clamp_day_to_period(self._day), refresh=False)
        self._sync_filter_control_states()
        self._save_filter_settings()
        self.refresh_board()

    def _on_day_filter_toggled(self, on: bool) -> None:
        if on:
            self._period_filter_check.blockSignals(True)
            self._period_filter_check.setChecked(False)
            self._period_filter_check.blockSignals(False)
            self._set_board_day(self._day_edit.date(), refresh=False)
        self._sync_filter_control_states()
        self._save_filter_settings()
        self.refresh_board()

    def _on_period_range_changed(self) -> None:
        if self._period_filter_check.isChecked():
            self._set_board_day(self._clamp_day_to_period(self._day), refresh=True)

    def _on_board_day_picker_changed(self, d: QDate) -> None:
        if self._day_filter_check.isChecked():
            self._set_board_day(d, refresh=True)

    def _on_board_today(self) -> None:
        today = _business_today()
        self._day_edit.blockSignals(True)
        self._day_filter_check.blockSignals(True)
        self._period_filter_check.blockSignals(True)
        try:
            self._day_filter_check.setChecked(True)
            self._period_filter_check.setChecked(False)
            self._day_edit.setDate(today)
        finally:
            self._day_edit.blockSignals(False)
            self._day_filter_check.blockSignals(False)
            self._period_filter_check.blockSignals(False)
        self._sync_filter_control_states()
        self._set_board_day(today, refresh=True)

    def _prev_day(self) -> None:
        self._set_board_day(self._day.addDays(-1), refresh=True)

    def _next_day(self) -> None:
        self._set_board_day(self._day.addDays(1), refresh=True)

    # ---------------------------------------------------------------- rebuild

    def refresh_board(self) -> None:
        """Queue a rebuild, coalescing the burst of calls an edit sets off."""
        self._refresh_timer.start()

    def refresh_now(self) -> None:
        """Rebuild immediately, skipping the debounce.

        For work somebody asked for and is waiting on - a search, F5. The
        debounce is right when an edit triggers several refreshes at once, but
        it makes the rebuild happen *after* the caller returns, which means a
        caller cannot show a spinner over it: the spinner would be taken down
        50ms before the work it was covering even started.
        """
        self._refresh_timer.stop()
        self._rebuild()

    def _rebuild(self) -> None:
        self._loading = True
        try:
            status_filter = self._selected_status_filter()
            date_bounds = self._active_card_date_bounds()

            # Every sheet, always - like the tabs along the bottom of a
            # spreadsheet. Sheets used to be filed under the board day they were
            # created on and only appeared on that day, so a sheet named "7.20"
            # made on the 13th was reachable only from the 13th, and there was
            # nothing on screen to say so. The day picker now filters cards by
            # date; it no longer decides which sheets exist.
            self._lists = _store().list_daily_lists_ordered()
            # Summaries carry no body, so the whole-board index is cheap even
            # over the network. Card bodies are fetched per sheet, on demand.
            summaries = _store().list_daily_card_summaries()
            # Offer the creators actually present before reading the selection,
            # so a filter restored from settings can still be found in the list.
            self._sync_creator_options(summaries)
            creator_filter = self._selected_creator_filter()
            # Group summaries by sheet, applying only the status/date/creator
            # filters here (text search is handled by the cross-sheet view).
            by_list: dict[int, list[db.DailyCardRow]] = {}
            filtered_count = 0
            for c in summaries:
                if not self._card_passes_filters(c, status_filter, creator_filter, date_bounds):
                    continue
                by_list.setdefault(c.list_id, []).append(c)
                filtered_count += 1
            self._cards_by_list = by_list

            if self._search_active:
                self._build_search_view()
            else:
                list_ids = [lst.id for lst in self._lists]
                if self._active_list_id not in list_ids:
                    self._active_list_id = list_ids[0] if list_ids else None
                # Only the sheet on screen needs its bodies.
                self._load_active_cards()
                self._has_sheet_col = False
                self._configure_columns()
                self._rebuild_tabbar()
                self._populate_active_sheet()
                filtered = (status_filter is not None or creator_filter is not None
                            or date_bounds is not None)
                self.board_counts_changed.emit(filtered_count if filtered else len(summaries))
        finally:
            self._loading = False

    def _card_passes_filters(self, card, status_filter, creator_filter, date_bounds) -> bool:
        """Whether a card survives the active status / creator / date filters.

        Shared by the summary grouping and the per-sheet full-card fetch so the
        grid and the tab counts can never disagree.
        """
        if status_filter is not None and (card.status or _DAILY_STATUS_NONE) != status_filter:
            return False
        if creator_filter is not None and _creator_key(card) != creator_filter:
            return False
        if date_bounds is not None and not self._card_in_bounds(card, date_bounds):
            return False
        return True

    def _full_cards_for_sheet(self, list_id: Optional[int]) -> list:
        """This sheet's cards, with body, after the active filters.

        The only path that pulls bodies over the wire, and only for one sheet at
        a time - the one being viewed, copied or searched.
        """
        if list_id is None:
            return []
        status_filter = self._selected_status_filter()
        creator_filter = self._selected_creator_filter()
        date_bounds = self._active_card_date_bounds()
        return [
            c for c in _store().list_daily_cards_for_list(list_id)
            if self._card_passes_filters(c, status_filter, creator_filter, date_bounds)
        ]

    def _load_active_cards(self) -> None:
        """Fetch the active sheet's full cards into ``_active_cards`` for the grid."""
        self._active_cards = self._full_cards_for_sheet(self._active_list_id)

    def _load_active_cards_with_spinner(self, message: str) -> None:
        """Same fetch, off the GUI thread, with a spinner if it takes a moment.

        The filters are read here, on the GUI thread, because they live in combo
        boxes and date editors - only the fetch itself, which touches nothing
        but the store, is handed to the worker.
        """
        list_id = self._active_list_id
        status_filter = self._selected_status_filter()
        creator_filter = self._selected_creator_filter()
        date_bounds = self._active_card_date_bounds()

        def fetch() -> list:
            if list_id is None:
                return []
            return [
                c for c in _store().list_daily_cards_for_list(list_id)
                if self._card_passes_filters(
                    c, status_filter, creator_filter, date_bounds
                )
            ]

        overlay = busy_overlay.for_window(self)
        # Delayed: a cached or local sheet lands in a few milliseconds and
        # should show nothing at all.
        overlay.start(message, delay_ms=_SPINNER_DELAY_MS)
        try:
            self._active_cards = busy_overlay.run_off_thread(overlay, message, fetch)
        finally:
            overlay.stop()

    def _report_progress(self, message: str) -> None:
        """Update the spinner's caption, if this run started one.

        Silent otherwise: the same code path runs from the background heartbeat,
        which must not put an overlay over anybody's window.
        """
        overlay = getattr(self, "_busy_session", None)
        if overlay is not None:
            overlay.set_message(message)
            overlay.pump()

    def _build_search_view(self) -> None:
        """Aggregate cards matching the committed needle across the chosen sheets
        into a temporary results view with a leading 'Sheet' column."""
        needle = self._search_needle_committed
        title_by_id = {lst.id: (lst.title or "Untitled") for lst in self._lists}
        entries: list[tuple[db.DailyCardRow, str]] = []
        scoped = [lst for lst in self._lists if lst.id in self._search_scope]
        for done, lst in enumerate(scoped, start=1):
            # One fetch per sheet, so this loop is where the waiting happens -
            # and therefore where the spinner gets to say how far along it is.
            self._report_progress(
                f"Searching {title_by_id.get(lst.id, '')} — sheet {done} of {len(scoped)}…"
            )
            # Search matches body text, so the scoped sheets need full cards -
            # fetched per sheet, only for the sheets the user chose to search.
            for c in self._full_cards_for_sheet(lst.id):
                if needle and not self._card_matches_search(c, needle):
                    continue
                entries.append((c, title_by_id.get(lst.id, "")))
        self._search_entries = entries
        self._has_sheet_col = True
        self._configure_columns()
        self._sheet_title.setText(f"Search results — {len(entries)} rows")
        for w in (self._add_btn, self._paste_btn, self._ai_paste_btn,
                  self._sheet_menu_btn, self._new_sheet_btn):
            w.setEnabled(False)
        # A single temporary results tab.
        self._tabbar.blockSignals(True)
        try:
            while self._tabbar.count():
                self._tabbar.removeTab(0)
            self._tabbar.addTab(f"🔎 Search results ({len(entries)})")
            self._tabbar.setTabData(0, None)
        finally:
            self._tabbar.blockSignals(False)
        self._populate_rows(entries)
        self.board_counts_changed.emit(len(entries))

    def _rebuild_tabbar(self) -> None:
        self._tabbar.blockSignals(True)
        try:
            while self._tabbar.count():
                self._tabbar.removeTab(0)
            active_index = 0
            for i, lst in enumerate(self._lists):
                label = lst.title or "Untitled"
                n = len(self._cards_by_list.get(lst.id, []))
                self._tabbar.addTab(f"{label} ({n})")
                self._tabbar.setTabData(i, lst.id)
                # Sheets are still filed under the day they were made on, and a
                # name like "7.20" often has nothing to do with it, so say which.
                self._tabbar.setTabToolTip(i, f"{label} — created on {lst.day}, {n} card(s)")
                if lst.id == self._active_list_id:
                    active_index = i
            if self._tabbar.count():
                self._tabbar.setCurrentIndex(active_index)
        finally:
            self._tabbar.blockSignals(False)

    def _list_row(self, list_id: Optional[int]) -> Optional[db.DailyListRow]:
        for lst in self._lists:
            if lst.id == list_id:
                return lst
        return None

    def _refresh_sheet_title(self) -> None:
        """'<sheet> - N rows', from the cards held for the active sheet."""
        lst = self._list_row(self._active_list_id)
        n = len(self._active_cards) if lst is not None else 0
        self._sheet_title.setText(f"{lst.title} — {n} rows" if lst else "—")

    def _populate_active_sheet(self) -> None:
        lst = self._list_row(self._active_list_id)
        have_sheet = lst is not None
        # Grid rows come from the full-body fetch; the count matches because both
        # apply the same filters (see _card_passes_filters).
        self._refresh_sheet_title()
        for w in (self._add_btn, self._paste_btn, self._ai_paste_btn, self._sheet_menu_btn):
            w.setEnabled(have_sheet)
        self._new_sheet_btn.setEnabled(True)
        entries = [(c, None) for c in (self._active_cards if have_sheet else [])]
        self._populate_rows(entries)

    def _populate_rows(self, entries: list) -> None:
        self._populating = True
        # Fill in natural (position / search) order; disable header sorting while
        # populating so item inserts aren't reordered mid-fill.
        self._table.setSortingEnabled(False)
        self._checked.clear()
        self._table.clearSelection()
        self._table.setRowCount(0)
        base = self._field_base()
        self._table.setRowCount(len(entries))
        for r, (card, sheet_name) in enumerate(entries):
            created, status, site, company, role, link, desc = _card_field_values(card)
            check = QTableWidgetItem()
            check.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            check.setCheckState(Qt.CheckState.Unchecked)
            check.setData(Qt.ItemDataRole.UserRole, card.id)
            self._table.setItem(r, _COL_CHECK, check)
            if self._has_sheet_col:
                sheet_item = QTableWidgetItem(sheet_name or "")
                sheet_item.setToolTip(sheet_name or "")
                self._table.setItem(r, 1, sheet_item)

            creator = _creator_label(card)
            creator_item = QTableWidgetItem(creator)
            creator_item.setToolTip(creator)
            self._table.setItem(r, base, creator_item)
            created_item = QTableWidgetItem(created)
            created_item.setToolTip(created)
            self._table.setItem(r, base + 1, created_item)
            status_item = QTableWidgetItem(status or "—")
            color = _daily_status_color(status)
            if color:
                status_item.setIcon(_daily_status_swatch(color))
            self._table.setItem(r, base + 2, status_item)
            for i, value in enumerate(
                (site, company, role, link, " ".join(desc.split())), start=3
            ):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self._table.setItem(r, base + i, item)
        self._detail_panel.setVisible(False)
        # Clear the sort indicator FIRST (while sorting is still off) so that
        # re-enabling doesn't re-apply the previous column's sort — each (re)load
        # starts in natural order and the user clicks a header to sort.
        self._table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self._table.setSortingEnabled(True)
        self._populating = False

    def _schedule_refresh(self) -> None:
        if not self._loading:
            self._refresh_timer.start()

    # ---------------------------------------------------------------- table events

    def _row_card_id(self, row: int) -> Optional[int]:
        item = self._table.item(row, _COL_CHECK)
        if item is None:
            return None
        cid = item.data(Qt.ItemDataRole.UserRole)
        return int(cid) if cid is not None else None

    def _selected_card_id(self) -> Optional[int]:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if rows:
            return self._row_card_id(rows[0].row())
        r = self._table.currentRow()
        return self._row_card_id(r) if r >= 0 else None

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._populating or item.column() != _COL_CHECK:
            return
        cid = item.data(Qt.ItemDataRole.UserRole)
        if cid is None:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self._checked.add(int(cid))
        else:
            self._checked.discard(int(cid))

    def _on_row_selection_changed(self) -> None:
        cid = self._selected_card_id()
        if cid is None:
            self._detail_panel.setVisible(False)
            return
        self._show_details(cid)

    def _on_cell_double_clicked(self, row: int, _col: int) -> None:
        cid = self._row_card_id(row)
        if cid is not None:
            self._open_editor(cid)

    def _show_details(self, card_id: int) -> None:
        card = _store().get_daily_card(card_id)
        if card is None:
            self._detail_panel.setVisible(False)
            return
        created, status, site, company, role, link, desc = _card_field_values(card)
        # Extra fields the AI parser now captures (shown when present).
        text = _plain_text_from_daily_body(card.body)
        location = _excel_field_from_text(text, "Location")
        salary = (_excel_field_from_text(text, "Salary")
                  or _excel_field_from_text(text, "Compensation")
                  or _excel_field_from_text(text, "Pay"))
        emp_type = (_excel_field_from_text(text, "Employment type")
                    or _excel_field_from_text(text, "Job type"))
        contact = (_excel_field_from_text(text, "Contact")
                   or _excel_field_from_text(text, "Recruiter"))
        self._detail_title.setText(card.title or "(untitled)")
        self._detail_field_labels["Creator"].setText(_creator_label(card))
        self._detail_field_labels["Created"].setText(created or "—")
        self._detail_field_labels["Status"].setText(status or "—")
        self._detail_field_labels["Site"].setText(site or "—")
        self._detail_field_labels["Company"].setText(company or "—")
        self._detail_field_labels["Role"].setText(role or "—")
        self._detail_field_labels["Location"].setText(location or "—")
        self._detail_field_labels["Salary"].setText(salary or "—")
        self._detail_field_labels["Employment type"].setText(emp_type or "—")
        self._detail_field_labels["Contact"].setText(contact or "—")
        if link:
            safe = link.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            self._detail_field_labels["Link"].setText(f'<a href="{safe}">{safe}</a>')
        else:
            self._detail_field_labels["Link"].setText("—")
        self._detail_field_labels["Description"].setText(desc or "—")
        self._detail_panel.setVisible(True)

    # ---------------------------------------------------------------- context menus

    def _on_row_context_menu(self, pos) -> None:
        row = self._table.rowAt(pos.y())
        if row < 0:
            return
        self._table.selectRow(row)
        cid = self._row_card_id(row)
        if cid is None:
            return
        menu = QMenu(self)
        edit_act = menu.addAction("Edit")
        move_menu = menu.addMenu("Move to sheet")
        targets: dict[object, int] = {}
        card_row = _store().get_daily_card(cid)
        own_list = card_row.list_id if card_row else self._active_list_id
        for lst in self._lists:
            if lst.id != own_list:
                targets[move_menu.addAction(lst.title or "Untitled")] = lst.id
        if not targets:
            na = move_menu.addAction("(no other sheets)")
            na.setEnabled(False)
        export_menu = menu.addMenu("Export")
        exp_md = export_menu.addAction("Markdown (.md)…")
        exp_html = export_menu.addAction("HTML (.html)…")
        menu.addSeparator()
        del_act = menu.addAction("Delete")
        chosen = menu.exec(self._table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == edit_act:
            self._open_editor(cid)
        elif chosen in targets:
            self._move_cards_to_list([cid], targets[chosen])
        elif chosen == exp_md:
            self._export_card(cid, "md")
        elif chosen == exp_html:
            self._export_card(cid, "html")
        elif chosen == del_act:
            self._delete_card(cid)

    def _show_sheet_menu_at_button(self) -> None:
        self._show_sheet_menu(
            self._sheet_menu_btn.mapToGlobal(self._sheet_menu_btn.rect().bottomLeft()),
            self._active_list_id,
        )

    def _on_tab_context_menu(self, pos) -> None:
        idx = self._tabbar.tabAt(pos)
        if idx < 0:
            return
        list_id = self._tabbar.tabData(idx)
        self._show_sheet_menu(self._tabbar.mapToGlobal(pos), int(list_id) if list_id is not None else None)

    def _show_sheet_menu(self, global_pos, list_id: Optional[int]) -> None:
        lst = self._list_row(list_id)
        if lst is None:
            return
        checked_here = [cid for cid in self._checked] if list_id == self._active_list_id else []
        menu = QMenu(self)
        rename_act = menu.addAction("Rename…")
        always_act = menu.addAction("Always shown")
        always_act.setCheckable(True)
        always_act.setChecked(bool(lst.always_shown))
        menu.addSeparator()
        sort_menu = menu.addMenu("Sort cards by")
        current_sort = _store().get_daily_list_sort_mode(list_id)
        sort_acts = {}
        for label, key in (("Manual (drag)", ""), ("Newest first", "newest"),
                           ("Oldest first", "oldest"), ("Status", "status")):
            a = sort_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(current_sort == key)
            sort_acts[a] = key
        menu.addSeparator()
        copy_all_act = menu.addAction(
            f"Copy all cards → Excel ({len(self._cards_by_list.get(list_id, []))})"
        )
        copy_sel_act = menu.addAction(f"Copy selected → Excel ({len(checked_here)})")
        copy_sel_act.setEnabled(bool(checked_here))
        select_all_act = menu.addAction("Select all cards")
        clear_sel_act = menu.addAction("Clear selection")
        clear_sel_act.setEnabled(bool(checked_here))
        move_menu = menu.addMenu(f"Move selected to ({len(checked_here)})")
        move_menu.setEnabled(bool(checked_here))
        move_targets: dict[object, int] = {}
        for other in self._lists:
            if other.id != list_id:
                move_targets[move_menu.addAction(other.title or "Untitled")] = other.id
        stats_act = menu.addAction("Card statistics by site…")
        menu.addSeparator()
        delete_act = menu.addAction("Delete sheet")

        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        if chosen == rename_act:
            self._rename_sheet(list_id)
        elif chosen == always_act:
            self._set_always_shown(list_id, always_act.isChecked())
        elif chosen in sort_acts:
            self._sort_column(list_id, sort_acts[chosen])
        elif chosen == copy_all_act:
            # Excel export needs the bodies; fetch this sheet's full cards now.
            self._copy_to_excel(self._full_cards_for_sheet(list_id))
        elif chosen == copy_sel_act:
            self._copy_to_excel([_store().get_daily_card(c) for c in checked_here])
        elif chosen == select_all_act:
            self._set_all_checked(True)
        elif chosen == clear_sel_act:
            self._set_all_checked(False)
        elif chosen in move_targets:
            self._move_cards_to_list(checked_here, move_targets[chosen])
        elif chosen == stats_act:
            self._show_site_stats(list_id)
        elif chosen == delete_act:
            self._delete_sheet(list_id)

    def _on_tab_changed(self, index: int) -> None:
        if self._loading or index < 0 or self._search_active:
            return
        list_id = self._tabbar.tabData(index)
        new_active = int(list_id) if list_id is not None else None
        if new_active == self._active_list_id:
            # A reorder can shift indices without changing the active sheet —
            # don't repopulate the table needlessly.
            return
        self._active_list_id = new_active
        # The newly shown sheet's bodies are not loaded yet; fetch them (cached
        # per version on a client, so revisiting a sheet is instant).
        label = self._tabbar.tabText(index).split(" (")[0]
        self._load_active_cards_with_spinner(f"Loading {label}…")
        self._populate_active_sheet()

    def _on_tab_moved(self, _from: int, _to: int) -> None:
        """Persist the new sheet order after a drag (Excel-style tab reorder)."""
        if self._loading or self._search_active:
            return
        ids: list[int] = []
        for i in range(self._tabbar.count()):
            d = self._tabbar.tabData(i)
            if d is not None:
                ids.append(int(d))
        if not ids:
            return
        _store().reorder_daily_lists(self._day_str(), ids)
        by_id = {lst.id: lst for lst in self._lists}
        self._lists = [by_id[i] for i in ids if i in by_id]

    # ---------------------------------------------------------------- checkbox helpers

    def _set_all_checked(self, on: bool) -> None:
        self._populating = True
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        for r in range(self._table.rowCount()):
            item = self._table.item(r, _COL_CHECK)
            if item is not None:
                item.setCheckState(state)
                cid = item.data(Qt.ItemDataRole.UserRole)
                if cid is not None:
                    if on:
                        self._checked.add(int(cid))
                    else:
                        self._checked.discard(int(cid))
        self._populating = False

    # ---------------------------------------------------------------- card actions

    def _add_card(self) -> None:
        if self._active_list_id is None:
            return
        cid = _store().add_daily_card(self._active_list_id, "")
        self._apply_current_sort(self._active_list_id)
        self.refresh_board()
        QTimer.singleShot(0, lambda c=cid: self._deferred_open_editor(c))

    def paste_card_from_clipboard(self) -> bool:
        """What F2 does, callable from outside - the global hotkey uses this.

        Returns whether a sheet was there to paste into, so a caller that
        brought the window up for this can say why nothing happened.
        """
        if self._active_list_id is None:
            return False
        self._paste_card()
        return True

    def _paste_card(self) -> None:
        if self._active_list_id is None:
            return
        text = QApplication.clipboard().text()
        if not text.strip():
            QMessageBox.information(self, "Paste card", "The clipboard has no text to paste.")
            return
        cid = _store().add_daily_card(self._active_list_id, "")
        _store().update_daily_card(cid, _first_line_title(text), text)
        self._apply_current_sort(self._active_list_id)
        self.refresh_board()
        QTimer.singleShot(0, lambda c=cid: self._deferred_open_editor(c))

    def _ai_paste_card(self) -> None:
        if self._active_list_id is None:
            return
        text = QApplication.clipboard().text()
        if not text.strip():
            QMessageBox.information(self, "AI paste", "The clipboard has no text to analyze.")
            return
        if not ai_assistant.has_api_key():
            QMessageBox.information(
                self, "AI paste",
                f"Add your {ai_assistant.provider_label()} API key in Settings > "
                "App settings to use AI paste.",
            )
            return
        list_id = self._active_list_id
        cid = _store().add_daily_card(list_id, "")
        _store().update_daily_card(cid, "", text)
        self._apply_current_sort(list_id)
        self.refresh_board()
        QTimer.singleShot(0, lambda c=cid, t=text: self._deferred_open_ai_paste(c, t))

    def _deferred_open_ai_paste(self, card_id: int, text: str) -> None:
        if self._loading:
            return
        self._open_editor(card_id)
        dlg = self._editors.get(card_id)
        if dlg is not None and not sip.isdeleted(dlg):
            dlg.begin_ai_paste(text)

    def _deferred_open_editor(self, card_id: int) -> None:
        if self._loading:
            return
        self._open_editor(card_id)

    def _open_editor(self, card_id: int) -> None:
        if self._loading:
            return
        w = self._editors.get(card_id)
        if w is not None:
            if sip.isdeleted(w):
                self._editors.pop(card_id, None)
            else:
                try:
                    w.reload_from_db()
                except RuntimeError:
                    self._editors.pop(card_id, None)
                else:
                    show_existing_window(w)
                    return
        w = DailyCardEditDialog(card_id, self)
        w.saved.connect(self._on_editor_saved)
        w.deleted.connect(self._on_editor_deleted)
        w.closed_for_id.connect(self._forget_editor)
        # Clicking a similar/duplicate application opens that card's editor.
        w.open_card_requested.connect(self._open_editor)
        w.destroyed.connect(lambda *args, cid=card_id: self._editors.pop(cid, None))
        self._editors[card_id] = w
        w.show()

    def _forget_editor(self, card_id: int) -> None:
        self._editors.pop(card_id, None)

    def _on_editor_saved(self, card_id: int) -> None:
        # A save can change status/search matching; when a filter or the search
        # view is active just rebuild. Otherwise update the row in place.
        if self._search_active or self._selected_status_filter() is not None \
                or self._selected_creator_filter() is not None \
                or self._active_card_date_bounds() is not None:
            self._schedule_refresh()
            return
        if not self._update_row_for_card(card_id):
            self._schedule_refresh()

    def _on_editor_deleted(self, card_id: int) -> None:
        """A card was deleted - take its row out rather than rebuilding the board.

        The rebuild this replaces refetched every summary and every body in the
        sheet, which on a client is the whole job-description corpus over the
        network, and then rebuilt every row. Dropping the one row that went is
        the same result for a fraction of the work. Anything unexpected (a row
        that isn't there, a search view) falls back to the rebuild.
        """
        row = None
        for r in range(self._table.rowCount()):
            if self._row_card_id(r) == card_id:
                row = r
                break
        if row is None:
            self._schedule_refresh()
            return

        self._populating = True
        try:
            self._table.removeRow(row)
        finally:
            self._populating = False
        self._checked.discard(card_id)

        # Keep the cached card lists in step, so a later in-place update or a
        # count does not still believe the card exists.
        self._active_cards = [c for c in self._active_cards if c.id != card_id]
        for list_id, cards in self._cards_by_list.items():
            self._cards_by_list[list_id] = [c for c in cards if c.id != card_id]
        if self._search_active:
            self._search_entries = [
                e for e in self._search_entries if e[0].id != card_id
            ]
            self._sheet_title.setText(
                f"Search results — {len(self._search_entries)} rows"
            )
            self.board_counts_changed.emit(len(self._search_entries))
        else:
            self._refresh_sheet_title()
            self._rebuild_tabbar()
            self.board_counts_changed.emit(
                sum(len(v) for v in self._cards_by_list.values())
            )

    def _update_row_for_card(self, card_id: int) -> bool:
        card = _store().get_daily_card(card_id)
        if card is None:
            return False
        base = self._field_base()
        for r in range(self._table.rowCount()):
            if self._row_card_id(r) == card_id:
                created, status, site, company, role, link, desc = _card_field_values(card)
                creator = _creator_label(card)
                self._populating = True
                self._table.item(r, base).setText(creator)
                self._table.item(r, base).setToolTip(creator)
                self._table.item(r, base + 1).setText(created)
                self._table.item(r, base + 1).setToolTip(created)
                self._table.item(r, base + 2).setText(status or "—")
                color = _daily_status_color(status)
                self._table.item(r, base + 2).setIcon(
                    _daily_status_swatch(color) if color else QIcon()
                )
                for i, value in enumerate(
                    (site, company, role, link, " ".join(desc.split())), start=3
                ):
                    self._table.item(r, base + i).setText(value)
                    self._table.item(r, base + i).setToolTip(value)
                self._populating = False
                if self._selected_card_id() == card_id:
                    self._show_details(card_id)
                return True
        return False

    def _delete_card(self, card_id: int) -> None:
        card = _store().get_daily_card(card_id)
        name = (card.title or "").strip() if card else ""
        prompt = f'Delete "{name}"?' if name else "Delete this card?"
        if QMessageBox.question(
            self, "Delete card", f"{prompt}\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        ed = self._editors.pop(card_id, None)
        if ed is not None and not sip.isdeleted(ed):
            ed.close()
        try:
            _store().delete_daily_card(card_id)
        except PermissionDenied as exc:
            QMessageBox.warning(self, "Delete card", str(exc))
            return
        self.refresh_board()

    def _move_cards_to_list(self, card_ids: list[int], target_list_id: int) -> None:
        rows = [_store().get_daily_card(cid) for cid in card_ids]
        ids = [r.id for r in rows if r is not None and r.list_id != target_list_id]
        if not ids:
            return
        target_name = self._list_title(target_list_id)
        if len(ids) == 1:
            prompt = f'Move this card to "{target_name}"?'
        else:
            prompt = f'Move {len(ids)} selected card(s) to "{target_name}"?'
        if QMessageBox.question(
            self, "Move card(s)", prompt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        refused = 0
        for cid in ids:
            try:
                _store().move_daily_card(cid, target_list_id)
            except PermissionDenied:
                refused += 1
        if refused:
            QMessageBox.warning(
                self, "Move card(s)",
                f"{refused} card(s) were added by somebody else and stayed where "
                f"they were. Only their applier, or the PM, can move them.",
            )
        self._apply_current_sort(target_list_id)
        self.refresh_board()

    def _list_title(self, list_id: int) -> str:
        lst = self._list_row(list_id)
        return (lst.title if lst else "") or "this sheet"

    # --- detail-panel button proxies (operate on the selected row) ---

    def _edit_selected(self) -> None:
        cid = self._selected_card_id()
        if cid is not None:
            self._open_editor(cid)

    def _copy_selected_details(self) -> None:
        """Copy the selected job's full details (all fields) to the clipboard."""
        cid = self._selected_card_id()
        if cid is None:
            return
        card = _store().get_daily_card(cid)
        if card is None:
            return
        created, status, site, company, role, link, desc = _card_field_values(card)
        text = "\n".join(
            [
                card.title or "(untitled)",
                f"Creator: {_creator_label(card)}",
                f"Created: {created}",
                f"Status: {status or '—'}",
                f"Site: {site or '—'}",
                f"Company: {company or '—'}",
                f"Role: {role or '—'}",
                f"Link: {link or '—'}",
                "Description:",
                desc or "—",
            ]
        )
        QApplication.clipboard().setText(text)
        QToolTip.showText(self.cursor().pos(), "Job details copied to the clipboard.", self)

    def _delete_selected(self) -> None:
        cid = self._selected_card_id()
        if cid is not None:
            self._delete_card(cid)

    def _move_selected_via_menu(self) -> None:
        cid = self._selected_card_id()
        if cid is None:
            return
        card_row = _store().get_daily_card(cid)
        own_list = card_row.list_id if card_row else self._active_list_id
        menu = QMenu(self)
        targets: dict[object, int] = {}
        for lst in self._lists:
            if lst.id != own_list:
                targets[menu.addAction(lst.title or "Untitled")] = lst.id
        if not targets:
            na = menu.addAction("(no other sheets)")
            na.setEnabled(False)
        chosen = menu.exec(self.cursor().pos())
        if chosen in targets:
            self._move_cards_to_list([cid], targets[chosen])

    def _export_selected(self, fmt: str) -> None:
        cid = self._selected_card_id()
        if cid is not None:
            self._export_card(cid, fmt)

    def _export_card(self, card_id: int, fmt: str) -> None:
        row = _store().get_daily_card(card_id)
        if row is None:
            QMessageBox.warning(self, "Export card", "This card no longer exists.")
            return
        if fmt == "md":
            content, ext, filt = _card_to_markdown(row), "md", "Markdown (*.md)"
        else:
            content, ext, filt = _card_to_html_document(row), "html", "HTML (*.html)"
        base = _safe_filename((row.title or "").strip() or "card")
        path, _ = QFileDialog.getSaveFileName(
            self, f"Export card as {ext.upper()}", f"{base}.{ext}", filt
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            QMessageBox.critical(self, "Export card", f"Could not write the file:\n{exc}")

    def _copy_to_excel(self, cards: list) -> None:
        rows = [_excel_row_for_card(c.title, c.body) for c in cards if c is not None]
        if not rows:
            return
        QApplication.clipboard().setText("\n".join(rows))
        QMessageBox.information(
            self, "Copy → Excel",
            f"Copied {len(rows)} card(s) to the clipboard in spreadsheet format.",
        )

    def _show_site_stats(self, list_id: int) -> None:
        counts: dict[str, int] = {}
        for c in self._cards_by_list.get(list_id, []):
            site = _job_site_from_title(c.title)
            counts[site] = counts.get(site, 0) + 1
        _StatsDialog("Card statistics by site", counts, self).exec()

    # ---------------------------------------------------------------- sort

    def _sort_column(self, list_id: int, mode: str) -> None:
        if self._loading:
            return
        _store().set_daily_list_sort_mode(
            list_id, mode if mode in ("newest", "oldest", "status") else ""
        )
        self._apply_sort_order(list_id, mode)
        self.refresh_board()

    def _apply_sort_order(self, list_id: int, mode: str) -> None:
        cards = _store().list_daily_cards_for_list(list_id)
        if len(cards) < 2:
            return

        def status_rank(c: db.DailyCardRow) -> int:
            try:
                return _DAILY_STATUS_ORDER.index(c.status)
            except ValueError:
                return -1

        if mode == "newest":
            cards.sort(key=lambda c: (c.created_at, c.id), reverse=True)
        elif mode == "oldest":
            cards.sort(key=lambda c: (c.created_at, c.id))
        elif mode == "status":
            cards.sort(key=lambda c: (status_rank(c), c.created_at, c.id), reverse=True)
        else:
            return
        _store().reorder_daily_cards(list_id, [c.id for c in cards])

    def _apply_current_sort(self, list_id: int) -> None:
        mode = _store().get_daily_list_sort_mode(list_id)
        if mode:
            self._apply_sort_order(list_id, mode)

    # ---------------------------------------------------------------- sheet ops

    def _new_sheet(self) -> None:
        title, ok = QInputDialog.getText(self, "New sheet", "Sheet title:", text="New sheet")
        if not ok:
            return
        new_id = _store().add_daily_list(self._day_str(), title.strip() or "New sheet")
        self._active_list_id = new_id
        self.refresh_board()

    def _rename_sheet(self, list_id: int) -> None:
        lst = self._list_row(list_id)
        current = lst.title if lst else ""
        title, ok = QInputDialog.getText(self, "Rename sheet", "Sheet title:", text=current)
        if not ok:
            return
        try:
            _store().update_daily_list(list_id, title.strip() or "Untitled")
        except PermissionDenied as exc:
            QMessageBox.warning(self, "Rename sheet", str(exc))
            return
        self.refresh_board()

    def _set_always_shown(self, list_id: int, on: bool) -> None:
        _store().set_daily_list_always_shown(list_id, on)
        self.refresh_board()

    def _delete_sheet(self, list_id: int) -> None:
        n = _store().count_daily_cards_for_list(list_id)
        msg = f"Delete this sheet and its {n} card(s)?" if n else "Delete this empty sheet?"
        if QMessageBox.question(
            self, "Delete sheet", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        for c in _store().list_daily_cards_for_list(list_id):
            ed = self._editors.pop(c.id, None)
            if ed is not None and not sip.isdeleted(ed):
                ed.close()
        try:
            _store().delete_daily_list(list_id)
        except PermissionDenied as exc:
            QMessageBox.warning(self, "Delete sheet", str(exc))
            self.refresh_board()      # the editors above were closed; redraw
            return
        if self._active_list_id == list_id:
            self._active_list_id = None
        self.refresh_board()

    def _show_job_bid_stats(self) -> None:
        # Every sheet holds job bids, so count them all - across every sheet,
        # from the first day to the last, ignoring the on-screen filters. The
        # body-less summaries carry the titles the job-site is parsed from, so
        # this stays a single light call even over the network.
        counts: dict[str, int] = {}
        for c in _store().list_daily_card_summaries():
            site = _job_site_from_title(c.title)
            counts[site] = counts.get(site, 0) + 1
        _StatsDialog("Job bids — statistics by job site (all sheets)",
                     counts, self).exec()

    # ---------------------------------------------------------------- lifecycle

    def shutdown(self) -> None:
        self._refresh_timer.stop()
        self._save_filter_settings()
        for w in list(self._editors.values()):
            if not sip.isdeleted(w):
                w.close()
        self._editors.clear()


class _SheetSelectDialog(QDialog):
    """Pick which sheets a cross-sheet search should cover."""

    def __init__(self, lists: list, preselect: set, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Search in sheets")
        self.setMinimumSize(320, 360)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Search these sheets:"))
        self._checks: list[tuple[QCheckBox, int]] = []
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        vb = QVBoxLayout(host)
        for lst in lists:
            cb = QCheckBox(lst.title or "Untitled")
            cb.setChecked(lst.id in preselect)
            vb.addWidget(cb)
            self._checks.append((cb, lst.id))
        vb.addStretch()
        scroll.setWidget(host)
        lay.addWidget(scroll, stretch=1)

        btn_row = QHBoxLayout()
        all_btn = QPushButton("Select all")
        all_btn.clicked.connect(lambda: self._set_all(True))
        none_btn = QPushButton("Clear")
        none_btn.clicked.connect(lambda: self._set_all(False))
        btn_row.addWidget(all_btn)
        btn_row.addWidget(none_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _set_all(self, on: bool) -> None:
        for cb, _ in self._checks:
            cb.setChecked(on)

    def selected_ids(self) -> set:
        return {lid for cb, lid in self._checks if cb.isChecked()}


class _StatsDialog(QDialog):
    """Card-count-by-site statistics as a scrollable table."""

    def __init__(self, title: str, counts: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(360, 440)
        lay = QVBoxLayout(self)
        total = sum(counts.values())
        header = QLabel(f"Total: {total}")
        hf = QFont()
        hf.setBold(True)
        header.setFont(hf)
        lay.addWidget(header)

        self._table = QTableWidget(0, 2)
        table = self._table
        table.setHorizontalHeaderLabels(["Site", "Count"])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Cells are selectable so the content can be dragged over with the mouse
        # and copied (Ctrl+C or the Copy button).
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        rows = sorted(counts.items(), key=lambda kv: (-kv[1], (kv[0] or "").lower()))
        table.setRowCount(len(rows))
        for r, (site, n) in enumerate(rows):
            table.setItem(r, 0, QTableWidgetItem(site or "(no site)"))
            n_item = QTableWidgetItem(str(n))
            n_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(r, 1, n_item)
        lay.addWidget(table, stretch=1)

        copy_sc = QShortcut(QKeySequence.StandardKey.Copy, table)
        copy_sc.activated.connect(self._copy)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        copy_btn = QPushButton("Copy")
        copy_btn.setToolTip("Copy the selected rows (or all rows) to the clipboard")
        copy_btn.clicked.connect(self._copy)
        bb.addButton(copy_btn, QDialogButtonBox.ButtonRole.ActionRole)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)

    def _copy(self) -> None:
        table = self._table
        items = table.selectedItems()
        if items:
            by_row: dict[int, dict[int, str]] = {}
            for it in items:
                by_row.setdefault(it.row(), {})[it.column()] = it.text()
            lines = [
                "\t".join(cols[c] for c in sorted(cols))
                for _r, cols in sorted(by_row.items())
            ]
        else:
            lines = [
                "\t".join(
                    (table.item(r, c).text() if table.item(r, c) else "")
                    for c in range(table.columnCount())
                )
                for r in range(table.rowCount())
            ]
        QApplication.clipboard().setText("\n".join(lines))
