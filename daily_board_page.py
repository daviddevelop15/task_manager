"""Daily Kanban board tab (simple Trello-style columns per calendar day)."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html import escape, unescape
from typing import Literal

from PyQt6.QtCore import (
    QByteArray,
    QDate,
    QDateTime,
    QEvent,
    QObject,
    QPointF,
    QRectF,
    QSettings,
    QSize,
    Qt,
    QTime,
    QTimer,
    QThread,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QDrag,
    QFont,
    QFontInfo,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QShortcut,
    QBrush,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextListFormat,
    QWheelEvent,
)
from PyQt6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QDateEdit,
    QDateTimeEdit,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsBlurEffect,
    QColorDialog,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QMessageBox,
    QPushButton,
    QScrollBar,
    QTextEdit,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

import ai_assistant
import app_variant
import database as db
from app_store import PermissionDenied, author_label, get_store as _store
from PyQt6 import sip
from app_resources import app_icon
from constants import DAILY_MIME_CARD, DAILY_MIME_LIST
from task_helpers import fields_match_search, normalize_search
from window_utils import show_existing_window

_DAILY_ICON_BTN_PX = 26
_DAILY_ICON_PIX_PX = 16
_DAILY_ICON_BTN_GAP_PX = 2

# Fixed column width: every Kanban column is this wide so the board never has to
# recompute / stretch column widths on relayout (only vertical reflow remains).
_DAILY_COLUMN_WIDTH_PX = 220

# Columns whose title starts with this prefix are job-bid columns; the board's
# "Job stats" button aggregates job-site counts across all of them.
_JOB_BID_COLUMN_PREFIX = "JobBid"

_DAILY_FONT_SIZES = ("9", "10", "11", "12", "14", "16", "18", "20", "24")
_DAILY_EMOJIS = (
    "😀",
    "😊",
    "😂",
    "🙂",
    "😉",
    "👍",
    "👎",
    "🙏",
    "❤️",
    "🎉",
    "✅",
    "❌",
    "⭐",
    "🔥",
    "💡",
    "📌",
    "⚠️",
    "📅",
    "📝",
    "✏️",
)
_CHECKBOX_UNCHECKED = "☐"
_CHECKBOX_CHECKED = "☑"

_DAILY_TEXT_COLORS: tuple[tuple[str, str | None], ...] = (
    ("Default", None),
    ("White", "#F5F5F5"),
    ("Red", "#EF5350"),
    ("Orange", "#FF9800"),
    ("Yellow", "#FFEE58"),
    ("Green", "#66BB6A"),
    ("Blue", "#42A5F5"),
    ("Purple", "#AB47BC"),
    ("Gray", "#9E9E9E"),
)
_DAILY_HIGHLIGHT_COLORS: tuple[tuple[str, str | None], ...] = (
    ("Remove highlight", None),
    ("Yellow", "#FFF59D"),
    ("Green", "#C8E6C9"),
    ("Blue", "#BBDEFB"),
    ("Pink", "#F8BBD0"),
    ("Orange", "#FFE0B2"),
)

_DAILY_EMOJIS = (
    "😀",
    "😊",
    "😂",
    "🙂",
    "😉",
    "😍",
    "🤔",
    "😅",
    "👍",
    "👎",
    "🙏",
    "👏",
    "💪",
    "✅",
    "❌",
    "⭐",
    "🔥",
    "💡",
    "📌",
    "📅",
    "⏰",
    "🎉",
    "❤️",
    "⚠️",
)

_DAILY_FONT_SIZES = ("10", "11", "12", "13", "14", "16", "18", "20", "24")

_SETTINGS_ORG = app_variant.SETTINGS_ORG
_SETTINGS_APP = app_variant.SETTINGS_APP
_DAILY_CARD_EDITOR_GEOMETRY_KEY = "daily_card_editor_geometry"
_DAILY_CARD_BODY_HIDDEN_PREFIX = "daily_card_body_hidden"
_DAILY_AI_CHAR_LIMIT_KEY = "daily_ai_char_limit"
_DAILY_AI_CHAR_LIMIT_DEFAULT = 500


def _restored_ai_char_limit() -> int:
    """The AI answer character limit the user last chose, or the default."""
    raw = QSettings(_SETTINGS_ORG, _SETTINGS_APP).value(
        _DAILY_AI_CHAR_LIMIT_KEY, _DAILY_AI_CHAR_LIMIT_DEFAULT
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DAILY_AI_CHAR_LIMIT_DEFAULT
    return value if 0 <= value <= 10000 else _DAILY_AI_CHAR_LIMIT_DEFAULT


def _remember_ai_char_limit(value: int) -> None:
    QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue(
        _DAILY_AI_CHAR_LIMIT_KEY, int(value)
    )
_JOB_BIDS_COLUMN_KEY = "job_bids"
_JOB_BIDS_TITLE = "Job bids"
_OPENAI_API_KEY_SETTINGS_KEY = "openai_api_key"
_OPENAI_RESUME_MODEL = "gpt-4o-mini"

# Card status options shown at the card's top-right corner. The order matches the
# selection menu; each colour is chosen to reflect the meaning of the status.
_DAILY_STATUS_NONE = ""
# Sentinel for the board-level status filter meaning "don't filter by status".
_DAILY_STATUS_FILTER_ALL = "__all__"
_DAILY_STATUS_COLORS: dict[str, str] = {
    "Active": "#43A047",        # green — currently active / healthy
    "Deactive": "#757575",      # gray — paused / inactive
    "Closed": "#455A64",        # slate — closed / archived
    "Initial": "#1E88E5",       # blue — fresh / starting point
    "Waiting": "#B8860B",       # dark yellow — awaiting a response
    "Deselected": "#C62828",    # red — rejected / dropped
    "Selected": "#00897B",      # teal — chosen / positive
    "1st-Meet": "#AB47BC",      # purple ramp for the meeting funnel…
    "2nd-Meet": "#8E24AA",
    "3rd-Meet": "#6A1B9A",
    "Tech-Meet": "#3949AB",     # indigo — technical round
    "Final-Meet": "#283593",    # deep indigo — final round
    "On-Work": "#FB8C00",       # orange — actively working
    "Finished-Job": "#2E7D32",  # strong green — completed successfully
}
_DAILY_STATUS_ORDER: tuple[str, ...] = (
    "Active",
    "Deactive",
    "Closed",
    "Initial",
    "Waiting",
    "Deselected",
    "Selected",
    "1st-Meet",
    "2nd-Meet",
    "3rd-Meet",
    "Tech-Meet",
    "Final-Meet",
    "On-Work",
    "Finished-Job",
)


def _daily_status_color(status: str) -> str | None:
    return _DAILY_STATUS_COLORS.get(status)


def _daily_status_swatch(color: str) -> QIcon:
    """Small rounded colour chip used as a menu icon for each status."""
    px = 12
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawRoundedRect(0, 0, px, px, 3, 3)
    p.end()
    return QIcon(pm)


@dataclass(frozen=True)
class _DailyThemeColors:
    window: str
    panel: str
    card: str
    border: str
    text: str
    muted: str
    highlight: str


def _daily_theme_colors() -> _DailyThemeColors:
    """Pick dark-friendly surfaces; Qt AlternateBase/Mid are often light on Windows dark mode."""
    pal = QApplication.palette()
    window = pal.color(QPalette.ColorRole.Window)
    base = pal.color(QPalette.ColorRole.Base)
    text = pal.color(QPalette.ColorRole.Text)
    highlight = pal.color(QPalette.ColorRole.Highlight)
    dark_app = window.lightness() < 128

    if dark_app:
        panel = base if base.lightness() < 72 else window.darker(108)
        if panel.lightness() > 72:
            panel = QColor(37, 37, 38)
        card = panel.lighter(108)
        if card.lightness() <= panel.lightness():
            card = panel.lighter(112)
        border = window.darker(115)
        if border.lightness() > 72:
            border = QColor(62, 62, 66)
        fg = text if text.lightness() > 168 else QColor(232, 232, 232)
        muted = QColor(158, 158, 158)
    else:
        panel = pal.color(QPalette.ColorRole.AlternateBase)
        card = base
        border = pal.color(QPalette.ColorRole.Mid)
        fg = text
        muted = pal.color(QPalette.ColorRole.PlaceholderText)
        if muted.lightness() > 200 or muted == fg:
            muted = QColor(96, 96, 96)

    return _DailyThemeColors(
        window=window.name(),
        panel=panel.name(),
        card=card.name(),
        border=border.name(),
        text=fg.name(),
        muted=muted.name(),
        highlight=highlight.name(),
    )


def _daily_board_stylesheet() -> str:
    c = _daily_theme_colors()
    return (
        f"QWidget#DailyBoardPage, QWidget#dailyBoardHost {{ background-color: {c.window}; "
        f"color: {c.text}; }}"
        f"QScrollArea#dailyBoardScroll {{ background-color: {c.window}; border: none; }}"
        f"QScrollArea#dailyBoardScroll > QWidget > QWidget {{ background-color: {c.window}; }}"
        f"QWidget#dailyColumnHeader {{ background-color: {c.panel}; }}"
        f"QFrame#DailyColumnWidget {{ background-color: {c.panel}; border: 1px solid {c.border}; "
        f"border-radius: 8px; }}"
        f"QFrame#DailyCardWidget {{ background-color: {c.card}; border: 1px solid {c.border}; "
        f"border-radius: 6px; }}"
        f"QFrame#DailyCardWidget:hover {{ border-color: {c.highlight}; }}"
        f"QFrame#dailyAddColumn {{ background-color: transparent; border: 1px dashed {c.border}; "
        f"border-radius: 8px; }}"
        f"QLabel#dailyColumnTitle {{ font-weight: bold; font-size: 13px; color: {c.text}; "
        f"background-color: transparent; padding: 2px 0px; }}"
        f"QWidget#dailyColumnTitleHost {{ background-color: transparent; }}"
        f"QLineEdit#dailyColumnTitleEdit {{ font-weight: bold; font-size: 13px; color: {c.text}; "
        f"background-color: {c.card}; border: 1px solid {c.highlight}; border-radius: 4px; "
        f"padding: 2px 6px; }}"
        f"QLabel#dailyCardTitle {{ color: {c.text}; background: transparent; "
        f"font-weight: bold; }}"
        f"QLabel#dailyCardPreview {{ color: {c.muted}; background: transparent; font-size: 11px; }}"
        f"QLabel#dailyCardDate {{ color: {c.muted}; background: transparent; font-size: 10px; }}"
        f"QToolButton#dailyIconBtn, QWidget#DailyBoardPage QToolButton#dailyIconBtn {{ "
        f"background-color: transparent; border: none; outline: none; border-radius: 0px; "
        f"padding: 0px; margin: 0px; "
        f"min-width: {_DAILY_ICON_BTN_PX}px; max-width: {_DAILY_ICON_BTN_PX}px; "
        f"min-height: {_DAILY_ICON_BTN_PX}px; max-height: {_DAILY_ICON_BTN_PX}px; }}"
        f"QToolButton#dailyIconBtn:hover, QWidget#DailyBoardPage QToolButton#dailyIconBtn:hover {{ "
        f"background-color: transparent; border: none; outline: none; }}"
        f"QToolButton#dailyIconBtn:pressed, QWidget#DailyBoardPage QToolButton#dailyIconBtn:pressed {{ "
        f"background-color: transparent; border: none; outline: none; }}"
        f"QToolButton#dailyIconBtn:focus, QWidget#DailyBoardPage QToolButton#dailyIconBtn:focus {{ "
        f"border: none; outline: none; }}"
        f"QPushButton#dailyAddColumnBtn {{ color: {c.text}; background-color: {c.card}; "
        f"border: 1px solid {c.border}; border-radius: 6px; padding: 8px 12px; }}"
        f"QPushButton#dailyAddColumnBtn:hover {{ border-color: {c.highlight}; "
        f"background-color: {c.panel}; }}"
        f"QWidget#DailyBoardPage QCheckBox {{ color: {c.text}; }}"
        f"QWidget#DailyBoardPage QLabel {{ color: {c.text}; }}"
        f"QWidget#DailyBoardPage QDateEdit, QWidget#DailyBoardPage QDateTimeEdit {{ "
        f"color: {c.text}; background-color: {c.card}; border: 1px solid {c.border}; "
        f"border-radius: 3px; padding: 2px 4px; }}"
        f"QWidget#DailyBoardPage QPushButton:not(#dailyIconBtn) {{ color: {c.text}; "
        f"background-color: {c.card}; border: 1px solid {c.border}; border-radius: 4px; "
        f"padding: 4px 10px; }}"
        f"QWidget#DailyBoardPage QPushButton:not(#dailyIconBtn):hover {{ "
        f"border-color: {c.highlight}; }}"
    )


# A board "day" does not run midnight-to-midnight: day D spans
# [D-1 17:00, D 17:00) in local time (e.g. day Jun 16 = Jun 15 5:00 PM .. Jun 16
# 5:00 PM). created_at is stored in local time, so the cutoff is a local hour.
_DAY_CUTOFF_HOUR = 17  # 5:00 PM


def _business_today() -> QDate:
    """The board day whose [prev 17:00, this 17:00) window contains 'now'."""
    now = QDateTime.currentDateTime()
    d = now.date()
    return d.addDays(1) if now.time().hour() >= _DAY_CUTOFF_HOUR else d


def _day_window_bounds(day: QDate) -> tuple[str, str]:
    """Inclusive created_at bounds (yyyy-MM-dd HH:mm:ss) for board day ``day`` =
    [day-1 17:00:00, day 16:59:59]  (i.e. up to, but not including, day 17:00)."""
    fmt = "yyyy-MM-dd HH:mm:ss"
    start = QDateTime(day.addDays(-1), QTime(_DAY_CUTOFF_HOUR, 0, 0))
    end = QDateTime(day, QTime(_DAY_CUTOFF_HOUR, 0, 0)).addSecs(-1)
    return start.toString(fmt), end.toString(fmt)


def _configure_board_datetime_editor(w: QDateTimeEdit) -> None:
    w.setCalendarPopup(True)
    w.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
    w.setMinimumWidth(200)
    w.setTimeSpec(Qt.TimeSpec.LocalTime)


def _default_period_range() -> tuple[QDateTime, QDateTime]:
    now = QDateTime.currentDateTime()
    d = now.date()
    start = QDateTime(d, QTime(0, 0, 0), Qt.TimeSpec.LocalTime)
    end = QDateTime(d, QTime(23, 59, 59), Qt.TimeSpec.LocalTime)
    return start, end


def _looks_like_html(body: str) -> bool:
    t = (body or "").lstrip()
    return bool(t) and t[0] == "<"


def _plain_text_from_daily_body(body: str) -> str:
    if not (body or "").strip():
        return ""
    if _looks_like_html(body):
        doc = QTextDocument()
        doc.setHtml(body)
        return doc.toPlainText()
    return body


def _first_line_title(text: str) -> str:
    """First non-empty line of plain text, capped for use as a card title."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:120]
    return ""


def _soft_wrap_long_tokens(text: str, chunk_size: int = 28) -> str:
    """Allow QLabel word-wrap to break long URLs/keys without widening the card."""
    if not text:
        return ""
    parts: list[str] = []
    for word in text.split(" "):
        if len(word) <= chunk_size:
            parts.append(word)
            continue
        chunks = [word[i : i + chunk_size] for i in range(0, len(word), chunk_size)]
        parts.append("\u200b".join(chunks))
    return " ".join(parts)


def _body_preview(body: str, max_len: int = 80) -> str:
    line = " ".join(_plain_text_from_daily_body(body).replace("\r\n", "\n").split())
    if not line:
        return ""
    if len(line) <= max_len:
        return _soft_wrap_long_tokens(line)
    return _soft_wrap_long_tokens(line[: max_len - 1].rstrip() + "…")


def _format_created(created_at: str) -> str:
    s = (created_at or "").strip()
    if len(s) >= 10:
        return s[:10]
    return s or "—"


# --- Excel/TSV export of job-bid cards --------------------------------------
# Row layout: Applier, Site, Company, Role, Link, Description.
# Applier is the name set in Settings > App settings; Site is the job-site prefix
# taken from the card title.
EXCEL_APPLIER_SETTINGS_KEY = "excel_applier"
DEFAULT_EXCEL_APPLIER = "Sebastian"


def _excel_applier() -> str:
    """The applier name used as the first Excel column (from app settings)."""
    val = QSettings(_SETTINGS_ORG, _SETTINGS_APP).value(
        EXCEL_APPLIER_SETTINGS_KEY, DEFAULT_EXCEL_APPLIER
    )
    return val.strip() if isinstance(val, str) and val.strip() else DEFAULT_EXCEL_APPLIER


def _excel_cell(value: str) -> str:
    """Quote a value as a tab-separated clipboard cell Excel/Sheets accept."""
    v = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    if any(ch in v for ch in ("\t", "\n", '"')):
        v = '"' + v.replace('"', '""') + '"'
    return v


def _excel_field_from_text(body_text: str, label: str) -> str:
    """Value after a 'Label:' line in the body text (first match, any case)."""
    pattern = re.compile(rf"^\s*{re.escape(label)}\s*:\s*(.*)$", re.IGNORECASE)
    for line in (body_text or "").splitlines():
        m = pattern.match(line)
        if m:
            return m.group(1).strip()
    return ""


def _excel_role_from_title(title: str) -> str:
    """Card title with a leading 'Site - ' source prefix removed."""
    title = (title or "").strip()
    m = re.match(r"^.+?\s[-–—]\s+(.+)$", title)
    return m.group(1).strip() if m else title


def _excel_description_from_text(body_text: str) -> str:
    """Full body text minus the labeled metadata lines and section markers."""
    drop = re.compile(
        r"^\s*(URL|Company Link|Company|Location|Role|Salary|Compensation|Pay|"
        r"Employment type|Job type|Contact|Recruiter)\s*:",
        re.IGNORECASE,
    )
    marker = re.compile(r"^\s*-{2,}.*-{2,}\s*$")  # e.g. '--- Job Description ---'
    kept = [
        ln
        for ln in (body_text or "").splitlines()
        if not drop.match(ln) and not marker.match(ln)
    ]
    return "\n".join(kept).strip()


def _domain_from_url(url: str) -> str:
    """Bare host (no scheme, port, or leading 'www.') from a job link URL."""
    url = (url or "").strip()
    if not url:
        return ""
    # urlparse only fills netloc/hostname when a scheme is present.
    if "://" not in url:
        url = "http://" + url
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


# Two-label public suffixes treated as one TLD so the registrable name is taken
# correctly (e.g. linkedin.co.uk -> linkedin, not co).
_COMPOUND_TLDS = frozenset(
    {
        "co.uk", "com.au", "co.jp", "co.in", "co.nz", "com.br", "co.za",
        "com.mx", "co.kr", "com.sg", "com.hk", "co.il", "com.tr", "com.cn",
        "co.id", "com.ph", "com.my", "co.th", "com.ar", "com.co",
    }
)


def _site_name_from_url(url: str) -> str:
    """The core site name: domain minus any subdomain and the TLD suffix.

    e.g. https://careers.google.com/... -> 'google', jobs.lever.co -> 'lever',
    linkedin.co.uk -> 'linkedin'."""
    host = _domain_from_url(url)
    if not host:
        return ""
    labels = host.split(".")
    if len(labels) < 2:
        return host
    # Drop a known two-part suffix, else the single TLD label; the last remaining
    # label is the registrable name, which also discards every subdomain.
    if len(labels) >= 3 and ".".join(labels[-2:]) in _COMPOUND_TLDS:
        core = labels[:-2]
    else:
        core = labels[:-1]
    return core[-1] if core else host


# --- Duplicate-application detection ----------------------------------------
# Each daily card is one applied job. Before applying again, the card editor
# surfaces earlier applications that share the same company, role, or link so
# the user can spot a duplicate. Matching runs against an in-memory index built
# once per editor session (see AppliedJobIndex), so it stays cheap as the board
# grows.

# Legal-form and filler tokens dropped when comparing company names.
_COMPANY_NOISE = frozenset(
    {
        "inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
        "gmbh", "srl", "sa", "plc", "pvt", "llp", "the", "group", "holdings",
        "technologies", "technology", "labs", "software", "solutions",
    }
)

