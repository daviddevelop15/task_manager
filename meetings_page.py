"""Meetings board: a month-grid calendar of meetings with per-meeting alarms.

The page shows one month at a time (navigable). Each day cell lists that day's
meetings and has a ``+`` to add one. A meeting carries a title, body, meeting
link, start time and an alarm config (lead time + repeat interval) consumed by
:mod:`meeting_alerts`.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from html import escape
from typing import Optional

from PyQt6.QtCore import (
    QDate,
    QDateTime,
    QSettings,
    QSize,
    Qt,
    QTimer,
    QTimeZone,
    pyqtSignal,
)
from PyQt6.QtGui import QFont, QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

import database as db
from app_store import PermissionDenied, author_display, author_label, get_store as _store
import ics_import
import job_match
from daily_board_page import (
    DailyCardEditDialog,
    _DAILY_STATUS_COLORS,
    _DAILY_STATUS_NONE,
    _DAILY_STATUS_ORDER,
    _daily_glyph_icon,
    _daily_status_color,
    _daily_status_swatch,
)
from task_helpers import fields_match_search, normalize_search

# Lead-time presets (label -> minutes before start when alarms begin).
_LEAD_CHOICES: tuple[tuple[str, int], ...] = (
    ("15 minutes", 15),
    ("30 minutes", 30),
    ("1 hour", 60),
    ("6 hours", 360),
    ("12 hours", 720),
)
_WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def _parse_dt(start_at: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(start_at, fmt)
        except ValueError:
            continue
    return None


# --- timezones -------------------------------------------------------------
# meetings.start_at is stored as LOCAL machine wall-clock. The board can display
# those times in any timezone; Qt's QTimeZone carries its own database, so this
# needs no extra dependency (Python's zoneinfo would need `tzdata` on Windows).

MEETINGS_TZ_SETTINGS_KEY = "meetings_timezone"


def _local_tz() -> QTimeZone:
    return QTimeZone(QTimeZone.systemTimeZoneId())


def _qdt_from_stored(start_at: str) -> QDateTime:
    """Stored local wall-clock string -> QDateTime anchored to the local zone."""
    dt = QDateTime.fromString(start_at, "yyyy-MM-dd HH:mm:ss")
    if not dt.isValid():
        dt = QDateTime.fromString(start_at, "yyyy-MM-dd HH:mm")
    if dt.isValid():
        dt.setTimeZone(_local_tz())
    return dt


def _to_tz(start_at: str, tz: QTimeZone) -> QDateTime:
    """The stored time, expressed in `tz` (same instant, different wall clock)."""
    dt = _qdt_from_stored(start_at)
    return dt.toTimeZone(tz) if dt.isValid() else dt


def _stored_from_tz(qdt: QDateTime, tz: QTimeZone) -> str:
    """A wall-clock entered in `tz` -> the stored local wall-clock string."""
    d = QDateTime(qdt.date(), qdt.time())
    d.setTimeZone(tz)
    return d.toTimeZone(_local_tz()).toString("yyyy-MM-dd HH:mm:ss")


# Creator-filter combo data. Real creators are stored under their own name, so
# the two sentinels use a shape a display name can never take.
_CREATOR_FILTER_ALL = "\x00all"
_CREATOR_UNATTRIBUTED = "\x00none"
_CREATOR_UNATTRIBUTED_LABEL = "Unattributed"


def _creator_key(meeting: db.MeetingRow) -> str:
    """Filter key for a meeting's creator; rows predating authorship group together."""
    return (meeting.created_by or "").strip() or _CREATOR_UNATTRIBUTED


def _creator_label(meeting: db.MeetingRow) -> str:
    """Who scheduled this meeting, as shown to the user.

    Resolved from the stored account id, so a rename does not change who a
    meeting belongs to - only how their name reads.
    """
    return author_label((meeting.created_by or "").strip()) or "—"


def _status_dot(status: str) -> Optional[QLabel]:
    """Small round colour chip for a meeting's status (same palette as the
    daily board), or None when the meeting has no status."""
    color = _daily_status_color(status)
    if not color:
        return None
    dot = QLabel()
    dot.setFixedSize(10, 10)
    dot.setStyleSheet(f"background-color: {color}; border-radius: 5px;")
    dot.setToolTip(status)
    return dot


