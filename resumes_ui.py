"""Manage stored resumes (markdown, HTML, or uploaded PDF) used for AI scoring/answers.

PDF resumes are stored directly in the database as a (DPAPI/password-encrypted)
BLOB and sent to Claude as a document — no conversion or text extraction.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

from PyQt6.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QPointF,
    QSettings,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QTextOption
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
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
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

import app_variant
import database as db
from app_resources import app_icon
from app_store import PermissionDenied, get_store as _store
from window_utils import show_existing_window
from clipboard_utils import (
    COPY_ICON_BTN_QSS,
    apply_copy_button_flash,
    orange_copy_icon,
)

_MAX_PDF_BYTES = 10 * 1024 * 1024  # 10 MB — resumes are tiny; guard against accidents

_SETTINGS_ORG = app_variant.SETTINGS_ORG
_SETTINGS_APP = app_variant.SETTINGS_APP
_MANAGER_GEOMETRY_KEY = "resume_manager_geometry"
_MANAGER_SPLITTER_KEY = "resume_manager_splitter"


def _open_externally(pdf_bytes: bytes, parent: QWidget | None = None) -> bool:
    """Write the PDF to a temp file and open it with the OS default viewer."""
    try:
        fd, path = tempfile.mkstemp(suffix=".pdf", prefix="resume_")
        with os.fdopen(fd, "wb") as fh:
            fh.write(pdf_bytes)
        os.startfile(path)  # noqa: S606 - Windows default PDF handler
        return True
    except OSError as e:
        if parent is not None:
            QMessageBox.critical(parent, "Preview", f"Could not open the PDF:\n{e}")
        return False


class ResumePreviewDialog(QDialog):
    """Render a resume — Markdown/HTML as rich text, PDF in an embedded viewer."""

    def __init__(
        self,
        name: str,
        is_pdf: bool,
        content: str,
        fmt: str,
        pdf_bytes: bytes | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Preview — {name or 'Resume'}")
        self.setModal(True)
        self.setMinimumSize(720, 720)
        layout = QVBoxLayout(self)

        if is_pdf:
            self._show_pdf(pdf_bytes or b"", layout)
        else:
            browser = QTextBrowser(self)
            browser.setOpenExternalLinks(True)
            if fmt == "html":
                browser.setHtml(content)
            else:
                browser.document().setMarkdown(content)
            layout.addWidget(browser, stretch=1)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.accept)
        bb.accepted.connect(self.accept)
        layout.addWidget(bb)

    def _show_pdf(self, pdf_bytes: bytes, layout: QVBoxLayout) -> None:
        try:
            from PyQt6.QtPdf import QPdfDocument
            from PyQt6.QtPdfWidgets import QPdfView

            # Keep the byte buffer alive for the document's lifetime.
            self._pdf_qba = QByteArray(pdf_bytes)
            self._pdf_buffer = QBuffer(self._pdf_qba, self)
            self._pdf_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            self._pdf_doc = QPdfDocument(self)
            self._pdf_doc.load(self._pdf_buffer)
            view = QPdfView(self)
            view.setDocument(self._pdf_doc)
            view.setPageMode(QPdfView.PageMode.MultiPage)
            view.setZoomMode(QPdfView.ZoomMode.FitToWidth)

            # QPdfView has no text selection, so add a selectable text tab.
            tabs = QTabWidget(self)
            tabs.addTab(view, "View")
            text = self._extract_pdf_text(self._pdf_doc)
            text_view = QTextBrowser(self)
            text_view.setPlainText(text or "(No selectable text found in this PDF.)")
            tabs.addTab(text_view, "Text (selectable)")
            layout.addWidget(tabs, stretch=1)

            open_btn = QPushButton("Open in external viewer")
            open_btn.clicked.connect(lambda: _open_externally(pdf_bytes, self))
            layout.addWidget(open_btn)
        except Exception:  # noqa: BLE001 - QtPdf missing/unusable → fall back
            _open_externally(pdf_bytes, self)
            note = QLabel("Opened the PDF in your default viewer.")
            note.setWordWrap(True)
            layout.addWidget(note)

    @staticmethod
    def _extract_pdf_text(doc) -> str:  # type: ignore[no-untyped-def]
        try:
            parts = []
            for i in range(doc.pageCount()):
                sel = doc.getAllText(i)
                t = sel.text() if sel is not None else ""
                if t:
                    parts.append(t)
            return "\n\n".join(parts).strip()
        except Exception:  # noqa: BLE001
            return ""


# Past this, an entry scrolls inside its own box instead of taking over the panel.
# Sections are shown whole: an opened section grows to whatever its text needs
# and the panel around it does the scrolling. There is one scrollbar on this
# window, not one per section - a box that scrolls inside a box that scrolls is
# the thing people were fighting when reading a long job entry.
_MIN_ENTRY_HEIGHT = 30


class _ClickableLabel(QLabel):
    """A label that reports clicks, so a section title can open its own text."""

    clicked = pyqtSignal()

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        # On release, not press, so dragging off the title the way you would to
        # cancel a button press cancels this too.
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def _eye_icon(shown: bool) -> QIcon:
    """The show/hide glyph: an open eye, or the same eye struck through.

    Drawn rather than shipped so it takes the button's text colour and matches
    the eye used on the daily board, where hiding a card body is the same idea.
    """
    px = 18
    color = QColor("#ECEFF1")
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    center = px / 2.0
    p.setPen(QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QPointF(center, center), px * 0.35, px * 0.22)
    p.setBrush(color)
    p.drawEllipse(QPointF(center, center), 2.0, 2.0)
    if not shown:
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(px * 0.22, px * 0.78), QPointF(px * 0.78, px * 0.22))
    p.end()
    return QIcon(pm)


def _small_button(label: str, tooltip: str, width: int = 26) -> QPushButton:
    btn = QPushButton(label)
    btn.setToolTip(tooltip)
    btn.setFixedSize(width, 24)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def _copy_button(tooltip: str) -> QPushButton:
    btn = QPushButton()
    btn.setObjectName("copyIconBtn")       # the stylesheet keys off this
    btn.setIcon(orange_copy_icon(QSize(15, 15)))
    btn.setFixedSize(26, 24)
    btn.setToolTip(tooltip)
    btn.setStyleSheet(COPY_ICON_BTN_QSS)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


class ResumeSectionDialog(QDialog):
    """Type one section: what to call it, and the text to copy."""

    def __init__(self, title: str = "", content: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Resume section")
        self.setModal(True)
        self.setMinimumSize(560, 420)
        root = QVBoxLayout(self)

        root.addWidget(QLabel("Section name"))
        self._title = QLineEdit(title)
        self._title.setPlaceholderText(
            "Work Experience - DevF, or Summary, or Core Skills"
        )
        root.addWidget(self._title)

        root.addWidget(QLabel("Text"))
        self._content = QPlainTextEdit(content)
        self._content.setPlaceholderText(
            "Paste the part of the resume this section covers. This is exactly "
            "what the copy button puts on the clipboard."
        )
        root.addWidget(self._content, stretch=1)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)
        self._title.setFocus()

    @property
    def title(self) -> str:
        return self._title.text().strip()

    @property
    def content(self) -> str:
        return self._content.toPlainText().strip()


class ResumeSectionsPanel(QScrollArea):
    """A resume's sections as the person registered them, each with a copy button.

    These are typed in, not detected. Reading them out of the resume was tried
    and dropped: a PDF arrives as whatever its exporter produced - headings
    without markers, bullets missing, a title swallowed by the line above - and
    a guess that is usually right is still wrong on the resume being sent
    today. Registered once, they stay correct, and the copy buttons are the
    point of the window.
    """

    changed = pyqtSignal()          # a section was added, edited, or removed

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._resume_id: int | None = None
        self._may_edit = True
        self._texts: list[QTextBrowser] = []
        # Section ids the person has opened. Sections start closed - the window
        # is a list of things to copy, and a screen of full resume text buries
        # that list - but an edit reloads the panel, and collapsing what someone
        # was just reading would be its own annoyance. Switching resumes clears it.
        self._expanded: set[int] = set()

        self._body = QWidget()
        self._layout = QVBoxLayout(self._body)
        self._layout.setContentsMargins(2, 2, 6, 2)
        self._layout.setSpacing(10)
        self.setWidget(self._body)

        bar = QHBoxLayout()
        self._add_btn = QPushButton("+ Add section")
        self._add_btn.setToolTip("Register a section of this resume")
        self._add_btn.clicked.connect(self._on_add)
        bar.addWidget(self._add_btn)
        bar.addStretch(1)
        self._layout.addLayout(bar)

        self._empty = QLabel()
        self._empty.setWordWrap(True)
        self._empty.setStyleSheet("color: #90A4AE;")
        self._layout.addWidget(self._empty)
        self._layout.addStretch(1)
        self.show_message("Select a resume to see its sections.")

    # --- sizing ------------------------------------------------------------

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._autosize()

    def _autosize(self) -> None:
        """Give each section the height its text needs, and no more."""
        for text in list(self._texts):
            try:
                if text.isHidden():   # collapsed: nothing to measure
                    continue
                doc = text.document()
                doc.setTextWidth(max(80, text.viewport().width()))
                needed = int(doc.size().height()) + 2 * int(doc.documentMargin()) + 2
                text.setFixedHeight(max(_MIN_ENTRY_HEIGHT, needed))
            except RuntimeError:      # the widget went with a reload
                self._texts.remove(text)

    # --- content -----------------------------------------------------------

    def show_message(self, message: str) -> None:
        self._clear_cards()
        self._empty.setText(message)
        self._empty.show()

    def set_resume(self, resume_id: int | None, may_edit: bool = True) -> None:
        if resume_id != self._resume_id:
            self._expanded.clear()
        self._resume_id = resume_id
        self._may_edit = may_edit
        self._add_btn.setEnabled(resume_id is not None and may_edit)
        if resume_id is None:
            self.show_message("Select a resume to see its sections.")
            return
        self.reload()

    def reload(self) -> None:
        self._clear_cards()
        if self._resume_id is None:
            self.show_message("Select a resume to see its sections.")
            return
        try:
            sections = list(_store().list_resume_sections(self._resume_id))
        except Exception:            # noqa: BLE001 - offline, or an old server
            sections = []
        if not sections:
            self._empty.setText(
                "No sections registered for this resume yet.\n\n"
                "Add one for each part you want to copy on its own - a summary, "
                "each job, each project, skills, education. The copy button then "
                "puts exactly that text on the clipboard."
                if self._may_edit else
                "No sections registered for this resume."
            )
            self._empty.show()
            return
        self._empty.hide()
        for index, section in enumerate(sections):
            self._layout.insertWidget(
                self._layout.count() - 1,
                self._section_card(section, index, len(sections)),
            )
        QTimer.singleShot(0, self._autosize)

    def _clear_cards(self) -> None:
        self._texts.clear()
        for i in reversed(range(self._layout.count())):
            item = self._layout.itemAt(i)
            w = item.widget() if item is not None else None
            if w is not None and w is not self._empty:
                self._layout.takeAt(i)
                w.setParent(None)
                w.deleteLater()

    # --- one section -------------------------------------------------------

    def _section_card(self, section, index: int, total: int) -> QWidget:
        box = QWidget()
        box.setObjectName("sectionCard")
        box.setStyleSheet(
            "QWidget#sectionCard { border: 1px solid #37474F; border-radius: 6px; }"
        )
        lay = QVBoxLayout(box)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(4)
        title = _ClickableLabel(section.title or "Untitled section")
        title.setStyleSheet("font-weight: 600; color: #ECEFF1;")
        title.setToolTip("Show or hide this section's text")
        # The title takes the whole width the buttons leave, rather than sitting
        # in a label the size of its own text with dead space beside it: the
        # target you aim at is the header row, so that is what has to be live.
        title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        head.addWidget(title, stretch=1)

        if self._may_edit:
            up = _small_button("▲", "Move this section up")
            up.setEnabled(index > 0)
            up.clicked.connect(lambda _=False, s=section: self._move(s, -1))
            head.addWidget(up)
            down = _small_button("▼", "Move this section down")
            down.setEnabled(index < total - 1)
            down.clicked.connect(lambda _=False, s=section: self._move(s, 1))
            head.addWidget(down)
            edit = _small_button("Edit", "Change this section's name or text", width=44)
            edit.clicked.connect(lambda _=False, s=section: self._on_edit(s))
            head.addWidget(edit)
            remove = _small_button("✕", "Remove this section")
            remove.clicked.connect(lambda _=False, s=section: self._on_remove(s))
            head.addWidget(remove)

        copy = _copy_button("Copy this section")
        copy.clicked.connect(
            lambda _=False, b=copy, t=section.content: self._copy(t, b)
        )
        head.addWidget(copy)

        eye = _small_button("", "Show this section's text")
        eye.setCheckable(True)
        head.addWidget(eye)
        lay.addLayout(head)

        text = QTextBrowser()
        text.setPlainText(section.content)
        text.setReadOnly(True)
        text.setFrameShape(QFrame.Shape.NoFrame)
        text.document().setDocumentMargin(4)
        # No scrollbars of its own - _autosize gives it the height for all of it.
        text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        text.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self._texts.append(text)
        lay.addWidget(text)

        eye.toggled.connect(
            lambda shown, t=text, b=eye, sid=section.id: self._set_section_shown(
                sid, t, b, shown
            )
        )
        # The title is the big target and the one people go for first; it drives
        # the same button rather than the text directly, so both routes end up
        # in one place.
        title.clicked.connect(eye.toggle)
        # Closed unless this section was open before an edit reloaded the panel.
        eye.setChecked(section.id in self._expanded)
        # setChecked only emits when the state actually changes, so a section
        # that stays closed would never get its icon or its hidden text.
        self._set_section_shown(section.id, text, eye, eye.isChecked())
        return box

    def _set_section_shown(
        self, section_id: int, text: QTextBrowser, button: QPushButton, shown: bool
    ) -> None:
        """Show or hide one section's text, and remember which it is."""
        text.setVisible(shown)
        button.setIcon(_eye_icon(shown))
        button.setToolTip(
            "Hide this section's text" if shown else "Show this section's text"
        )
        if shown:
            self._expanded.add(section_id)
            self._autosize()
        else:
            self._expanded.discard(section_id)

    # --- editing -----------------------------------------------------------

    def _on_add(self) -> None:
        if self._resume_id is None:
            return
        dlg = ResumeSectionDialog(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if not dlg.title and not dlg.content:
            return
        if self._guarded(lambda: _store().add_resume_section(
                self._resume_id, dlg.title, dlg.content)):
            self.reload()
            self.changed.emit()

    def _on_edit(self, section) -> None:
        dlg = ResumeSectionDialog(section.title, section.content, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if self._guarded(lambda: _store().update_resume_section(
                section.id, dlg.title, dlg.content)):
            self.reload()
            self.changed.emit()

    def _on_remove(self, section) -> None:
        name = section.title or "this section"
        if QMessageBox.question(
            self, "Remove section",
            f"Remove {name}?\nThe resume itself is not changed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        if self._guarded(lambda: _store().delete_resume_section(section.id)):
            self.reload()
            self.changed.emit()

    def _move(self, section, delta: int) -> None:
        if self._resume_id is None:
            return
        try:
            ids = [s.id for s in _store().list_resume_sections(self._resume_id)]
        except Exception:            # noqa: BLE001
            return
        try:
            at = ids.index(section.id)
        except ValueError:
            return
        target = at + delta
        if not 0 <= target < len(ids):
            return
        ids[at], ids[target] = ids[target], ids[at]
        if self._guarded(lambda: _store().reorder_resume_sections(self._resume_id, ids)):
            self.reload()
            self.changed.emit()

    def _guarded(self, action) -> bool:
        """Run a write, turning a refusal into a message rather than a crash."""
        try:
            action()
            return True
        except PermissionDenied as exc:
            QMessageBox.warning(self, "Resume sections", str(exc))
        except Exception as exc:     # noqa: BLE001 - offline, or an old server
            QMessageBox.warning(
                self, "Resume sections",
                f"Could not save that section:\n{exc}",
            )
        return False

    def _copy(self, text: str, btn: QPushButton) -> None:
        QApplication.clipboard().setText(text)
        apply_copy_button_flash(btn)


_open_manager: "ResumeManagerDialog | None" = None


def open_resume_manager(on_changed=None) -> "ResumeManagerDialog":
    """Show the Resumes window, reusing the one already open.

    Non-modal windows need someone to hold a reference or Python collects them
    the moment the caller returns, and a second click must raise the window
    that is already up rather than stack another copy of it - so both jobs live
    here instead of in each of the places that opens it.

    Deliberately parentless. Parented to the card editor it was opened from, it
    would be destroyed the moment that card was closed - and this is a window
    people leave open beside the work they are copying into, so it has to
    outlive whichever window happened to launch it.
    """
    global _open_manager
    dlg = _open_manager
    if dlg is not None:
        try:
            dlg.isVisible()          # still a live C++ object?
        except RuntimeError:
            dlg = _open_manager = None
    if dlg is None:
        dlg = _open_manager = ResumeManagerDialog()

        def _forget() -> None:
            global _open_manager
            if _open_manager is dlg:
                _open_manager = None

        dlg.destroyed.connect(_forget)
    if on_changed is not None:
        # Live, not just on close: with the window open beside them, someone who
        # adds a resume expects to see it in the list behind straight away.
        # Unique, because reopening the same window would otherwise stack a
        # second copy of the same connection and reload the list twice.
        try:
            dlg.resumes_changed.connect(on_changed, Qt.ConnectionType.UniqueConnection)
        except TypeError:
            pass                     # already connected

    show_existing_window(dlg)
    return dlg


class ResumeManagerDialog(QDialog):
    """Create, edit, upload (PDF), and delete resumes stored in the database."""

    resumes_changed = pyqtSignal()   # the library gained, lost, or changed a resume

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Resumes")
        # Deliberately not modal. This window is a reference people keep open
        # while they work - copying a section into an application form, checking
        # what a resume says against the job - and a modal one locks the rest of
        # the app while they do it. Non-modal costs the caller its exec(): open
        # it through open_resume_manager() below, which keeps the reference the
        # window needs to stay alive.
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        # A plain QDialog gets a bare title bar on Windows: no minimize, no
        # maximize. This window is worked in - resumes are read, edited and
        # copied from - so it needs the full control box, a size grip, and a
        # remembered size like any other editor window in the app.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )
        self.setWindowIcon(app_icon())
        self.setSizeGripEnabled(True)
        self.setMinimumSize(720, 520)
        self.resize(1040, 640)
        self._current_id: int | None = None
        self._current_is_pdf = False
        self._dirty = False

        root = QVBoxLayout(self)
        # A splitter, not a fixed stretch: the list of names and the editor want
        # different widths depending on how long the names are, and that is the
        # person's call to make by dragging.
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        root.addWidget(self._splitter, stretch=1)

        # Left: list of resumes + add / upload / delete.
        left_pane = QWidget()
        left = QVBoxLayout(left_pane)
        left.setContentsMargins(0, 0, 0, 0)
        left.addWidget(QLabel("Resumes"))
        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_selection_changed)
        left.addWidget(self._list, stretch=1)
        list_btns = QHBoxLayout()
        new_btn = QPushButton("New")
        new_btn.clicked.connect(self._on_new)
        upload_btn = QPushButton("Upload PDF…")
        upload_btn.clicked.connect(self._on_upload_pdf)
        # Kept on self so it can be disabled for a resume this person may not
        # remove (someone else's upload).
        self._del_btn = QPushButton("Delete")
        self._del_btn.clicked.connect(self._on_delete)
        list_btns.addWidget(new_btn)
        list_btns.addWidget(upload_btn)
        list_btns.addWidget(self._del_btn)
        left.addLayout(list_btns)
        self._splitter.addWidget(left_pane)

        # Right: editor for the selected resume.
        right_pane = QWidget()
        right = QVBoxLayout(right_pane)
        right.setContentsMargins(0, 0, 0, 0)
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name"))
        self._name = QLineEdit()
        self._name.textEdited.connect(self._mark_dirty)
        name_row.addWidget(self._name, stretch=1)
        name_row.addWidget(QLabel("Format"))
        self._fmt = QComboBox()
        self._fmt.addItem("Markdown", "md")
        self._fmt.addItem("HTML", "html")
        self._fmt.addItem("PDF", "pdf")
        self._fmt.currentIndexChanged.connect(self._mark_dirty)
        name_row.addWidget(self._fmt)
        self._replace_btn = QPushButton("Replace PDF…")
        self._replace_btn.clicked.connect(self._on_replace_pdf)
        self._replace_btn.hide()
        name_row.addWidget(self._replace_btn)
        right.addLayout(name_row)

        self._content = QPlainTextEdit()
        self._content.setPlaceholderText(
            "Paste your resume as Markdown or HTML — skills, experience, projects, "
            "education. This is sent to the AI to score job matches and draft answers. "
            "Or use “Upload PDF…” to store a PDF instead."
        )
        self._content.textChanged.connect(self._mark_dirty)

        # Sections first: copying one part into an application form is what
        # this window is opened for most of the time, and editing the resume
        # itself is the occasional task. The raw text stays a tab away.
        self._tabs = QTabWidget()
        self._sections = ResumeSectionsPanel()
        self._tabs.addTab(self._sections, "Sections")
        self._tabs.addTab(self._content, "Text")
        right.addWidget(self._tabs, stretch=1)

        save_row = QHBoxLayout()
        self._preview_btn = QPushButton("Preview")
        self._preview_btn.setToolTip("Show the resume rendered (Markdown/HTML or PDF)")
        self._preview_btn.clicked.connect(self._on_preview)
        save_row.addWidget(self._preview_btn)
        save_row.addStretch()
        self._save_btn = QPushButton("Save resume")
        self._save_btn.clicked.connect(self._on_save)
        save_row.addWidget(self._save_btn)
        right.addLayout(save_row)
        self._splitter.addWidget(right_pane)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 2)
        self._splitter.setSizes([320, 720])

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.accept)
        bb.accepted.connect(self.accept)
        right.addWidget(bb)

        self._restore_saved_geometry()
        self._reload(select_first=True)
        self._set_editor_enabled(self._current_id is not None)

    # --- window geometry ---

    def _restore_saved_geometry(self) -> None:
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        geo = settings.value(_MANAGER_GEOMETRY_KEY)
        if isinstance(geo, QByteArray) and not geo.isEmpty():
            self.restoreGeometry(geo)
        state = settings.value(_MANAGER_SPLITTER_KEY)
        if isinstance(state, QByteArray) and not state.isEmpty():
            self._splitter.restoreState(state)

    def _persist_geometry(self) -> None:
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        settings.setValue(_MANAGER_GEOMETRY_KEY, self.saveGeometry())
        settings.setValue(_MANAGER_SPLITTER_KEY, self._splitter.saveState())

    def done(self, result: int) -> None:  # type: ignore[override]
        # done() is the one funnel every way out goes through - the Close
        # button, Esc, and the title bar X (QDialog turns that into reject()).
        self._persist_geometry()
        super().done(result)

    # --- list / loading ---

    def _reload(self, select_id: int | None = None, select_first: bool = False) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        rows = _store().list_resumes()
        for r in rows:
            label = f"{r.name or 'Untitled'}  (PDF)" if r.is_pdf else (r.name or "Untitled")
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, r.id)
            self._list.addItem(item)
        self._list.blockSignals(False)
        target_row = -1
        if select_id is not None:
            for i in range(self._list.count()):
                if self._list.item(i).data(Qt.ItemDataRole.UserRole) == select_id:
                    target_row = i
                    break
        elif select_first and self._list.count():
            target_row = 0
        self.resumes_changed.emit()
        if target_row >= 0:
            self._list.setCurrentRow(target_row)
        else:
            self._current_id = None
            self._current_is_pdf = False
            self._name.clear()
            self._content.clear()
            self._set_editor_enabled(False)

    def _on_selection_changed(self, current: QListWidgetItem | None, _prev=None) -> None:  # type: ignore[no-untyped-def]
        if not self._maybe_discard_changes():
            self._reload(select_id=self._current_id)
            return
        if current is None:
            self._current_id = None
            self._current_is_pdf = False
            self._set_editor_enabled(False)
            return
        rid = current.data(Qt.ItemDataRole.UserRole)
        row = _store().get_resume(int(rid)) if isinstance(rid, int) else None
        if row is None:
            self._set_editor_enabled(False)
            return
        self._current_id = row.id
        self._current_is_pdf = row.is_pdf
        self._name.setText(row.name)
        idx = self._fmt.findData(row.fmt)
        self._fmt.setCurrentIndex(idx if idx >= 0 else 0)
        self._set_editor_enabled(True)
        if row.is_pdf:
            kb = max(1, len((row.pdf or b"")) // 1024)
            self._fmt.setEnabled(False)
            self._replace_btn.show()
            self._content.setReadOnly(True)
            self._content.setPlainText(
                f"[PDF resume — {kb} KB]\n\n"
                "The AI reads this PDF directly. Use “Replace PDF…” to change it, "
                "or rename it above and click Save."
            )
        else:
            self._fmt.setEnabled(True)
            self._replace_btn.hide()
            self._content.setReadOnly(False)
            self._content.setPlainText(row.content)
        # Last, so it has the final say over what the pdf/text branches above
        # just enabled. Everyone reads the whole library, but only the person
        # who uploaded a resume (or the PM) may change it - showing that up
        # front beats letting someone edit for a while and be refused on Save.
        # The writes are checked again underneath, so this is presentation.
        self._apply_write_permission(row.id)
        self._refresh_sections()
        self._dirty = False

    def _refresh_sections(self) -> None:
        """Show the sections registered for the selected resume."""
        if self._current_id is None:
            self._sections.set_resume(None)
            return
        may_edit = True
        checker = getattr(_store(), "may_edit_resume", None)
        if callable(checker):
            try:
                may_edit = bool(checker(self._current_id))
            except Exception:        # noqa: BLE001 - a failed check must not
                may_edit = True      # lock someone out of their own resume
        self._sections.set_resume(self._current_id, may_edit)

    def _set_editor_enabled(self, on: bool) -> None:
        for w in (self._name, self._fmt, self._content, self._save_btn, self._preview_btn):
            w.setEnabled(on)
        if not on:
            self._replace_btn.hide()
            # The panel stays usable - copying needs no write permission - but
            # with nothing selected there is nothing to show.
            self._sections.set_resume(None)

    def _apply_write_permission(self, resume_id: int) -> None:
        """Grey out editing for a resume this person may not change."""
        store = _store()
        may = True
        checker = getattr(store, "may_edit_resume", None)
        if callable(checker):
            try:
                may = bool(checker(resume_id))
            except Exception:          # noqa: BLE001 - a check that fails must
                may = True             # not lock someone out of their own work
        self._may_edit_current = may
        for w in (self._name, self._fmt, self._save_btn):
            w.setEnabled(may)
        self._content.setReadOnly(not may or self._current_is_pdf)
        self._del_btn.setEnabled(may)
        self._replace_btn.setEnabled(may)
        if not may:
            self._replace_btn.hide()
            self._name.setToolTip(
                "Uploaded by somebody else. You can read and use it, but only "
                "the person who uploaded it (or the PM) can change it."
            )
        else:
            self._name.setToolTip("")

    def _on_preview(self) -> None:
        if self._current_id is None:
            return
        if self._current_is_pdf:
            row = _store().get_resume(self._current_id)
            if row is None or not row.pdf:
                QMessageBox.information(self, "Preview", "This PDF resume is empty.")
                return
            dlg = ResumePreviewDialog(
                self._name.text().strip() or "Resume", True, "", "pdf", row.pdf, self
            )
        else:
            # Preview the current editor text (including unsaved edits).
            dlg = ResumePreviewDialog(
                self._name.text().strip() or "Resume",
                False,
                self._content.toPlainText(),
                self._fmt.currentData(),
                None,
                self,
            )
        dlg.exec()

    # --- editing ---

    def _mark_dirty(self, *_args) -> None:  # type: ignore[no-untyped-def]
        self._dirty = True

    def _maybe_discard_changes(self) -> bool:
        if not self._dirty or self._current_id is None:
            return True
        resp = QMessageBox.question(
            self,
            "Unsaved changes",
            "Discard unsaved changes to this resume?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return resp == QMessageBox.StandardButton.Discard

    def _on_new(self) -> None:
        if not self._maybe_discard_changes():
            return
        try:
            rid = _store().add_resume("New resume", "", "md")
        except (PermissionDenied, sqlite3.Error) as e:
            QMessageBox.critical(self, "Resumes", str(e))
            return
        self._dirty = False
        self._reload(select_id=rid)
        self._name.setFocus()
        self._name.selectAll()

    def _pick_pdf(self) -> bytes | None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a PDF resume", "", "PDF files (*.pdf)"
        )
        if not path:
            return None
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as e:
            QMessageBox.critical(self, "Resumes", f"Could not read the file:\n{e}")
            return None
        if not data[:5].startswith(b"%PDF"):
            QMessageBox.warning(self, "Resumes", "That file does not look like a PDF.")
            return None
        if len(data) > _MAX_PDF_BYTES:
            QMessageBox.warning(
                self, "Resumes",
                f"PDF is too large ({len(data) // (1024 * 1024)} MB). Limit is 10 MB.",
            )
            return None
        self._last_pdf_name = os.path.splitext(os.path.basename(path))[0]
        return data

    def _on_upload_pdf(self) -> None:
        if not self._maybe_discard_changes():
            return
        data = self._pick_pdf()
        if data is None:
            return
        try:
            rid = _store().add_resume_pdf(self._last_pdf_name or "Resume", data)
        except (PermissionDenied, sqlite3.Error) as e:
            QMessageBox.critical(self, "Resumes", str(e))
            return
        self._dirty = False
        self._reload(select_id=rid)

    def _on_replace_pdf(self) -> None:
        if self._current_id is None:
            return
        data = self._pick_pdf()
        if data is None:
            return
        try:
            _store().set_resume_pdf(self._current_id, data)
        except (PermissionDenied, sqlite3.Error) as e:
            QMessageBox.critical(self, "Resumes", str(e))
            return
        self._dirty = False
        self._reload(select_id=self._current_id)

    def _on_save(self) -> None:
        if self._current_id is None:
            return
        name = self._name.text().strip() or "Untitled"
        if self._current_is_pdf:
            # Rename only — keep the stored PDF bytes untouched.
            try:
                _store().update_resume(self._current_id, name, "", "pdf")
            except (PermissionDenied, sqlite3.Error) as e:
                QMessageBox.critical(self, "Resumes", str(e))
                return
        else:
            fmt = self._fmt.currentData()
            if fmt == "pdf":
                QMessageBox.information(
                    self, "Resumes",
                    "Use “Upload PDF…” (or “Replace PDF…”) to attach a PDF resume.",
                )
                return
            try:
                _store().update_resume(
                    self._current_id, name, self._content.toPlainText(), fmt
                )
            except (PermissionDenied, sqlite3.Error) as e:
                QMessageBox.critical(self, "Resumes", str(e))
                return
        self._dirty = False
        self._reload(select_id=self._current_id)

    def _on_delete(self) -> None:
        if self._current_id is None:
            return
        if (
            QMessageBox.question(
                self,
                "Delete resume",
                f'Delete "{self._name.text().strip() or "Untitled"}"? This cannot be undone.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            _store().delete_resume(self._current_id)
        except (PermissionDenied, sqlite3.Error) as e:
            QMessageBox.critical(self, "Resumes", str(e))
            return
        self._dirty = False
        self._current_id = None
        self._reload(select_first=True)

    def accept(self) -> None:  # type: ignore[override]
        if not self._maybe_discard_changes():
            return
        super().accept()