def _index_plain_text(body: str) -> str:
    """Plain-ish text from a card body for field extraction, without building a
    QTextDocument per card (a regex tag-strip is thread-safe and far cheaper)."""
    b = body or ""
    if not b.strip():
        return ""
    if not _looks_like_html(b):
        return b
    b = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", b)
    b = re.sub(r"(?is)<br\s*/?>", "\n", b)
    b = re.sub(r"(?is)</(p|div|li|tr|h[1-6])>", "\n", b)
    b = re.sub(r"(?s)<[^>]+>", " ", b)
    return unescape(b)


def _norm_company(name: str) -> str:
    """Company name reduced to comparable tokens (lowercase, no punctuation,
    legal-form/filler words removed)."""
    s = re.sub(r"[^\w\s]", " ", (name or "").lower())
    toks = [t for t in s.split() if t and t not in _COMPANY_NOISE]
    return " ".join(toks)


def _norm_role(role: str) -> str:
    """Role title reduced to comparable tokens (lowercase, no punctuation)."""
    return " ".join(re.sub(r"[^\w\s]", " ", (role or "").lower()).split())


def _title_overlap(a: set[str], b: set[str]) -> float:
    """Overlap coefficient of two title token sets.

    Divides by the *shorter* set, so a seniority prefix or an extra qualifier
    ('Senior Backend Developer' vs 'Backend Engineer') still reads as
    comparable. Only used to rank within the 'similar' tier — never to call two
    postings the same job."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


@dataclass(frozen=True)
class AppliedJob:
    """One previously-applied card. Company and title are normalized for
    matching; the link is carried for display only, never compared."""

    card_id: int
    company: str
    role: str
    link: str
    created_at: str
    status: str
    n_company: str
    n_role: str


def _parse_applied_job(card: db.DailyCardRow) -> AppliedJob:
    text = _index_plain_text(card.body)
    company = _excel_field_from_text(text, "Company")
    role = _excel_role_from_title(card.title)
    link = _excel_field_from_text(text, "URL") or _excel_field_from_text(
        text, "Company Link"
    )
    return AppliedJob(
        card_id=card.id,
        company=company,
        role=role,
        link=link,
        created_at=card.created_at or "",
        status=card.status or "",
        n_company=_norm_company(company),
        n_role=_norm_role(role),
    )


def applied_index_row(card: db.DailyCardRow) -> tuple | None:
    """One card reduced to the fields duplicate-matching needs, or None.

    A plain tuple of strings, because this crosses the wire: the store builds
    these where the rows live so a client never has to download the card bodies
    they were parsed from. Cards with no company can never match, so they are
    dropped here rather than shipped and discarded.
    """
    job = _parse_applied_job(card)
    if not job.n_company:
        return None
    return (job.card_id, job.company, job.role, job.link,
            job.created_at, job.status, job.n_company, job.n_role)


def _applied_job_from_row(row) -> AppliedJob:
    return AppliedJob(
        card_id=int(row[0]), company=row[1], role=row[2], link=row[3],
        created_at=row[4], status=row[5], n_company=row[6], n_role=row[7],
    )


def build_applied_job_index(exclude_card_id: int | None = None) -> list[AppliedJob]:
    """Every daily card as a matchable index, skipping the card being edited.

    Asks the store for the pre-reduced index when it offers one - on a client
    that is a few kilobytes over the LAN instead of every job description in the
    database, which is what used to freeze the window for seconds after making a
    card. Older stores (and any that lack it) still get parsed the long way.
    """
    store = _store()
    build_rows = getattr(store, "list_applied_job_index", None)
    if build_rows is not None:
        try:
            rows = build_rows()
        except Exception:            # noqa: BLE001 - fall back rather than fail
            rows = None
        if rows is not None:
            return [
                _applied_job_from_row(r) for r in rows
                if exclude_card_id is None or int(r[0]) != exclude_card_id
            ]
    jobs: list[AppliedJob] = []
    for card in store.list_all_daily_cards():
        if exclude_card_id is not None and card.id == exclude_card_id:
            continue
        job = _parse_applied_job(card)
        if job.n_company:
            jobs.append(job)
    return jobs


# Match tiers, strongest first.
MATCH_SAME_JOB = 3      # same company and same title — a real duplicate
MATCH_SIMILAR_JOB = 2   # same company, comparable title
MATCH_SAME_COMPANY = 1  # same company, unrelated title


@dataclass(frozen=True)
class SimilarJob:
    job: AppliedJob
    score: int  # one of the MATCH_* tiers above
    reason: str
    same_job: bool  # a definite duplicate, not merely a resemblance


def find_similar_applied_jobs(
    company: str,
    role: str,
    index: list[AppliedJob],
    limit: int = 10,
) -> list[SimilarJob]:
    """Earlier applications at the same company, strongest match first.

    The company must match for anything to be reported; the title then decides
    the tier: identical means the **same job**, comparable means a **similar
    job**, anything else is simply another application at the **same company**.
    The same title at a *different* company is not reported at all — a shared
    job title says nothing about the posting. The link is never consulted: job
    sites that open each posting in a modal keep one address for many unrelated
    jobs, so it cannot tell postings apart."""
    nc, nr = _norm_company(company), _norm_role(role)
    if not nc:  # no company -> nothing can match
        return []
    role_toks = set(nr.split())
    out: list[SimilarJob] = []
    for job in index:
        if not job.n_company or job.n_company != nc:
            continue
        if nr and job.n_role and nr == job.n_role:
            score, reason, same = MATCH_SAME_JOB, "Same job — same company and title", True
        elif nr and job.n_role and _title_overlap(role_toks, set(job.n_role.split())) >= 0.5:
            score, reason, same = (
                MATCH_SIMILAR_JOB,
                "Similar job — same company, comparable title",
                False,
            )
        else:
            score, reason, same = MATCH_SAME_COMPANY, "Same company", False
        out.append(SimilarJob(job=job, score=score, reason=reason, same_job=same))
    out.sort(key=lambda s: (s.score, s.job.created_at), reverse=True)
    return out[:limit]


def _similar_job_item(match: SimilarJob) -> QListWidgetItem:
    """A compact one-line list entry for a related past application."""
    job = match.job
    marker = {
        MATCH_SAME_JOB: "⛔",
        MATCH_SIMILAR_JOB: "●",
        MATCH_SAME_COMPANY: "🏢",
    }.get(match.score, "○")
    company = job.company or "—"
    role = job.role or "—"
    date = (job.created_at or "")[:10] or "—"
    status = job.status or "no status"
    # Date first, because "when did I last apply here?" is the question this
    # list exists to answer. Company and role are long enough to push anything
    # after them off the right-hand edge, where it can only be read by
    # scrolling - which is no use for a list you glance at.
    item = QListWidgetItem(f"{marker}  {date}   ·   {company} — {role}   ·   {status}")
    item.setData(Qt.ItemDataRole.UserRole, job.card_id)  # clicking opens this card
    tip_lines = [f"{match.reason} · applied {date} · {status}"]
    if job.link:
        tip_lines.append(job.link)
    tip_lines.append("Click to open this application")
    item.setToolTip("\n".join(tip_lines))
    if match.same_job:
        f = item.font()
        f.setBold(True)  # duplicates read at a glance
        item.setFont(f)
    return item


def _excel_row_for_card(title: str, body: str) -> str:
    """One tab-separated row for a stored card.

    Columns: Applier, Site, Company, Role, Link, Description. Applier comes from
    app settings and Site is the core name of the job link's domain."""
    text = _plain_text_from_daily_body(body)
    company = _excel_field_from_text(text, "Company")
    role = _excel_role_from_title(title)
    link = _excel_field_from_text(text, "URL") or _excel_field_from_text(text, "Company Link")
    site = _site_name_from_url(link)
    description = _excel_description_from_text(text)
    return "\t".join(
        _excel_cell(v)
        for v in (_excel_applier(), site, company, role, link, description)
    )


def _job_site_from_title(title: str) -> str:
    """The job-site prefix before the first ' - ' separator in a card title."""
    title = (title or "").strip()
    if not title:
        return "(no title)"
    m = re.match(r"^(.+?)\s[-–—]\s+.+$", title)
    return m.group(1).strip() if m else "(no site)"


def _safe_filename(name: str, fallback: str = "card") -> str:
    """Turn a card title into a filesystem-safe base name (no extension)."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned[:80].strip() or fallback)


def _wrap_inline(text: str, mark: str) -> str:
    """Wrap with a Markdown marker, keeping surrounding whitespace outside it."""
    stripped = text.strip()
    if not stripped:
        return text
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()) :]
    return f"{lead}{mark}{stripped}{mark}{trail}"


def _fragment_markdown(fragment) -> str:  # type: ignore[no-untyped-def]
    fmt = fragment.charFormat()
    text = fragment.text().replace(" ", "  \n")
    if not text:
        return ""
    if fmt.fontStrikeOut():
        text = _wrap_inline(text, "~~")
    if fmt.fontItalic():
        text = _wrap_inline(text, "*")
    if fmt.fontWeight() >= QFont.Weight.Bold:
        text = _wrap_inline(text, "**")
    if fmt.isAnchor() and fmt.anchorHref():
        text = f"[{text.strip()}]({fmt.anchorHref()})"
    return text


def _block_inline_markdown(block) -> str:  # type: ignore[no-untyped-def]
    parts: list[str] = []
    it = block.begin()
    while not it.atEnd():
        frag = it.fragment()
        if frag.isValid():
            parts.append(_fragment_markdown(frag))
        it += 1
    text = "".join(parts)
    if text.startswith(f"{_CHECKBOX_UNCHECKED} "):
        return f"- [ ] {text[2:]}"
    if text.startswith(f"{_CHECKBOX_CHECKED} "):
        return f"- [x] {text[2:]}"
    return text


def _qt_html_to_markdown(body: str) -> str:
    """Convert a card's rich-text body (Qt HTML or plain text) to Markdown."""
    if not (body or "").strip():
        return ""
    doc = QTextDocument()
    if _looks_like_html(body):
        doc.setHtml(body)
    else:
        doc.setPlainText(body)
    lines: list[str] = []
    block = doc.begin()
    while block.isValid():
        inline = _block_inline_markdown(block)
        text_list = block.textList()
        if text_list is not None:
            indent = "  " * max(0, block.blockFormat().indent() - 1)
            if text_list.format().style() == QTextListFormat.Style.ListDecimal:
                prefix = f"{text_list.itemNumber(block) + 1}. "
            else:
                prefix = "- "
            lines.append(f"{indent}{prefix}{inline}".rstrip())
        elif inline.strip():
            indent = "  " * block.blockFormat().indent()
            lines.append(f"{indent}{inline}".rstrip())
        else:
            lines.append("")
        block = block.next()
    return "\n".join(lines).strip()