class MeetingEditorDialog(QDialog):
    """Create or edit a single meeting."""

    def __init__(
        self,
        parent: QWidget | None = None,
        meeting: Optional[db.MeetingRow] = None,
        default_day: Optional[date] = None,
        tz: Optional[QTimeZone] = None,
    ) -> None:
        super().__init__(parent)
        self._meeting = meeting
        self._deleted = False
        # Times are shown/entered in the board's display timezone and converted
        # back to local wall-clock on save.
        self._tz = tz if (tz is not None and tz.isValid()) else _local_tz()
        # Name whoever scheduled it, and their role. Meetings are shared, so the
        # first question on opening one is whose it is - and that decides
        # whether it can be changed.
        if meeting is None:
            self.setWindowTitle("New meeting")
        else:
            who = author_display(meeting.created_by)
            self.setWindowTitle(f"Edit meeting — {who}" if who else "Edit meeting")
        self.setMinimumWidth(420)

        form = QVBoxLayout(self)

        form.addWidget(QLabel("Title"))
        self._title = QLineEdit()
        self._title.setPlaceholderText("Meeting title")
        form.addWidget(self._title)

        form.addWidget(QLabel("Notes"))
        self._body = QPlainTextEdit()
        self._body.setPlaceholderText("Agenda, notes, dial-in details…")
        self._body.setMinimumHeight(80)
        form.addWidget(self._body)

        form.addWidget(QLabel("Meeting link"))
        # Read-only by default so an imported link can't be changed by accident;
        # the pencil toggles edit mode, the copy button yields the raw link.
        link_row = QHBoxLayout()
        self._link = QLineEdit()
        self._link.setPlaceholderText("https://… (Zoom, Meet, Teams)")
        self._link.setReadOnly(True)
        link_row.addWidget(self._link, stretch=1)
        icon_color = QApplication.palette().color(QPalette.ColorRole.ButtonText)

        def _link_btn(kind: str, tooltip: str) -> QToolButton:
            b = QToolButton()
            b.setIcon(_daily_glyph_icon(kind, icon_color))
            b.setIconSize(QSize(18, 18))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            b.setAutoRaise(True)
            b.setToolTip(tooltip)
            return b

        self._link_copy_btn = _link_btn("copy", "Copy the meeting link")
        self._link_copy_btn.clicked.connect(self._copy_link)
        link_row.addWidget(self._link_copy_btn)
        self._link_edit_btn = _link_btn("edit", "Edit the meeting link")
        self._link_edit_btn.setCheckable(True)
        self._link_edit_btn.toggled.connect(self._on_link_edit_toggled)
        link_row.addWidget(self._link_edit_btn)
        form.addLayout(link_row)

        form.addWidget(QLabel(f"Date & time ({bytes(self._tz.id()).decode()})"))
        self._when = QDateTimeEdit()
        self._when.setDisplayFormat("yyyy-MM-dd HH:mm")
        self._when.setCalendarPopup(True)
        form.addWidget(self._when)

        form.addWidget(QLabel("Status"))
        self._status = QComboBox()
        self._status.addItem("No status", _DAILY_STATUS_NONE)
        for name in _DAILY_STATUS_ORDER:
            self._status.addItem(_daily_status_swatch(_DAILY_STATUS_COLORS[name]), name, name)
        form.addWidget(self._status)

        alert_row = QHBoxLayout()
        alert_row.addWidget(QLabel("Alert from"))
        self._lead = QComboBox()
        for label, minutes in _LEAD_CHOICES:
            self._lead.addItem(label, minutes)
        alert_row.addWidget(self._lead)
        alert_row.addWidget(QLabel("before, every"))
        self._interval = QSpinBox()
        self._interval.setRange(1, 720)
        self._interval.setSuffix(" min")
        self._interval.setValue(5)
        alert_row.addWidget(self._interval)
        alert_row.addStretch(1)
        form.addLayout(alert_row)

        self._alerts_enabled = QCheckBox("Alerts enabled")
        self._alerts_enabled.setChecked(True)
        form.addWidget(self._alerts_enabled)

        # Linked job application: which daily-board applied job this meeting is
        # for. Filled by picking a card (auto-recommended or searched).
        self._linked_card_id: Optional[int] = (
            meeting.linked_card_id if meeting is not None else None
        )
        form.addWidget(QLabel("Linked job application"))
        linked_row = QHBoxLayout()
        self._linked_label = QLabel("")
        self._linked_label.setWordWrap(True)
        self._linked_label.setToolTip("Click to open the full job details")
        # Clicking the linked job opens its detail dialog (rendered as a link).
        self._linked_label.linkActivated.connect(self._open_linked_job)
        linked_row.addWidget(self._linked_label, stretch=1)
        self._link_job_btn = QPushButton("Link job…")
        self._link_job_btn.setToolTip(
            "Connect this meeting to the applied job it belongs to"
        )
        self._link_job_btn.clicked.connect(self._on_link_job)
        linked_row.addWidget(self._link_job_btn)
        self._unlink_job_btn = QPushButton("Unlink")
        self._unlink_job_btn.clicked.connect(self._on_unlink_job)
        linked_row.addWidget(self._unlink_job_btn)
        form.addLayout(linked_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        if meeting is not None:
            delete_btn = QPushButton("Delete")
            delete_btn.clicked.connect(self._on_delete)
            buttons.addButton(delete_btn, QDialogButtonBox.ButtonRole.DestructiveRole)
        form.addWidget(buttons)

        self._load(meeting, default_day)

    def _load(self, meeting: Optional[db.MeetingRow], default_day: Optional[date]) -> None:
        if meeting is not None:
            self._title.setText(meeting.title)
            self._body.setPlainText(meeting.body)
            self._link.setText(meeting.link)
            shown = _to_tz(meeting.start_at, self._tz)  # stored local -> display tz
            if shown.isValid():
                self._when.setDateTime(QDateTime(shown.date(), shown.time()))
            idx = self._lead.findData(meeting.alert_lead_minutes)
            self._lead.setCurrentIndex(idx if idx >= 0 else 0)
            self._interval.setValue(max(1, meeting.alert_interval_minutes))
            self._alerts_enabled.setChecked(meeting.alerts_enabled)
            sidx = self._status.findData(meeting.status or _DAILY_STATUS_NONE)
            self._status.setCurrentIndex(sidx if sidx >= 0 else 0)
        else:
            if default_day is not None:
                start_date = QDate(default_day.year, default_day.month, default_day.day)
            else:  # today in the display timezone, matching the board
                start_date = QDateTime.currentDateTime().toTimeZone(self._tz).date()
            start = QDateTime(start_date, self._default_time())
            self._when.setDateTime(start)
            self._lead.setCurrentIndex(0)
        # An existing meeting's link starts protected; a new one has nothing to
        # protect, so it opens ready to type.
        is_new = meeting is None
        self._link_edit_btn.setChecked(is_new)
        self._link.setReadOnly(not is_new)
        self._refresh_linked_label()

    def _default_time(self):
        from PyQt6.QtCore import QTime

        # Next hour, on the hour, in the display timezone.
        now = QDateTime.currentDateTime().toTimeZone(self._tz).time()
        return QTime((now.hour() + 1) % 24, 0)

    def _copy_link(self) -> None:
        link = self._link.text().strip()
        if not link:
            return
        QApplication.clipboard().setText(link)
        QToolTip.showText(
            self._link_copy_btn.mapToGlobal(self._link_copy_btn.rect().bottomLeft()),
            "Meeting link copied to the clipboard.",
            self._link_copy_btn,
        )

    def _on_link_edit_toggled(self, on: bool) -> None:
        self._link.setReadOnly(not on)
        self._link_edit_btn.setToolTip(
            "Finish editing (link is editable)" if on else "Edit the meeting link"
        )
        if on:
            self._link.setFocus()
            self._link.selectAll()

    # --- linked job application ---------------------------------------------

    def _refresh_linked_label(self) -> None:
        if self._linked_card_id is None:
            self._linked_label.setText("<i>Not linked to a job application</i>")
            self._linked_label.setToolTip("")
            self._link_job_btn.setText("Link job…")
            self._unlink_job_btn.setVisible(False)
            return
        card = _store().get_daily_card(self._linked_card_id)
        if card is None:
            self._linked_label.setText(
                f"<i>Linked job #{self._linked_card_id} no longer exists</i>"
            )
            self._linked_label.setToolTip("")
        else:
            info = job_match.index_cards([card])
            label = job_match.card_label(info[0]) if info else (card.title or "job")
            # A link so a click opens the job's detail dialog (linkActivated).
            self._linked_label.setText(
                f'🔗 <a href="open">{escape(label)}</a>'
            )
            self._linked_label.setToolTip("Click to open the full job details")
        self._link_job_btn.setText("Change…")
        self._unlink_job_btn.setVisible(True)

    def _open_linked_job(self, *_a) -> None:
        """Open the linked applied job's detail dialog."""
        if self._linked_card_id is None:
            return
        if _store().get_daily_card(self._linked_card_id) is None:
            QMessageBox.information(
                self, "Linked job", "That job application no longer exists."
            )
            self._linked_card_id = None
            self._refresh_linked_label()
            return
        dlg = DailyCardEditDialog(self._linked_card_id, self)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _meeting_context_widget(self):
        """A lightweight meeting-like object from the current dialog fields, so
        auto-recommend reflects what the user is looking at (even unsaved)."""
        from types import SimpleNamespace

        return SimpleNamespace(
            title=self._title.text(),
            body=self._body.toPlainText(),
            link=self._link.text(),
        )

    def _on_link_job(self) -> None:
        try:
            cards = _store().list_all_daily_cards()
        except Exception:
            cards = []
        dlg = _LinkJobDialog(
            self, self._meeting_context_widget(), cards, self._linked_card_id
        )
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_card_id is not None:
            self._linked_card_id = dlg.selected_card_id
            self._refresh_linked_label()

    def _on_unlink_job(self) -> None:
        self._linked_card_id = None
        self._refresh_linked_label()

    def _on_save(self) -> None:
        title = self._title.text().strip()
        if not title:
            QMessageBox.warning(self, "Meeting", "Please enter a title.")
            return
        self.accept()

    def _on_delete(self) -> None:
        if (
            QMessageBox.question(
                self,
                "Delete meeting",
                "Delete this meeting?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self._deleted = True
            self.accept()

    # --- results ---

    def was_deleted(self) -> bool:
        return self._deleted

    def values(self) -> dict:
        status = self._status.currentData()
        return {
            "title": self._title.text().strip(),
            "body": self._body.toPlainText().strip(),
            "link": self._link.text().strip(),
            # The picker's wall-clock is in the display tz -> store local.
            "start_at": _stored_from_tz(self._when.dateTime(), self._tz),
            "lead": int(self._lead.currentData()),
            "interval": int(self._interval.value()),
            "alerts_enabled": self._alerts_enabled.isChecked(),
            "status": status if isinstance(status, str) else _DAILY_STATUS_NONE,
            "linked_card_id": self._linked_card_id,
        }


class _LinkJobDialog(QDialog):
    """Pick the daily job card a meeting belongs to.

    Auto mode lists cards the app recommends for this meeting (by company,
    recruiter email/name, and title). Manual mode searches all applied jobs by
    any combination of company, recruiter/person name, and position.
    """

    def __init__(self, parent, meeting_ctx, cards, current_id) -> None:
        super().__init__(parent)
        self.setWindowTitle("Link job application")
        self.setMinimumSize(560, 460)
        self.selected_card_id: Optional[int] = None

        self._index = job_match.index_cards(cards)
        self._ctx = job_match.parse_meeting(meeting_ctx)
        self._recs = job_match.recommend(self._ctx, self._index)

        root = QVBoxLayout(self)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Find the matching job:"))
        self._mode = QComboBox()
        self._mode.addItem("Auto — recommended", "auto")
        self._mode.addItem("Manual — search", "manual")
        self._mode.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self._mode)
        mode_row.addStretch(1)
        root.addLayout(mode_row)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_auto_page())
        self._stack.addWidget(self._build_manual_page())
        root.addWidget(self._stack, stretch=1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_btn.setText("Select")
        self._ok_btn.setEnabled(False)
        buttons.accepted.connect(self._accept_selection)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._populate_auto()
        # Start on whichever mode is useful: Auto if it found matches, else Manual.
        if not self._recs:
            self._mode.setCurrentIndex(1)
        self._on_mode_changed()

    # --- pages ---

    def _build_auto_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        self._auto_header = QLabel("")
        lay.addWidget(self._auto_header)
        self._auto_list = QListWidget()
        self._auto_list.itemSelectionChanged.connect(self._sync_ok)
        self._auto_list.itemDoubleClicked.connect(lambda _i: self._accept_selection())
        lay.addWidget(self._auto_list, stretch=1)
        return page

    def _build_manual_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel(
            "Search applied jobs. Fields combine (all must match)."
        ))
        self._q_company = QLineEdit()
        self._q_company.setPlaceholderText("Company name")
        self._q_recruiter = QLineEdit()
        self._q_recruiter.setPlaceholderText("Recruiter / person name or email")
        self._q_position = QLineEdit()
        self._q_position.setPlaceholderText("Position / job title")
        for w in (self._q_company, self._q_recruiter, self._q_position):
            w.returnPressed.connect(self._run_search)
            lay.addWidget(w)
        search_row = QHBoxLayout()
        search_btn = QPushButton("Search")
        search_btn.clicked.connect(self._run_search)
        search_row.addStretch(1)
        search_row.addWidget(search_btn)
        lay.addLayout(search_row)
        self._manual_header = QLabel("")
        lay.addWidget(self._manual_header)
        self._manual_list = QListWidget()
        self._manual_list.itemSelectionChanged.connect(self._sync_ok)
        self._manual_list.itemDoubleClicked.connect(lambda _i: self._accept_selection())
        lay.addWidget(self._manual_list, stretch=1)
        # Pre-fill the search from what the meeting looks like, as a starting point.
        if self._ctx.company:
            self._q_company.setText(self._ctx.company)
        elif self._ctx.role:
            self._q_position.setText(self._ctx.role)
        return page

    # --- population ---

    def _add_card_item(self, lst: QListWidget, card, reasons=None) -> None:
        item = QListWidgetItem(job_match.card_label(card))
        item.setData(Qt.ItemDataRole.UserRole, card.card_id)
        tip = card.title
        if reasons:
            tip += "\nMatched on: " + ", ".join(reasons)
        item.setToolTip(tip)
        lst.addItem(item)

    def _populate_auto(self) -> None:
        self._auto_list.clear()
        if not self._recs:
            self._auto_header.setText(
                "No automatic matches. Switch to Manual search to find the job."
            )
            return
        self._auto_header.setText(
            f"{len(self._recs)} recommended "
            f"match{'es' if len(self._recs) != 1 else ''} for this meeting:"
        )
        for s in self._recs:
            self._add_card_item(self._auto_list, s.card, s.reasons)

    def _run_search(self) -> None:
        results = job_match.search(
            self._index,
            company=self._q_company.text(),
            recruiter=self._q_recruiter.text(),
            position=self._q_position.text(),
        )
        self._manual_list.clear()
        if not (self._q_company.text().strip() or self._q_recruiter.text().strip()
                or self._q_position.text().strip()):
            self._manual_header.setText("Enter at least one search field above.")
            self._sync_ok()
            return
        self._manual_header.setText(
            f"{len(results)} match{'es' if len(results) != 1 else ''}."
        )
        for card in results:
            self._add_card_item(self._manual_list, card)
        self._sync_ok()

    # --- selection ---

    def _active_list(self) -> QListWidget:
        return self._auto_list if self._mode.currentData() == "auto" else self._manual_list

    def _on_mode_changed(self, *_a) -> None:
        self._stack.setCurrentIndex(0 if self._mode.currentData() == "auto" else 1)
        self._sync_ok()

    def _sync_ok(self) -> None:
        self._ok_btn.setEnabled(self._active_list().currentItem() is not None)

    def _accept_selection(self) -> None:
        item = self._active_list().currentItem()
        if item is None:
            return
        self.selected_card_id = int(item.data(Qt.ItemDataRole.UserRole))
        self.accept()


class _DayCell(QFrame):
    """A single day in the month grid."""

    def __init__(self, page: "MeetingsPage", day: date, in_month: bool, is_today: bool) -> None:
        super().__init__()
        self._page = page
        self._day = day
        self._is_today = is_today
        self.setObjectName("dayCell")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumSize(120, 96)

        pal = QApplication.palette()
        muted = pal.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text).name()
        highlight = pal.color(QPalette.ColorRole.Highlight).name()
        border = highlight if is_today else "palette(mid)"
        bg = "palette(base)" if in_month else "palette(window)"
        # Scope the rule to this frame by object name so it doesn't bleed onto
        # the child labels/buttons.
        self.setStyleSheet(
            f"QFrame#dayCell {{ background-color: {bg}; "
            f"border: {2 if is_today else 1}px solid {border}; border-radius: 4px; }}"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 4)
        lay.setSpacing(2)

        head = QHBoxLayout()
        head.setSpacing(0)
        num = QLabel(str(day.day))
        nf = QFont()
        nf.setBold(is_today)
        num.setFont(nf)
        if not in_month:
            num.setStyleSheet(f"color: {muted};")
        head.addWidget(num)
        head.addStretch(1)
        add = QToolButton()
        add.setText("+")
        add.setAutoRaise(True)
        add.setToolTip("Add a meeting on this day")
        add.clicked.connect(lambda: self._page.add_meeting_on(self._day))
        head.addWidget(add)
        lay.addLayout(head)

        self._meetings_box = QVBoxLayout()
        self._meetings_box.setSpacing(1)
        lay.addLayout(self._meetings_box)
        lay.addStretch(1)

    def add_meeting_entry(self, meeting: db.MeetingRow) -> None:
        highlighted = meeting.id == self._page._highlight_meeting_id
        chip = _MeetingChip(self._page, meeting, highlighted)
        if highlighted:
            self._page._highlight_widget = chip
        self._meetings_box.addWidget(chip)


