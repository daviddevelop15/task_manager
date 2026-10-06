"""Main application window: tasks tab, menus, backup, status bar."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

from PyQt6.QtCore import (
    QByteArray,
    QDate,
    QDateTime,
    QEvent,
    QObject,
    QSettings,
    QSize,
    Qt,
    QTimer,
    QTime,
)
from PyQt6.QtGui import QAction, QBrush, QColor, QKeySequence, QWheelEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDateTimeEdit,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import ai_assistant
import app_store
import app_variant
import busy_overlay
import daily_board_page
import database as db
import network_ui
import global_hotkey
import secure_store
import security_ui
import tray_icon
import window_utils
from app_resources import app_icon
from clipboard_utils import COPY_ICON_BTN_QSS, apply_copy_button_flash, orange_copy_icon
from constants import (
    COL_DATETIME,
    COL_DESC,
    COL_INDEX,
    COL_LINK,
    COL_STATUS,
    ROLE_TASK_ID,
)
from delegates import DecorationFreeTableWidget, DescriptionDelegate, StatusComboBox
from accounts_page import AccountsPage
from collabor_board import CollaborBoardPage
from daily_sheet_page import DailyBoardPage
from meeting_alerts import MeetingAlertEngine
from meetings_page import MeetingsPage
from sticky_notes import StickyNotesPage

# Tab positions in the main window, in display order. Kept as names so the
# status bar and any future logic never hard-code a shifting index.
_TAB_DAILY = 0
_TAB_MEETINGS = 1
_TAB_COLLABOR = 2
_TAB_NOTES = 3
_TAB_ACC = 4

# How often a client asks the server whether anything changed. Short enough
# that two people editing the same board feel live, long enough that it is a
# negligible amount of traffic and no database writes (see sync_server).
_SYNC_POLL_MS = 4000

# The combination that runs the Daily board's F2 paste from anywhere on the
# machine. Stored per build, like every other setting.
GLOBAL_PASTE_HOTKEY_KEY = "global_paste_hotkey"
# Whether plain F2 is reserved machine-wide as well as the combination above.
GLOBAL_PASTE_PLAIN_F2_KEY = "global_paste_plain_f2"
from task_helpers import (
    normalize_search,
    parse_created_at,
    qdatetime_to_naive_local,
    status_background,
    status_foreground,
    status_label,
    task_matches_search,
)


class AppSettingsDialog(QDialog):
    """Main app settings stored with QSettings."""

    OPENAI_API_KEY_SETTINGS_KEY = "openai_api_key"
    ANTHROPIC_API_KEY_SETTINGS_KEY = "anthropic_api_key"
    LINKEDIN_URL_SETTINGS_KEY = "linkedin_url"
    GITHUB_URL_SETTINGS_KEY = "github_url"
    PORTFOLIO_URL_SETTINGS_KEY = "portfolio_url"
    ADDRESS_SETTINGS_KEY = "address"
    PHONE_SETTINGS_KEY = "phone"
    AI_PROVIDER_SETTINGS_KEY = "ai_provider"
    EXCEL_APPLIER_SETTINGS_KEY = daily_board_page.EXCEL_APPLIER_SETTINGS_KEY

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setMinimumWidth(520)

        settings = QSettings(MainWindow.SETTINGS_ORG, MainWindow.SETTINGS_APP)
        saved_key = settings.value(self.OPENAI_API_KEY_SETTINGS_KEY, "")
        saved_anthropic = settings.value(self.ANTHROPIC_API_KEY_SETTINGS_KEY, "")
        saved_linkedin = settings.value(self.LINKEDIN_URL_SETTINGS_KEY, "")
        saved_github = settings.value(self.GITHUB_URL_SETTINGS_KEY, "")
        saved_portfolio = settings.value(self.PORTFOLIO_URL_SETTINGS_KEY, "")
        saved_address = settings.value(self.ADDRESS_SETTINGS_KEY, "")
        saved_phone = settings.value(self.PHONE_SETTINGS_KEY, "")
        saved_applier = settings.value(self.EXCEL_APPLIER_SETTINGS_KEY, "")
        saved_hotkey = settings.value(
            GLOBAL_PASTE_HOTKEY_KEY, global_hotkey.DEFAULT_SEQUENCE, type=str
        )
        saved_plain_f2 = settings.value(GLOBAL_PASTE_PLAIN_F2_KEY, True, type=bool)
        saved_provider = settings.value(self.AI_PROVIDER_SETTINGS_KEY, "anthropic")
        saved_provider = saved_provider if isinstance(saved_provider, str) else "anthropic"

        # The settings have outgrown a short screen, so they scroll. Only the
        # fields scroll: Save and Cancel stay pinned at the bottom, because a
        # dialog whose only way out is below the fold is a trap.
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        page = QWidget()
        # Named `root` still: everything below adds to it exactly as before.
        root = QVBoxLayout(page)
        scroll.setWidget(page)
        outer.addWidget(scroll, 1)
        self._scroll = scroll        # _fit_to_screen sizes the dialog from it

        provider_row = QHBoxLayout()
        provider_row.addWidget(QLabel("AI provider"))
        self._ai_provider = QComboBox()
        self._ai_provider.addItem("Anthropic (Claude)", "anthropic")
        self._ai_provider.addItem("OpenAI", "openai")
        idx = self._ai_provider.findData(
            "openai" if saved_provider.strip().lower() == "openai" else "anthropic"
        )
        self._ai_provider.setCurrentIndex(idx if idx >= 0 else 0)
        provider_row.addWidget(self._ai_provider, stretch=1)
        root.addLayout(provider_row)
        provider_note = QLabel(
            "Which AI the Daily card AI Assistant uses. Each provider uses its own "
            "API key below."
        )
        provider_note.setWordWrap(True)
        root.addWidget(provider_note)

        # Model selection for the active provider (editable — type any model id).
        self._model_values = {
            ai_assistant.PROVIDER_ANTHROPIC: ai_assistant.selected_model(
                ai_assistant.PROVIDER_ANTHROPIC
            ),
            ai_assistant.PROVIDER_OPENAI: ai_assistant.selected_model(
                ai_assistant.PROVIDER_OPENAI
            ),
        }
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Model"))
        self._ai_model = QComboBox()
        self._ai_model.setEditable(True)
        model_row.addWidget(self._ai_model, stretch=1)
        root.addLayout(model_row)
        self._model_note = QLabel("")
        self._model_note.setWordWrap(True)
        root.addWidget(self._model_note)
        self._current_model_provider = (
            "openai" if saved_provider.strip().lower() == "openai" else "anthropic"
        )
        self._populate_model_combo(self._current_model_provider)
        self._ai_provider.currentIndexChanged.connect(self._on_provider_changed)
        root.addSpacing(8)

        root.addWidget(QLabel("OpenAI API key"))
        self._openai_key = QLineEdit()
        self._openai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._openai_key.setPlaceholderText("sk-...")
        self._openai_key.setText(saved_key if isinstance(saved_key, str) else "")
        root.addWidget(self._openai_key)

        show_key = QCheckBox("Show key")
        show_key.toggled.connect(self._on_show_key_toggled)
        root.addWidget(show_key)

        note = QLabel(
            "Used by Daily > Job bids resume generation. Leave blank to clear the saved key; "
            "the app can still use OPENAI_API_KEY from the environment."
        )
        note.setWordWrap(True)
        root.addWidget(note)

        root.addSpacing(8)
        root.addWidget(QLabel("Anthropic (Claude) API key"))
        self._anthropic_key = QLineEdit()
        self._anthropic_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._anthropic_key.setPlaceholderText("sk-ant-...")
        self._anthropic_key.setText(
            saved_anthropic if isinstance(saved_anthropic, str) else ""
        )
        root.addWidget(self._anthropic_key)
        show_anthropic = QCheckBox("Show key")
        show_anthropic.toggled.connect(self._on_show_anthropic_toggled)
        root.addWidget(show_anthropic)
        anthropic_note = QLabel(
            "Used by Daily card > AI Assistant (match score and application answers). "
            "Leave blank to clear; the app can still use ANTHROPIC_API_KEY from the environment."
        )
        anthropic_note.setWordWrap(True)
        root.addWidget(anthropic_note)

        root.addSpacing(8)
        root.addWidget(QLabel("LinkedIn profile URL"))
        self._linkedin_url = QLineEdit()
        self._linkedin_url.setPlaceholderText("https://www.linkedin.com/in/your-handle")
        self._linkedin_url.setText(saved_linkedin if isinstance(saved_linkedin, str) else "")
        root.addWidget(self._linkedin_url)

        root.addWidget(QLabel("GitHub (Git) profile URL"))
        self._github_url = QLineEdit()
        self._github_url.setPlaceholderText("https://github.com/your-handle")
        self._github_url.setText(saved_github if isinstance(saved_github, str) else "")
        root.addWidget(self._github_url)

        root.addWidget(QLabel("Web portfolio URL"))
        self._portfolio_url = QLineEdit()
        self._portfolio_url.setPlaceholderText("https://your-portfolio.example.com")
        self._portfolio_url.setText(saved_portfolio if isinstance(saved_portfolio, str) else "")
        root.addWidget(self._portfolio_url)

        root.addWidget(QLabel("Address"))
        self._address = QLineEdit()
        self._address.setPlaceholderText("City, State, Country")
        self._address.setText(saved_address if isinstance(saved_address, str) else "")
        root.addWidget(self._address)

        root.addWidget(QLabel("Phone"))
        self._phone = QLineEdit()
        self._phone.setPlaceholderText("+1 555 123 4567")
        self._phone.setText(saved_phone if isinstance(saved_phone, str) else "")
        root.addWidget(self._phone)
        links_note = QLabel(
            "Used by the Daily card AI Assistant 'Copy LinkedIn' / 'Copy Git' / "
            "'Copy Portfolio' / 'Copy Address' / 'Copy Phone' buttons."
        )
        links_note.setWordWrap(True)
        root.addWidget(links_note)

        root.addSpacing(8)
        root.addWidget(QLabel("Applier name (Excel copy)"))
        self._excel_applier = QLineEdit()
        self._excel_applier.setPlaceholderText(daily_board_page.DEFAULT_EXCEL_APPLIER)
        self._excel_applier.setText(
            saved_applier if isinstance(saved_applier, str) else ""
        )
        root.addWidget(self._excel_applier)
        applier_note = QLabel(
            "First column of every 'Copy → Excel' row from the Daily board. "
            f"Leave blank to use the default ({daily_board_page.DEFAULT_EXCEL_APPLIER})."
        )
        applier_note.setWordWrap(True)
        root.addWidget(applier_note)

        root.addSpacing(8)
        root.addWidget(QLabel("Global paste-card hotkey"))
        hotkey_row = QHBoxLayout()
        # A list as well as a capture field: Windows swallows some Win key
        # combinations before an application sees them, so a field you press
        # the keys into cannot be the only way to choose one.
        self._hotkey_presets = QComboBox()
        self._hotkey_presets.addItem("Choose…", "")
        for combo in global_hotkey.SUGGESTED:
            self._hotkey_presets.addItem(global_hotkey.display_name(combo), combo)
        self._hotkey_presets.currentIndexChanged.connect(self._on_hotkey_preset)
        hotkey_row.addWidget(self._hotkey_presets)
        self._global_hotkey = QKeySequenceEdit()
        self._global_hotkey.setKeySequence(QKeySequence(saved_hotkey))
        hotkey_row.addWidget(self._global_hotkey, stretch=1)
        clear_hotkey = QPushButton("Turn off")
        clear_hotkey.setToolTip("Stop reserving a system-wide key")
        clear_hotkey.clicked.connect(self._global_hotkey.clear)
        hotkey_row.addWidget(clear_hotkey)
        root.addLayout(hotkey_row)
        hotkey_note = QLabel(
            "Runs the Daily board's F2 paste-card from anywhere, without switching to "
            f"this window first (default {global_hotkey.display_name(global_hotkey.DEFAULT_SEQUENCE)}"
            ", shown as Meta in the field — Meta is the Windows key). Pick from the list, or "
            "click the field and press the keys. Windows reserves the combination for this app "
            "alone while it runs, so a plain key like F2 would take that key away from every "
            "other program — use one with the Windows key, Ctrl, Alt or Shift. Leave empty to "
            "turn it off."
        )
        hotkey_note.setWordWrap(True)
        root.addWidget(hotkey_note)

        self._plain_f2 = QCheckBox("Also trigger with plain F2, anywhere")
        self._plain_f2.setChecked(saved_plain_f2)
        root.addWidget(self._plain_f2)
        plain_note = QLabel(
            "F2 on its own, with no modifier. Windows gives a reserved key to one "
            "program exclusively, so while Task Manager is running F2 will no longer "
            "reach anything else — Explorer's rename, Excel's edit-cell, editors that "
            "use it. Turn this off to give F2 back; the combination above keeps working "
            "either way."
        )
        plain_note.setWordWrap(True)
        root.addWidget(plain_note)

        root.addStretch()          # keep the fields together when there is room

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self.accept)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(save_btn)
        outer.addLayout(buttons)   # outside the scroll area, always reachable

        self._fit_to_screen()

    def _fit_to_screen(self) -> None:
        """Open at the content's natural size, but never taller than the screen.

        A scroll area reports its own size hint, which says nothing about how
        tall the thing inside it is - size the dialog from that alone and it
        opens short and scrolls on a display with room to spare. So the
        content's height is added back, then clamped: a tall screen shows
        everything, a short one scrolls.
        """
        page = self._scroll.widget()
        hint = self.sizeHint()
        grow = max(0, page.sizeHint().height() - self._scroll.sizeHint().height())
        wanted_h = hint.height() + grow
        # Room for the scrollbar, so its arrival never causes a reflow.
        wanted_w = max(hint.width(), page.sizeHint().width() + 24)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            self.resize(wanted_w, wanted_h)
            return
        area = screen.availableGeometry()
        self.resize(
            min(wanted_w, int(area.width() * 0.9)),
            min(wanted_h, int(area.height() * 0.9)),
        )

    def _populate_model_combo(self, provider: str) -> None:
        self._ai_model.blockSignals(True)
        self._ai_model.clear()
        self._ai_model.addItems(list(ai_assistant.models_for(provider)))
        self._ai_model.setCurrentText(self._model_values.get(provider, ""))
        self._ai_model.blockSignals(False)
        default = ai_assistant.default_model(provider)
        label = "OpenAI" if provider == "openai" else "Anthropic (Claude)"
        self._model_note.setText(
            f"Primary model {label} uses for answers and cover letters "
            f"(default {default}). Type any model id if it's not listed."
        )

    def _on_provider_changed(self, _index: int) -> None:
        # Remember the model typed for the provider we're leaving, then show the
        # newly selected provider's model.
        old = self._current_model_provider
        self._model_values[old] = self._ai_model.currentText().strip()
        new = self._ai_provider.currentData()
        new = "openai" if new == "openai" else "anthropic"
        self._current_model_provider = new
        self._populate_model_combo(new)

    def _on_hotkey_preset(self, index: int) -> None:
        combo = self._hotkey_presets.itemData(index)
        if combo:
            self._global_hotkey.setKeySequence(QKeySequence(combo))

    def _on_show_key_toggled(self, on: bool) -> None:
        self._openai_key.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
        )

    def _on_show_anthropic_toggled(self, on: bool) -> None:
        self._anthropic_key.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
        )

    def accept(self) -> None:  # type: ignore[override]
        settings = QSettings(MainWindow.SETTINGS_ORG, MainWindow.SETTINGS_APP)
        settings.setValue(self.AI_PROVIDER_SETTINGS_KEY, self._ai_provider.currentData())
        # Capture the model shown for the active provider, then persist both.
        self._model_values[self._current_model_provider] = self._ai_model.currentText().strip()
        ai_assistant.set_selected_model(
            ai_assistant.PROVIDER_ANTHROPIC, self._model_values.get("anthropic")
        )
        ai_assistant.set_selected_model(
            ai_assistant.PROVIDER_OPENAI, self._model_values.get("openai")
        )
        key = self._openai_key.text().strip()
        if key:
            settings.setValue(self.OPENAI_API_KEY_SETTINGS_KEY, key)
        else:
            settings.remove(self.OPENAI_API_KEY_SETTINGS_KEY)
        anthropic_key = self._anthropic_key.text().strip()
        if anthropic_key:
            settings.setValue(self.ANTHROPIC_API_KEY_SETTINGS_KEY, anthropic_key)
        else:
            settings.remove(self.ANTHROPIC_API_KEY_SETTINGS_KEY)
        for key, widget in (
            (self.LINKEDIN_URL_SETTINGS_KEY, self._linkedin_url),
            (self.GITHUB_URL_SETTINGS_KEY, self._github_url),
            (self.PORTFOLIO_URL_SETTINGS_KEY, self._portfolio_url),
            (self.ADDRESS_SETTINGS_KEY, self._address),
            (self.PHONE_SETTINGS_KEY, self._phone),
            (self.EXCEL_APPLIER_SETTINGS_KEY, self._excel_applier),
        ):
            value = widget.text().strip()
            if value:
                settings.setValue(key, value)
            else:
                settings.remove(key)
        # Stored even when empty: blank means "no global hotkey", which has to
        # be distinguishable from "never set", or the default would come back.
        settings.setValue(
            GLOBAL_PASTE_HOTKEY_KEY,
            self._global_hotkey.keySequence().toString(),
        )
        settings.setValue(GLOBAL_PASTE_PLAIN_F2_KEY, self._plain_f2.isChecked())
        super().accept()


class MainWindow(QMainWindow):
    SETTINGS_ORG = app_variant.SETTINGS_ORG
    SETTINGS_APP = app_variant.SETTINGS_APP

    def __init__(self) -> None:
        super().__init__()
        # The title names the build, so two windows open side by side during a
        # LAN test are tellable apart at a glance.
        self.setWindowTitle(app_variant.window_title("Task Manager"))
        self.setWindowIcon(app_icon())
        self.setMinimumSize(800, 420)
        sb = self.statusBar()
        sb.setSizeGripEnabled(True)
        sb.show()
        # stretch=1 so the label keeps horizontal space on Windows (stretch 0 can collapse to 0 width).
        self._status_rows_label = QLabel()
        self._status_rows_label.setObjectName("statusRowCount")
        self._status_rows_label.setMinimumHeight(22)
        self._status_rows_label.setSizePolicy(
            QSizePolicy.Policy.MinimumExpanding,
            QSizePolicy.Policy.Minimum,
        )
        sb.addWidget(self._status_rows_label, 1)
        # Connection state sits on the right, permanently: in team mode the
        # difference between "nothing changed" and "not talking to the server"
        # has to be visible without going looking for it.
        self._status_net_label = QLabel()
        self._status_net_label.setObjectName("networkState")
        sb.addPermanentWidget(self._status_net_label, 0)
        sb.setStyleSheet("QStatusBar { min-height: 24px; }")
        self._status_visible_rows = 0
        self._status_all_tasks = 0
        self._status_sticky_count = 0
        self._status_acc_count = 0
        self._status_daily_count = 0

        db.init_db()

        task_tab = QWidget()
        root = QVBoxLayout(task_tab)

        # --- Add link ---
        add_row = QHBoxLayout()
        paste_btn = QPushButton("Paste")
        paste_btn.setToolTip("Paste clipboard into the link field")
        paste_btn.clicked.connect(self._on_paste_link)
        self.link_input = QLineEdit()
        self.link_input.setPlaceholderText("Paste or type a link…")
        add_btn = QPushButton("Add link")
        add_btn.clicked.connect(self._on_add_link)
        self.link_input.returnPressed.connect(self._on_add_link)
        add_row.addWidget(paste_btn)
        add_row.addWidget(self.link_input, stretch=1)
        add_row.addWidget(add_btn)
        root.addLayout(add_row)

        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "Search — matches link, description, status, date/time, or id (partial or whole text)…"
        )
        self.search_input.setClearButtonEnabled(True)
        # Search only on Enter or the Search button — live filtering on every
        # keystroke was a performance problem as data grew. Clearing the box
        # (the X button or deleting the text) restores all rows.
        self.search_input.returnPressed.connect(self.refresh_list)
        self.search_input.textChanged.connect(
            lambda text: self.refresh_list() if not text.strip() else None
        )
        search_row.addWidget(self.search_input)
        self._search_btn = QPushButton("Search")
        self._search_btn.clicked.connect(self.refresh_list)
        search_row.addWidget(self._search_btn)
        root.addLayout(search_row)

        # --- Created date range ---
        date_row = QHBoxLayout()
        self.date_filter_check = QCheckBox("Filter by &created date")
        self.date_filter_check.setToolTip(
            "When enabled, only tasks whose created time falls between From and To are shown. "
            "Turn off Daily window filter to use this."
        )
        self.date_filter_check.toggled.connect(self._on_date_filter_toggled)
        date_row.addWidget(self.date_filter_check)

        self.date_from_label = QLabel("From")
        date_row.addWidget(self.date_from_label)
        self.date_from = QDateTimeEdit()
        self._configure_datetime_editor(self.date_from)
        self.date_from.dateTimeChanged.connect(self._on_date_range_changed)
        date_row.addWidget(self.date_from)

        self.date_to_label = QLabel("To")
        date_row.addWidget(self.date_to_label)
        self.date_to = QDateTimeEdit()
        self._configure_datetime_editor(self.date_to)
        self.date_to.dateTimeChanged.connect(self._on_date_range_changed)
        date_row.addWidget(self.date_to)
        date_row.addStretch()
        root.addLayout(date_row)

        # --- Daily window (prev. day 17:00 → selected day 13:00) ---
        daily_row = QHBoxLayout()
        self.daily_filter_check = QCheckBox("Daily &window filter")
        self.daily_filter_check.setToolTip(
            "When enabled, shows tasks created from the previous day at 17:00 through the selected calendar day at 13:00. "
            "Example: day May 12 → May 11 17:00 through May 12 13:00. "
            "Cannot be used together with the custom created-date range above."
        )
        self.daily_filter_check.toggled.connect(self._on_daily_filter_toggled)
        daily_row.addWidget(self.daily_filter_check)

        self.daily_anchor_label = QLabel("Day")
        daily_row.addWidget(self.daily_anchor_label)
        self.daily_date = QDateEdit()
        self.daily_date.setCalendarPopup(True)
        self.daily_date.setDisplayFormat("yyyy-MM-dd")
        self.daily_date.setMinimumWidth(140)
        self.daily_date.setDate(QDate.currentDate())
        self.daily_date.dateChanged.connect(self._on_daily_anchor_changed)
        daily_row.addWidget(self.daily_date)

        daily_today_btn = QPushButton("Today")
        daily_today_btn.setToolTip("Set the daily window day to today and enable this filter")
        daily_today_btn.clicked.connect(self._on_daily_window_today)
        daily_row.addWidget(daily_today_btn)

        daily_row.addStretch()
        remove_btn = QPushButton("Remove selected row(s)")
        remove_btn.clicked.connect(self._on_remove_selected)
        daily_row.addWidget(remove_btn)
        root.addLayout(daily_row)

        # --- Task table ---
        self.table = DecorationFreeTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["#", "Date / time", "Link", "Status", "Description"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(False)
        self.table.setWordWrap(True)
        self.table.setObjectName("taskTable")
        self.table.setStyleSheet(
            "QTableWidget#taskTable::item:selected { background-color: #5C9CE6; color: #111111; }"
            "QTableWidget#taskTable::item:selected:!active { background-color: #A5D6FA; color: #111111; }"
        )
        hdr = self.table.horizontalHeader()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(COL_INDEX, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(COL_DATETIME, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(COL_LINK, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(COL_DESC, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(COL_INDEX, 48)
        self.table.setColumnWidth(COL_DATETIME, 170)
        self.table.setColumnWidth(COL_LINK, 260)
        self.table.setColumnWidth(COL_STATUS, 130)
        self.table.setColumnWidth(COL_DESC, 280)
        self.table.itemChanged.connect(self._on_table_item_changed)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
        self._desc_delegate = DescriptionDelegate(self.table)
        self.table.setItemDelegateForColumn(COL_DESC, self._desc_delegate)
        root.addWidget(self.table, stretch=1)

        self._daily_page = DailyBoardPage()
        self._sticky_page = StickyNotesPage()
        self._acc_page = AccountsPage()
        self._meetings_page = MeetingsPage()
        self._collabor_page = CollaborBoardPage()
        # The retired Tasks tab is no longer displayed, but its widgets are still
        # referenced by legacy code paths (self.table and friends). Hold a
        # reference so Qt does not destroy them along with the unparented page.
        self._retired_task_tab = task_tab
        # Tab order: Daily, Meetings, Collabor, Notes, Acc. The Tasks tab was
        # retired; its data stays in the DB untouched and is simply not shown.
        # _TAB_* below must match these positions (the status bar switches on them).
        self._main_tabs = QTabWidget()
        self._main_tabs.addTab(self._daily_page, "Daily")
        self._main_tabs.addTab(self._meetings_page, "Meetings")
        self._main_tabs.addTab(self._collabor_page, "Collabor")
        self._main_tabs.addTab(self._sticky_page, "Notes")
        self._main_tabs.addTab(self._acc_page, "Acc")
        self._main_tabs.currentChanged.connect(self._on_main_tab_changed)
        self.setCentralWidget(self._main_tabs)
        # Covers the whole window, so it hides the menu bar and tabs too - while
        # a board is loading there is nothing here worth clicking. Registered on
        # the window so the pages inside reach this same one.
        self._busy = busy_overlay.for_window(self)
        self._daily_page.board_counts_changed.connect(self._on_daily_counts_changed)
        self._sticky_page.list_counts_changed.connect(self._on_sticky_counts_changed)
        self._acc_page.list_counts_changed.connect(self._on_acc_counts_changed)
        self._sticky_page._refresh_table()
        self._acc_page._refresh_table()

        # Background meeting-alarm engine: polls the meetings table and raises
        # alarm popups inside each meeting's lead window. Re-evaluate at once
        # whenever the user changes a meeting.
        self._alert_engine = MeetingAlertEngine(self)
        self._meetings_page.meetings_changed.connect(self._alert_engine.notify_changed)
        self._alert_engine.start()

        self._restore_date_filter_settings()
        self._sync_filter_control_states()

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(350)
        self._save_timer.timeout.connect(self._flush_save_details)

        self._current_id: int | None = None
        self._loading_table = False
        self._pending_desc_row: int | None = None

        self._restore_geometry()
        self._restore_table_header()
        self._build_settings_menu()
        self._build_menu_always_on_top()
        self._build_backup_menu()
        self._build_tray_icon()
        self._build_global_hotkey()
        self.refresh_list()

        # Network mode last: the pages have to exist before a poll can refresh
        # them, and before the server can start serving what they show.
        self._sync_server = None
        self._sync_seen_version = -1
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_SYNC_POLL_MS)
        self._poll_timer.timeout.connect(self._poll_sync)
        self._relogin_open = False
        self._build_refresh_action()
        self._build_identity_corner()
        self._setup_network_mode()

    def _on_paste_link(self) -> None:
        self.link_input.setFocus()
        self.link_input.paste()

    def _configure_datetime_editor(self, w: QDateTimeEdit) -> None:
        w.setCalendarPopup(True)
        w.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        w.setMinimumWidth(200)
        w.setTimeSpec(Qt.TimeSpec.LocalTime)

    def _default_date_range(self) -> tuple[QDateTime, QDateTime]:
        now = QDateTime.currentDateTime()
        d = now.date()
        start = QDateTime(d, QTime(0, 0, 0), Qt.TimeSpec.LocalTime)
        end = QDateTime(d, QTime(23, 59, 59), Qt.TimeSpec.LocalTime)
        return start, end

    def _restore_date_filter_settings(self) -> None:
        settings = QSettings(self.SETTINGS_ORG, self.SETTINGS_APP)
        self.date_filter_check.blockSignals(True)
        self.daily_filter_check.blockSignals(True)
        self.date_from.blockSignals(True)
        self.date_to.blockSignals(True)
        self.daily_date.blockSignals(True)
        try:
            daily_on = settings.value("daily_filter_enabled", False, type=bool)
            custom_on = settings.value("date_filter_enabled", False, type=bool)
            if daily_on and custom_on:
                custom_on = False
            self.daily_filter_check.setChecked(daily_on)
            self.date_filter_check.setChecked(custom_on)

            fs = settings.value("date_from", "")
            ts = settings.value("date_to", "")
            loaded = False
            if isinstance(fs, str) and isinstance(ts, str) and fs and ts:
                qf = QDateTime.fromString(fs, "yyyy-MM-dd HH:mm:ss")
                qt = QDateTime.fromString(ts, "yyyy-MM-dd HH:mm:ss")
                if qf.isValid() and qt.isValid():
                    self.date_from.setDateTime(qf)
                    self.date_to.setDateTime(qt)
                    loaded = True
            if not loaded:
                df, dt = self._default_date_range()
                self.date_from.setDateTime(df)
                self.date_to.setDateTime(dt)

            ds = settings.value("daily_anchor_date", "")
            if isinstance(ds, str) and ds:
                qd = QDate.fromString(ds, "yyyy-MM-dd")
                if qd.isValid():
                    self.daily_date.setDate(qd)
            else:
                self.daily_date.setDate(QDate.currentDate())
        finally:
            self.date_from.blockSignals(False)
            self.date_to.blockSignals(False)
            self.date_filter_check.blockSignals(False)
            self.daily_filter_check.blockSignals(False)
            self.daily_date.blockSignals(False)

    def _sync_filter_control_states(self) -> None:
        custom_on = self.date_filter_check.isChecked()
        daily_on = self.daily_filter_check.isChecked()
        for w in (
            self.date_from,
            self.date_to,
            self.date_from_label,
            self.date_to_label,
        ):
            w.setEnabled(custom_on)
        for w in (self.daily_date, self.daily_anchor_label):
            w.setEnabled(daily_on)

    def _on_date_filter_toggled(self, on: bool) -> None:
        if on:
            self.daily_filter_check.blockSignals(True)
            self.daily_filter_check.setChecked(False)
            self.daily_filter_check.blockSignals(False)
            df, dt = self._default_date_range()
            if self.date_from.dateTime() > self.date_to.dateTime():
                self.date_from.setDateTime(df)
                self.date_to.setDateTime(dt)
        self._sync_filter_control_states()
        self.refresh_list()

    def _on_daily_filter_toggled(self, on: bool) -> None:
        if on:
            self.date_filter_check.blockSignals(True)
            self.date_filter_check.setChecked(False)
            self.date_filter_check.blockSignals(False)
        self._sync_filter_control_states()
        self.refresh_list()

    def _on_daily_anchor_changed(self) -> None:
        if self.daily_filter_check.isChecked():
            self.refresh_list()

    def _daily_window_bounds(self) -> tuple[datetime, datetime]:
        qd = self.daily_date.date()
        anchor = date(qd.year(), qd.month(), qd.day())
        start = datetime.combine(anchor - timedelta(days=1), time(17, 0, 0))
        end = datetime.combine(anchor, time(13, 0, 0))
        return start, end

    def _created_in_daily_window(self, created_at_str: str) -> bool:
        created = parse_created_at(created_at_str)
        if created is None:
            return False
        lo, hi = self._daily_window_bounds()
        return lo <= created <= hi

    def _on_daily_window_today(self) -> None:
        self.daily_date.blockSignals(True)
        self.daily_filter_check.blockSignals(True)
        self.date_filter_check.blockSignals(True)
        try:
            self.daily_date.setDate(QDate.currentDate())
            self.daily_filter_check.setChecked(True)
            self.date_filter_check.setChecked(False)
        finally:
            self.daily_date.blockSignals(False)
            self.daily_filter_check.blockSignals(False)
            self.date_filter_check.blockSignals(False)
        self._sync_filter_control_states()
        self.refresh_list()

    def _on_date_range_changed(self) -> None:
        if self.date_filter_check.isChecked() and not self.daily_filter_check.isChecked():
            self.refresh_list()

    def _task_matches_created_filter(self, created_at_str: str) -> bool:
        if self.daily_filter_check.isChecked():
            return self._created_in_daily_window(created_at_str)
        if not self.date_filter_check.isChecked():
            return True
        created = parse_created_at(created_at_str)
        if created is None:
            return False
        d0 = qdatetime_to_naive_local(self.date_from.dateTime())
        d1 = qdatetime_to_naive_local(self.date_to.dateTime())
        lo, hi = (d0, d1) if d0 <= d1 else (d1, d0)
        return lo <= created <= hi

    def _build_settings_menu(self) -> None:
        settings_menu = self.menuBar().addMenu("&Settings")
        act_settings = QAction("&App settings...", self)
        act_settings.setToolTip("Edit app settings, including the OpenAI API key")
        act_settings.triggered.connect(self._open_app_settings)
        settings_menu.addAction(act_settings)

        act_security = QAction("&Security...", self)
        act_security.setToolTip("Change how the database is unlocked (password / Windows account)")
        act_security.triggered.connect(self._open_security_settings)
        settings_menu.addAction(act_security)

        act_resumes = QAction("&Resumes...", self)
        act_resumes.setToolTip("Manage resumes used by the Daily card AI Assistant")
        act_resumes.triggered.connect(self._open_resumes)
        settings_menu.addAction(act_resumes)

        settings_menu.addSeparator()
        act_network = QAction("&Network / Sharing...", self)
        act_network.setToolTip("Share the daily board and meetings with the team")
        act_network.triggered.connect(self._open_network_settings)
        settings_menu.addAction(act_network)

        self._act_users = QAction("&Users...", self)
        self._act_users.setToolTip("Who may connect to this server, and from where")
        self._act_users.triggered.connect(self._open_users)
        settings_menu.addAction(self._act_users)
        # Only the machine that owns the data administers accounts.
        self._act_users.setVisible(app_store.network_mode() == app_store.MODE_SERVER)

    def _open_network_settings(self) -> None:
        network_ui.NetworkSettingsDialog(self, window=self).exec()
        self._act_users.setVisible(app_store.network_mode() == app_store.MODE_SERVER)
        self._refresh_network_status()

    def _open_users(self) -> None:
        from user_admin_ui import UsersDialog

        UsersDialog(self, server=self._sync_server).exec()
        # Creating the PM account is what makes this machine somebody, so pick
        # the identity up straight away rather than at the next restart.
        self._adopt_server_identity()
        self._refresh_network_status()

    def _open_app_settings(self) -> None:
        dlg = AppSettingsDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.statusBar().showMessage("Settings saved.", 5000)
            # Re-reserve straight away: a hotkey that only takes effect after a
            # restart looks like one that does not work.
            self._apply_global_hotkey()

    def _open_security_settings(self) -> None:
        security_ui.SecuritySettingsDialog(self).exec()

    def _open_resumes(self) -> None:
        from resumes_ui import open_resume_manager

        open_resume_manager()

    # ---------------------------------------------------------------- network

    def _build_identity_corner(self) -> None:
        """Show who is signed in, in the menu bar's right-hand corner.

        With three builds open at once and every shared row now attributed, the
        first question is "who am I here?" - and the answer decides what can be
        edited, so it belongs on screen rather than buried in a settings dialog.
        """
        self._identity_label = QLabel()
        self._identity_label.setObjectName("identityCorner")
        self._identity_label.setContentsMargins(8, 0, 10, 0)
        self.menuBar().setCornerWidget(
            self._identity_label, Qt.Corner.TopRightCorner
        )
        self._refresh_identity_corner()

    def _current_identity(self) -> tuple[str, str]:
        """``(name, role)`` for whoever this window is acting as."""
        store = app_store.get_store()
        user = getattr(store, "user", None)
        if isinstance(user, dict) and user:          # a logged-in client
            role = "PM" if user.get("role") == "pm" else "Developer"
            return str(user.get("label") or user.get("username") or ""), role
        if app_store.network_mode() == app_store.MODE_SERVER:
            try:
                import sync_users

                pms = [u for u in sync_users.list_users() if u.is_pm]
                if len(pms) == 1:
                    return pms[0].label, "PM"
            except (sqlite3.Error, ValueError):
                pass
            return "", ""
        return app_store.display_name(), ""          # single-user app

    def _refresh_identity_corner(self) -> None:
        name, role = self._current_identity()
        self._apply_window_title(name, role)
        label = getattr(self, "_identity_label", None)
        if label is None:
            return
        if not name:
            label.setText("")
            label.setToolTip("")
            return
        label.setText(f"{name} · {role}" if role else name)
        label.setToolTip(
            f"Signed in as {name}"
            + (f" ({role})" if role else "")
            + ".\nRows you create are recorded under this account."
        )

    def _apply_window_title(self, name: str, role: str) -> None:
        """Put the build and the signed-in account in the title bar.

        The title is what the taskbar and Alt-Tab show, so with two or three
        builds open it needs to answer both "which app is this?" and "who am I
        in it?" without the window being focused.
        """
        parts = ["Task Manager"]
        if not name:
            # Nobody signed in yet: fall back to the build's own name, which for
            # the server still spells out that it is the PM's copy.
            self.setWindowTitle(app_variant.window_title("Task Manager"))
            return
        build = app_variant.short_label()
        if build:
            parts.append(build)
        parts.append(f"{name} ({role})" if role else name)
        self.setWindowTitle(" — ".join(parts))

    def _build_refresh_action(self) -> None:
        """F5, a View menu entry and a status-bar button, all reloading."""
        self._act_refresh = QAction("&Refresh", self)
        self._act_refresh.setShortcut(QKeySequence(Qt.Key.Key_F5))
        # ApplicationShortcut so it works whatever has focus, including a card
        # editor opened from the board.
        self._act_refresh.setShortcutContext(
            Qt.ShortcutContext.ApplicationShortcut
        )
        self._act_refresh.setToolTip("Reload every board from the database (F5)")
        self._act_refresh.triggered.connect(self.refresh_all)
        self.addAction(self._act_refresh)
        menu = getattr(self, "_view_menu", None)
        if menu is not None:
            menu.addSeparator()
            menu.addAction(self._act_refresh)

        btn = QPushButton("⟳ Refresh")
        btn.setFlat(True)
        btn.setToolTip(self._act_refresh.toolTip())
        # Through the action, not straight to refresh_all: one path for the key,
        # the menu and the button, so they cannot drift apart.
        btn.clicked.connect(self._act_refresh.trigger)
        # Beside the connection state: both answer "is what I am looking at
        # current?", so they belong together.
        self.statusBar().addPermanentWidget(btn, 0)
        self._refresh_btn = btn

    def _adopt_server_identity(self) -> None:
        """Tell the local store which account is sitting at this machine.

        Without this the server's own window acts as nobody: its private queries
        run unscoped (so the PM sees developers' Acc and Notes) and the rows it
        creates are stamped with no author.
        """
        try:
            import sync_users

            # Rows written by an earlier release hold names; point them at the
            # accounts they belong to so a later rename cannot orphan them.
            sync_users.migrate_authorship_to_ids()
            pms = [u for u in sync_users.list_users() if u.is_pm]
        except (sqlite3.Error, ValueError):
            return
        if len(pms) != 1:
            # No account yet, or more than one PM and no way to tell which is
            # here. Leaving it unset keeps the pre-setup app working as before.
            return
        app_store.local_store().set_identity(
            sync_users.principal(pms[0]), is_pm=True
        )

    def _setup_network_mode(self) -> None:
        """Start whatever the configured mode needs, and say so in the status bar."""
        mode = app_store.network_mode()
        if mode == app_store.MODE_SERVER:
            self._adopt_server_identity()
        if mode == app_store.MODE_SERVER and network_ui.server_autostart():
            try:
                self.start_sync_server()
            except OSError as exc:
                QMessageBox.warning(
                    self, "Sharing",
                    f"Could not start sharing on port {network_ui.server_port()}.\n\n"
                    f"{exc}\n\nThe app works normally; the team cannot connect "
                    f"until this is fixed in Settings > Network / Sharing.",
                )
        if mode == app_store.MODE_CLIENT and self._is_remote_store():
            self._poll_timer.start()
        elif mode == app_store.MODE_SERVER:
            # Same heartbeat, different job: watch the store's change counter so
            # a client's edit shows up here without anyone pressing F5.
            self._sync_seen_version = self._shared_change_version()
            self._poll_timer.start()
        self._refresh_network_status()

    def _is_remote_store(self) -> bool:
        return hasattr(app_store.get_store(), "poll_changes")

    def start_sync_server(self) -> None:
        """Serve the shared boards. Raises OSError if the port is unavailable."""
        import sync_server

        if self._sync_server is not None and self._sync_server.is_running:
            return
        srv = sync_server.SyncServer(
            # Always this machine's own database - never whatever store the UI
            # happens to be using, which on a client is a remote one.
            store=app_store.local_store(),
            host=network_ui.server_host(),
            port=network_ui.server_port(),
        )
        srv.start()
        self._sync_server = srv
        self._refresh_network_status()

    def stop_sync_server(self) -> None:
        if self._sync_server is not None:
            self._sync_server.stop()
            self._sync_server = None
        self._refresh_network_status()

    def client_login(self) -> bool:
        """Log in (or log in again) and install the remote store."""
        import client_login_ui
        import sync_client

        result = client_login_ui.prompt_login(self)
        if result is None:
            return False
        store = sync_client.RemoteStore(result)
        # Pull the id -> name map before the first render, so the Creator column
        # shows names straight away rather than filling in a moment later. It is
        # a network round trip, so it runs off the GUI thread with the spinner
        # up - on a slow link this is several seconds of otherwise dead window.
        busy_overlay.run_off_thread(
            self._busy, "Connecting to the server…", store.refresh_directory
        )
        app_store.set_store(store)
        self._poll_timer.start()
        self._refresh_all_shared_views()
        self._refresh_network_status()
        return True

    def refresh_all(self, quiet: bool = False) -> None:
        """Reload every board from the database.

        The server's own window is not told when a client writes - the change
        happens on an HTTP thread, under the same store, with nothing to push it
        to the UI. F5 and the status-bar button both land here, and so does the
        server's own change check.

        `quiet` is for that last one. A refresh somebody asked for should say it
        is working; one the heartbeat starts by itself should not throw a
        full-window overlay over the PM every few seconds because a developer
        edited a card.
        """
        # Every step builds widgets, so none of it can leave the GUI thread; the
        # spinner is brought up to date between steps instead. On a client each
        # board is a network round trip, which is where the window used to
        # appear to hang.
        steps = (
            # refresh_now, not refresh_board: the debounced one returns before
            # doing anything, so the caption would come down before the work.
            ("Loading the daily board…", self._daily_page.refresh_now),
            ("Loading meetings…", self._meetings_page.refresh),
            ("Loading notes…", self._sticky_page._refresh_table),
            ("Loading accounts…", self._acc_page._refresh_table),
            ("Loading tasks…", self.refresh_list),
        )
        if quiet:
            for _message, step in steps:
                step()
        else:
            busy_overlay.run_steps(self._busy, steps)
        self._alert_engine.notify_changed()
        self._sync_seen_version = self._shared_change_version()
        self._refresh_status_bar_for_current_tab()
        self._refresh_network_status()
        self.statusBar().showMessage("Refreshed.", 2000)

    def _shared_change_version(self) -> int:
        try:
            return int(app_store.get_store().change_version())
        except Exception:              # noqa: BLE001 - a count must never break a poll
            return -1

    def _poll_sync(self) -> None:
        """Heartbeat: pick up other people's changes, notice outages."""
        if self._busy.active:
            # A busy section runs a local event loop, which this timer would
            # otherwise fire inside - a second request on the same store while
            # the first is still in flight on a worker thread.
            return
        store = app_store.get_store()
        if not hasattr(store, "poll_changes"):
            # Server mode: no polling to do, but clients write through this same
            # store on HTTP threads, so watch its change counter and redraw when
            # it moves. Skipped while a dialog is open, so a board never rebuilds
            # underneath someone who is in the middle of editing.
            if app_store.network_mode() == app_store.MODE_SERVER:
                version = self._shared_change_version()
                if version != self._sync_seen_version:
                    if QApplication.activeModalWidget() is None:
                        self.refresh_all(quiet=True)   # nobody asked for this one
                    return
                self._refresh_network_status()
                return
            self._poll_timer.stop()
            return
        try:
            changed = store.poll_changes()
        except Exception as exc:                        # noqa: BLE001
            # An expired or rejected session is the one case worth interrupting
            # for - everything else is a transient outage the store handles.
            import sync_client

            if isinstance(exc, sync_client.AuthError):
                self._prompt_relogin(exc)
            self._refresh_network_status()
            return
        if changed:
            self._refresh_all_shared_views(quiet=True)   # the heartbeat, not the person
        self._refresh_network_status()

    def _prompt_relogin(self, exc) -> None:
        if self._relogin_open:
            return                    # the dialog is already up; do not stack them
        self._relogin_open = True
        self._poll_timer.stop()
        try:
            QMessageBox.information(
                self, "Session ended",
                f"{exc}\n\nLog in again to carry on.",
            )
            self.client_login()
        finally:
            self._relogin_open = False

    def _refresh_all_shared_views(self, quiet: bool = False) -> None:
        # Both are network round trips on a client. Quiet for the heartbeat,
        # which redraws every few seconds on its own account; visible when it
        # follows something the person did, like logging in.
        steps = (
            # refresh_now, not refresh_board: the debounced one returns before
            # doing anything, so the caption would come down before the work.
            ("Loading the daily board…", self._daily_page.refresh_now),
            ("Loading meetings…", self._meetings_page.refresh),
        )
        if quiet:
            for _message, step in steps:
                step()
        else:
            busy_overlay.run_steps(self._busy, steps)
        self._refresh_status_bar_for_current_tab()

    def _refresh_network_status(self) -> None:
        mode = app_store.network_mode()
        store = app_store.get_store()
        if mode == app_store.MODE_SERVER:
            srv = self._sync_server
            if srv is not None and srv.is_running:
                n = len({s.username for s in srv.service.sessions.active()})
                text = f"Sharing · {n} connected"
                tip = "\n".join(["Clients can connect to:"] + srv.client_urls())
            else:
                text = "Not sharing"
                tip = "Start sharing in Settings > Network / Sharing."
        elif mode == app_store.MODE_CLIENT:
            if not hasattr(store, "online"):
                text = "Not logged in"
                tip = "Settings > Network / Sharing > Log in again."
            elif store.online:
                text = f"Connected · {store.author}"
                tip = "Working against the team server."
            else:
                pending = store.pending_writes
                text = "Not connected" + (f" · {pending} to send" if pending else "")
                tip = ("The server cannot be reached. What you see is the last "
                       "data received; edits will be sent when it returns.")
        else:
            text = ""
            tip = ""
        self._status_net_label.setText(text)
        self._status_net_label.setToolTip(tip)
        # Logging in or creating the PM account changes who this window is.
        self._refresh_identity_corner()

    def _build_menu_always_on_top(self) -> None:
        settings = QSettings(self.SETTINGS_ORG, self.SETTINGS_APP)
        on_top = settings.value("always_on_top", False, type=bool)

        view_menu = self.menuBar().addMenu("&View")
        self._view_menu = view_menu       # Refresh is added here once it exists
        self._act_on_top = QAction("Always on &top", self)
        self._act_on_top.setCheckable(True)
        self._act_on_top.setChecked(on_top)
        self._act_on_top.triggered.connect(self._on_always_on_top)
        view_menu.addAction(self._act_on_top)
        self._apply_always_on_top(on_top)

    def _build_tray_icon(self) -> None:
        """Tray icon, plus the View entry that does the same thing by keyboard.

        With the icon in place the app no longer quits when its last window
        closes: hiding to the tray would otherwise be indistinguishable from
        exiting, and closing an editor window while the main window was hidden
        would end the process without saving. closeEvent quits explicitly.
        """
        self._tray = None
        if not tray_icon.TrayIcon.is_available():
            return
        self._tray = tray_icon.TrayIcon(self)
        self._tray.show()
        app = QApplication.instance()
        if app is not None:
            app.setQuitOnLastWindowClosed(False)

        act_here = QAction("Move to &next monitor", self)
        act_here.setToolTip("Send the window to the other screen")
        act_here.setShortcut(QKeySequence("Ctrl+Shift+M"))
        act_here.triggered.connect(self._tray.fetch_to_my_screen)
        self._view_menu.addAction(act_here)

    def _build_global_hotkey(self) -> None:
        """Make the Daily board's F2 paste work from anywhere on the machine.

        F2 itself stays as it was, a shortcut inside the window. This is the
        same action on a combination Windows reserves system-wide, because the
        text being pasted was copied from another program - having to find this
        window first is the part worth removing.
        """
        self._hotkey = global_hotkey.GlobalHotkey(self)
        self._hotkey.activated.connect(self._on_global_paste)
        # Plain F2 is a second, separate reservation rather than a replacement:
        # the combination keeps working even when F2 cannot be had, and losing
        # F2 machine-wide is a decision that deserves its own switch.
        self._hotkey_plain = global_hotkey.GlobalHotkey(self)
        self._hotkey_plain.activated.connect(self._on_global_paste)
        self._apply_global_hotkey()

    def _apply_global_hotkey(self) -> None:
        settings = QSettings(self.SETTINGS_ORG, self.SETTINGS_APP)
        seq = settings.value(
            GLOBAL_PASTE_HOTKEY_KEY, global_hotkey.DEFAULT_SEQUENCE, type=str
        )
        plain = settings.value(GLOBAL_PASTE_PLAIN_F2_KEY, True, type=bool)
        problems = [
            self._hotkey.apply(seq or ""),
            self._hotkey_plain.apply(global_hotkey.PLAIN_KEY if plain else ""),
        ]
        problems = [p for p in problems if p]
        for problem in problems:
            # Never silent: a hotkey that did not register is a feature that
            # does not exist, and the person has no other way to find that out.
            db.log_diagnostic(f"global hotkey: {problem}")
        if problems:
            self.statusBar().showMessage("  ".join(problems), 15000)
        live = [
            hk.sequence for hk in (self._hotkey, self._hotkey_plain) if hk.sequence
        ]
        if live:
            db.log_diagnostic(f"global hotkey registered: {', '.join(live)}")

    def _on_global_paste(self) -> None:
        # Where the person is, captured before we take focus - after that the
        # foreground window is ours and the answer is useless.
        where = window_utils.screen_of_foreground_window()
        window_utils.bring_to_screen(self, where)
        self._main_tabs.setCurrentIndex(_TAB_DAILY)
        if not self._daily_page.paste_card_from_clipboard():
            self.statusBar().showMessage(
                "Open a Daily sheet first - there is nowhere to paste the card.", 8000
            )

    def _build_backup_menu(self) -> None:
        backup_menu = self.menuBar().addMenu("Data &backup")
        act_import = QAction("&Import from…", self)
        act_import.setToolTip("Load tasks, sticky notes, and accounts from a JSON backup file")
        act_import.triggered.connect(self._backup_import)
        backup_menu.addAction(act_import)
        act_export = QAction("&Export to…", self)
        act_export.setToolTip("Save all tasks, sticky notes, and accounts to a JSON backup file")
        act_export.triggered.connect(self._backup_export)
        backup_menu.addAction(act_export)

        backup_menu.addSeparator()
        act_init = QAction("&Initialize database (delete all)…", self)
        act_init.setToolTip(
            "Permanently delete ALL data (tasks, daily board, sticky notes, accounts)"
        )
        act_init.triggered.connect(self._initialize_database)
        backup_menu.addAction(act_init)

    def _initialize_database(self) -> None:
        if (
            QMessageBox.warning(
                self,
                "Initialize database",
                "This permanently DELETES ALL DATA — tasks, daily board, sticky "
                "notes, and accounts. This cannot be undone.\n\n"
                "Consider exporting a backup first.\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        if not security_ui.confirm_vault_password(self):
            return
        if (
            QMessageBox.question(
                self,
                "Final confirmation",
                "Last chance — delete everything now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return

        self._save_timer.stop()
        self._flush_save_details()
        self._acc_page.shutdown()
        self._sticky_page.shutdown()
        self._daily_page.shutdown()
        try:
            db.initialize_database()
        except (sqlite3.Error, OSError) as e:
            QMessageBox.critical(self, "Initialize failed", str(e))
            return

        self._current_id = None
        self.refresh_list()
        self._daily_page.refresh_board()
        self._sticky_page._refresh_table()
        self._acc_page._refresh_table()
        self._meetings_page.refresh()
        self._alert_engine.notify_changed()
        QMessageBox.information(
            self, "Database initialized", "All data has been removed."
        )

    def _backup_export(self) -> None:
        choice = QMessageBox.question(
            self,
            "Encrypt backup?",
            "Protect this backup file with a password?\n\n"
            "• Yes — the file is encrypted (.tmbk) and needs the password to import.\n"
            "• No — plain readable JSON (.json).",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if choice == QMessageBox.StandardButton.Cancel:
            return
        encrypt = choice == QMessageBox.StandardButton.Yes
        ext = ".tmbk" if encrypt else ".json"
        date_str = datetime.now().strftime("%Y-%m-%d")
        default_name = f"task_manager_backup_{date_str}{ext}"
        file_filter = (
            "Encrypted backup (*.tmbk);;All files (*.*)"
            if encrypt
            else "JSON backup (*.json);;All files (*.*)"
        )
        path, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export backup", str(Path.home() / default_name), file_filter
        )
        if not path:
            return
        if not path.lower().endswith(ext):
            path += ext

        passphrase = None
        if encrypt:
            passphrase = security_ui.ask_passphrase(
                self, "Backup password", "Choose a password for this backup:"
            )
            if passphrase is None:
                return

        try:
            doc = db.build_backup_document()
            raw = (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            out = secure_store.encrypt_backup(raw, passphrase) if encrypt else raw
            with open(path, "wb") as f:
                f.write(out)
        except OSError as e:
            QMessageBox.critical(self, "Export failed", str(e))
            return
        how = "encrypted backup" if encrypt else "backup"
        self.statusBar().showMessage(f"Exported {how} to {path}", 8000)

    def _backup_import(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Import backup",
            "",
            "Task Manager backup (*.tmbk *.json);;All files (*.*)",
        )
        if not path:
            return
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as e:
            QMessageBox.critical(self, "Import failed", f"Could not read the file:\n{e}")
            return

        if secure_store.is_encrypted_backup(raw):
            passphrase = security_ui.ask_passphrase_once(
                self, "Backup password", "Enter the password for this backup:"
            )
            if passphrase is None:
                return
            try:
                raw = secure_store.decrypt_backup(raw, passphrase)
            except secure_store.VaultError:
                QMessageBox.warning(
                    self, "Wrong password",
                    "Could not decrypt the backup (wrong password or corrupted file).",
                )
                return

        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as e:
            QMessageBox.critical(self, "Import failed", f"Invalid backup contents:\n{e}")
            return
        try:
            (tasks, stickies, accounts, daily_lists, daily_cards,
             meetings) = db.parse_backup_payload(data)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid backup", str(e))
            return

        box = QMessageBox(self)
        box.setWindowTitle("Import backup")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            "How should this backup be applied?\n\n"
            "• Merge — add tasks whose links are not already stored; append stickies, accounts, and daily board data.\n\n"
            "• Replace all — remove all stored data, then restore from the file (preserves saved ids)."
        )
        merge_btn = box.addButton("Merge", QMessageBox.ButtonRole.AcceptRole)
        replace_btn = box.addButton("Replace all", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked == merge_btn:
            mode = "merge"
        elif clicked == replace_btn:
            mode = "replace"
        else:
            return

        self._save_timer.stop()
        self._flush_save_details()

        try:
            if mode == "merge":
                ta, ts, sa, aa, dl, dc, dcs, ma = db.import_backup_merge(
                    tasks, stickies, accounts, daily_lists, daily_cards, meetings
                )
                msg = (
                    f"Tasks added: {ta}\n"
                    f"Tasks skipped (duplicate link or invalid row): {ts}\n"
                    f"Sticky notes added: {sa}\n"
                    f"Accounts added: {aa}\n"
                    f"Daily sheets added: {dl}\n"
                    f"Daily cards added: {dc}\n"
                    f"Meetings added: {ma}"
                )
                if dcs:
                    # Never let cards go missing quietly - the count is the only
                    # way to notice a file whose sheets and cards disagree.
                    msg += f"\nDaily cards skipped (their sheet is not in the file): {dcs}"
                if len(daily_lists) > dl:
                    msg += (
                        f"\n\n{len(daily_lists) - dl} sheet(s) in the file were "
                        f"already here; their cards were added to the existing ones."
                    )
            else:
                confirm = QMessageBox.question(
                    self,
                    "Confirm replace",
                    "This will permanently delete all tasks, daily board data, sticky notes, and accounts, "
                    "then load the backup. This cannot be undone.\n\nContinue?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if confirm != QMessageBox.StandardButton.Yes:
                    return
                self._acc_page.shutdown()
                self._sticky_page.shutdown()
                self._daily_page.shutdown()
                self._meetings_page.shutdown()
                tc, sc, ac, dl, dc, mc = db.import_backup_replace(
                    tasks, stickies, accounts, daily_lists, daily_cards, meetings
                )
                msg = (
                    f"Tasks loaded: {tc}\n"
                    f"Sticky notes loaded: {sc}\n"
                    f"Accounts loaded: {ac}\n"
                    f"Daily sheets loaded: {dl}\n"
                    f"Daily cards loaded: {dc}\n"
                    f"Meetings loaded: {mc}"
                )
        except (sqlite3.Error, OSError, ValueError, RuntimeError) as e:
            QMessageBox.critical(self, "Import failed", str(e))
            return

        attributed = self._attribute_imported_rows()
        if attributed:
            msg += f"\n\n{attributed}"
        QMessageBox.information(self, "Import complete", msg)
        self._current_id = None
        self.refresh_list()
        self._daily_page.refresh_board()
        self._sticky_page._refresh_table()
        self._acc_page._refresh_table()
        self._meetings_page.refresh()
        self._alert_engine.notify_changed()

    def _attribute_imported_rows(self) -> str:
        """Put a name on imported rows that arrived without one.

        A backup from the single-user app carries no authorship, so on a team
        server every imported row would show an empty Creator column. Those rows
        are the PM's - they are importing their own data - so blanks are filled
        with the PM's name. Rows that came with an author keep it, and the fill
        is idempotent.
        """
        if app_store.network_mode() != app_store.MODE_SERVER:
            return ""
        try:
            import sync_users

            pms = [u for u in sync_users.list_users() if u.is_pm]
            if not pms:
                # No PM account yet; creating one runs this same backfill.
                return ""
            counts = db.backfill_authorship(pms[0].label)
        except (sqlite3.Error, ValueError):
            return ""
        filled = counts.get("daily_cards.created_by", 0)
        if not filled:
            return ""
        return f"{filled} imported row(s) had no creator and are now shown as {pms[0].label}."

    def _on_always_on_top(self, checked: bool) -> None:
        settings = QSettings(self.SETTINGS_ORG, self.SETTINGS_APP)
        settings.setValue("always_on_top", checked)
        self._apply_always_on_top(checked)

    def _apply_always_on_top(self, on: bool) -> None:
        if on:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        else:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
        self.show()

    def _restore_geometry(self) -> None:
        settings = QSettings(self.SETTINGS_ORG, self.SETTINGS_APP)
        geo = settings.value("geometry")
        if geo is not None and isinstance(geo, QByteArray):
            if self.restoreGeometry(geo):
                return
        st = settings.value("window_state")
        if st is not None and isinstance(st, QByteArray):
            self.restoreState(st)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._tray is not None:
            self._tray.hide()
        # Hand the combinations back, or they stay reserved until Windows
        # notices the process is gone - and F2 would be missing from every
        # other program in the meantime.
        self._hotkey.stop()
        self._hotkey_plain.stop()
        self._save_timer.stop()
        self._poll_timer.stop()
        self.stop_sync_server()
        self._flush_save_details()
        self._alert_engine.stop()
        self._acc_page.shutdown()
        self._sticky_page.shutdown()
        self._daily_page.shutdown()
        self._meetings_page.shutdown()
        self._collabor_page.shutdown()
        settings = QSettings(self.SETTINGS_ORG, self.SETTINGS_APP)
        settings.setValue("geometry", self.saveGeometry())
        settings.setValue("window_state", self.saveState())
        settings.setValue("table_header_state", self.table.horizontalHeader().saveState())
        settings.setValue("date_filter_enabled", self.date_filter_check.isChecked())
        settings.setValue(
            "date_from", self.date_from.dateTime().toString("yyyy-MM-dd HH:mm:ss")
        )
        settings.setValue("date_to", self.date_to.dateTime().toString("yyyy-MM-dd HH:mm:ss"))
        settings.setValue("daily_filter_enabled", self.daily_filter_check.isChecked())
        settings.setValue("daily_anchor_date", self.daily_date.date().toString("yyyy-MM-dd"))
        db.close()  # final flush + release the encrypted in-memory database
        super().closeEvent(event)
        if event.isAccepted():
            # The tray icon keeps the app alive after its last window closes
            # (see _build_tray_icon), so closing the main window has to say so.
            QApplication.quit()

    def _restore_table_header(self) -> None:
        settings = QSettings(self.SETTINGS_ORG, self.SETTINGS_APP)
        data = settings.value("table_header_state")
        if data is not None and isinstance(data, QByteArray):
            self.table.horizontalHeader().restoreState(data)

    def _readonly_item(self, text: str, task_id: int) -> QTableWidgetItem:
        it = QTableWidgetItem(text)
        it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
        it.setData(ROLE_TASK_ID, task_id)
        return it

    def _apply_row_status_appearance(self, row: int, status: str) -> None:
        bg = status_background(status)
        fg = status_foreground(status)
        bg_brush = QBrush(bg)
        fg_brush = QBrush(fg)
        for col in (COL_INDEX, COL_DATETIME, COL_DESC):
            it = self.table.item(row, col)
            if it is not None:
                it.setBackground(bg_brush)
                it.setForeground(fg_brush)
        link_cell = self.table.cellWidget(row, COL_LINK)
        if link_cell is not None:
            self._style_link_cell_widget(link_cell, status, bg, fg)
        w = self.table.cellWidget(row, COL_STATUS)
        if isinstance(w, QComboBox):
            c = bg.name()
            t = fg.name()
            border = "#C62828" if status == "closed" else "#546E7A"
            w.setStyleSheet(
                f"QComboBox {{ background-color: {c}; color: {t}; padding: 2px 6px; "
                f"border: 1px solid {border}; border-radius: 3px; font-weight: 500; }}"
                f"QComboBox:hover {{ background-color: {bg.lighter(108).name()}; color: {t}; }}"
                f"QComboBox::drop-down {{ border: none; width: 20px; }}"
                f"QComboBox QAbstractItemView {{ background-color: #FFFFFF; color: #0A0A0A; "
                f"selection-background-color: #BBDEFB; selection-color: #0A0A0A; }}"
            )

    def _style_link_cell_widget(
        self, w: QWidget, status: str, bg: QColor, fg: QColor
    ) -> None:
        c = bg.name()
        t = fg.name()
        bcol = "#C62828" if status == "closed" else "#90A4AE"
        w.setStyleSheet(
            f"QWidget#linkCell {{ background-color: {c}; }}"
            f"QLineEdit {{ background-color: {c}; color: {t}; border: 1px solid {bcol}; "
            f"border-radius: 3px; padding: 2px 6px; selection-background-color: #90CAF9; "
            f"selection-color: #0A0A0A; }}"
            + COPY_ICON_BTN_QSS
        )

    def _make_link_cell_widget(self, link: str, task_id: int) -> QWidget:
        w = QWidget()
        w.setObjectName("linkCell")
        w.setProperty("task_id", task_id)
        w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(4)
        le = QLineEdit(link)
        le.setReadOnly(True)
        le.setFrame(True)
        le.setCursorPosition(0)
        copy_btn = QPushButton()
        ico = orange_copy_icon()
        if not ico.isNull():
            copy_btn.setIcon(ico)
            copy_btn.setIconSize(QSize(18, 18))
        else:
            copy_btn.setText("⧉")
        copy_btn.setFixedSize(30, 28)
        copy_btn.setObjectName("copyIconBtn")
        copy_btn.setToolTip("Copy link to clipboard")
        copy_btn.clicked.connect(
            lambda _checked=False, u=link, b=copy_btn: self._copy_link_to_clipboard(u, b)
        )
        lay.addWidget(le, stretch=1)
        lay.addWidget(copy_btn, stretch=0)
        for cw in (w, le, copy_btn):
            cw.installEventFilter(self)
        return w

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:  # type: ignore[override]
        if (
            event is not None
            and event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.FocusIn)
            and isinstance(obj, QWidget)
        ):
            w: QWidget | None = obj
            while w is not None:
                tid = w.property("task_id")
                if isinstance(tid, int):
                    row = self._row_for_task_id(tid)
                    if row >= 0:
                        self.table.selectRow(row)
                    break
                w = w.parentWidget()
        return False

    def _copy_link_to_clipboard(self, link: str, copy_btn: QPushButton | None = None) -> None:
        if not link:
            return
        QApplication.clipboard().setText(link)
        self.statusBar().showMessage("Link copied to clipboard.", 5000)
        if copy_btn is not None:
            apply_copy_button_flash(copy_btn)

    def _make_status_combo(self, task_id: int, current_status: str) -> StatusComboBox:
        cb = StatusComboBox()
        for s in db.ALLOWED_STATUSES:
            cb.addItem(status_label(s), s)
        ix = cb.findData(current_status)
        cb.setCurrentIndex(ix if ix >= 0 else 0)
        cb.currentIndexChanged.connect(lambda _i, tid=task_id: self._on_status_combo_changed(tid))
        return cb

    def _row_for_task_id(self, task_id: int) -> int:
        for r in range(self.table.rowCount()):
            it = self.table.item(r, COL_DATETIME)
            if it is not None and it.data(ROLE_TASK_ID) == task_id:
                return r
        return -1

    def _on_status_combo_changed(self, task_id: int) -> None:
        if self._loading_table:
            return
        row = self._row_for_task_id(task_id)
        if row < 0:
            return
        w = self.table.cellWidget(row, COL_STATUS)
        if not isinstance(w, QComboBox):
            return
        status = w.currentData()
        if not isinstance(status, str):
            return
        desc_it = self.table.item(row, COL_DESC)
        desc = desc_it.text() if desc_it else ""
        try:
            db.update_task(task_id, status, desc)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid data", str(e))
            return
        self._apply_row_status_appearance(row, status)

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading_table or item.column() != COL_DESC:
            return
        self._pending_desc_row = item.row()
        self._save_timer.start()

    def _on_table_selection_changed(self) -> None:
        self._save_timer.stop()
        self._flush_save_details()
        row = self.table.currentRow()
        if row < 0:
            self._current_id = None
        else:
            it = self.table.item(row, COL_DATETIME)
            tid = it.data(ROLE_TASK_ID) if it else None
            self._current_id = tid if isinstance(tid, int) else None
        self._refresh_status_bar_for_current_tab()

    def _on_main_tab_changed(self, _index: int) -> None:
        self._refresh_status_bar_for_current_tab()

    def _on_sticky_counts_changed(self, n: int) -> None:
        self._status_sticky_count = n
        self._refresh_status_bar_for_current_tab()

    def _on_acc_counts_changed(self, n: int) -> None:
        self._status_acc_count = n
        self._refresh_status_bar_for_current_tab()

    def _on_daily_counts_changed(self, n: int) -> None:
        self._status_daily_count = n
        self._refresh_status_bar_for_current_tab()

    def _format_tasks_status_text(self) -> str:
        v, t = self._status_visible_rows, self._status_all_tasks
        shown_word = "rows" if v != 1 else "row"
        db_word = "rows" if t != 1 else "row"
        base = f"Tasks — Shown: {v} {shown_word} · Database total: {t} {db_word}"
        n_sel = len(self.table.selectionModel().selectedRows())
        if n_sel > 0:
            sel_word = "rows" if n_sel != 1 else "row"
            return f"{base}  ·  Selected: {n_sel} {sel_word}"
        return base

    def _format_sticky_status_text(self) -> str:
        n = self._status_sticky_count
        nw = "notes" if n != 1 else "note"
        return f"Sticky notes — {n} {nw}"

    def _format_acc_status_text(self) -> str:
        n = self._status_acc_count
        nw = "accounts" if n != 1 else "account"
        return f"Acc — {n} {nw}"

    def _format_daily_status_text(self) -> str:
        n = self._status_daily_count
        day = self._daily_page.current_day_str()
        nw = "cards" if n != 1 else "card"
        return f"Daily — {day} · {n} {nw}"

    def _format_meetings_status_text(self) -> str:
        try:
            n = len(db.list_meetings())
        except Exception:
            n = 0
        nw = "meetings" if n != 1 else "meeting"
        return f"Meetings — {n} {nw}"

    def _refresh_status_bar_for_current_tab(self) -> None:
        ix = self._main_tabs.currentIndex()
        if ix == _TAB_DAILY:
            self._status_rows_label.setText(self._format_daily_status_text())
        elif ix == _TAB_MEETINGS:
            self._status_rows_label.setText(self._format_meetings_status_text())
        elif ix == _TAB_COLLABOR:
            self._status_rows_label.setText("")  # nothing to count yet
        elif ix == _TAB_NOTES:
            self._status_rows_label.setText(self._format_sticky_status_text())
        else:
            self._status_rows_label.setText(self._format_acc_status_text())

    def refresh_list(self) -> None:
        self._save_timer.stop()
        self._flush_save_details()

        sel_id = self._current_id
        needle = normalize_search(self.search_input.text())
        all_tasks = db.list_tasks()
        tasks = [
            t
            for t in all_tasks
            if task_matches_search(t, needle) and self._task_matches_created_filter(t.created_at)
        ]
        self._status_visible_rows = len(tasks)
        self._status_all_tasks = len(all_tasks)

        self._loading_table = True
        self.table.blockSignals(True)
        self.table.setRowCount(len(tasks))
        for r, t in enumerate(tasks):
            self.table.setItem(r, COL_INDEX, self._readonly_item(str(r + 1), t.id))
            self.table.setItem(r, COL_DATETIME, self._readonly_item(t.created_at, t.id))
            self.table.setCellWidget(r, COL_LINK, self._make_link_cell_widget(t.link, t.id))
            self.table.setItem(
                r,
                COL_DESC,
                self._desc_item(t.description, t.id),
            )
            self.table.setCellWidget(r, COL_STATUS, self._make_status_combo(t.id, t.status))
            self._apply_row_status_appearance(r, t.status)
            self.table.resizeRowToContents(r)
        self.table.blockSignals(False)
        self._loading_table = False

        if sel_id is not None:
            for r in range(self.table.rowCount()):
                it = self.table.item(r, COL_DATETIME)
                if it is not None and it.data(ROLE_TASK_ID) == sel_id:
                    self.table.selectRow(r)
                    self.table.scrollToItem(
                        it, QAbstractItemView.ScrollHint.PositionAtCenter
                    )
                    break
            else:
                self.table.clearSelection()
                self._current_id = None
        else:
            self.table.clearSelection()
            self._current_id = None

        self._refresh_status_bar_for_current_tab()

    def _desc_item(self, text: str, task_id: int) -> QTableWidgetItem:
        it = QTableWidgetItem(text)
        it.setData(ROLE_TASK_ID, task_id)
        flags = it.flags() | Qt.ItemFlag.ItemIsEditable
        it.setFlags(flags)
        return it

    def _on_add_link(self) -> None:
        raw = self.link_input.text()
        link = db.normalize_link(raw)
        if not link:
            QMessageBox.warning(self, "Invalid link", "Please enter a non-empty link.")
            return
        if db.link_exists(link):
            QMessageBox.information(
                self,
                "Link already exists",
                "This link is already saved. Duplicate links are not allowed.",
            )
            return
        try:
            new_id = db.add_task(link)
        except sqlite3.IntegrityError:
            QMessageBox.information(
                self,
                "Link already exists",
                "This link is already saved. Duplicate links are not allowed.",
            )
            return
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return
        self.link_input.clear()
        self.search_input.clear()
        self._select_task_id(new_id)

    def _select_task_id(self, task_id: int) -> None:
        self._current_id = task_id
        self.refresh_list()
        for r in range(self.table.rowCount()):
            it = self.table.item(r, COL_DATETIME)
            if it is not None and it.data(ROLE_TASK_ID) == task_id:
                self.table.selectRow(r)
                if it is not None:
                    self.table.scrollToItem(
                        it, QAbstractItemView.ScrollHint.PositionAtCenter
                    )
                break

    def _flush_save_details(self) -> None:
        if self._loading_table:
            return
        row = self._pending_desc_row
        self._pending_desc_row = None
        if row is None or row < 0 or row >= self.table.rowCount():
            return
        tid_item = self.table.item(row, COL_DATETIME)
        if tid_item is None:
            return
        tid = tid_item.data(ROLE_TASK_ID)
        if not isinstance(tid, int):
            return
        desc_it = self.table.item(row, COL_DESC)
        desc = desc_it.text() if desc_it else ""
        w = self.table.cellWidget(row, COL_STATUS)
        if not isinstance(w, QComboBox):
            return
        status = w.currentData()
        if not isinstance(status, str):
            return
        try:
            db.update_task(tid, status, desc)
        except ValueError as e:
            QMessageBox.warning(self, "Invalid data", str(e))
            return
        self._apply_row_status_appearance(row, status)

    def _on_remove_selected(self) -> None:
        self._save_timer.stop()
        self._flush_save_details()
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()}, reverse=True)
        if not rows:
            QMessageBox.information(self, "Remove", "Select one or more rows in the table first.")
            return
        ids: list[int] = []
        for row in rows:
            tid_item = self.table.item(row, COL_DATETIME)
            if tid_item is None:
                continue
            tid = tid_item.data(ROLE_TASK_ID)
            if isinstance(tid, int):
                ids.append(tid)
        ids = list(dict.fromkeys(ids))
        if not ids:
            return
        n = len(ids)
        msg = (
            f"Remove {n} link(s) from the database?"
            if n > 1
            else "Remove this link from the database?"
        )
        reply = QMessageBox.question(
            self,
            "Remove link(s)" if n > 1 else "Remove link",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        for tid in ids:
            db.delete_task(tid)
        self._current_id = None
        self.refresh_list()