def _extract_html_body(html: str) -> str:
    """Pull the inner <body> of a full Qt HTML document; passthrough otherwise."""
    m = re.search(r"<body[^>]*>(.*)</body>", html, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else html


def _card_to_markdown(row: db.DailyCardRow) -> str:
    title = (row.title or "").strip() or "Untitled"
    parts: list[str] = [f"# {title}", ""]
    meta: list[str] = []
    if row.status:
        meta.append(f"**Status:** {row.status}")
    created = _format_created(row.created_at)
    if created and created != "—":
        meta.append(f"**Created:** {created}")
    if meta:
        parts.append("  \n".join(meta))
        parts.append("")
    body_md = _qt_html_to_markdown(row.body)
    if body_md:
        parts.append(body_md)
    return "\n".join(parts).rstrip() + "\n"


def _card_to_html_document(row: db.DailyCardRow) -> str:
    title = (row.title or "").strip() or "Untitled"
    if _looks_like_html(row.body):
        body_html = _extract_html_body(row.body)
    elif row.body.strip():
        body_html = f"<p>{escape(row.body).replace(chr(10), '<br>')}</p>"
    else:
        body_html = ""
    meta: list[str] = []
    if row.status:
        meta.append(f"Status: {escape(row.status)}")
    created = _format_created(row.created_at)
    if created and created != "—":
        meta.append(f"Created: {escape(created)}")
    meta_html = (
        f'<p class="card-meta">{" &middot; ".join(meta)}</p>' if meta else ""
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{escape(title)}</title>\n"
        "<style>\n"
        "body { font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; "
        "max-width: 760px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; "
        "color: #1f2328; }\n"
        "h1 { margin-bottom: 0.25rem; }\n"
        ".card-meta { color: #57606a; font-size: 0.85rem; margin-top: 0; }\n"
        "hr { border: none; border-top: 1px solid #d0d7de; margin: 1rem 0; }\n"
        "</style>\n</head>\n<body>\n"
        f"<h1>{escape(title)}</h1>\n"
        f"{meta_html}\n<hr>\n"
        f"{body_html}\n"
        "</body>\n</html>\n"
    )


def _openai_api_key() -> str:
    saved = QSettings(_SETTINGS_ORG, _SETTINGS_APP).value(
        _OPENAI_API_KEY_SETTINGS_KEY, ""
    )
    if isinstance(saved, str) and saved.strip():
        return saved.strip()
    return os.environ.get("OPENAI_API_KEY", "").strip()


def _generate_resume_with_openai(api_key: str, job_description: str, current_resume: str) -> str:
    payload = {
        "model": _OPENAI_RESUME_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an expert resume writer. Rewrite the candidate resume for the "
                    "specific job description. Keep facts truthful, preserve the candidate's "
                    "real experience, improve relevance and wording, and return only the new resume."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Job description:\n"
                    f"{job_description}\n\n"
                    "Current resume:\n"
                    f"{current_resume}\n\n"
                    "Generate a tailored resume."
                ),
            },
        ],
        "temperature": 0.4,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI request failed ({e.code}): {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenAI request failed: {e.reason}") from e

    data = json.loads(raw)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("OpenAI response did not include a generated resume.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise RuntimeError("OpenAI response did not include message content.")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("OpenAI returned an empty resume.")
    return content.strip()


class _OpenAIResumeWorker(QObject):
    finished = pyqtSignal(str, str)

    def __init__(self, api_key: str, job_description: str, current_resume: str) -> None:
        super().__init__()
        self._api_key = api_key
        self._job_description = job_description
        self._current_resume = current_resume

    def run(self) -> None:
        try:
            text = _generate_resume_with_openai(
                self._api_key,
                self._job_description,
                self._current_resume,
            )
        except Exception as e:  # noqa: BLE001 - surface request errors in the dialog.
            self.finished.emit("", str(e))
            return
        self.finished.emit(text, "")


def _daily_glyph_icon(
    kind: Literal[
        "add", "menu", "eye", "eye_off", "paste", "ai_paste", "copy",
        "trash", "edit", "move", "export", "close",
    ],
    color: QColor,
) -> QIcon:
    """Draw small add / vertical-menu glyphs with identical metrics for alignment."""
    px = _DAILY_ICON_PIX_PX
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    center = px / 2.0
    if kind == "add":
        pen = QPen(color, 1.75, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        arm = px * 0.26
        p.drawLine(QPointF(center - arm, center), QPointF(center + arm, center))
        p.drawLine(QPointF(center, center - arm), QPointF(center, center + arm))
    elif kind == "copy":
        pen = QPen(
            color, 1.3, Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin,
        )
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        # Two offset sheets — the classic "copy" glyph.
        p.drawRoundedRect(QRectF(px * 0.34, px * 0.16, px * 0.42, px * 0.42), 1.6, 1.6)
        p.drawRoundedRect(QRectF(px * 0.20, px * 0.36, px * 0.42, px * 0.42), 1.6, 1.6)
    elif kind in ("paste", "ai_paste"):
        pen = QPen(
            color, 1.4, Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin,
        )
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        # Clipboard body + the clip tab on top.
        p.drawRoundedRect(QRectF(px * 0.26, px * 0.26, px * 0.48, px * 0.56), 2.0, 2.0)
        p.drawRoundedRect(QRectF(px * 0.39, px * 0.15, px * 0.22, px * 0.15), 1.0, 1.0)
        if kind == "ai_paste":
            # A small sparkle marks the AI-assisted variant.
            sx, sy = px * 0.70, px * 0.62
            big, small = px * 0.17, px * 0.08
            p.drawLine(QPointF(sx - big, sy), QPointF(sx + big, sy))
            p.drawLine(QPointF(sx, sy - big), QPointF(sx, sy + big))
            p.drawLine(QPointF(sx - small, sy - small), QPointF(sx + small, sy + small))
            p.drawLine(QPointF(sx - small, sy + small), QPointF(sx + small, sy - small))
    elif kind == "trash":
        pen = QPen(
            color, 1.4, Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin,
        )
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        # Lid + handle.
        p.drawLine(QPointF(px * 0.24, px * 0.31), QPointF(px * 0.76, px * 0.31))
        p.drawLine(QPointF(px * 0.42, px * 0.31), QPointF(px * 0.42, px * 0.22))
        p.drawLine(QPointF(px * 0.42, px * 0.22), QPointF(px * 0.58, px * 0.22))
        p.drawLine(QPointF(px * 0.58, px * 0.22), QPointF(px * 0.58, px * 0.31))
        # Tapered can body + bottom.
        p.drawLine(QPointF(px * 0.31, px * 0.31), QPointF(px * 0.35, px * 0.79))
        p.drawLine(QPointF(px * 0.69, px * 0.31), QPointF(px * 0.65, px * 0.79))
        p.drawLine(QPointF(px * 0.35, px * 0.79), QPointF(px * 0.65, px * 0.79))
        # Ribs.
        p.drawLine(QPointF(px * 0.44, px * 0.39), QPointF(px * 0.45, px * 0.71))
        p.drawLine(QPointF(px * 0.56, px * 0.39), QPointF(px * 0.55, px * 0.71))
    elif kind == "edit":
        pen = QPen(
            color, 1.5, Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin,
        )
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        # Pencil body (a slim parallelogram) from nib (lower-left) to eraser.
        p.drawLine(QPointF(px * 0.32, px * 0.68), QPointF(px * 0.62, px * 0.38))
        p.drawLine(QPointF(px * 0.40, px * 0.74), QPointF(px * 0.70, px * 0.44))
        p.drawLine(QPointF(px * 0.62, px * 0.38), QPointF(px * 0.70, px * 0.44))
        p.drawLine(QPointF(px * 0.32, px * 0.68), QPointF(px * 0.40, px * 0.74))
        # The written line under the nib.
        p.drawLine(QPointF(px * 0.24, px * 0.79), QPointF(px * 0.40, px * 0.79))
    elif kind == "move":
        pen = QPen(
            color, 1.35, Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin,
        )
        p.setPen(pen)
        c = px * 0.5
        a = px * 0.30
        h = px * 0.075
        p.drawLine(QPointF(c, c - a), QPointF(c, c + a))
        p.drawLine(QPointF(c - a, c), QPointF(c + a, c))
        p.drawLine(QPointF(c, c - a), QPointF(c - h, c - a + h))
        p.drawLine(QPointF(c, c - a), QPointF(c + h, c - a + h))
        p.drawLine(QPointF(c, c + a), QPointF(c - h, c + a - h))
        p.drawLine(QPointF(c, c + a), QPointF(c + h, c + a - h))
        p.drawLine(QPointF(c - a, c), QPointF(c - a + h, c - h))
        p.drawLine(QPointF(c - a, c), QPointF(c - a + h, c + h))
        p.drawLine(QPointF(c + a, c), QPointF(c + a - h, c - h))
        p.drawLine(QPointF(c + a, c), QPointF(c + a - h, c + h))
    elif kind == "export":
        pen = QPen(
            color, 1.4, Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin,
        )
        p.setPen(pen)
        c = px * 0.5
        # Up arrow rising out of an open tray.
        p.drawLine(QPointF(c, px * 0.64), QPointF(c, px * 0.26))
        p.drawLine(QPointF(c, px * 0.26), QPointF(c - px * 0.10, px * 0.40))
        p.drawLine(QPointF(c, px * 0.26), QPointF(c + px * 0.10, px * 0.40))
        p.drawLine(QPointF(px * 0.28, px * 0.58), QPointF(px * 0.28, px * 0.76))
        p.drawLine(QPointF(px * 0.28, px * 0.76), QPointF(px * 0.72, px * 0.76))
        p.drawLine(QPointF(px * 0.72, px * 0.76), QPointF(px * 0.72, px * 0.58))
    elif kind == "close":
        pen = QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        arm = px * 0.22
        p.drawLine(QPointF(center - arm, center - arm), QPointF(center + arm, center + arm))
        p.drawLine(QPointF(center - arm, center + arm), QPointF(center + arm, center - arm))
    elif kind == "menu":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        r = 1.4
        step = px * 0.21
        for offset in (-step, 0.0, step):
            p.drawEllipse(QPointF(center, center + offset), r, r)
    else:
        pen = QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(center, center), px * 0.35, px * 0.22)
        p.setBrush(color)
        p.drawEllipse(QPointF(center, center), 2.0, 2.0)
        if kind == "eye_off":
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawLine(QPointF(px * 0.22, px * 0.78), QPointF(px * 0.78, px * 0.22))
    p.end()
    return QIcon(pm)


def _rich_fmt_button(
    text: str,
    tooltip: str,
    *,
    width: int = 28,
    checkable: bool = False,
    font: QFont | None = None,
) -> QToolButton:
    btn = QToolButton()
    btn.setText(text)
    btn.setToolTip(tooltip)
    btn.setCheckable(checkable)
    btn.setFixedWidth(width)
    if font is not None:
        btn.setFont(font)
    return btn


def _daily_icon_button(
    tooltip: str,
    icon_kind: Literal["add", "menu", "eye", "eye_off", "paste", "ai_paste"],
) -> QToolButton:
    colors = _daily_theme_colors()
    icon = _daily_glyph_icon(icon_kind, QColor(colors.muted))
    btn = QToolButton()
    btn.setObjectName("dailyIconBtn")
    btn.setToolTip(tooltip)
    btn.setIcon(icon)
    btn.setIconSize(QSize(_DAILY_ICON_PIX_PX, _DAILY_ICON_PIX_PX))
    btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    btn.setAutoRaise(True)
    btn.setFixedSize(_DAILY_ICON_BTN_PX, _DAILY_ICON_BTN_PX)
    btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


class _ColumnTitleWidget(QWidget):
    """Column title as label; switches to line edit only while renaming."""

    committed = pyqtSignal(str)

    def __init__(
        self,
        title: str,
        list_id: int,
        page: "DailyBoardPage",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("dailyColumnTitleHost")
        self._list_id = list_id
        self._page = page
        self._drag_start: QPointF | None = None
        self._editing = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Drag to reorder column · double-click to rename")
        self._title_text = title or "Untitled"
        self._card_count: int | None = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._label = QLabel(self._title_text)
        self._label.setObjectName("dailyColumnTitle")
        self._label.setWordWrap(True)
        self._label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self._edit: QLineEdit | None = None
        lay.addWidget(
            self._label,
            stretch=1,
            alignment=Qt.AlignmentFlag.AlignVCenter,
        )

    def _render_label(self) -> None:
        if self._card_count is None:
            self._label.setText(self._title_text)
        else:
            self._label.setText(f"{self._title_text} ({self._card_count})")

    def text(self) -> str:
        if self._editing and self._edit is not None:
            return self._edit.text()
        return self._title_text

    def set_text(self, title: str) -> None:
        self._title_text = title or "Untitled"
        self._render_label()

    def set_card_count(self, count: int) -> None:
        self._card_count = count
        self._render_label()

    def start_edit(self) -> None:
        if self._editing:
            return
        if self._edit is None:
            self._edit = QLineEdit()
            self._edit.setObjectName("dailyColumnTitleEdit")
            self._edit.returnPressed.connect(self._commit_edit)
            self._edit.editingFinished.connect(self._commit_edit)
        self._editing = True
        self._edit.setText(self._title_text)
        self._label.hide()
        self.layout().addWidget(
            self._edit,
            stretch=1,
            alignment=Qt.AlignmentFlag.AlignVCenter,
        )
        self._edit.show()
        self._edit.setFocus()
        self._edit.selectAll()

    def _commit_edit(self) -> None:
        if not self._editing or self._edit is None:
            return
        self._editing = False
        self._edit.blockSignals(True)
        title = self._edit.text().strip() or "Untitled"
        self._title_text = title
        self._render_label()
        self._edit.hide()
        self.layout().removeWidget(self._edit)
        self._edit.blockSignals(False)
        self._label.show()
        self.committed.emit(title)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.start_edit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _start_column_drag_if_ready(self, event: QMouseEvent) -> bool:
        if self._editing:
            return False
        if self._page._space_pan_down():
            return False
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return False
        if self._drag_start is None:
            return False
        if (
            event.position() - self._drag_start
        ).manhattanLength() < QApplication.startDragDistance():
            return False
        drag = QDrag(self)
        drag.setMimeData(_daily_list_mime_data(self._list_id))
        drag.exec(Qt.DropAction.MoveAction)
        self._drag_start = None
        return True

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if (
            event.button() == Qt.MouseButton.LeftButton
            and not self._editing
            and not self._page._space_pan_down()
        ):
            self._drag_start = event.position()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._start_column_drag_if_ready(event):
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        self._drag_start = None
        super().mouseReleaseEvent(event)


class _ColumnHeaderHost(QWidget):
    """Column header bar; accepts column reorder drops on the title row."""

    def __init__(
        self,
        list_id: int,
        page: "DailyBoardPage",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_id = list_id
        self._page = page
        self.setObjectName("dailyColumnHeader")
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if _list_id_from_mime(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if _list_id_from_mime(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        dragged_id = _list_id_from_mime(event.mimeData())
        if dragged_id is None or dragged_id == self._list_id:
            return
        after = event.position().x() > self.width() * 0.5
        self._page._move_column_relative(dragged_id, self._list_id, after)
        event.acceptProposedAction()


class _BoardHost(QWidget):
    """Horizontal board strip; accepts column drops past the last column."""

    def __init__(self, page: "DailyBoardPage", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._page = page
        self.setObjectName("dailyBoardHost")
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if _list_id_from_mime(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if _list_id_from_mime(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        dragged_id = _list_id_from_mime(event.mimeData())
        if dragged_id is not None:
            self._page._move_column_to_end(dragged_id)
            event.acceptProposedAction()


class _CheckboxClickFilter(QObject):
    """Click ☐ / ☑ in the rich-text body to toggle (filter on QTextEdit viewport)."""

    def __init__(self, editor: QTextEdit, on_changed) -> None:  # type: ignore[no-untyped-def]
        super().__init__(editor)
        self._editor = editor
        self._viewport = editor.viewport()
        self._on_changed = on_changed

    def _toggle_at_document_index(self, index: int) -> bool:
        doc = self._editor.document()
        if index < 0 or index >= doc.characterCount():
            return False
        ch = doc.characterAt(index)
        if ch not in (_CHECKBOX_UNCHECKED, _CHECKBOX_CHECKED):
            return False
        cur = QTextCursor(doc)
        cur.setPosition(index)
        cur.setPosition(index + 1, QTextCursor.MoveMode.KeepAnchor)
        cur.insertText(
            _CHECKBOX_CHECKED if ch == _CHECKBOX_UNCHECKED else _CHECKBOX_UNCHECKED
        )
        self._editor.setTextCursor(cur)
        self._on_changed()
        return True

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if obj is not self._viewport:
            return super().eventFilter(obj, event)
        if event.type() != QEvent.Type.MouseButtonPress:
            return super().eventFilter(obj, event)
        me = event
        if not isinstance(me, QMouseEvent) or me.button() != Qt.MouseButton.LeftButton:
            return super().eventFilter(obj, event)
        hit = self._editor.cursorForPosition(me.position().toPoint())
        base = hit.position()
        for index in (base - 1, base):
            if self._toggle_at_document_index(index):
                return True
        return super().eventFilter(obj, event)


class DailyCardEditDialog(QDialog):
    """Edit one daily card; auto-save on debounce; ESC closes after saving."""

    saved = pyqtSignal(int)
    # Deletion is its own signal, not a `saved` with a missing row. Reported as a
    # save, the board could only find out the card had gone by rebuilding
    # everything - refetching every summary and every body in the sheet - which
    # is what made F4 take seconds. Told plainly, it drops one row.
    deleted = pyqtSignal(int)
    closed_for_id = pyqtSignal(int)
    # A similar/duplicate application was clicked — the board opens that card so
    # its editor is reused rather than duplicated.
    open_card_requested = pyqtSignal(int)

    def __init__(self, card_id: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._card_id = card_id
        self._loading = False
        self._snapshot: tuple[str, str] = ("", "")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )
        self.setModal(False)
        self.setMinimumSize(820, 460)
        self.resize(960, 560)
        self._restore_saved_geometry()
        self.setWindowIcon(app_icon())

        root = QVBoxLayout(self)
        self._title = QLineEdit()
        _base = self.font()
        _fi = QFontInfo(_base)
        _pt = _fi.pointSizeF()
        if _pt <= 0:
            _pt = float(_fi.pointSize() or 10)
        title_font = QFont(_base)
        title_font.setPointSizeF(_pt * 1.25)
        self._title.setFont(title_font)

        self._body = QTextEdit()
        self._body.setAcceptRichText(True)
        self._body.setTabChangesFocus(True)
        self._body.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self._body.setMinimumHeight(160)
        _body_font = QFont(_base)
        _body_font.setPointSizeF(_pt * 1.05)
        self._body.setFont(_body_font)

        fmt_host = QWidget()
        fmt_outer = QVBoxLayout(fmt_host)
        fmt_outer.setContentsMargins(0, 0, 0, 0)
        fmt_outer.setSpacing(4)

        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(4)
        bold_f = QFont(_base)
        bold_f.setBold(True)
        italic_f = QFont(_base)
        italic_f.setItalic(True)
        self._fmt_bold = _rich_fmt_button("B", "Bold (Ctrl+B)", checkable=True, font=bold_f)
        self._fmt_bold.clicked.connect(self._toggle_bold)
        self._fmt_italic = _rich_fmt_button(
            "I", "Italic (Ctrl+I)", checkable=True, font=italic_f
        )
        self._fmt_italic.clicked.connect(self._toggle_italic)
        self._fmt_underline = _rich_fmt_button("U", "Underline (Ctrl+U)", checkable=True)
        self._fmt_underline.clicked.connect(self._toggle_underline)
        self._fmt_strike = _rich_fmt_button("S", "Strikethrough", checkable=True)
        strike_f = QFont(_base)
        strike_f.setStrikeOut(True)
        self._fmt_strike.setFont(strike_f)
        self._fmt_strike.clicked.connect(self._toggle_strikethrough)
        self._font_size = QComboBox()
        self._font_size.addItems(list(_DAILY_FONT_SIZES))
        self._font_size.setToolTip("Font size")
        self._font_size.setFixedWidth(52)
        self._font_size.currentTextChanged.connect(self._apply_font_size)
        self._color_btn = _rich_fmt_button("A", "Text color", width=28)
        self._color_btn.clicked.connect(self._show_text_color_menu)
        self._highlight_btn = _rich_fmt_button("▮", "Highlight color", width=28)
        self._highlight_btn.clicked.connect(self._show_highlight_menu)
        self._clear_fmt_btn = _rich_fmt_button("Tx", "Clear formatting", width=32)
        self._clear_fmt_btn.clicked.connect(self._clear_formatting)
        for w in (
            self._fmt_bold,
            self._fmt_italic,
            self._fmt_underline,
            self._fmt_strike,
            QLabel("Size"),
            self._font_size,
            self._color_btn,
            self._highlight_btn,
            self._clear_fmt_btn,
        ):
            row1.addWidget(w)
        row1.addStretch()

        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(4)
        self._align_left = _rich_fmt_button("⫷", "Align left", checkable=True, width=30)
        self._align_center = _rich_fmt_button("≡", "Align center", checkable=True, width=30)
        self._align_right = _rich_fmt_button("⫸", "Align right", checkable=True, width=30)
        self._align_left.clicked.connect(lambda: self._set_alignment(Qt.AlignmentFlag.AlignLeft))
        self._align_center.clicked.connect(
            lambda: self._set_alignment(Qt.AlignmentFlag.AlignCenter)
        )
        self._align_right.clicked.connect(
            lambda: self._set_alignment(Qt.AlignmentFlag.AlignRight)
        )
        bullet_btn = _rich_fmt_button("•", "Bullet list")
        bullet_btn.clicked.connect(self._toggle_bullet_list)
        numbered_btn = _rich_fmt_button("1.", "Numbered list", width=30)
        numbered_btn.clicked.connect(self._toggle_numbered_list)
        check_btn = _rich_fmt_button("☐", "Checkbox line")
        check_btn.setToolTip("Checkbox line (click the box in text to toggle)")
        check_btn.clicked.connect(self._insert_checkbox_line)
        outdent_btn = _rich_fmt_button("⇤", "Decrease indent", width=30)
        outdent_btn.clicked.connect(self._decrease_indent)
        indent_btn = _rich_fmt_button("⇥", "Increase indent", width=30)
        indent_btn.clicked.connect(self._increase_indent)
        self._emoji_btn = _rich_fmt_button("😀", "Insert emoji", width=32)
        self._emoji_btn.clicked.connect(self._show_emoji_menu)
        link_btn = _rich_fmt_button("🔗", "Insert link", width=32)
        link_btn.clicked.connect(self._insert_link)
        for w in (
            QLabel("Align"),
            self._align_left,
            self._align_center,
            self._align_right,
            bullet_btn,
            numbered_btn,
            check_btn,
            outdent_btn,
            indent_btn,
            link_btn,
            self._emoji_btn,
        ):
            row2.addWidget(w)
        row2.addStretch()
        self._delete_card_btn = _rich_fmt_button(
            "",
            "Delete this card — removes it and closes the editor "
            "(Ctrl+Del deletes straight away, without asking)",
            width=32,
        )
        self._delete_card_btn.setIcon(_daily_glyph_icon("trash", QColor("#C62828")))
        self._delete_card_btn.clicked.connect(self._delete_card_and_close)
        row2.addWidget(self._delete_card_btn)
        # Ctrl+Del is the keyboard way to the same button, and deliberately skips
        # the confirmation: someone reaching for a two-key combination has already
        # decided. A window shortcut wins over the text widgets' own Ctrl+Del
        # (delete-word), so it works wherever the caret is in the editor.
        self._delete_shortcut = QShortcut(QKeySequence("Ctrl+Del"), self)
        self._delete_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._delete_shortcut.activated.connect(self._delete_card_without_confirm)
        self._copy_excel_btn = _rich_fmt_button(
            "Copy → Excel",
            "Copy this card as a spreadsheet row: Company, Role, Link, Description",
            width=100,
        )
        self._copy_excel_btn.clicked.connect(self._copy_excel_row)
        row2.addWidget(self._copy_excel_btn)

        fmt_outer.addLayout(row1)
        fmt_outer.addLayout(row2)

        self._status_combo = QComboBox()
        self._status_combo.addItem("No status", _DAILY_STATUS_NONE)
        for name in _DAILY_STATUS_ORDER:
            self._status_combo.addItem(
                _daily_status_swatch(_DAILY_STATUS_COLORS[name]), name, name
            )
        self._status_combo.setToolTip("Card status")
        self._status_combo.currentIndexChanged.connect(self._on_status_changed)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(QLabel("Title"))
        title_row.addStretch()
        title_row.addWidget(QLabel("Status"))
        title_row.addWidget(self._status_combo)

        root.addLayout(title_row)
        root.addWidget(self._title)

        # Description editor (left) and AI Assistant (right), side-by-side.
        left_col = QWidget()
        left_lay = QVBoxLayout(left_col)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.addWidget(QLabel("Description"))
        left_lay.addWidget(fmt_host)
        left_lay.addWidget(self._body, stretch=1)

        self._ai_thread: QThread | None = None
        self._ai_worker: ai_assistant.AIWorker | None = None
        # Duplicate-application detection: the index is parsed once, lazily, the
        # first time this card actually has a company/role/link to compare.
        self._applied_index: list[AppliedJob] | None = None
        self._similar_timer = QTimer(self)
        self._similar_timer.setSingleShot(True)
        self._similar_timer.setInterval(300)
        self._similar_timer.timeout.connect(self._refresh_similar_jobs)
        ai_panel = self._build_ai_panel()

        self._body_split = QSplitter(Qt.Orientation.Horizontal)
        self._body_split.addWidget(left_col)
        self._body_split.addWidget(ai_panel)
        self._body_split.setStretchFactor(0, 3)
        self._body_split.setStretchFactor(1, 2)
        self._body_split.setChildrenCollapsible(False)
        self._body_split.setSizes([560, 400])
        root.addWidget(self._body_split, stretch=1)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._flush_save)

        # self._title.textChanged.connect(self._on_field_changed)
        # self._body.textChanged.connect(self._on_field_changed)
        self._body.cursorPositionChanged.connect(self._sync_format_buttons)
        # Re-check for duplicate applications as the company/role/link change.
        self._title.textChanged.connect(self._schedule_similar_refresh)
        self._body.textChanged.connect(self._schedule_similar_refresh)

        for key, slot in (
            (QKeySequence.StandardKey.Bold, self._toggle_bold),
            (QKeySequence.StandardKey.Italic, self._toggle_italic),
            (QKeySequence.StandardKey.Underline, self._toggle_underline),
        ):
            sc = QShortcut(key, self._body)
            sc.activated.connect(slot)

        esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        esc_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        esc_shortcut.activated.connect(self.close)

        self._checkbox_filter = _CheckboxClickFilter(self._body, self._on_field_changed)
        self._body.viewport().installEventFilter(self._checkbox_filter)
        self._default_body_pt = _pt * 1.05

        self._load_from_db()
        # Somebody else's application opens read-only. The write would be refused
        # anyway, but only after they had typed into it - better to say so before
        # they start than to take the edit and hand it back.
        self._apply_write_permission()
        self._update_window_title()
        # Only a card that opened blank (a freshly-added placeholder) is discarded
        # when left empty. A card that already had content is never auto-removed.
        self._was_empty_on_open = self._is_empty()

    def _set_body_content(self, body: str) -> None:
        if _looks_like_html(body):
            self._body.setHtml(body)
        else:
            self._body.setPlainText(body or "")

    def _body_html(self) -> str:
        return self._body.toHtml()

    def _merge_char_format(self, fmt: QTextCharFormat) -> None:
        cursor = self._body.textCursor()
        if not cursor.hasSelection():
            cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        cursor.mergeCharFormat(fmt)
        self._body.mergeCurrentCharFormat(fmt)

    def _toggle_bold(self) -> None:
        fmt = self._body.currentCharFormat()
        weight = (
            QFont.Weight.Normal
            if fmt.fontWeight() == QFont.Weight.Bold
            else QFont.Weight.Bold
        )
        next_fmt = QTextCharFormat()
        next_fmt.setFontWeight(weight)
        self._merge_char_format(next_fmt)
        self._sync_format_buttons()
        self._on_field_changed()

    def _toggle_italic(self) -> None:
        fmt = self._body.currentCharFormat()
        next_fmt = QTextCharFormat()
        next_fmt.setFontItalic(not fmt.fontItalic())
        self._merge_char_format(next_fmt)
        self._sync_format_buttons()
        self._on_field_changed()

    def _toggle_underline(self) -> None:
        fmt = self._body.currentCharFormat()
        next_fmt = QTextCharFormat()
        next_fmt.setFontUnderline(not fmt.fontUnderline())
        self._merge_char_format(next_fmt)
        self._sync_format_buttons()
        self._on_field_changed()

    def _toggle_strikethrough(self) -> None:
        fmt = self._body.currentCharFormat()
        next_fmt = QTextCharFormat()
        next_fmt.setFontStrikeOut(not fmt.fontStrikeOut())
        self._merge_char_format(next_fmt)
        self._sync_format_buttons()
        self._on_field_changed()

    def _apply_text_color(self, color: QColor | None) -> None:
        fmt = QTextCharFormat()
        if color is None:
            fmt.clearForeground()
        else:
            fmt.setForeground(color)
        self._merge_char_format(fmt)
        self._sync_format_buttons()
        self._on_field_changed()

    def _apply_highlight(self, color: QColor | None) -> None:
        cursor = self._body.textCursor()
        if not cursor.hasSelection():
            cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        if color is None:
            fmt = QTextCharFormat(cursor.charFormat())
            fmt.setBackground(QBrush(Qt.BrushStyle.NoBrush))
            fmt.clearBackground()
            cursor.setCharFormat(fmt)
        else:
            fmt = QTextCharFormat()
            fmt.setBackground(color)
            cursor.mergeCharFormat(fmt)
        self._body.setTextCursor(cursor)
        self._body.mergeCurrentCharFormat(cursor.charFormat())
        self._on_field_changed()

    def _show_color_menu(
        self,
        btn: QToolButton,
        presets: tuple[tuple[str, str | None], ...],
        on_pick,
    ) -> None:  # type: ignore[no-untyped-def]
        menu = QMenu(self)
        for label, hex_color in presets:
            act = menu.addAction(label)
            act.triggered.connect(
                lambda _checked=False, h=hex_color: on_pick(
                    QColor(h) if h else None
                )
            )
        menu.addSeparator()
        more = menu.addAction("Custom color…")

        def _pick_custom() -> None:
            chosen = QColorDialog.getColor(parent=self, title="Choose color")
            if chosen.isValid():
                on_pick(chosen)

        more.triggered.connect(_pick_custom)
        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _show_text_color_menu(self) -> None:
        def pick(c: QColor | None) -> None:
            if c is not None and not c.isValid():
                return
            self._apply_text_color(c)

        self._show_color_menu(self._color_btn, _DAILY_TEXT_COLORS, pick)

    def _show_highlight_menu(self) -> None:
        def pick(c: QColor | None) -> None:
            if c is not None and not c.isValid():
                return
            self._apply_highlight(c)

        self._show_color_menu(self._highlight_btn, _DAILY_HIGHLIGHT_COLORS, pick)

    def _clear_formatting(self) -> None:
        cursor = self._body.textCursor()
        if not cursor.hasSelection():
            cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        plain = QTextCharFormat()
        plain.setFontPointSize(self._default_body_pt)
        cursor.setCharFormat(plain)
        block_fmt = QTextBlockFormat()
        block_fmt.setAlignment(Qt.AlignmentFlag.AlignLeft)
        cursor.setBlockFormat(block_fmt)
        self._body.setTextCursor(cursor)
        self._body.mergeCurrentCharFormat(plain)
        self._sync_format_buttons()
        self._on_field_changed()

    def _delete_card_and_close(self) -> None:
        """Delete this card and close the editor, after confirming with the user."""
        if self._delete_blocked_by_ai():
            return
        reply = QMessageBox.question(
            self,
            "Delete card",
            "Delete this card?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._do_delete_card()

    def _delete_card_without_confirm(self) -> None:
        """Ctrl+Del: delete this card and close, with no confirmation prompt.

        Gated on the delete button rather than on a permission flag of its own,
        so the shortcut is dead in exactly the cases the button is - a read-only
        card someone else owns, above all.
        """
        if not self._delete_card_btn.isEnabled():
            return
        if self._delete_blocked_by_ai():
            return
        self._do_delete_card()

    def _delete_blocked_by_ai(self) -> bool:
        """True (and warns) when the AI is mid-run, so a delete must wait."""
        if getattr(self, "_ai_thread", None) is not None and self._ai_thread.isRunning():
            QMessageBox.information(
                self, "AI Assistant", "Please wait for the AI Assistant to finish."
            )
            return True
        return False

    def _do_delete_card(self) -> None:
        """Remove the card and close the editor."""
        self._save_timer.stop()  # don't let a pending debounce re-save the row
        try:
            _store().delete_daily_card(self._card_id)
        except PermissionDenied as exc:
            QMessageBox.warning(self, "Delete card", str(exc))
            return
        self.deleted.emit(self._card_id)  # board drops the one row
        self.close()

    def _set_alignment(self, alignment: Qt.AlignmentFlag) -> None:
        cursor = self._body.textCursor()
        block_fmt = cursor.blockFormat()
        block_fmt.setAlignment(alignment)
        cursor.setBlockFormat(block_fmt)
        self._body.setTextCursor(cursor)
        self._sync_format_buttons()
        self._on_field_changed()

    def _toggle_bullet_list(self) -> None:
        cursor = self._body.textCursor()
        list_fmt = QTextListFormat()
        list_fmt.setStyle(QTextListFormat.Style.ListDisc)
        cursor.createList(list_fmt)
        self._on_field_changed()

    def _toggle_numbered_list(self) -> None:
        cursor = self._body.textCursor()
        list_fmt = QTextListFormat()
        list_fmt.setStyle(QTextListFormat.Style.ListDecimal)
        cursor.createList(list_fmt)
        self._on_field_changed()

    def _change_block_indent(self, delta: int) -> None:
        cursor = self._body.textCursor()
        doc = self._body.document()
        start_pos = cursor.selectionStart()
        end_pos = cursor.selectionEnd()
        if start_pos == end_pos:
            end_pos = start_pos + 1
        probe = QTextCursor(doc)
        probe.setPosition(start_pos)
        first_block = probe.block().blockNumber()
        probe.setPosition(max(start_pos, end_pos - 1))
        last_block = probe.block().blockNumber()
        for block_num in range(first_block, last_block + 1):
            block = doc.findBlockByNumber(block_num)
            if not block.isValid():
                continue
            bf = QTextBlockFormat(block.blockFormat())
            bf.setIndent(max(0, bf.indent() + delta))
            block_cursor = QTextCursor(block)
            block_cursor.setBlockFormat(bf)
        self._body.setTextCursor(cursor)
        self._on_field_changed()

    def _increase_indent(self) -> None:
        self._change_block_indent(1)

    def _decrease_indent(self) -> None:
        self._change_block_indent(-1)

    def _insert_link(self) -> None:
        url, ok = QInputDialog.getText(
            self,
            "Insert link",
            "URL:",
            text="https://",
        )
        if not ok or not url.strip():
            return
        cursor = self._body.textCursor()
        text = cursor.selectedText() or url.strip()
        fmt = QTextCharFormat()
        fmt.setAnchor(True)
        fmt.setAnchorHref(url.strip())
        fmt.setForeground(QColor("#64B5F6"))
        fmt.setFontUnderline(True)
        if cursor.hasSelection():
            cursor.mergeCharFormat(fmt)
        else:
            cursor.insertText(text, fmt)
        self._body.setTextCursor(cursor)
        self._on_field_changed()

    def _insert_checkbox_line(self) -> None:
        cursor = self._body.textCursor()
        block = cursor.block()
        text = block.text()
        plain = QTextCharFormat()
        if not text.strip():
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cursor.insertText(f"{_CHECKBOX_UNCHECKED} ", plain)
        elif text.startswith(f"{_CHECKBOX_UNCHECKED} ") or text.startswith(
            f"{_CHECKBOX_CHECKED} "
        ):
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            cursor.insertBlock()
            cursor.insertText(f"{_CHECKBOX_UNCHECKED} ", plain)
        else:
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cursor.insertText(f"{_CHECKBOX_UNCHECKED} ", plain)
        self._body.setTextCursor(cursor)
        self._on_field_changed()

    def _apply_font_size(self, size_text: str) -> None:
        if self._loading or not size_text:
            return
        try:
            pt = float(size_text)
        except ValueError:
            return
        fmt = QTextCharFormat()
        fmt.setFontPointSize(pt)
        self._merge_char_format(fmt)
        self._sync_format_buttons()
        self._on_field_changed()

    def _show_emoji_menu(self) -> None:
        menu = QMenu(self)
        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setSpacing(2)
        cols = 5
        for i, em in enumerate(_DAILY_EMOJIS):
            btn = QToolButton()
            btn.setText(em)
            btn.setFixedSize(32, 32)
            btn.clicked.connect(
                lambda _checked=False, e=em, m=menu: self._insert_emoji(e, m)
            )
            grid.addWidget(btn, i // cols, i % cols)
        grid_action = QWidgetAction(menu)
        grid_action.setDefaultWidget(grid_host)
        menu.addAction(grid_action)
        menu.exec(
            self._emoji_btn.mapToGlobal(self._emoji_btn.rect().bottomLeft())
        )

    def _insert_emoji(self, emoji: str, menu: QMenu | None = None) -> None:
        if menu is not None:
            menu.close()
        self._body.textCursor().insertText(emoji)
        self._on_field_changed()

    def _sync_format_buttons(self) -> None:
        fmt = self._body.currentCharFormat()
        block_fmt = self._body.textCursor().blockFormat()
        align = block_fmt.alignment()
        widgets_block = (
            self._fmt_bold,
            self._fmt_italic,
            self._fmt_underline,
            self._fmt_strike,
            self._font_size,
            self._align_left,
            self._align_center,
            self._align_right,
        )
        for w in widgets_block:
            w.blockSignals(True)
        try:
            self._fmt_bold.setChecked(fmt.fontWeight() == QFont.Weight.Bold)
            self._fmt_italic.setChecked(fmt.fontItalic())
            self._fmt_underline.setChecked(fmt.fontUnderline())
            self._fmt_strike.setChecked(fmt.fontStrikeOut())
            pt = fmt.fontPointSize()
            if pt <= 0:
                pt = self._default_body_pt
            size_key = str(int(round(pt)))
            idx = self._font_size.findText(size_key)
            if idx >= 0:
                self._font_size.setCurrentIndex(idx)
            self._align_left.setChecked(
                align == Qt.AlignmentFlag.AlignLeft
                or align == Qt.AlignmentFlag.AlignJustify
            )
            self._align_center.setChecked(align == Qt.AlignmentFlag.AlignCenter)
            self._align_right.setChecked(align == Qt.AlignmentFlag.AlignRight)
        finally:
            for w in widgets_block:
                w.blockSignals(False)

    def _apply_write_permission(self) -> None:
        """Lock the editor when this card belongs to somebody else.

        Presentation only - every write is checked again in the store - so a
        wrong answer here is a cosmetic bug, never a way in.
        """
        self._may_edit = True
        checker = getattr(_store(), "may_edit_card", None)
        if callable(checker):
            try:
                self._may_edit = bool(checker(self._card_id))
            except Exception:            # noqa: BLE001 - a failed check must not
                self._may_edit = True    # lock someone out of their own card
        if self._may_edit:
            return

        self._save_timer.stop()          # nothing here should autosave
        self._title.setReadOnly(True)
        self._body.setReadOnly(True)
        self._status_combo.setEnabled(False)
        # Every button that would change the card: the formatting rows, the
        # colour pickers, the delete. Reading, copying and the AI panel stay
        # available - they do not write to the card.
        for widget in self.findChildren((QToolButton, QPushButton)):
            if widget in self._readonly_safe_buttons():
                continue
            widget.setEnabled(False)

        who = author_label((self._card_creator or "").strip()) or "somebody else"
        banner = QLabel(
            f"Read-only — this application was added by {who}. "
            f"Only they, or the PM, can change it."
        )
        banner.setWordWrap(True)
        banner.setStyleSheet(
            "QLabel { background: palette(alternate-base); border: 1px solid "
            "palette(mid); border-radius: 4px; padding: 6px; }"
        )
        layout = self.layout()
        if layout is not None:
            layout.insertWidget(0, banner)

    def _readonly_safe_buttons(self) -> set:
        """Buttons that stay live in a read-only card: they only read."""
        safe = set()
        for name in ("_copy_excel_btn", "_copy_btn", "_preview_btn",
                     "_ai_run_btn", "_ai_ask_btn", "_ai_copy_btn"):
            widget = getattr(self, name, None)
            if widget is not None:
                safe.add(widget)
        return safe

    def _load_from_db(self) -> None:
        row = _store().get_daily_card(self._card_id)
        self._card_creator = (row.created_by or "") if row is not None else ""
        self._loading = True
        self._title.blockSignals(True)
        self._body.blockSignals(True)
        self._status_combo.blockSignals(True)
        try:
            if row is None:
                self._title.setText("")
                self._body.clear()
                self._set_status_combo(_DAILY_STATUS_NONE)
                self.setWindowTitle("Daily card (missing)")
                self._snapshot = ("", "")
                return
            self._title.setText(row.title)
            self._set_body_content(row.body)
            self._set_status_combo(row.status)
            self._snapshot = (row.title, row.body)
        finally:
            self._title.blockSignals(False)
            self._body.blockSignals(False)
            self._status_combo.blockSignals(False)
            self._loading = False
        self._apply_status_to_title()
        self._sync_format_buttons()
        self._refresh_quick_questions()
        self._refresh_similar_jobs()

    # --- duplicate-application list -----------------------------------------

    def _schedule_similar_refresh(self) -> None:
        if self._loading:
            return
        self._similar_timer.start()

    def _current_job_fields(self) -> tuple[str, str]:
        """(company, role) parsed from the card as it stands right now — the
        only two fields that identify a job (see find_similar_applied_jobs)."""
        text = self._body.toPlainText()
        return (
            _excel_field_from_text(text, "Company"),
            _excel_role_from_title(self._title.text()),
        )

    def _on_similar_job_clicked(self, item: QListWidgetItem) -> None:
        """Open the clicked past application in its own card editor."""
        card_id = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(card_id, int) or card_id == self._card_id:
            return
        if _store().get_daily_card(card_id) is None:  # deleted since the index was built
            QMessageBox.information(
                self, "Similar application", "That card no longer exists."
            )
            self._applied_index = None  # rebuild without it
            self._refresh_similar_jobs()
            return
        # Prefer the board: it reuses an already-open editor for that card.
        try:
            has_listener = self.receivers(self.open_card_requested) > 0
        except Exception:
            has_listener = False
        if has_listener:
            self.open_card_requested.emit(card_id)
            return
        dlg = DailyCardEditDialog(card_id, self.parentWidget())
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _refresh_similar_jobs(self) -> None:
        if getattr(self, "_similar_list", None) is None:
            return
        company, role = self._current_job_fields()
        if not _norm_company(company):
            # Every tier requires the company to match, so without one there is
            # nothing to look for — and no reason to build the index.
            self._similar_list.clear()
            self._similar_box.hide()
            return
        if self._applied_index is None:  # build once, on first real need
            try:
                self._applied_index = build_applied_job_index(self._card_id)
            except Exception:
                self._applied_index = []
        matches = find_similar_applied_jobs(company, role, self._applied_index)
        self._similar_list.clear()
        if not matches:
            self._similar_box.hide()
            return
        for m in matches:
            self._similar_list.addItem(_similar_job_item(m))
        # A duplicate is a real warning, a comparable title a heads-up, and
        # another role at the same company merely informative.
        dupes = sum(1 for m in matches if m.score == MATCH_SAME_JOB)
        similar = sum(1 for m in matches if m.score == MATCH_SIMILAR_JOB)
        same_co = sum(1 for m in matches if m.score == MATCH_SAME_COMPANY)
        parts: list[str] = []
        if similar:
            parts.append(f"{similar} similar")
        if same_co:
            parts.append(f"{same_co} at this company")
        tail = (" · " + " · ".join(parts)) if parts else ""
        if dupes:
            text, color = f"⛔ Already applied to this job ({dupes}){tail}", "#C62828"
        elif similar:
            text = f"⚠ {similar} similar job{'s' if similar != 1 else ''} applied"
            if same_co:
                text += f" · {same_co} at this company"
            color = "#C77800"
        else:
            text = f"🏢 {same_co} earlier application{'s' if same_co != 1 else ''} at this company"
            color = "#1565C0"
        self._similar_header.setText(text)
        self._similar_header.setStyleSheet(f"color: {color};")
        self._similar_box.show()

    def _set_status_combo(self, status: str) -> None:
        idx = self._status_combo.findData(status or _DAILY_STATUS_NONE)
        self._status_combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _current_status(self) -> str:
        data = self._status_combo.currentData()
        return data if isinstance(data, str) else _DAILY_STATUS_NONE

    def _apply_status_to_title(self) -> None:
        """Tint the title field with the status colour, matching the board card."""
        color = _daily_status_color(self._current_status())
        if color:
            self._title.setStyleSheet(
                f"QLineEdit {{ background-color: {color}; color: #FFFFFF; "
                "border: none; border-radius: 4px; padding: 4px 6px; }}"
            )
        else:
            self._title.setStyleSheet("")

    def _on_status_changed(self) -> None:
        if self._loading:
            return
        if _store().get_daily_card(self._card_id) is None:
            return
        try:
            _store().update_daily_card_status(self._card_id, self._current_status())
        except PermissionDenied as exc:
            QMessageBox.warning(self, "Status not changed", str(exc))
            self._load_from_db()       # put the combo back to what is stored
            return
        self._apply_status_to_title()
        self.saved.emit(self._card_id)

    def _update_window_title(self) -> None:
        t = self._title.text().strip() or "Untitled"
        self.setWindowTitle(f"Daily — {t}")

    def reload_from_db(self) -> None:
        self._load_from_db()
        self._update_window_title()

    def _on_field_changed(self) -> None:
        if self._loading:
            return
        self._update_window_title()
        self._save_timer.start()

    def _first_body_line(self) -> str:
        """First non-empty line of the body as plain text, capped for a title."""
        for line in self._body.toPlainText().splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:120]
        return ""

    def _is_empty(self) -> bool:
        """True when the card has neither a title nor any body text."""
        return not self._title.text().strip() and not self._body.toPlainText().strip()

    def _flush_save(self) -> None:
        if self._loading:
            return
        if not getattr(self, "_may_edit", True):
            # Opened read-only, so there is nothing to save and the store would
            # only refuse. Closing the window used to trigger that refusal as a
            # popup, which is noise: the dialog already said it was read-only.
            return
        if _store().get_daily_card(self._card_id) is None:
            return
        if self._is_empty():
            # A card with no title and no body is not persisted (removed on close).
            return
        title = self._title.text()
        body = self._body_html()
        # When the user leaves the title blank, seed it from the first line of
        # the body so the card is never untitled.
        if not title.strip():
            seeded = self._first_body_line()
            if seeded:
                title = seeded
                self._title.blockSignals(True)
                self._title.setText(title)
                self._title.blockSignals(False)
                self._update_window_title()
        if (title, body) == self._snapshot:
            return
        try:
            _store().update_daily_card(self._card_id, title, body)
        except PermissionDenied as exc:
            # This is a debounced autosave, so a silent failure would quietly
            # discard the typing. Stop the timer and say so once.
            self._save_timer.stop()
            QMessageBox.warning(self, "Card not saved", str(exc))
            return
        self._snapshot = (title, body)
        self.saved.emit(self._card_id)

    def _excel_field(self, label: str) -> str:
        return _excel_field_from_text(self._body.toPlainText(), label)

    def _copy_excel_row(self) -> None:
        row = _excel_row_for_card(self._title.text(), self._body.toPlainText())
        QApplication.clipboard().setText(row)
        self._copy_excel_btn.setText("Copied ✓")

        def _reset() -> None:
            if not sip.isdeleted(self._copy_excel_btn):
                self._copy_excel_btn.setText("Copy → Excel")

        QTimer.singleShot(1200, _reset)

    # --- AI Assistant (match score + application Q&A) -----------------------

    def _build_ai_panel(self) -> QWidget:
        box = QGroupBox("AI Assistant")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(6)

        # Duplicate-application warning: earlier applications that share this
        # card's company, role, or link. Hidden until there is at least one.
        self._similar_box = QWidget()
        sim_lay = QVBoxLayout(self._similar_box)
        sim_lay.setContentsMargins(0, 0, 0, 0)
        sim_lay.setSpacing(2)
        self._similar_header = QLabel("")
        _sim_hf = QFont(self.font())
        _sim_hf.setBold(True)
        self._similar_header.setFont(_sim_hf)
        self._similar_header.setStyleSheet("color: #C77800;")  # amber = heads-up
        sim_lay.addWidget(self._similar_header)
        self._similar_list = QListWidget()
        self._similar_list.setMaximumHeight(108)
        self._similar_list.setSelectionMode(
            QListWidget.SelectionMode.NoSelection
        )
        self._similar_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._similar_list.setCursor(Qt.CursorShape.PointingHandCursor)
        self._similar_list.setToolTip(
            "Earlier applications that look like this one — click one to open it."
        )
        self._similar_list.itemClicked.connect(self._on_similar_job_clicked)
        sim_lay.addWidget(self._similar_list)
        self._similar_box.hide()
        lay.addWidget(self._similar_box)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Resume"))
        self._ai_resume_combo = QComboBox()
        self._ai_resume_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._ai_resume_combo.currentIndexChanged.connect(self._on_ai_resume_changed)
        row1.addWidget(self._ai_resume_combo, stretch=1)
        manage_btn = QPushButton("Manage…")
        manage_btn.setToolTip("Add or edit resumes")
        manage_btn.clicked.connect(self._open_resume_manager)
        row1.addWidget(manage_btn)
        self._ai_match_btn = QPushButton("Check match score")
        self._ai_match_btn.clicked.connect(self._on_check_match)
        row1.addWidget(self._ai_match_btn)
        lay.addLayout(row1)

        links_row = QHBoxLayout()
        copy_icon = _daily_glyph_icon("copy", QColor(_daily_theme_colors().text))

        def _copy_button(label: str, tooltip: str, slot) -> QPushButton:  # type: ignore[no-untyped-def]
            # The copy icon replaces the word "Copy" so the row stays compact.
            btn = QPushButton(label)
            btn.setIcon(copy_icon)
            btn.setToolTip(tooltip)
            btn.clicked.connect(slot)
            links_row.addWidget(btn)
            return btn

        self._ai_linkedin_btn = _copy_button(
            "LinkedIn",
            "Copy your LinkedIn URL (set in Settings) to the clipboard",
            self._on_copy_linkedin,
        )
        self._ai_git_btn = _copy_button(
            "Git",
            "Copy your GitHub/Git URL (set in Settings) to the clipboard",
            self._on_copy_github,
        )
        self._ai_portfolio_btn = _copy_button(
            "Portfolio",
            "Copy your web portfolio URL (set in Settings) to the clipboard",
            self._on_copy_portfolio,
        )
        self._ai_address_btn = _copy_button(
            "Address",
            "Copy your address (set in Settings) to the clipboard",
            self._on_copy_address,
        )
        self._ai_phone_btn = _copy_button(
            "Phone",
            "Copy your phone number (set in Settings) to the clipboard",
            self._on_copy_phone,
        )
        self._ai_preview_btn = QPushButton("Preview resume")
        self._ai_preview_btn.setToolTip("Open the selected resume (its text is selectable to copy)")
        self._ai_preview_btn.clicked.connect(self._on_preview_resume)
        links_row.addWidget(self._ai_preview_btn)
        links_row.addStretch()
        lay.addLayout(links_row)

        self._ai_match_label = QLabel("")
        self._ai_match_label.setWordWrap(True)
        self._ai_match_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._ai_match_label.hide()
        lay.addWidget(self._ai_match_label)

        quick_row = QHBoxLayout()
        quick_row.addWidget(QLabel("Quick:"))
        self._ai_why_btn = QPushButton("Why this company?")
        self._ai_why_btn.setToolTip("Ask: why do you want to work at this company?")
        self._ai_why_btn.clicked.connect(self._on_why_company)
        quick_row.addWidget(self._ai_why_btn)
        self._ai_remote_btn = QPushButton("Is it remote?")
        self._ai_remote_btn.setToolTip("Ask whether this is a remote role (from the job description)")
        self._ai_remote_btn.clicked.connect(self._on_remote_question)
        quick_row.addWidget(self._ai_remote_btn)
        self._ai_cover_btn = QPushButton("Cover letter")
        self._ai_cover_btn.setToolTip("Draft a cover letter from your resume and this job")
        self._ai_cover_btn.clicked.connect(self._on_cover_letter)
        quick_row.addWidget(self._ai_cover_btn)
        quick_row.addStretch()
        lay.addLayout(quick_row)
        # Keep the button label in sync with the company in the job description.
        self._body.textChanged.connect(self._refresh_quick_questions)

        opts_row = QHBoxLayout()
        opts_row.addWidget(QLabel("Language"))
        self._ai_lang_combo = QComboBox()
        self._ai_lang_combo.addItem("En", "English")
        self._ai_lang_combo.addItem("Es", "Spanish")
        self._ai_lang_combo.addItem("En & Es", "English and Spanish")
        self._ai_lang_combo.setToolTip(
            "Language for AI answers: En (English), Es (Spanish), or En & Es "
            "(the answer is written in both English and Spanish)"
        )
        opts_row.addWidget(self._ai_lang_combo)
        opts_row.addWidget(QLabel("Max chars"))
        self._ai_char_limit = QSpinBox()
        self._ai_char_limit.setRange(0, 10000)
        self._ai_char_limit.setSingleStep(50)
        self._ai_char_limit.setSpecialValueText("No limit")
        self._ai_char_limit.setToolTip(
            "Limit the answer length in characters (0 = no limit). "
            "Your last choice is remembered."
        )
        # Start from whatever the user last set, so a preferred limit does not
        # have to be re-typed for every card.
        self._ai_char_limit.setValue(_restored_ai_char_limit())
        self._ai_char_limit.valueChanged.connect(_remember_ai_char_limit)
        opts_row.addWidget(self._ai_char_limit)
        opts_row.addStretch()
        lay.addLayout(opts_row)

        row2 = QHBoxLayout()
        self._ai_question = QLineEdit()
        self._ai_question.setPlaceholderText("Paste an application question…")
        self._ai_question.returnPressed.connect(self._on_get_answer)
        row2.addWidget(self._ai_question, stretch=1)
        self._ai_paste_question_btn = _daily_icon_button(
            "Replace the question with the clipboard contents", "paste"
        )
        self._ai_paste_question_btn.clicked.connect(self._on_paste_question)
        row2.addWidget(self._ai_paste_question_btn)
        self._ai_answer_btn = QPushButton("Get answer")
        self._ai_answer_btn.clicked.connect(self._on_get_answer)
        row2.addWidget(self._ai_answer_btn)
        lay.addLayout(row2)

        self._ai_answer = QPlainTextEdit()
        self._ai_answer.setReadOnly(True)
        self._ai_answer.setPlaceholderText("The answer will appear here.")
        self._ai_answer.setMinimumHeight(120)
        self._ai_answer.setMaximumHeight(300)
        self._ai_answer.hide()
        lay.addWidget(self._ai_answer)

        ans_row = QHBoxLayout()
        self._ai_status = QLabel("")
        self._ai_status.setStyleSheet("color: gray;")
        self._ai_status.setWordWrap(True)
        ans_row.addWidget(self._ai_status, stretch=1)
        self._ai_copy_btn = QPushButton("Add to description")
        self._ai_copy_btn.setIcon(copy_icon)
        self._ai_copy_btn.setToolTip(
            "Copy the answer and append it to the description as a Q & A block"
        )
        self._ai_copy_btn.clicked.connect(self._on_copy_answer)
        self._ai_copy_btn.hide()
        ans_row.addWidget(self._ai_copy_btn)
        lay.addLayout(ans_row)
        lay.addStretch(1)  # keep controls anchored to the top of the column

        # Make "Get answer" the dialog default (Enter), not "Manage…".
        for b in (
            manage_btn,
            self._ai_match_btn,
            self._ai_why_btn,
            self._ai_remote_btn,
            self._ai_cover_btn,
            self._ai_copy_btn,
            self._ai_linkedin_btn,
            self._ai_git_btn,
            self._ai_portfolio_btn,
            self._ai_address_btn,
            self._ai_phone_btn,
            self._ai_preview_btn,
        ):
            b.setAutoDefault(False)
            b.setDefault(False)
        self._ai_answer_btn.setAutoDefault(True)
        self._ai_answer_btn.setDefault(True)

        self._pending_question = ""
        self._reload_ai_resumes()
        self._refresh_quick_questions()
        return box

    def _company_name(self) -> str:
        """Company from the job description (the 'Company:' line), if present."""
        return self._excel_field("Company")

    def _refresh_quick_questions(self) -> None:
        if not hasattr(self, "_ai_why_btn"):
            return
        company = self._company_name()
        self._ai_why_btn.setText(f"Why {company}?" if company else "Why this company?")

    def _on_why_company(self) -> None:
        company = self._company_name()
        question = (
            f"Why do you want to work at {company}?"
            if company
            else "Why do you want to work at this company?"
        )
        self._ai_question.setText(question)
        self._on_get_answer()

    def _on_remote_question(self) -> None:
        # A factual question about the posting — uses the job description only.
        if self._ai_thread is not None:
            return
        if not self._require_api_key():
            return
        job = self._body.toPlainText().strip()
        if not job:
            QMessageBox.information(
                self, "AI Assistant",
                "Add the job description to the card's Description first.",
            )
            return
        question = "Is this a remote role?"
        self._ai_question.setText(question)
        self._pending_question = question
        language = self._ai_language()
        max_chars = self._ai_max_chars()
        self._begin_answer_stream()
        self._set_ai_busy(True, "Checking the posting…")
        self._run_ai(
            lambda on_chunk: ai_assistant.ask_job_question(
                job, question, language=language, max_chars=max_chars, on_chunk=on_chunk
            ),
            self._on_answer_done,
            self._on_answer_chunk,
        )

    def _on_cover_letter(self) -> None:
        pre = self._ai_preflight()
        if pre is None:
            return
        job, resume = pre
        db.touch_resume(resume.id)
        self._pending_question = "Cover letter"
        language = self._ai_language()
        max_chars = self._ai_max_chars()
        self._begin_answer_stream()
        self._set_ai_busy(True, "Writing cover letter…")
        self._run_ai(
            lambda on_chunk: ai_assistant.write_cover_letter(
                resume.content, job, resume.pdf,
                language=language, max_chars=max_chars, on_chunk=on_chunk,
            ),
            self._on_answer_done,
            self._on_answer_chunk,
        )

    def _copy_profile_link(self, settings_key: str, label: str) -> None:
        value = QSettings(_SETTINGS_ORG, _SETTINGS_APP).value(settings_key, "")
        value = value.strip() if isinstance(value, str) else ""
        if not value:
            QMessageBox.information(
                self, "Copy",
                f"Set your {label} in Settings > App settings first.",
            )
            return
        QApplication.clipboard().setText(value)
        self._ai_status.setText(f"{label} copied to clipboard.")

    def _on_copy_linkedin(self) -> None:
        self._copy_profile_link("linkedin_url", "LinkedIn")

    def _on_copy_github(self) -> None:
        self._copy_profile_link("github_url", "Git")

    def _on_copy_portfolio(self) -> None:
        self._copy_profile_link("portfolio_url", "Portfolio")

    def _on_copy_address(self) -> None:
        self._copy_profile_link("address", "Address")

    def _on_copy_phone(self) -> None:
        self._copy_profile_link("phone", "Phone")

    def _on_preview_resume(self) -> None:
        resume = self._selected_resume()
        if resume is None:
            QMessageBox.information(
                self, "Preview resume", "Select a resume, or add one via Manage…"
            )
            return
        from resumes_ui import ResumePreviewDialog

        ResumePreviewDialog(
            resume.name or "Resume",
            resume.is_pdf,
            resume.content,
            resume.fmt,
            resume.pdf,
            self,
        ).exec()

    def _reload_ai_resumes(self) -> None:
        combo = self._ai_resume_combo
        combo.blockSignals(True)
        combo.clear()
        resumes = db.list_resumes()
        for r in resumes:
            combo.addItem(r.name or "Untitled", r.id)
        if not resumes:
            combo.addItem("(no resumes — click Manage…)", None)
        combo.blockSignals(False)
        if not resumes:
            return
        last = QSettings(_SETTINGS_ORG, _SETTINGS_APP).value("daily_ai_last_resume_id")
        try:
            last_id = int(last) if last is not None else None
        except (TypeError, ValueError):
            last_id = None
        idx = combo.findData(last_id) if last_id is not None else -1
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _on_ai_resume_changed(self) -> None:
        rid = self._ai_resume_combo.currentData()
        if isinstance(rid, int):
            QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue("daily_ai_last_resume_id", rid)

    def _open_resume_manager(self) -> None:
        from resumes_ui import open_resume_manager

        # Non-modal, so there is no exec() to wait on: the combo is refreshed
        # from the window's own signal as resumes are added or renamed, rather
        # than once when it closes.
        open_resume_manager(on_changed=self._reload_ai_resumes)

    def _selected_resume(self) -> "db.ResumeRow | None":
        rid = self._ai_resume_combo.currentData()
        return db.get_resume(rid) if isinstance(rid, int) else None

    def _require_api_key(self) -> bool:
        """True when the active provider's API key is set; else show a hint."""
        if ai_assistant.has_api_key():
            return True
        QMessageBox.information(
            self, "AI Assistant",
            f"Add your {ai_assistant.provider_label()} API key in Settings > App "
            "settings to use the AI Assistant (switch provider there too).",
        )
        return False

    def _ai_preflight(self):  # type: ignore[no-untyped-def]
        """Return (job, resume) when ready, else show a hint and return None."""
        if self._ai_thread is not None:
            return None
        if not self._require_api_key():
            return None
        job = self._body.toPlainText().strip()
        if not job:
            QMessageBox.information(
                self, "AI Assistant",
                "Add the job description to the card's Description first.",
            )
            return None
        resume = self._selected_resume()
        if resume is None:
            QMessageBox.information(
                self, "AI Assistant", "Select a resume, or add one via Manage…"
            )
            return None
        return job, resume

    def _on_check_match(self) -> None:
        pre = self._ai_preflight()
        if pre is None:
            return
        job, resume = pre
        db.touch_resume(resume.id)
        self._set_ai_busy(True, "Scoring match…")
        self._run_ai(
            lambda on_chunk: ai_assistant.match_score(resume.content, job, resume.pdf),
            self._on_match_done,
        )

    def _on_match_done(self, result, error: str) -> None:  # type: ignore[no-untyped-def]
        self._set_ai_busy(False)
        if error:
            self._ai_status.setText(error)
            return
        self._ai_status.setText("")
        parts = [f"<b>Match score: {result.score}%</b>"]
        if result.summary:
            parts.append(escape(result.summary))
        if result.strengths:
            parts.append("<b>Strengths:</b> " + escape("; ".join(result.strengths)))
        if result.gaps:
            parts.append("<b>Gaps:</b> " + escape("; ".join(result.gaps)))
        self._ai_match_label.setText("<br>".join(parts))
        self._ai_match_label.show()

    def _ai_language(self) -> str:
        """Target language for AI answers, from the language selector
        ('English', 'Spanish', or 'English and Spanish')."""
        data = self._ai_lang_combo.currentData()
        return data if isinstance(data, str) else "English"

    def _ai_max_chars(self) -> "int | None":
        """Answer character limit, or None when set to 0 (no limit)."""
        value = self._ai_char_limit.value()
        return value if value > 0 else None

    def _on_paste_question(self) -> None:
        """Replace the question with the clipboard text, then get an answer."""
        text = QApplication.clipboard().text()
        self._ai_question.setText(text.strip())
        self._ai_question.setFocus()
        self._on_get_answer()

    def _on_get_answer(self) -> None:
        question = self._ai_question.text().strip()
        if not question:
            QMessageBox.information(self, "AI Assistant", "Type an application question first.")
            return
        pre = self._ai_preflight()
        if pre is None:
            return
        job, resume = pre
        db.touch_resume(resume.id)
        self._pending_question = question
        language = self._ai_language()
        max_chars = self._ai_max_chars()
        self._begin_answer_stream()
        self._set_ai_busy(True, "Writing answer…")
        self._run_ai(
            lambda on_chunk: ai_assistant.answer_question(
                resume.content, job, question, resume.pdf,
                language=language, max_chars=max_chars, on_chunk=on_chunk,
            ),
            self._on_answer_done,
            self._on_answer_chunk,
        )

    def _begin_answer_stream(self) -> None:
        """Reset the answer box so streamed tokens can fill it live."""
        self._ai_answer.clear()
        self._ai_answer.show()
        self._ai_copy_btn.setVisible(False)

    def _on_answer_chunk(self, text: str) -> None:
        """Append a streamed token chunk to the answer box (GUI thread)."""
        if not text:
            return
        self._ai_answer.show()
        self._ai_answer.moveCursor(QTextCursor.MoveOperation.End)
        self._ai_answer.insertPlainText(text)
        self._ai_answer.moveCursor(QTextCursor.MoveOperation.End)

    def _on_answer_done(self, result, error: str) -> None:  # type: ignore[no-untyped-def]
        self._set_ai_busy(False)
        if error:
            self._ai_status.setText(error)
            return
        self._ai_status.setText("")
        # Replace the streamed raw text with the final normalized text.
        self._ai_answer.setPlainText(result or "")
        self._ai_answer.show()
        self._ai_copy_btn.setVisible(bool(result))

    def begin_ai_paste(self, text: str) -> None:
        """Parse a copied job posting into structured details in the body."""
        if self._ai_thread is not None:
            return
        if not text.strip() or not self._require_api_key():
            return
        self._set_ai_busy(True, "Analyzing clipboard…")
        self._run_ai(
            lambda on_chunk: ai_assistant.parse_job_posting(text),
            self._on_ai_paste_done,
        )

    def _on_ai_paste_done(self, result, error: str) -> None:  # type: ignore[no-untyped-def]
        self._set_ai_busy(False)
        if error:
            self._ai_status.setText(error)
            return
        if result and result.strip():
            self._set_body_content(result)
            seeded = self._first_body_line()
            if seeded:
                self._title.setText(seeded)
            self._on_field_changed()
            self._ai_status.setText("Parsed clipboard job details into the description.")

    def _on_copy_answer(self) -> None:
        answer = self._ai_answer.toPlainText().strip()
        if not answer:
            return
        QApplication.clipboard().setText(answer)
        question = (self._pending_question or "").strip()
        block = f"Q: {question}\nA: {answer}" if question else f"A: {answer}"
        cursor = self._body.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText("\n\n" + block + "\n")
        self._body.setTextCursor(cursor)
        self._on_field_changed()
        self._ai_status.setText("Copied to clipboard and added to the description.")

    def _set_ai_busy(self, busy: bool, message: str = "") -> None:
        for w in (
            self._ai_match_btn,
            self._ai_answer_btn,
            self._ai_resume_combo,
            self._ai_why_btn,
            self._ai_remote_btn,
            self._ai_cover_btn,
            self._ai_lang_combo,
            self._ai_char_limit,
        ):
            w.setEnabled(not busy)
        if busy:
            self._ai_status.setText(message or "Working…")

    def _run_ai(self, fn, on_done, on_chunk=None) -> None:  # type: ignore[no-untyped-def]
        if self._ai_thread is not None:
            return
        thread = QThread(self)
        worker = ai_assistant.AIWorker(fn)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(on_done)  # bound method: auto-disconnects if dialog closes
        if on_chunk is not None:
            worker.chunk.connect(on_chunk)  # live token stream -> answer box
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_ai_thread)
        self._ai_thread = thread
        self._ai_worker = worker
        thread.start()

    def _clear_ai_thread(self) -> None:
        self._ai_thread = None
        self._ai_worker = None

    def _restore_saved_geometry(self) -> None:
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        geo = settings.value(_DAILY_CARD_EDITOR_GEOMETRY_KEY)
        if geo is not None and isinstance(geo, QByteArray) and not geo.isEmpty():
            self.restoreGeometry(geo)

    def _persist_geometry(self) -> None:
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        settings.setValue(_DAILY_CARD_EDITOR_GEOMETRY_KEY, self.saveGeometry())

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._ai_thread is not None and self._ai_thread.isRunning():
            QMessageBox.information(
                self, "AI Assistant", "Please wait for the AI Assistant to finish."
            )
            event.ignore()
            return
        self._save_timer.stop()
        if (self._is_empty() and getattr(self, "_was_empty_on_open", False)
                and getattr(self, "_may_edit", True)):
            # Discard a never-used placeholder (opened blank, still blank). A card
            # that already had content is kept even if its fields were cleared.
            # Never somebody else's, though: an empty card of theirs is theirs to
            # remove, and the attempt would raise on the way out of the window.
            if _store().get_daily_card(self._card_id) is not None:
                _store().delete_daily_card(self._card_id)
                self.deleted.emit(self._card_id)  # board drops the one row
        else:
            self._flush_save()
        self._persist_geometry()
        self.closed_for_id.emit(self._card_id)
        super().closeEvent(event)


class DailyCardWidget(QFrame):
    """Draggable card; whole surface drags except the ⋮ menu."""

    def __init__(
        self,
        card: db.DailyCardRow,
        page: "DailyBoardPage",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._card_id = card.id
        self._card_body = card.body
        self._page = page
        self._status = card.status or _DAILY_STATUS_NONE
        self._drag_start: object | None = None
        self._move_armed = False
        self._body_hidden = self._load_body_hidden(card)
        self.setObjectName("DailyCardWidget")
        self.setProperty("daily_card_id", card.id)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setAcceptDrops(False)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self.setMinimumWidth(0)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        title_text = card.title.strip() or "(no title)"
        self._title_label = QLabel(_soft_wrap_long_tokens(title_text))
        self._title_label.setObjectName("dailyCardTitle")
        self._title_label.setWordWrap(True)
        self._title_label.setMinimumWidth(0)
        self._title_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Maximum,
        )
        self._title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._status_badge = QToolButton()
        self._status_badge.setObjectName("dailyCardStatusBadge")
        self._status_badge.setToolTip("Set card status")
        self._status_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self._status_badge.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._status_badge.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self._status_badge.clicked.connect(self._show_status_menu)

        self._menu_btn = _daily_icon_button("Edit or delete card", "menu")
        self._menu_btn.clicked.connect(self._show_menu)
        self._hide_body_btn = _daily_icon_button("Hide body preview", "eye")
        self._hide_body_btn.setCheckable(True)
        self._hide_body_btn.toggled.connect(self._set_body_hidden)

        # Created date sits in the top-left corner so it stays visible even when
        # the body preview is hidden (compact mode).
        self._date_label = QLabel(_format_created(card.created_at))
        self._date_label.setObjectName("dailyCardDate")
        self._date_label.setMinimumWidth(0)
        self._date_label.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Maximum,
        )
        self._date_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        # Checkbox to select the card for the column's bulk Copy -> Excel.
        self._select_cb = QCheckBox()
        self._select_cb.setObjectName("dailyCardSelect")
        self._select_cb.setToolTip("Select this card for the column's Copy → Excel")
        self._select_cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._select_cb.setCursor(Qt.CursorShape.PointingHandCursor)

        # Top bar: select checkbox + created date pinned top-left; status badge +
        # icon buttons pinned top-right. The title gets its own full-width row
        # below so long titles wrap naturally instead of being squeezed.
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(8, 5, 4, 0)
        top_bar.setSpacing(4)
        top_bar.addWidget(self._select_cb, 0, Qt.AlignmentFlag.AlignVCenter)
        top_bar.addWidget(self._date_label, 0, Qt.AlignmentFlag.AlignVCenter)
        top_bar.addStretch(1)
        top_bar.addWidget(self._status_badge, 0, Qt.AlignmentFlag.AlignBottom)
        top_bar.addWidget(self._hide_body_btn, 0, Qt.AlignmentFlag.AlignBottom)
        top_bar.addWidget(self._menu_btn, 0, Qt.AlignmentFlag.AlignBottom)
        root.addLayout(top_bar)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(8, 2, 8, 0)
        title_row.setSpacing(0)
        title_row.addWidget(self._title_label, alignment=Qt.AlignmentFlag.AlignTop)
        root.addLayout(title_row)

        preview = _body_preview(card.body)
        self._preview_label = QLabel(preview)
        self._preview_label.setObjectName("dailyCardPreview")
        self._preview_label.setWordWrap(True)
        self._preview_label.setMinimumWidth(0)
        self._preview_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Maximum,
        )
        self._preview_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        if not preview:
            self._preview_label.hide()

        self._body_panel = QWidget()
        self._body_panel.setObjectName("dailyCardBody")
        self._body_panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # No interactive children here, so let mouse events fall through to the
        # card frame; this keeps the whole body usable as a drag handle.
        self._body_panel.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        body_lay = QVBoxLayout(self._body_panel)
        body_lay.setContentsMargins(10, 6, 4, 8)
        body_lay.setSpacing(4)
        body_lay.addWidget(self._preview_label)
        root.addWidget(self._body_panel)

        if not preview:
            self._hide_body_btn.hide()
        # Board-wide compact mode: drop the body panel entirely so more cards
        # fit on screen. The eye toggle only hides a visible body, so hide it too.
        if not self._page.card_content_visible():
            self._body_panel.hide()
            self._hide_body_btn.hide()
        self._apply_status_style()

    def _icon_glyph_color(self) -> QColor:
        """White icons read better over a coloured status header; muted otherwise."""
        if _daily_status_color(self._status):
            return QColor("#FFFFFF")
        return QColor(_daily_theme_colors().muted)

    def _apply_status_style(self) -> None:
        """Tint the card edge + top status bar and recolour the badge/icons."""
        color = _daily_status_color(self._status)
        theme = _daily_theme_colors()
        if color:
            # Status colour bleeds across the card edge and the top status bar;
            # the body keeps the normal surface so the preview stays readable.
            border = (
                f"2px dashed {theme.highlight}" if self._move_armed else f"1px solid {color}"
            )
            self.setStyleSheet(
                f"QFrame#DailyCardWidget {{ background-color: {color}; "
                f"border: {border}; border-radius: 6px; }}"
            )
            self._title_label.setStyleSheet(
                "QLabel#dailyCardTitle { background: transparent; color: #FFFFFF; "
                "font-weight: bold; padding: 0px; }"
            )
            self._status_badge.setText(self._status)
            self._status_badge.setStyleSheet(
                "QToolButton#dailyCardStatusBadge { color: #FFFFFF; "
                "background-color: rgba(0, 0, 0, 55); border: none; border-radius: 8px; "
                "padding: 2px 8px; font-size: 10px; font-weight: bold; }"
                "QToolButton#dailyCardStatusBadge:hover { background-color: rgba(0, 0, 0, 95); }"
            )
            self._status_badge.setToolTip(f"Status: {self._status} — click to change")
            self._body_panel.setStyleSheet(
                f"QWidget#dailyCardBody {{ background-color: {theme.card}; "
                "border-bottom-left-radius: 6px; border-bottom-right-radius: 6px; }}"
            )
            self._date_label.setStyleSheet(
                "QLabel#dailyCardDate { color: rgba(255, 255, 255, 200); "
                "background: transparent; font-size: 10px; }"
            )
        else:
            if self._move_armed:
                self.setStyleSheet(
                    f"QFrame#DailyCardWidget {{ border: 2px dashed {theme.highlight}; "
                    "border-radius: 6px; }}"
                )
            else:
                self.setStyleSheet("")  # fall back to the shared page stylesheet
            self._title_label.setStyleSheet(
                f"QLabel#dailyCardTitle {{ background: transparent; color: {theme.text}; "
                "font-weight: bold; padding: 0px; }}"
            )
            self._status_badge.setText("Status ▾")
            self._status_badge.setStyleSheet(
                f"QToolButton#dailyCardStatusBadge {{ color: {theme.muted}; "
                f"background-color: transparent; border: 1px solid {theme.border}; "
                "border-radius: 8px; padding: 2px 8px; font-size: 10px; }"
                f"QToolButton#dailyCardStatusBadge:hover {{ border-color: {theme.highlight}; }}"
            )
            self._status_badge.setToolTip("Set card status")
            self._body_panel.setStyleSheet(
                "QWidget#dailyCardBody { background: transparent; }"
            )
            self._date_label.setStyleSheet("")  # fall back to the page stylesheet
        self._menu_btn.setIcon(_daily_glyph_icon("menu", self._icon_glyph_color()))
        self._apply_body_hidden_state()

    def _show_status_menu(self) -> None:
        menu = QMenu(self)
        none_act = menu.addAction("No status")
        none_act.setCheckable(True)
        none_act.setChecked(not self._status)
        menu.addSeparator()
        actions: dict[object, str] = {none_act: _DAILY_STATUS_NONE}
        for name in _DAILY_STATUS_ORDER:
            act = menu.addAction(_daily_status_swatch(_DAILY_STATUS_COLORS[name]), name)
            act.setCheckable(True)
            act.setChecked(self._status == name)
            actions[act] = name
        chosen = menu.exec(
            self._status_badge.mapToGlobal(self._status_badge.rect().bottomLeft())
        )
        if chosen is None:
            return
        new_status = actions.get(chosen)
        if new_status is None or new_status == self._status:
            return
        self._set_status(new_status)

    def _set_status(self, status: str) -> None:
        _store().update_daily_card_status(self._card_id, status)
        row = _store().get_daily_card(self._card_id)
        self._status = row.status if row is not None else status
        self._apply_status_style()

    def _settings_key(self) -> str:
        return f"{_DAILY_CARD_BODY_HIDDEN_PREFIX}/{self._card_id}"

    def _load_body_hidden(self, card: db.DailyCardRow) -> bool:
        fresh = _store().get_daily_card(card.id)
        if fresh is not None:
            return bool(fresh.body_hidden)
        if card.body_hidden:
            return True
        # Migrate any local setting saved by older builds into the card row.
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        hidden = settings.value(self._settings_key(), False, type=bool)
        if hidden:
            _store().update_daily_card_body_hidden(self._card_id, True)
            settings.remove(self._settings_key())
        return hidden

    def _save_body_hidden(self) -> None:
        _store().update_daily_card_body_hidden(self._card_id, self._body_hidden)

    def _apply_body_hidden_state(self) -> None:
        self._hide_body_btn.blockSignals(True)
        self._hide_body_btn.setChecked(self._body_hidden)
        self._hide_body_btn.blockSignals(False)

        icon_color = self._icon_glyph_color()
        if self._body_hidden:
            blur = QGraphicsBlurEffect(self._preview_label)
            blur.setBlurRadius(8.0)
            self._preview_label.setGraphicsEffect(blur)
            self._preview_label.setText("Hidden body preview - double-click card to open")
            self._hide_body_btn.setIcon(_daily_glyph_icon("eye_off", icon_color))
            self._hide_body_btn.setToolTip(
                "Show body preview. Double-click the card to open and confirm content."
            )
        else:
            self._preview_label.setGraphicsEffect(None)
            self._preview_label.setText(_body_preview(self._card_body))
            self._hide_body_btn.setIcon(_daily_glyph_icon("eye", icon_color))
            self._hide_body_btn.setToolTip("Hide body preview")

    def _set_body_hidden(self, hidden: bool) -> None:
        _store().update_daily_card_body_hidden(self._card_id, hidden)
        row = _store().get_daily_card(self._card_id)
        self._body_hidden = bool(row.body_hidden) if row is not None else hidden
        self._apply_body_hidden_state()

    def update_content_from_row(self, row: db.DailyCardRow) -> None:
        """Refresh title, body preview, and status in place — no widget teardown.

        Lets an open editor's debounced save update just this card instead of
        rebuilding the whole board, which caused the column-width flicker and lag
        while typing.
        """
        title_text = (row.title or "").strip() or "(no title)"
        self._title_label.setText(_soft_wrap_long_tokens(title_text))
        self._card_body = row.body
        has_preview = bool(_body_preview(row.body))
        content_visible = self._page.card_content_visible()
        self._preview_label.setVisible(has_preview and content_visible)
        self._hide_body_btn.setVisible(has_preview and content_visible)
        self._status = row.status or _DAILY_STATUS_NONE
        # Re-renders the status styling and (via _apply_body_hidden_state) the
        # preview text from the refreshed body.
        self._apply_status_style()

    def card_id(self) -> int:
        return self._card_id

    def is_selected(self) -> bool:
        return self._select_cb.isChecked()

    def set_selected(self, on: bool) -> None:
        self._select_cb.setChecked(on)

    def _show_menu(self) -> None:
        menu = QMenu(self)
        edit_act = menu.addAction("Edit")
        move_act = menu.addAction("Move to another column…")
        export_menu = menu.addMenu("Export")
        export_md_act = export_menu.addAction("Markdown (.md)…")
        export_html_act = export_menu.addAction("HTML (.html)…")
        menu.addSeparator()
        delete_act = menu.addAction("Delete")
        chosen = menu.exec(self._menu_btn.mapToGlobal(self._menu_btn.rect().bottomLeft()))
        if chosen == edit_act:
            QTimer.singleShot(0, lambda cid=self._card_id: self._page._deferred_open_editor(cid))
        elif chosen == move_act:
            self._arm_move()
        elif chosen == export_md_act:
            self._export_card("md")
        elif chosen == export_html_act:
            self._export_card("html")
        elif chosen == delete_act:
            self._page._delete_card(self._card_id)

    def _arm_move(self) -> None:
        """Enter move mode: the next drag of this card relocates it (with a
        confirmation on drop). Avoids conflicting with board panning."""
        self._page._set_armed_card(self)
        self._set_move_armed(True)
        QToolTip.showText(
            self.mapToGlobal(self.rect().topLeft()),
            "Drag this card onto another column to move it.",
            self,
        )

    def _set_move_armed(self, on: bool) -> None:
        if self._move_armed == on:
            return
        self._move_armed = on
        self.setCursor(
            Qt.CursorShape.OpenHandCursor if on else Qt.CursorShape.OpenHandCursor
        )
        self._apply_status_style()  # re-render the armed highlight border

    def _export_card(self, fmt: Literal["md", "html"]) -> None:
        row = _store().get_daily_card(self._card_id)
        if row is None:
            QMessageBox.warning(self, "Export card", "This card no longer exists.")
            return
        if fmt == "md":
            content = _card_to_markdown(row)
            ext, filter_str = "md", "Markdown (*.md)"
        else:
            content = _card_to_html_document(row)
            ext, filter_str = "html", "HTML (*.html)"
        base = _safe_filename((row.title or "").strip() or "card")
        path, _ = QFileDialog.getSaveFileName(
            self,
            f"Export card as {ext.upper()}",
            f"{base}.{ext}",
            filter_str,
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            QMessageBox.critical(
                self, "Export card", f"Could not write the file:\n{exc}"
            )

    def _start_drag_if_ready(self, event: QMouseEvent) -> bool:
        # Normal drag does nothing (that pans the board); only a card armed via
        # the ⋮ "Move" menu item can be dragged to another column.
        if not self._move_armed:
            return False
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return False
        if self._drag_start is None:
            return False
        if (
            event.position().toPoint() - self._drag_start
        ).manhattanLength() < QApplication.startDragDistance():
            return False
        drag = QDrag(self)
        mime = QByteArray(str(self._card_id).encode("ascii"))
        drag.setMimeData(_daily_mime_data(mime))
        drag.exec(Qt.DropAction.MoveAction)
        self._drag_start = None
        self._set_move_armed(False)
        self._page._set_armed_card(None)
        return True

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton and self._move_armed:
            self._drag_start = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._start_drag_if_ready(event):
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        self._drag_start = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            QTimer.singleShot(0, lambda cid=self._card_id: self._page._deferred_open_editor(cid))
        super().mouseDoubleClickEvent(event)


def _daily_mime_data(payload: QByteArray):
    from PyQt6.QtCore import QMimeData

    mime = QMimeData()
    mime.setData(DAILY_MIME_CARD, payload)
    return mime


def _card_id_from_mime(mime) -> int | None:  # type: ignore[no-untyped-def]
    if not mime.hasFormat(DAILY_MIME_CARD):
        return None
    try:
        return int(bytes(mime.data(DAILY_MIME_CARD)).decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None


def _daily_list_mime_data(list_id: int):
    from PyQt6.QtCore import QMimeData

    mime = QMimeData()
    mime.setData(DAILY_MIME_LIST, QByteArray(str(list_id).encode("ascii")))
    return mime


def _list_id_from_mime(mime) -> int | None:  # type: ignore[no-untyped-def]
    if not mime.hasFormat(DAILY_MIME_LIST):
        return None
    try:
        return int(bytes(mime.data(DAILY_MIME_LIST)).decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None


class _CardsDropHost(QWidget):
    """Card list area that accepts dragged cards."""

    def __init__(self, list_id: int, page: "DailyBoardPage", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._list_id = list_id
        self._page = page
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if _card_id_from_mime(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if _card_id_from_mime(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        cid = _card_id_from_mime(event.mimeData())
        if cid is not None:
            self._page._move_card_to_list(cid, self._list_id)
            event.acceptProposedAction()


class DailyColumnWidget(QFrame):
    """One Kanban column; title is a label until the user renames."""

    def __init__(
        self,
        list_row: db.DailyListRow,
        page: "DailyBoardPage",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._list_row = list_row
        self._list_id = list_row.id
        self._always_shown = list_row.always_shown
        self._page = page
        self.setObjectName("DailyColumnWidget")
        if list_row.always_shown:
            self.setToolTip("Always shown — visible on every board day")
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFixedWidth(_DAILY_COLUMN_WIDTH_PX)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)

        hdr_host = _ColumnHeaderHost(self._list_id, page)
        hdr_row = QHBoxLayout(hdr_host)
        hdr_row.setContentsMargins(4, 4, 4, 4)
        hdr_row.setSpacing(6)
        self._title_widget = _ColumnTitleWidget(
            list_row.title or "Untitled",
            self._list_id,
            page,
        )
        self._title_widget.committed.connect(self._on_title_edited)
        add_btn = _daily_icon_button("Add card", "add")
        add_btn.clicked.connect(lambda: page._on_add_card(self._list_id))
        paste_btn = _daily_icon_button(
            "Paste clipboard into a new card", "paste"
        )
        paste_btn.clicked.connect(lambda: page._on_paste_card(self._list_id))
        ai_paste_btn = _daily_icon_button(
            "AI paste: parse clipboard job details into a new card", "ai_paste"
        )
        ai_paste_btn.clicked.connect(lambda: page._on_ai_paste_card(self._list_id))
        self._menu_btn = _daily_icon_button("Column options", "menu")
        self._menu_btn.clicked.connect(self._show_column_menu)
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(_DAILY_ICON_BTN_GAP_PX)
        btn_align = Qt.AlignmentFlag.AlignVCenter
        btn_row.addWidget(add_btn, 0, btn_align)
        btn_row.addWidget(paste_btn, 0, btn_align)
        btn_row.addWidget(ai_paste_btn, 0, btn_align)
        btn_row.addWidget(self._menu_btn, 0, btn_align)
        btn_host = QWidget()
        btn_host.setObjectName("dailyColumnIconBtns")
        btn_host.setLayout(btn_row)
        btn_host.setFixedWidth(
            _DAILY_ICON_BTN_PX * 4 + _DAILY_ICON_BTN_GAP_PX * 3
        )
        btn_host.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        align = Qt.AlignmentFlag.AlignVCenter
        hdr_row.addWidget(self._title_widget, stretch=1, alignment=align)
        hdr_row.addWidget(btn_host, stretch=0, alignment=align)
        outer.addWidget(hdr_host)

        self._cards_host = _CardsDropHost(self._list_id, page)
        self._cards_layout = QVBoxLayout(self._cards_host)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(6)
        self._cards_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self._cards_host)
        scroll.setMinimumHeight(200)
        scroll.viewport().setAcceptDrops(True)
        outer.addWidget(scroll, stretch=1)
        page._install_shift_horizontal_wheel(
            self,
            hdr_host,
            self._title_widget,
            scroll,
            scroll.viewport(),
            self._cards_host,
        )

    def list_id(self) -> int:
        return self._list_id

    def set_title(self, title: str) -> None:
        self._title_widget.set_text(title)

    def set_card_count(self, count: int) -> None:
        self._title_widget.set_card_count(count)

    def _on_title_edited(self, title: str) -> None:
        self._page._on_rename_column(self._list_id, title.strip() or "Untitled")

    def _show_column_menu(self) -> None:
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        always_act = menu.addAction("Always shown")
        always_act.setCheckable(True)
        always_act.setChecked(self._always_shown)
        always_act.setToolTip(
            "When checked, this column appears on the board for every day, "
            "regardless of the date filter."
        )
        content_act = menu.addAction("Show shortened content")
        content_act.setCheckable(True)
        content_act.setChecked(self._page.card_content_visible())
        content_act.setToolTip(
            "Show each card's body preview. Turn off to fit more cards on screen."
        )
        menu.addSeparator()
        cards = self._card_widgets()
        sel_count = sum(1 for c in cards if c.is_selected())
        copy_sel_act = menu.addAction(
            f"Copy selected cards → Excel ({sel_count})"
            if sel_count
            else "Copy selected cards → Excel"
        )
        copy_sel_act.setEnabled(sel_count > 0)
        copy_sel_act.setToolTip("Copy the checked cards as Excel rows (one card per row)")
        copy_all_act = menu.addAction(f"Copy all cards → Excel ({len(cards)})")
        copy_all_act.setEnabled(bool(cards))
        copy_all_act.setToolTip("Copy every card in this column as Excel rows")
        select_all_act = menu.addAction("Select all cards")
        select_all_act.setEnabled(bool(cards))
        clear_sel_act = menu.addAction("Clear selection")
        clear_sel_act.setEnabled(sel_count > 0)
        move_menu = menu.addMenu(
            f"Move selected to ({sel_count})" if sel_count else "Move selected to"
        )
        move_menu.setEnabled(sel_count > 0)
        move_targets: dict[object, int] = {}
        if sel_count > 0:
            target_rows = [
                r
                for r in _store().list_daily_lists_for_board(self._page._day_str())
                if r.id != self._list_id
            ]
            if target_rows:
                for r in target_rows:
                    move_targets[move_menu.addAction(r.title or "Untitled")] = r.id
            else:
                no_target = move_menu.addAction("(no other columns)")
                no_target.setEnabled(False)
        stats_act = menu.addAction("Card statistics by site…")
        stats_act.setEnabled(bool(cards))
        stats_act.setToolTip("Count cards grouped by job site (the part before ' - ')")
        menu.addSeparator()
        sort_menu = menu.addMenu("Sort cards by")
        current_sort = _store().get_daily_list_sort_mode(self._list_id)
        sort_manual_act = sort_menu.addAction("Manual (drag)")
        sort_manual_act.setToolTip("No automatic sorting; arrange cards by hand")
        sort_newest_act = sort_menu.addAction("Newest first")
        sort_newest_act.setToolTip("Most recently created cards on top")
        sort_oldest_act = sort_menu.addAction("Oldest first")
        sort_oldest_act.setToolTip("Earliest created cards on top")
        sort_status_act = sort_menu.addAction("Status")
        sort_status_act.setToolTip(
            "Sort by status: cards with the latest status (furthest down the "
            "status list) on top; cards with no status last"
        )
        # Show the remembered mode; it also decides where new cards are placed.
        for act, key in (
            (sort_manual_act, ""),
            (sort_newest_act, "newest"),
            (sort_oldest_act, "oldest"),
            (sort_status_act, "status"),
        ):
            act.setCheckable(True)
            act.setChecked(current_sort == key)
        menu.addSeparator()
        rename_act = menu.addAction("Rename…")
        delete_act = menu.addAction("Delete column")
        chosen = menu.exec(self._menu_btn.mapToGlobal(self._menu_btn.rect().bottomLeft()))
        if chosen in move_targets:
            ids = [c.card_id() for c in cards if c.is_selected()]
            self._page._move_cards_to_list(ids, move_targets[chosen])
        elif chosen == always_act:
            self._page._on_column_always_shown(self._list_id, always_act.isChecked())
        elif chosen == content_act:
            self._page._on_show_card_content_toggled(content_act.isChecked())
        elif chosen == copy_sel_act:
            self._copy_cards_to_excel(only_selected=True)
        elif chosen == copy_all_act:
            self._copy_cards_to_excel(only_selected=False)
        elif chosen == select_all_act:
            self._set_all_selected(True)
        elif chosen == clear_sel_act:
            self._set_all_selected(False)
        elif chosen == stats_act:
            self._show_site_stats()
        elif chosen == sort_manual_act:
            self._page._sort_column(self._list_id, "")
        elif chosen == sort_newest_act:
            self._page._sort_column(self._list_id, "newest")
        elif chosen == sort_oldest_act:
            self._page._sort_column(self._list_id, "oldest")
        elif chosen == sort_status_act:
            self._page._sort_column(self._list_id, "status")
        elif chosen == rename_act:
            self._title_widget.start_edit()
        elif chosen == delete_act:
            self._page._on_delete_column(self._list_id)

    def _card_widgets(self) -> list["DailyCardWidget"]:
        out: list[DailyCardWidget] = []
        for i in range(self._cards_layout.count()):
            item = self._cards_layout.itemAt(i)
            w = item.widget() if item is not None else None
            if isinstance(w, DailyCardWidget):
                out.append(w)
        return out

    def _set_all_selected(self, on: bool) -> None:
        for c in self._card_widgets():
            c.set_selected(on)

    def _copy_cards_to_excel(self, only_selected: bool) -> None:
        cards = self._card_widgets()
        if only_selected:
            cards = [c for c in cards if c.is_selected()]
        rows: list[str] = []
        for c in cards:
            row = _store().get_daily_card(c.card_id())
            if row is not None:
                rows.append(_excel_row_for_card(row.title, row.body))
        if not rows:
            return
        QApplication.clipboard().setText("\n".join(rows))
        QToolTip.showText(
            self._menu_btn.mapToGlobal(self._menu_btn.rect().bottomLeft()),
            f"Copied {len(rows)} card(s) to the clipboard in Excel format.",
            self._menu_btn,
        )

    def _show_site_stats(self) -> None:
        counts: dict[str, int] = {}
        total = 0
        for c in self._card_widgets():
            row = _store().get_daily_card(c.card_id())
            if row is None:
                continue
            site = _job_site_from_title(row.title)
            counts[site] = counts.get(site, 0) + 1
            total += 1

        lr = _store().get_daily_list(self._list_id)
        col_name = (lr.title if lr else "") or "Column"

        dlg = QDialog(self)
        dlg.setWindowTitle("Card statistics by job site")
        dlg.setModal(True)
        dlg.setMinimumWidth(340)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(f"<b>{escape(col_name)}</b> — {total} card(s)"))

        if counts:
            ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
            body_rows = "".join(
                f"<tr><td>{escape(site)}</td>"
                f"<td align='right'>{n}</td>"
                f"<td align='right'>{(n * 100 // total) if total else 0}%</td></tr>"
                for site, n in ordered
            )
            table = QLabel(
                "<table width='100%' cellspacing='0' cellpadding='4'>"
                "<tr><th align='left'>Job site</th>"
                "<th align='right'>Cards</th><th align='right'>Share</th></tr>"
                f"{body_rows}</table>"
            )
            table.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            table.setWordWrap(True)
            lay.addWidget(table)
        else:
            lay.addWidget(QLabel("No cards in this column."))

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)
        dlg.exec()

    def add_card_widget(self, card: DailyCardWidget) -> None:
        self._cards_layout.insertWidget(self._cards_layout.count() - 1, card)
        self._page._install_shift_horizontal_wheel(card)

    def dragEnterEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if _card_id_from_mime(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if _card_id_from_mime(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        cid = _card_id_from_mime(event.mimeData())
        if cid is not None:
            self._page._move_card_to_list(cid, self._list_id)
            event.acceptProposedAction()


class _ShiftHorizontalWheelFilter(QObject):
    """Shift + mouse wheel scrolls the daily board horizontally."""

    def __init__(self, page: "DailyBoardPage") -> None:
        super().__init__(page)
        self._page = page

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if event.type() != QEvent.Type.Wheel:
            return super().eventFilter(obj, event)
        we = event
        if not isinstance(we, QWheelEvent):
            return super().eventFilter(obj, event)
        if not (we.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            return super().eventFilter(obj, event)
        scroll = self._page._board_scroll
        w = obj if isinstance(obj, QWidget) else None
        if w is None:
            return super().eventFilter(obj, event)
        if (
            w is not scroll
            and w is not scroll.viewport()
            and w is not scroll.widget()
            and not scroll.isAncestorOf(w)
        ):
            return super().eventFilter(obj, event)
        delta = we.angleDelta().y()
        if delta == 0:
            delta = we.angleDelta().x()
        if delta != 0:
            bar = scroll.horizontalScrollBar()
            bar.setValue(bar.value() - delta)
            we.accept()
            return True
        return super().eventFilter(obj, event)


class _SpacePanFilter(QObject):
    """Drag empty board space (or a card body) to pan horizontally/vertically.

    Panning is always on: a left-drag that starts off any interactive control
    pans the board after it passes the drag threshold, so plain clicks and
    double-clicks still reach cards. Space is still tracked for the open-hand
    cursor hint but is no longer required.
    """

    _PAN_SPEED = 2.5

    def __init__(self, page: "DailyBoardPage") -> None:
        super().__init__(page)
        self._page = page
        self._space_down = False
        self._panning = False
        self._last_pos: QPointF | None = None
        self._vertical_scroll: QScrollArea | None = None
        self._cursor_active = False
        self._press_global: QPointF | None = None
        self._press_pannable = False

    def space_down(self) -> bool:
        return self._space_down

    def _set_pan_cursor(self, shape: Qt.CursorShape | None) -> None:
        if shape is None:
            if self._cursor_active:
                QApplication.restoreOverrideCursor()
                self._cursor_active = False
            return
        if self._cursor_active:
            QApplication.changeOverrideCursor(shape)
        else:
            QApplication.setOverrideCursor(shape)
            self._cursor_active = True

    def _is_board_widget(self, w: QWidget) -> bool:
        scroll = self._page._board_scroll
        return (
            w is scroll
            or w is scroll.viewport()
            or w is scroll.widget()
            or scroll.isAncestorOf(w)
        )

    def _column_scroll_for(self, w: QWidget) -> QScrollArea | None:
        cur: QWidget | None = w
        board_scroll = self._page._board_scroll
        while cur is not None:
            if isinstance(cur, QScrollArea) and cur is not board_scroll:
                return cur
            cur = cur.parentWidget()
        return None

    def _press_target(self, event: QMouseEvent) -> QWidget | None:
        return QApplication.widgetAt(event.globalPosition().toPoint())

    def _is_pannable_press(self, event: QMouseEvent) -> bool:
        """True when a drag at this position should pan: the widget under the
        cursor is inside the board scroll and not a control, column title, or a
        card armed for moving. Keyed off widgetAt (not the event's obj, which can
        be the QWindow) so it is consistent across Qt's duplicate deliveries."""
        scroll = self._page._board_scroll
        target = self._press_target(event)
        if target is None or not (target is scroll or scroll.isAncestorOf(target)):
            return False
        armed = self._page._armed_card
        cur: QWidget | None = target
        while cur is not None:
            if armed is not None and cur is armed:
                return False  # let the armed card start its move drag
            if isinstance(
                cur,
                (
                    QAbstractButton,
                    QComboBox,
                    QLineEdit,
                    QTextEdit,
                    QDateEdit,
                    QDateTimeEdit,
                    QScrollBar,
                    _ColumnTitleWidget,
                ),
            ):
                return False
            if cur is scroll:
                return True
            cur = cur.parentWidget()
        return False

    def _stop_pan(self, event: QMouseEvent) -> bool:
        if not self._panning:
            return False
        self._panning = False
        self._last_pos = None
        self._vertical_scroll = None
        self._set_pan_cursor(
            Qt.CursorShape.OpenHandCursor if self._space_down else None
        )
        event.accept()
        return True

    def _cancel_pan(self) -> None:
        if self._panning:
            self._panning = False
            self._last_pos = None
            self._vertical_scroll = None
            self._set_pan_cursor(
                Qt.CursorShape.OpenHandCursor if self._space_down else None
            )

    def _clear_space_state(self) -> None:
        self._space_down = False
        self._cancel_pan()
        self._set_pan_cursor(None)

    def _space_key_goes_to_editor(self, obj: QObject) -> bool:
        target = obj if isinstance(obj, QWidget) else QApplication.focusWidget()
        active_window = QApplication.activeWindow()
        if isinstance(active_window, QDialog):
            return True
        if target is None:
            return False
        if isinstance(target.window(), QDialog):
            return True

        cur: QWidget | None = target
        while cur is not None:
            if isinstance(
                cur,
                (QLineEdit, QTextEdit, QComboBox, QDateEdit, QDateTimeEdit),
            ):
                return True
            cur = cur.parentWidget()
        return False

    def _move_pan(self, event: QMouseEvent) -> bool:
        if not self._panning or self._last_pos is None:
            return False
        pos = event.globalPosition()
        delta = pos - self._last_pos
        self._last_pos = pos

        hbar = self._page._board_scroll.horizontalScrollBar()
        hbar.setValue(hbar.value() - int(delta.x() * self._PAN_SPEED))

        v_scroll = self._vertical_scroll or self._page._board_scroll
        vbar = v_scroll.verticalScrollBar()
        vbar.setValue(vbar.value() - int(delta.y() * self._PAN_SPEED))

        event.accept()
        return True

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            ke = event
            if isinstance(ke, QKeyEvent) and ke.key() == Qt.Key.Key_Space:
                if self._space_key_goes_to_editor(obj):
                    self._clear_space_state()
                    return super().eventFilter(obj, event)
                if ke.isAutoRepeat():
                    return True
                self._space_down = event.type() == QEvent.Type.KeyPress
                if self._space_down:
                    self._set_pan_cursor(Qt.CursorShape.OpenHandCursor)
                else:
                    self._cancel_pan()
                    self._set_pan_cursor(None)
                event.accept()
                return True
            return super().eventFilter(obj, event)
        if event.type() not in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseMove,
            QEvent.Type.MouseButtonRelease,
        ):
            return super().eventFilter(obj, event)
        me = event
        if not isinstance(me, QMouseEvent):
            return super().eventFilter(obj, event)
        # Qt delivers each mouse event to the app filter twice - once for the
        # QWindow and once for the target QWidget. Handle only the QWidget pass
        # so panning isn't double-applied and the QWindow pass can't reset state.
        if not isinstance(obj, QWidget):
            return super().eventFilter(obj, event)
        if event.type() == QEvent.Type.MouseButtonPress:
            # Arm panning but DON'T consume the press, so clicks / double-clicks
            # still reach cards. Panning kicks in on the first move past the
            # drag threshold (see MouseMove below).
            if me.button() == Qt.MouseButton.LeftButton:
                if self._is_pannable_press(me):
                    self._press_global = me.globalPosition()
                    self._press_pannable = True
                    self._vertical_scroll = self._column_scroll_for(
                        self._press_target(me)
                    )
                else:
                    self._press_global = None
                    self._press_pannable = False
        elif event.type() == QEvent.Type.MouseMove:
            if self._panning:
                if self._move_pan(me):
                    return True
            elif (
                self._press_pannable
                and self._press_global is not None
                and (me.buttons() & Qt.MouseButton.LeftButton)
                and (me.globalPosition() - self._press_global).manhattanLength()
                >= QApplication.startDragDistance()
            ):
                self._panning = True
                self._last_pos = me.globalPosition()
                self._set_pan_cursor(Qt.CursorShape.ClosedHandCursor)
                _hb = self._page._board_scroll.horizontalScrollBar()
                _vb = (self._vertical_scroll or self._page._board_scroll).verticalScrollBar()
                db._log(
                    f"PAN started hbar_max={_hb.maximum()} vbar_max={_vb.maximum()}"
                )
                self._move_pan(me)
                return True
        elif event.type() == QEvent.Type.MouseButtonRelease:
            if me.button() == Qt.MouseButton.LeftButton:
                was_panning = self._panning
                self._press_pannable = False
                self._press_global = None
                if was_panning:
                    self._stop_pan(me)
                    return True
        return super().eventFilter(obj, event)


class _AddColumnWidget(QFrame):
    """Placeholder column with button to add a new list."""

    def __init__(self, page: "DailyBoardPage", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dailyAddColumn")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFixedWidth(140)
        lay = QVBoxLayout(self)
        lay.addStretch()
        btn = QPushButton("+ Column")
        btn.setObjectName("dailyAddColumnBtn")
        btn.setToolTip("Add a new column to this day")
        btn.clicked.connect(page._on_add_column)
        lay.addWidget(btn, alignment=Qt.AlignmentFlag.AlignHCenter)
        lay.addStretch()
        page._install_shift_horizontal_wheel(self)


class DailyBoardPage(QWidget):
    """Kanban board for one calendar day."""

    board_counts_changed = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DailyBoardPage")
        self._theme_busy = False
        self._theme_qss = ""
        self._day = _business_today()
        self._loading = False
        self._editors: dict[int, DailyCardEditDialog] = {}
        self._columns: dict[int, DailyColumnWidget] = {}
        self._armed_card: "DailyCardWidget | None" = None
        self._show_card_content = QSettings(_SETTINGS_ORG, _SETTINGS_APP).value(
            "daily_tab_show_card_content", False, type=bool
        )
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(50)
        self._refresh_timer.timeout.connect(self._rebuild_board)
        # The Job bids column is no longer forced onto the board; drop the empty
        # auto-created default, and only recognise an existing one (with cards).
        _store().delete_daily_job_bids_list_if_empty()
        self._job_bids_list_id = _store().find_daily_job_bids_list_id()

        root = QVBoxLayout(self)

        search_row = QHBoxLayout()
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText(
            "Search — matches column title, card title, body, status, date, or id…"
        )
        self._search_input.setClearButtonEnabled(True)
        # Search only on Enter or the Search button — live filtering rebuilt the
        # whole board on every keystroke and was too slow on large boards.
        # Clearing the box (the X button or deleting the text) shows all cards.
        self._search_input.returnPressed.connect(self._on_search_changed)
        self._search_input.textChanged.connect(
            lambda text: self._on_search_changed() if not text.strip() else None
        )
        search_row.addWidget(self._search_input)
        self._search_btn = QPushButton("Search")
        self._search_btn.clicked.connect(self._on_search_changed)
        search_row.addWidget(self._search_btn)
        self._status_filter_label = QLabel("Status")
        search_row.addWidget(self._status_filter_label)
        self._status_filter_combo = QComboBox()
        self._status_filter_combo.setToolTip("Show only cards with the selected status")
        self._status_filter_combo.addItem("All statuses", _DAILY_STATUS_FILTER_ALL)
        self._status_filter_combo.addItem("No status", _DAILY_STATUS_NONE)
        for name in _DAILY_STATUS_ORDER:
            self._status_filter_combo.addItem(
                _daily_status_swatch(_DAILY_STATUS_COLORS[name]), name, name
            )
        self._status_filter_combo.currentIndexChanged.connect(
            self._on_status_filter_changed
        )
        search_row.addWidget(self._status_filter_combo)
        self._job_stats_btn = QPushButton("Job stats")
        self._job_stats_btn.setToolTip(
            f"Total cards by job site across every column titled "
            f"'{_JOB_BID_COLUMN_PREFIX}…' (all days)"
        )
        self._job_stats_btn.clicked.connect(self._show_job_bid_stats)
        search_row.addWidget(self._job_stats_btn)
        root.addLayout(search_row)

        period_row = QHBoxLayout()
        self._period_filter_check = QCheckBox("Filter by &date range")
        self._period_filter_check.setToolTip(
            "When enabled, the board day stays within the From and To dates "
            "and only cards created within that window are shown."
        )
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

        day_row = QHBoxLayout()
        self._day_filter_check = QCheckBox("Daily &board day")
        self._day_filter_check.setToolTip(
            "When enabled, pick the calendar day for this board and show only "
            "cards created on that day. Turn off date range filter to use this."
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
        today_btn.setToolTip("Set the board day to today and enable this filter")
        today_btn.clicked.connect(self._on_board_today)
        day_row.addWidget(today_btn)
        self._prev_btn = QPushButton("◀")
        self._prev_btn.setFixedWidth(36)
        self._prev_btn.setToolTip("Previous board day")
        self._prev_btn.clicked.connect(self._prev_day)
        day_row.addWidget(self._prev_btn)
        self._next_btn = QPushButton("▶")
        self._next_btn.setFixedWidth(36)
        self._next_btn.setToolTip("Next board day")
        self._next_btn.clicked.connect(self._next_day)
        day_row.addWidget(self._next_btn)
        day_row.addStretch()
        root.addLayout(day_row)

        self._restore_filter_settings()
        self._sync_filter_control_states()
        self._apply_theme()

        self._board_scroll = QScrollArea()
        self._board_scroll.setObjectName("dailyBoardScroll")
        self._board_scroll.setWidgetResizable(True)
        self._board_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._board_host = _BoardHost(self)
        self._board_layout = QHBoxLayout(self._board_host)
        self._board_layout.setContentsMargins(8, 8, 8, 8)
        self._board_layout.setSpacing(12)
        self._board_layout.addStretch()
        self._board_scroll.setWidget(self._board_host)
        # Open-hand cursor signals that empty board space can be dragged to pan.
        self._board_scroll.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        self._board_host.setCursor(Qt.CursorShape.OpenHandCursor)
        root.addWidget(self._board_scroll, stretch=1)

        self._shift_wheel_filter = _ShiftHorizontalWheelFilter(self)
        self._space_pan_filter = _SpacePanFilter(self)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self._space_pan_filter)
        self._shift_wheel_widgets: set[int] = set()
        self._install_shift_horizontal_wheel(
            self._board_scroll,
            self._board_scroll.viewport(),
            self._board_host,
        )

        self.refresh_board()

    def _install_shift_horizontal_wheel(self, *widgets: QWidget) -> None:
        for w in widgets:
            key = id(w)
            if key in self._shift_wheel_widgets:
                continue
            self._shift_wheel_widgets.add(key)
            w.installEventFilter(self._shift_wheel_filter)
            w.installEventFilter(self._space_pan_filter)

    def _space_pan_down(self) -> bool:
        return self._space_pan_filter.space_down()

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
        self.refresh_board()

    def _selected_status_filter(self) -> str | None:
        """Return the status to filter by, or None when showing all statuses."""
        data = self._status_filter_combo.currentData()
        if not isinstance(data, str) or data == _DAILY_STATUS_FILTER_ALL:
            return None
        return data

    def _on_status_filter_changed(self) -> None:
        self.refresh_board()

    def card_content_visible(self) -> bool:
        """Whether card body previews should be rendered on the board."""
        return self._show_card_content

    def _on_show_card_content_toggled(self, on: bool) -> None:
        self._show_card_content = on
        QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue(
            "daily_tab_show_card_content", on
        )
        self.refresh_board()

    def _apply_theme(self) -> None:
        if self._theme_busy:
            return
        self._theme_busy = True
        try:
            qss = _daily_board_stylesheet()
            if qss != self._theme_qss:
                self._theme_qss = qss
                self.setStyleSheet(qss)
        finally:
            self._theme_busy = False

    def changeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange and not self._theme_busy:
            self._apply_theme()

    def current_day_str(self) -> str:
        return self._day.toString("yyyy-MM-dd")

    def _day_str(self) -> str:
        return self.current_day_str()

    def _period_date_bounds(self) -> tuple[QDate, QDate]:
        d0 = self._period_from.dateTime().date()
        d1 = self._period_to.dateTime().date()
        return (d0, d1) if d0 <= d1 else (d1, d0)

    def _active_card_date_bounds(self) -> tuple[str, str] | None:
        """Inclusive ``created_at`` window the active date filter restricts cards
        to, as ``yyyy-MM-dd HH:mm:ss`` strings, or ``None`` when neither date
        filter is on.

        The range filter uses its From/To datetimes; the day filter uses the
        5pm-to-5pm window for the selected day (see _day_window_bounds). The two
        filters are mutually exclusive. The
        format matches ``daily_cards.created_at`` so the bounds can be compared
        against a card's timestamp lexicographically.
        """
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
        lo, hi = self._period_date_bounds()
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
        self._period_filter_check.blockSignals(True)
        self._day_filter_check.blockSignals(True)
        self._period_from.blockSignals(True)
        self._period_to.blockSignals(True)
        self._day_edit.blockSignals(True)
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

            status_sel = settings.value(
                "daily_tab_status_filter", _DAILY_STATUS_FILTER_ALL
            )
            self._status_filter_combo.blockSignals(True)
            try:
                if isinstance(status_sel, str):
                    idx = self._status_filter_combo.findData(status_sel)
                    self._status_filter_combo.setCurrentIndex(idx if idx >= 0 else 0)
            finally:
                self._status_filter_combo.blockSignals(False)
        finally:
            self._period_from.blockSignals(False)
            self._period_to.blockSignals(False)
            self._period_filter_check.blockSignals(False)
            self._day_filter_check.blockSignals(False)
            self._day_edit.blockSignals(False)

    def _save_filter_settings(self) -> None:
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        settings.setValue(
            "daily_tab_period_enabled", self._period_filter_check.isChecked()
        )
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

    def _sync_filter_control_states(self) -> None:
        period_on = self._period_filter_check.isChecked()
        day_on = self._day_filter_check.isChecked()
        for w in (
            self._period_from,
            self._period_to,
            self._period_from_label,
            self._period_to_label,
        ):
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
        # Refresh on both check and uncheck so the filtered view always updates.
        self.refresh_board()

    def _on_day_filter_toggled(self, on: bool) -> None:
        if on:
            self._period_filter_check.blockSignals(True)
            self._period_filter_check.setChecked(False)
            self._period_filter_check.blockSignals(False)
            self._set_board_day(self._day_edit.date(), refresh=False)
        self._sync_filter_control_states()
        self._save_filter_settings()
        # Refresh on both check and uncheck so the filtered view always updates.
        self.refresh_board()

    def _on_period_range_changed(self) -> None:
        if self._period_filter_check.isChecked():
            self._set_board_day(self._clamp_day_to_period(self._day), refresh=True)

    def _on_board_day_picker_changed(self, d: QDate) -> None:
        if self._day_filter_check.isChecked():
            self._set_board_day(d, refresh=True)

    def _on_board_today(self) -> None:
        self._day_edit.blockSignals(True)
        self._day_filter_check.blockSignals(True)
        self._period_filter_check.blockSignals(True)
        today = _business_today()
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

    def refresh_board(self) -> None:
        self._refresh_timer.start()

    def _rebuild_board(self) -> None:
        self._loading = True
        self._armed_card = None  # cards are recreated below; drop the stale ref
        self._job_bids_list_id = _store().find_daily_job_bids_list_id()
        day = self._day_str()
        needle = self._search_needle()
        status_filter = self._selected_status_filter()
        date_bounds = self._active_card_date_bounds()
        filtering = (
            bool(needle) or status_filter is not None or date_bounds is not None
        )
        lists = _store().list_daily_lists_for_board(day)
        cards = _store().list_daily_cards_for_day(day)
        by_list: dict[int, list[db.DailyCardRow]] = {}
        visible_count = 0
        for c in cards:
            if status_filter is not None and (c.status or _DAILY_STATUS_NONE) != status_filter:
                continue
            if date_bounds is not None and not self._card_in_bounds(c, date_bounds):
                continue
            if needle and not self._card_matches_search(c, needle):
                continue
            by_list.setdefault(c.list_id, []).append(c)
            visible_count += 1

        while self._board_layout.count() > 1:
            item = self._board_layout.takeAt(0)
            if item is None:
                break
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._columns.clear()

        insert_at = 0
        for lst in lists:
            list_cards = by_list.get(lst.id, [])
            if filtering and not list_cards:
                title_only_match = (
                    bool(needle)
                    and status_filter is None
                    and fields_match_search(needle, lst.title)
                )
                if not title_only_match:
                    continue
            col = DailyColumnWidget(lst, self)
            self._columns[lst.id] = col
            for card in list_cards:
                col.add_card_widget(DailyCardWidget(card, self))
            col.set_card_count(len(list_cards))
            self._board_layout.insertWidget(insert_at, col)
            insert_at += 1

        self._board_layout.insertWidget(insert_at, _AddColumnWidget(self))

        self._loading = False
        self.board_counts_changed.emit(visible_count if filtering else len(cards))

    def _schedule_refresh(self) -> None:
        if not self._loading:
            self._refresh_timer.start()

    def _ordered_list_ids(self) -> list[int]:
        return [row.id for row in _store().list_daily_lists_for_board(self._day_str())]

    def _move_column_relative(
        self, dragged_id: int, target_id: int, after: bool
    ) -> None:
        if self._loading or dragged_id == target_id:
            return
        ids = self._ordered_list_ids()
        if dragged_id not in ids or target_id not in ids:
            return
        ids.remove(dragged_id)
        idx = ids.index(target_id)
        if after:
            idx += 1
        ids.insert(idx, dragged_id)
        _store().reorder_daily_lists(self._day_str(), ids)
        self.refresh_board()

    def _move_column_to_end(self, dragged_id: int) -> None:
        if self._loading:
            return
        ids = self._ordered_list_ids()
        if dragged_id not in ids:
            return
        ids.remove(dragged_id)
        ids.append(dragged_id)
        _store().reorder_daily_lists(self._day_str(), ids)
        self.refresh_board()

    def _is_job_bids_list(self, list_id: int) -> bool:
        if list_id == self._job_bids_list_id:
            return True
        row = _store().get_daily_list(list_id)
        return row is not None and row.column_key == _JOB_BIDS_COLUMN_KEY

    def _on_add_card(self, list_id: int) -> None:
        # if self._is_job_bids_list(list_id):
        #     self._on_add_job_bid_card(list_id)
        #     return
        cid = _store().add_daily_card(list_id, "")
        self._apply_current_sort(list_id)
        self.refresh_board()
        QTimer.singleShot(0, lambda c=cid: self._deferred_open_editor(c))

    def _on_paste_card(self, list_id: int) -> None:
        """Create a card seeded with the clipboard text and open it for editing."""
        text = QApplication.clipboard().text()
        if not text.strip():
            QMessageBox.information(
                self, "Paste card", "The clipboard has no text to paste."
            )
            return
        cid = _store().add_daily_card(list_id, "")
        _store().update_daily_card(cid, _first_line_title(text), text)
        self._apply_current_sort(list_id)
        self.refresh_board()
        QTimer.singleShot(0, lambda c=cid: self._deferred_open_editor(c))

    def _on_ai_paste_card(self, list_id: int) -> None:
        """Create a card, then have the editor parse the clipboard job posting
        into structured details in the body."""
        text = QApplication.clipboard().text()
        if not text.strip():
            QMessageBox.information(
                self, "AI paste", "The clipboard has no text to analyze."
            )
            return
        if not ai_assistant.has_api_key():
            QMessageBox.information(
                self, "AI paste",
                f"Add your {ai_assistant.provider_label()} API key in Settings > "
                "App settings to use AI paste.",
            )
            return
        cid = _store().add_daily_card(list_id, "")
        # Seed the body with the raw text so the card persists while the AI runs;
        # the editor replaces it with the parsed details when the call returns.
        _store().update_daily_card(cid, "", text)
        self._apply_current_sort(list_id)
        self.refresh_board()
        QTimer.singleShot(
            0, lambda c=cid, t=text: self._deferred_open_ai_paste(c, t)
        )

    def _deferred_open_ai_paste(self, card_id: int, text: str) -> None:
        if self._loading:
            return
        self._open_editor(card_id)
        dlg = self._editors.get(card_id)
        if dlg is not None and not sip.isdeleted(dlg):
            dlg.begin_ai_paste(text)

    # def _on_add_job_bid_card(self, list_id: int) -> None:
    #     # dlg = JobBidsDialog(self)
    #     if dlg.exec() != QDialog.DialogCode.Accepted:
    #         return
    #     cid = _store().add_daily_card(list_id, dlg.title())
    #     _store().update_daily_card(cid, dlg.title(), dlg.body_html())
    #     self.refresh_board()

    def _on_add_column(self) -> None:
        title, ok = QInputDialog.getText(
            self,
            "New column",
            "Column title:",
            text="New column",
        )
        if not ok:
            return
        _store().add_daily_list(self._day_str(), title.strip() or "New column")
        self.refresh_board()

    def _on_rename_column(self, list_id: int, title: str) -> None:
        if self._loading:
            return
        _store().update_daily_list(list_id, title)
        col = self._columns.get(list_id)
        if col is not None:
            col.set_title(title)

    def _on_column_always_shown(self, list_id: int, on: bool) -> None:
        if self._loading:
            return
        _store().set_daily_list_always_shown(list_id, on)
        col = self._columns.get(list_id)
        if col is not None:
            col._always_shown = on
            col.setToolTip(
                "Always shown — visible on every board day" if on else ""
            )
        self.refresh_board()

    def _on_delete_column(self, list_id: int) -> None:
        n = _store().count_daily_cards_for_list(list_id)
        msg = (
            f"Delete this column and its {n} card(s)?"
            if n
            else "Delete this empty column?"
        )
        reply = QMessageBox.question(
            self,
            "Delete column",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        for c in _store().list_daily_cards_for_list(list_id):
            ed = self._editors.pop(c.id, None)
            if ed is not None and not sip.isdeleted(ed):
                ed.close()
        _store().delete_daily_list(list_id)
        self.refresh_board()

    def _set_armed_card(self, card: "DailyCardWidget | None") -> None:
        prev = self._armed_card
        if prev is not None and prev is not card and not sip.isdeleted(prev):
            prev._set_move_armed(False)
        self._armed_card = card

    def _list_title(self, list_id: int) -> str:
        col = self._columns.get(list_id)
        if col is not None:
            return col._title_widget.text() or "this column"
        for row in _store().list_daily_lists_for_board(self._day_str()):
            if row.id == list_id:
                return row.title or "this column"
        return "this column"

    def _move_card_to_list(self, card_id: int, list_id: int) -> None:
        if self._loading:
            return
        row = _store().get_daily_card(card_id)
        if row is None:
            return
        if row.list_id == list_id:
            return
        name = (row.title or "").strip() or "this card"
        if (
            QMessageBox.question(
                self,
                "Move card",
                f'Move "{name}"\nfrom "{self._list_title(row.list_id)}" '
                f'to "{self._list_title(list_id)}"?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        _store().move_daily_card(card_id, list_id)
        self.refresh_board()

    def _move_cards_to_list(self, card_ids: list[int], target_list_id: int) -> None:
        """Move several selected cards to another column in one confirmed action."""
        if self._loading:
            return
        rows = [_store().get_daily_card(cid) for cid in card_ids]
        ids = [r.id for r in rows if r is not None and r.list_id != target_list_id]
        if not ids:
            return
        target_name = self._list_title(target_list_id)
        if (
            QMessageBox.question(
                self,
                "Move cards",
                f'Move {len(ids)} selected card(s) to "{target_name}"?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        for cid in ids:
            _store().move_daily_card(cid, target_list_id)
        self.refresh_board()

    def _sort_column(self, list_id: int, mode: str) -> None:
        """Column menu action: remember ``mode`` for the column and apply it now.

        ``mode`` is '' (manual — stop auto-sorting), 'newest', 'oldest', or
        'status'. The remembered mode also decides where newly added cards land.
        """
        if self._loading:
            return
        _store().set_daily_list_sort_mode(
            list_id, mode if mode in ("newest", "oldest", "status") else ""
        )
        self._apply_sort_order(list_id, mode)
        self.refresh_board()

    def _apply_sort_order(self, list_id: int, mode: str) -> None:
        """Reorder and persist a column's card positions by ``mode``.

        ``newest``/``oldest`` sort by creation time; ``status`` sorts by the
        card's status position in ``_DAILY_STATUS_ORDER`` — the latest status
        (furthest down the list) on top, cards with no status last. Any other
        value (e.g. '' for manual) leaves the order untouched.
        """
        cards = _store().list_daily_cards_for_list(list_id)
        if len(cards) < 2:
            return

        def status_rank(c: db.DailyCardRow) -> int:
            try:
                return _DAILY_STATUS_ORDER.index(c.status)
            except ValueError:
                return -1  # "" / unknown status sorts last

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
        """Re-apply the column's remembered sort (used after a card is added so
        the new card lands in the right place)."""
        mode = _store().get_daily_list_sort_mode(list_id)
        if mode:
            self._apply_sort_order(list_id, mode)

    def _delete_card(self, card_id: int) -> None:
        ed = self._editors.pop(card_id, None)
        if ed is not None and not sip.isdeleted(ed):
            ed.close()
        _store().delete_daily_card(card_id)
        self.refresh_board()

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
        # The retired Kanban page has no row to drop, so a delete goes down the
        # same path it always did: try in place, then rebuild.
        w.deleted.connect(self._on_editor_saved)
        w.closed_for_id.connect(self._forget_editor)
        w.open_card_requested.connect(self._open_editor)
        w.destroyed.connect(lambda *args, cid=card_id: self._editors.pop(cid, None))
        self._editors[card_id] = w
        w.show()

    def _show_job_bid_stats(self) -> None:
        """Board-wide job-site statistics summed over all 'JobBid…' columns."""
        col_count, titles = _store().job_bid_card_titles(_JOB_BID_COLUMN_PREFIX)
        counts: dict[str, int] = {}
        for title in titles:
            site = _job_site_from_title(title)
            counts[site] = counts.get(site, 0) + 1
        total = len(titles)

        dlg = QDialog(self)
        dlg.setWindowTitle("Job bids — statistics by job site")
        dlg.setModal(True)
        dlg.setMinimumWidth(360)
        lay = QVBoxLayout(dlg)
        lay.addWidget(
            QLabel(
                f"<b>{col_count} '{escape(_JOB_BID_COLUMN_PREFIX)}…' column(s)</b>"
                f" — {total} card(s) total"
            )
        )

        if counts:
            ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
            body_rows = "".join(
                f"<tr><td>{escape(site)}</td>"
                f"<td align='right'>{n}</td>"
                f"<td align='right'>{(n * 100 // total) if total else 0}%</td></tr>"
                for site, n in ordered
            )
            table = QLabel(
                "<table width='100%' cellspacing='0' cellpadding='4'>"
                "<tr><th align='left'>Job site</th>"
                "<th align='right'>Cards</th><th align='right'>Share</th></tr>"
                f"{body_rows}</table>"
            )
            table.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            table.setWordWrap(True)
            lay.addWidget(table)
        else:
            lay.addWidget(
                QLabel(f"No '{escape(_JOB_BID_COLUMN_PREFIX)}…' columns with cards found.")
            )

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)
        dlg.exec()

    def _forget_editor(self, card_id: int) -> None:
        self._editors.pop(card_id, None)

    def _on_editor_saved(self, card_id: int) -> None:
        # Fast path: update just the saved card in place when it is already on the
        # board and no filter could change its visibility. This avoids tearing down
        # and recreating every column/card on each debounced keystroke-save, which
        # caused the board-wide width flicker and lag while typing.
        if self._update_card_in_place(card_id):
            return
        self._schedule_refresh()

    def _is_filtering(self) -> bool:
        return (
            bool(self._search_needle())
            or self._selected_status_filter() is not None
            or self._active_card_date_bounds() is not None
        )

    def _find_card_widget(self, card_id: int) -> "DailyCardWidget | None":
        for col in self._columns.values():
            if sip.isdeleted(col):
                continue
            for w in col._card_widgets():
                if w.card_id() == card_id:
                    return w
        return None

    def _update_card_in_place(self, card_id: int) -> bool:
        """Refresh one on-board card from the DB without a full board rebuild.

        Returns False (so the caller falls back to a full refresh) when a filter
        is active — an edit could change which cards match — or when the card is
        not currently rendered (newly added, moved, or deleted)."""
        if self._is_filtering():
            return False
        row = _store().get_daily_card(card_id)
        if row is None:
            return False
        widget = self._find_card_widget(card_id)
        if widget is None:
            return False
        widget.update_content_from_row(row)
        return True

    def shutdown(self) -> None:
        self._refresh_timer.stop()
        self._save_filter_settings()
        for w in list(self._editors.values()):
            if not sip.isdeleted(w):
                w.close()
        self._editors.clear()