class _MeetingChip(QFrame):
    """A clickable meeting entry inside a day cell.

    Title on the left; a bell on the right when alerts are enabled. Paused
    meetings render muted with no bell.
    """

    def __init__(self, page: "MeetingsPage", meeting: db.MeetingRow, highlighted: bool) -> None:
        super().__init__()
        self._page = page
        self._mid = meeting.id
        self.setObjectName("meetingChip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        enabled = meeting.alerts_enabled
        bg = "palette(highlight)" if enabled else "palette(mid)"
        fg = "palette(highlighted-text)" if enabled else "palette(text)"
        border = "2px solid palette(text)" if highlighted else "1px solid transparent"
        self.setStyleSheet(
            f"QFrame#meetingChip {{ background-color: {bg}; border: {border}; "
            f"border-radius: 3px; }}"
            f"QFrame#meetingChip QLabel {{ color: {fg}; background: transparent; }}"
        )

        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 1, 3, 1)
        lay.setSpacing(3)
        dot = _status_dot(meeting.status)
        if dot is not None:
            lay.addWidget(dot, 0)
        title = QLabel(f"{page.display_time(meeting.start_at)} {meeting.title}".strip())
        # Ignore the label's width hint so long titles never widen the column.
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        tip = meeting.title + (f"\n{meeting.body}" if meeting.body else "")
        if meeting.created_by:
            tip += f"\n\nCreated by {author_label(meeting.created_by)}"
        if meeting.status:
            tip = f"[{meeting.status}] " + tip
        if not enabled:
            tip = "Alerts paused for this meeting\n\n" + tip
        title.setToolTip(tip)
        lay.addWidget(title, 1)
        if enabled:
            bell = QLabel("\U0001F514")  # 🔔 alerts enabled
            bell.setToolTip("Alerts enabled")
            lay.addWidget(bell, 0)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._page.edit_meeting(self._mid)
        super().mousePressEvent(event)


class _MeetingListRow(QFrame):
    """A clickable row in List view: status dot · date/time · title · status · bell."""

    def __init__(self, page: "MeetingsPage", meeting: db.MeetingRow) -> None:
        super().__init__()
        self._page = page
        self._mid = meeting.id
        self.setObjectName("meetingRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QFrame#meetingRow { border: 1px solid palette(mid); border-radius: 4px; }"
            "QFrame#meetingRow:hover { background-color: palette(alternate-base); }"
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(10)

        dot = _status_dot(meeting.status)
        if dot is not None:
            lay.addWidget(dot, 0)

        when = QLabel(page.display_datetime(meeting.start_at))
        when.setMinimumWidth(120)
        lay.addWidget(when, 0)

        title = QLabel(meeting.title or "(untitled)")
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title.setToolTip(meeting.body or meeting.title)
        lay.addWidget(title, 1)

        if meeting.created_by:
            who = QLabel(author_label(meeting.created_by))
            who.setToolTip("Created by")
            who.setStyleSheet("color: palette(mid);")
            lay.addWidget(who, 0)

        if meeting.status:
            st = QLabel(meeting.status)
            color = _daily_status_color(meeting.status)
            if color:
                st.setStyleSheet(f"color: {color};")
            lay.addWidget(st, 0)

        if meeting.alerts_enabled:
            bell = QLabel("\U0001F514")
            bell.setToolTip("Alerts enabled")
            lay.addWidget(bell, 0)
        else:
            paused = QLabel("paused")
            paused.setStyleSheet("color: palette(mid);")
            lay.addWidget(paused, 0)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._page.edit_meeting(self._mid)
        super().mousePressEvent(event)


class MeetingsPage(QWidget):
    """Month-grid calendar of meetings."""

    meetings_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MeetingsPage")
        self._view_mode = "calendar"  # or "list"
        self._tz = self._restore_timezone()
        self._today = self.today_in_tz()
        self._year = self._today.year
        self._month = self._today.month

        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        self._view_combo = QComboBox()
        self._view_combo.addItem("Calendar", "calendar")
        self._view_combo.addItem("List", "list")
        self._view_combo.setToolTip("Switch between calendar and list views")
        self._view_combo.currentIndexChanged.connect(
            lambda _i: self._set_view_mode(self._view_combo.currentData())
        )
        bar.addWidget(self._view_combo)
        bar.addSpacing(8)
        self._prev_btn = QPushButton("‹")
        self._prev_btn.setFixedWidth(36)
        self._prev_btn.clicked.connect(self._go_prev)
        self._next_btn = QPushButton("›")
        self._next_btn.setFixedWidth(36)
        self._next_btn.clicked.connect(self._go_next)
        self._month_label = QLabel()
        ml = QFont()
        ml.setPointSize(13)
        ml.setBold(True)
        self._month_label.setFont(ml)
        self._today_btn = QPushButton("Today")
        self._today_btn.clicked.connect(self._go_today)
        new_btn = QPushButton("New meeting")
        new_btn.clicked.connect(lambda: self.add_meeting_on(self.today_in_tz()))
        import_btn = QPushButton("Import from ICS")
        import_btn.setToolTip("Create meetings from an iCalendar (.ics) file")
        import_btn.clicked.connect(self.import_from_ics)
        bar.addWidget(self._prev_btn)
        bar.addWidget(self._next_btn)
        bar.addSpacing(8)
        bar.addWidget(self._month_label)
        bar.addStretch(1)
        bar.addWidget(self._today_btn)
        bar.addWidget(new_btn)
        bar.addWidget(import_btn)
        root.addLayout(bar)

        # Timezone selector + live clock. All meeting times on the board are
        # shown converted into the selected zone.
        tz_row = QHBoxLayout()
        tz_row.addWidget(QLabel("Timezone"))
        self._tz_combo = QComboBox()
        self._tz_combo.setToolTip(
            "Show every meeting time (and the clock) in this timezone"
        )
        self._tz_combo.setMaxVisibleItems(20)
        for tz_id in sorted(bytes(b).decode() for b in QTimeZone.availableTimeZoneIds()):
            self._tz_combo.addItem(tz_id, tz_id)
        idx = self._tz_combo.findData(bytes(self._tz.id()).decode())
        if idx >= 0:
            self._tz_combo.setCurrentIndex(idx)
        self._tz_combo.currentIndexChanged.connect(self._on_timezone_changed)
        tz_row.addWidget(self._tz_combo)
        self._now_label = QLabel()
        nf = QFont()
        nf.setBold(True)
        self._now_label.setFont(nf)
        self._now_label.setToolTip("Current time in the selected timezone")
        tz_row.addWidget(self._now_label)
        tz_row.addStretch(1)
        root.addLayout(tz_row)
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start()
        self._update_clock()

        self._highlight_meeting_id: Optional[int] = None
        self._highlight_widget: Optional[QWidget] = None

        # Search row: same UX as the other boards — search on Enter or the
        # Search button; clearing the box restores the full calendar.
        search_row = QHBoxLayout()
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText(
            "Search meetings — title, notes, link, date, or id…"
        )
        self._search_input.setClearButtonEnabled(True)
        self._search_input.returnPressed.connect(self._run_search)
        self._search_input.textChanged.connect(
            lambda text: self._run_search() if not text.strip() else None
        )
        search_row.addWidget(self._search_input)
        self._search_btn = QPushButton("Search")
        self._search_btn.clicked.connect(self._run_search)
        search_row.addWidget(self._search_btn)
        # Creator filter — who scheduled the meeting. The options are learned
        # from the meetings themselves, so it stays correct as the team changes.
        search_row.addWidget(QLabel("Creator"))
        self._creator_filter_combo = QComboBox()
        self._creator_filter_combo.setToolTip("Show only meetings created by the selected person")
        self._creator_filter_combo.setMinimumWidth(130)
        self._creator_filter_combo.addItem("All creators", _CREATOR_FILTER_ALL)
        self._restore_creator_filter()
        self._creator_filter_combo.currentIndexChanged.connect(self._on_creator_filter_changed)
        search_row.addWidget(self._creator_filter_combo)
        root.addLayout(search_row)

        # Matches appear as a selectable list; picking one jumps the calendar
        # to that meeting's month and highlights it.
        self._results_list = QListWidget()
        self._results_list.setMaximumHeight(170)
        self._results_list.itemClicked.connect(self._on_result_selected)
        self._results_list.itemActivated.connect(self._on_result_selected)
        self._results_list.hide()
        root.addWidget(self._results_list)

        # Calendar view (weekday header + month grid) and List view share a
        # stack; the View selector switches between them.
        self._calendar_container = QWidget()
        cal_lay = QVBoxLayout(self._calendar_container)
        cal_lay.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        for name in _WEEKDAYS:
            lbl = QLabel(name)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hf = QFont()
            hf.setBold(True)
            lbl.setFont(hf)
            header.addWidget(lbl, 1)
        cal_lay.addLayout(header)
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setSpacing(4)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._grid_host)
        cal_lay.addWidget(self._scroll, 1)

        self._list_container = QWidget()
        list_lay = QVBoxLayout(self._list_container)
        list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_host = QWidget()
        self._list_box = QVBoxLayout(self._list_host)
        self._list_box.setContentsMargins(2, 2, 2, 2)
        self._list_box.setSpacing(3)
        self._list_box.addStretch(1)
        self._list_scroll = QScrollArea()
        self._list_scroll.setWidgetResizable(True)
        self._list_scroll.setWidget(self._list_host)
        list_lay.addWidget(self._list_scroll, 1)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._calendar_container)
        self._stack.addWidget(self._list_container)
        root.addWidget(self._stack, 1)

        # Restore the last-used view (calendar by default) and build it.
        saved = "calendar"
        try:
            val = QSettings().value("meetings_view_mode", "calendar")
            if isinstance(val, str) and val in ("calendar", "list"):
                saved = val
        except Exception:
            saved = "calendar"
        self._view_combo.blockSignals(True)
        self._view_combo.setCurrentIndex(self._view_combo.findData(saved))
        self._view_combo.blockSignals(False)
        self._set_view_mode(saved)

    # --- timezone ---

    @staticmethod
    def _restore_timezone() -> QTimeZone:
        """The saved display timezone, or the machine's local zone."""
        try:
            val = QSettings().value(MEETINGS_TZ_SETTINGS_KEY, "")
            if isinstance(val, str) and val.strip():
                tz = QTimeZone(val.strip().encode())
                if tz.isValid():
                    return tz
        except Exception:
            pass
        return _local_tz()

    def _on_timezone_changed(self, _index: int) -> None:
        tz_id = self._tz_combo.currentData()
        tz = QTimeZone(str(tz_id).encode()) if tz_id else _local_tz()
        if not tz.isValid():
            return
        self._tz = tz
        try:
            QSettings().setValue(MEETINGS_TZ_SETTINGS_KEY, str(tz_id))
        except Exception:
            pass
        self._update_clock()
        self._refresh_current_view()  # re-render every time in the new zone

    def today_in_tz(self) -> date:
        """Today's date in the board's display timezone, not the machine's."""
        d = QDateTime.currentDateTime().toTimeZone(self._tz).date()
        return date(d.year(), d.month(), d.day())

    def display_dt(self, start_at: str) -> QDateTime:
        """A stored meeting time expressed in the board's display timezone."""
        return _to_tz(start_at, self._tz)

    def display_time(self, start_at: str) -> str:
        dt = self.display_dt(start_at)
        return dt.toString("HH:mm") if dt.isValid() else ""

    def display_datetime(self, start_at: str) -> str:
        dt = self.display_dt(start_at)
        return dt.toString("yyyy-MM-dd HH:mm") if dt.isValid() else start_at[:16]

    def _update_clock(self) -> None:
        now = QDateTime.currentDateTime().toTimeZone(self._tz)
        abbrev = self._tz.abbreviation(now)
        self._now_label.setText(
            now.toString("ddd yyyy-MM-dd HH:mm:ss") + (f"  {abbrev}" if abbrev else "")
        )
        d = now.date()
        rolled = date(d.year(), d.month(), d.day())
        if rolled != self._today:  # midnight passed in the display zone
            self._today = rolled
            if self._view_mode == "calendar":
                self._rebuild_calendar()

    # main_window initializes pages via _refresh_table(); keep the same name.
    def _refresh_table(self) -> None:
        self._refresh_current_view()

    def refresh(self) -> None:
        self._refresh_current_view()

    def _refresh_current_view(self) -> None:
        if self._view_mode == "list":
            self._rebuild_list()
        else:
            self._rebuild_calendar()

    # --- creator filter ---

    def _restore_creator_filter(self) -> None:
        """Re-offer the remembered creator; the real list arrives with the data."""
        try:
            saved = QSettings().value("meetings_creator_filter", _CREATOR_FILTER_ALL)
        except Exception:
            return
        if not isinstance(saved, str) or saved == _CREATOR_FILTER_ALL:
            return
        label = (_CREATOR_UNATTRIBUTED_LABEL if saved == _CREATOR_UNATTRIBUTED
                 else (author_label(saved) or saved))
        self._creator_filter_combo.addItem(label, saved)
        self._creator_filter_combo.setCurrentIndex(self._creator_filter_combo.count() - 1)

    def _selected_creator_filter(self) -> Optional[str]:
        data = self._creator_filter_combo.currentData()
        if not isinstance(data, str) or data == _CREATOR_FILTER_ALL:
            return None
        return data

    def _on_creator_filter_changed(self) -> None:
        data = self._creator_filter_combo.currentData()
        try:
            QSettings().setValue(
                "meetings_creator_filter",
                data if isinstance(data, str) else _CREATOR_FILTER_ALL,
            )
        except Exception:
            pass
        if self._view_mode == "calendar" and self._search_input.text().strip():
            self._run_search()   # rebuilds the calendar and the results list
        else:
            self._refresh_current_view()

    def _sync_creator_options(self, meetings: list[db.MeetingRow]) -> None:
        """Offer every creator seen in `meetings`, keeping the current choice.

        The selected creator is kept even when the visible month has none of
        their meetings, so paging through months never drops the filter.
        """
        keys = {_creator_key(m) for m in meetings}
        current = self._creator_filter_combo.currentData()
        if isinstance(current, str) and current != _CREATOR_FILTER_ALL:
            keys.add(current)
        wanted = [(_CREATOR_FILTER_ALL, "All creators")]
        # Keys are account ids; the list is sorted and shown by name.
        wanted += [(k, author_label(k) or k)
                   for k in sorted((k for k in keys if k != _CREATOR_UNATTRIBUTED),
                                   key=lambda k: author_label(k).lower())]
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

    def _apply_creator_filter(self, meetings: list[db.MeetingRow]) -> list[db.MeetingRow]:
        """Refresh the creator options from `meetings`, then filter them."""
        self._sync_creator_options(meetings)
        wanted = self._selected_creator_filter()
        if wanted is None:
            return meetings
        return [m for m in meetings if _creator_key(m) == wanted]

    def _set_view_mode(self, mode: str) -> None:
        self._view_mode = mode if mode in ("calendar", "list") else "calendar"
        is_cal = self._view_mode == "calendar"
        # Month navigation only makes sense in calendar view.
        for w in (self._prev_btn, self._next_btn, self._today_btn, self._month_label):
            w.setVisible(is_cal)
        self._stack.setCurrentWidget(
            self._calendar_container if is_cal else self._list_container
        )
        if is_cal:
            self._rebuild_calendar()
            if self._search_input.text().strip():
                self._run_search()  # repopulate the results list for the query
        else:
            self._results_list.hide()  # in list view the list itself is filtered
            self._rebuild_list()
        try:
            QSettings().setValue("meetings_view_mode", self._view_mode)
        except Exception:
            pass

    def _go_prev(self) -> None:
        self._month -= 1
        if self._month < 1:
            self._month = 12
            self._year -= 1
        self._rebuild_calendar()

    def _go_next(self) -> None:
        self._month += 1
        if self._month > 12:
            self._month = 1
            self._year += 1
        self._rebuild_calendar()

    def _go_today(self) -> None:
        self._today = self.today_in_tz()
        self._year, self._month = self._today.year, self._today.month
        self._rebuild_calendar()

    def _clear_grid(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _rebuild_calendar(self) -> None:
        self._clear_grid()
        self._highlight_widget = None
        self._month_label.setText(
            date(self._year, self._month, 1).strftime("%B %Y")
        )
        cal = calendar.Calendar(firstweekday=6)  # Sunday
        weeks = cal.monthdatescalendar(self._year, self._month)

        first_day = weeks[0][0]
        last_day = weeks[-1][-1]
        # Query a day wider on each side: converting to the display timezone can
        # shift a meeting onto the neighbouring day.
        start_iso = f"{(first_day - timedelta(days=1)).isoformat()} 00:00:00"
        end_iso = f"{(last_day + timedelta(days=2)).isoformat()} 00:00:00"
        try:
            meetings = _store().list_meetings_between(start_iso, end_iso)
        except Exception:
            meetings = []
        meetings = self._apply_creator_filter(meetings)
        by_day: dict[str, list[db.MeetingRow]] = {}
        for m in meetings:
            shown = self.display_dt(m.start_at)  # group by the displayed day
            key = shown.date().toString("yyyy-MM-dd") if shown.isValid() else m.start_at[:10]
            by_day.setdefault(key, []).append(m)
        for items in by_day.values():  # keep each day's chips in time order
            items.sort(key=lambda mm: mm.start_at)

        self._today = self.today_in_tz()  # "today" is whatever the display zone says
        for r, week in enumerate(weeks):
            for c, day in enumerate(week):
                cell = _DayCell(
                    self,
                    day,
                    in_month=(day.month == self._month),
                    is_today=(day == self._today),
                )
                for m in by_day.get(day.isoformat(), []):
                    cell.add_meeting_entry(m)
                self._grid.addWidget(cell, r, c)
        for c in range(7):
            self._grid.setColumnStretch(c, 1)

        if self._highlight_widget is not None:
            self._scroll.ensureWidgetVisible(self._highlight_widget)

    def _rebuild_list(self) -> None:
        while self._list_box.count():
            item = self._list_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        needle = normalize_search(self._search_input.text())
        try:
            meetings = _store().list_meetings()
        except Exception:
            meetings = []
        creator_on = self._selected_creator_filter() is not None
        meetings = self._apply_creator_filter(meetings)
        if needle:
            meetings = [m for m in meetings if self._meeting_matches(m, needle)]
        if not meetings:
            empty = QLabel(
                "No meetings match your search." if (needle or creator_on)
                else "No meetings yet."
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._list_box.addWidget(empty)
        else:
            for m in meetings:
                self._list_box.addWidget(_MeetingListRow(self, m))
        self._list_box.addStretch(1)

    # --- search ---

    def _meeting_matches(self, m: db.MeetingRow, needle: str) -> bool:
        return fields_match_search(needle, m.title, m.body, m.link, m.start_at, str(m.id))

    def _run_search(self) -> None:
        if self._view_mode == "list":
            # In list view the search filters the list in place.
            self._results_list.hide()
            self._rebuild_list()
            return
        needle = normalize_search(self._search_input.text())
        if not needle:
            self._results_list.clear()
            self._results_list.hide()
            self._highlight_meeting_id = None
            self._rebuild_calendar()
            return
        try:
            meetings = _store().list_meetings()
        except Exception:
            meetings = []
        meetings = self._apply_creator_filter(meetings)
        matches = [m for m in meetings if self._meeting_matches(m, needle)]
        self._results_list.clear()
        if not matches:
            item = QListWidgetItem("No meetings found")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._results_list.addItem(item)
        else:
            for m in matches:
                label = f"{self.display_datetime(m.start_at)}  ·  {m.title or '(untitled)'}"
                if not m.alerts_enabled:
                    label += "   (alerts paused)"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, m.id)
                self._results_list.addItem(item)
        self._results_list.show()

    def _on_result_selected(self, item: QListWidgetItem) -> None:
        mid = item.data(Qt.ItemDataRole.UserRole)
        if mid is None:
            return
        meeting = _store().get_meeting(int(mid))
        if meeting is None:
            self._run_search()  # stale list; refresh it
            return
        # Results always reveal the meeting on the calendar.
        if self._view_mode != "calendar":
            self._view_combo.setCurrentIndex(self._view_combo.findData("calendar"))
        dt = _parse_dt(meeting.start_at)
        if dt is not None:
            self._year, self._month = dt.year, dt.month
        self._highlight_meeting_id = int(mid)
        self._rebuild_calendar()  # scrolls to the highlighted chip

    # --- actions ---

    def import_from_ics(self) -> None:
        """Create meetings from every dated event in a chosen .ics file."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Import meetings from ICS", "",
            "iCalendar files (*.ics);;All files (*.*)",
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            QMessageBox.critical(self, "Import from ICS", f"Could not read the file:\n{exc}")
            return
        events = ics_import.parse_ics_events(text)
        if not events:
            QMessageBox.information(
                self, "Import from ICS",
                "No events with a start time were found in that file.",
            )
            return
        for ev in events:
            # Default alert config, same as a new meeting: alerts on, 15-min lead,
            # every 5 minutes. Edit any imported meeting to change it.
            _store().add_meeting(ev.title, ev.body, ev.link, ev.start_at, 15, 5, True, "")
        last = self.display_dt(events[-1].start_at)  # month as displayed
        if last.isValid():
            self._year, self._month = last.date().year(), last.date().month()
        self._refresh_after_change()
        QMessageBox.information(
            self, "Import from ICS",
            f"Imported {len(events)} meeting(s) from the ICS file.",
        )

    def add_meeting_on(self, day: date) -> None:
        dlg = MeetingEditorDialog(self, meeting=None, default_day=day, tz=self._tz)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        _store().add_meeting(
            v["title"], v["body"], v["link"], v["start_at"],
            v["lead"], v["interval"], v["alerts_enabled"], v["status"],
            v["linked_card_id"],
        )
        self._refresh_after_change()

    def edit_meeting(self, meeting_id: int) -> None:
        meeting = _store().get_meeting(meeting_id)
        if meeting is None:
            self._refresh_current_view()
            return
        dlg = MeetingEditorDialog(self, meeting=meeting, tz=self._tz)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            if dlg.was_deleted():
                _store().delete_meeting(meeting_id)
            else:
                v = dlg.values()
                _store().update_meeting(
                    meeting_id, v["title"], v["body"], v["link"], v["start_at"],
                    v["lead"], v["interval"], v["alerts_enabled"], v["status"],
                    v["linked_card_id"],
                )
        except PermissionDenied as exc:
            QMessageBox.warning(self, "Meeting not changed", str(exc))
            return
        self._refresh_after_change()

    def _refresh_after_change(self) -> None:
        if self._view_mode == "list":
            self._rebuild_list()
        else:
            self._rebuild_calendar()
            if self._search_input.text().strip():
                self._run_search()  # keep the visible results list in sync
        self.meetings_changed.emit()

    def shutdown(self) -> None:
        self._clock_timer.stop()
