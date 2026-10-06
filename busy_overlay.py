"""A spinner for the moments the app is thinking.

Loading a board and talking to the server both happen on the GUI thread, so the
window simply stops responding while they run - no repaint, no cursor change,
nothing to say whether the app is working or has died. On a slow network that
silence can last several seconds, and the only available reading of it is "it
has hung".

The catch is that a spinner is a repaint, and a blocked GUI thread cannot
repaint. Showing one without dealing with that produces a frozen spinner, which
is worse than none - it looks like a crash rather than a wait. So there are two
ways to run work here, and which one applies depends on what the work touches:

* `run_off_thread` - for work that only moves data (an HTTP call, a file read).
  It runs on a worker thread while a local event loop keeps the window painting,
  so the spinner turns smoothly throughout.
* `run_steps` - for work that touches widgets, which must stay on the GUI
  thread. Each step still blocks, but the spinner and its caption are brought up
  to date between steps, so a long refresh reads as progress rather than a hang.

Input is swallowed while either runs: the overlay covers its window and eats
mouse events, and the pump between steps excludes user input, so nothing can be
clicked into a half-loaded board.
"""

from __future__ import annotations

from typing import Callable, Iterable

from PyQt6.QtCore import QEvent, QEventLoop, QObject, QRectF, Qt, QThread, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QWidget

# One turn of the arc. Slow enough not to look frantic, fast enough to read as
# movement the moment it appears.
_SPIN_MS = 40
_DEGREES_PER_TICK = 12

_ARC_SPAN = 100 * 16          # Qt angles are sixteenths of a degree
_RING_PX = 46
_PEN_PX = 5


class _Spinner(QWidget):
    """The rotating arc itself, on a transparent background."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._angle = 0
        self.setFixedSize(_RING_PX + _PEN_PX * 2, _RING_PX + _PEN_PX * 2)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._timer = QTimer(self)
        self._timer.setInterval(_SPIN_MS)
        self._timer.timeout.connect(self._advance)

    def start(self) -> None:
        self._angle = 0
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _advance(self) -> None:
        self._angle = (self._angle + _DEGREES_PER_TICK) % 360
        self.update()

    def advance_manually(self) -> None:
        """Move the arc on when the event loop is too busy to tick the timer."""
        self._advance()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(_PEN_PX, _PEN_PX, _RING_PX, _RING_PX)
        # A faint full ring under the bright arc, so the shape reads as a circle
        # rather than a stray comma while it turns.
        p.setPen(QPen(QColor(255, 255, 255, 40), _PEN_PX,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(box, 0, 360 * 16)
        p.setPen(QPen(QColor("#4FC3F7"), _PEN_PX,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(box, -self._angle * 16, -_ARC_SPAN)


class BusyOverlay(QWidget):
    """Covers its parent window with a scrim, a spinner and a caption."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._message = ""
        self._depth = 0                 # nested busy sections
        self._spinner = _Spinner(self)
        self._delay_timer = QTimer(self)
        self._delay_timer.setSingleShot(True)
        self._delay_timer.timeout.connect(self._show_now)
        self.hide()
        parent.installEventFilter(self)  # follow the window's size
        self.setGeometry(parent.rect())

    # --- appearance --------------------------------------------------------

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if obj is self.parent() and event.type() in (
            QEvent.Type.Resize, QEvent.Type.Show,
        ):
            self.setGeometry(self.parentWidget().rect())
            self._centre_spinner()
        return False

    def _centre_spinner(self) -> None:
        # Sits just above centre, leaving room for the caption underneath.
        self._spinner.move(
            (self.width() - self._spinner.width()) // 2,
            (self.height() - self._spinner.height()) // 2 - 16,
        )

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(0, 0, 0, 120))
        if not self._message:
            return
        f = QFont(self.font())
        f.setPointSize(max(9, f.pointSize() + 1))
        p.setFont(f)
        p.setPen(QColor("#ECEFF1"))
        box = self.rect().adjusted(24, 0, -24, 0)
        box.setTop(self._spinner.geometry().bottom() + 14)
        box.setHeight(60)
        p.drawText(box, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                   self._message)

    # --- lifecycle ---------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._depth > 0

    def start(self, message: str = "", delay_ms: int = 0) -> None:
        """Begin a busy section.

        `delay_ms` holds the overlay back until the work has taken that long,
        which is what makes it usable on something like switching sheets: that
        is instant when the data is local or cached, and throwing a full-window
        scrim up for 5ms reads as a flicker or a fault. Slow enough to notice,
        and the spinner appears; quick, and nothing happens at all.

        The delay only pays off if the event loop is reached while the work
        runs - so pair it with `run_off_thread`, or `pump` between steps.
        """
        self._depth += 1
        self._message = message
        if self._depth == 1:
            if delay_ms > 0:
                self._delay_timer.start(delay_ms)
            else:
                self._show_now()
        self.update()

    def _show_now(self) -> None:
        if self._depth == 0:
            return                      # the work finished before the delay
        self._delay_timer.stop()
        self.setGeometry(self.parentWidget().rect())
        self._centre_spinner()
        self.raise_()
        self.show()
        self._spinner.start()

    def set_message(self, message: str) -> None:
        self._message = message
        self.update()

    def stop(self) -> None:
        self._depth = max(0, self._depth - 1)
        if self._depth == 0:
            self._delay_timer.stop()    # quick enough that it never appeared
            self._spinner.stop()
            self.hide()

    def pump(self) -> None:
        """Repaint now, mid-operation, without letting a click through.

        Excluding user input is what makes this safe to call from inside a
        sequence of steps: the window redraws, but a stray click cannot start a
        second operation on top of the one already running.
        """
        self._spinner.advance_manually()
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)


class StartupSplash(QWidget):
    """A small spinner window for the stretch before the main window exists.

    Unlocking a large encrypted database and building the boards happens before
    there is any window to put an overlay on, so a launch can spend seconds
    showing nothing at all - the app looks like it failed to start. This is a
    frameless panel on the screen the person is looking at, closed as soon as
    the real window appears.
    """

    def __init__(self, message: str = "Starting Task Manager…") -> None:
        # Deliberately NOT always-on-top: a message box can still be raised
        # while this is up (a port that will not bind, a first-run prompt), and
        # a splash floating over a modal dialog is how an app gets stuck with
        # nobody able to see what it is asking.
        super().__init__(None, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._message = message
        self._spinner = _Spinner(self)
        self.resize(320, 150)
        self._spinner.move((self.width() - self._spinner.width()) // 2, 30)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(32, 33, 36, 235))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)
        p.setPen(QColor("#ECEFF1"))
        box = self.rect().adjusted(16, self._spinner.geometry().bottom() + 12, -16, -12)
        p.drawText(box, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                   self._message)

    def start(self) -> None:
        screen = QApplication.screenAt(self.cursor().pos()) or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.center().x() - self.width() // 2,
                      area.center().y() - self.height() // 2)
        self.show()
        self._spinner.start()
        self.pump()

    def set_message(self, message: str) -> None:
        self._message = message
        self.update()
        self.pump()

    def pump(self) -> None:
        self._spinner.advance_manually()
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

    def finish(self) -> None:
        self._spinner.stop()
        self.close()


_WINDOW_ATTR = "_busy_overlay_instance"


def for_window(widget: QWidget) -> BusyOverlay:
    """The overlay for whichever window `widget` sits in, made on first use.

    Pages several layers down need the spinner too - a search runs from the
    daily board, not from the main window - and threading an overlay through
    every constructor to get there would be noise. One per window, found from
    anything inside it.
    """
    window = widget.window()
    existing = getattr(window, _WINDOW_ATTR, None)
    if isinstance(existing, BusyOverlay):
        return existing
    overlay = BusyOverlay(window)
    setattr(window, _WINDOW_ATTR, overlay)
    return overlay


class _Worker(QThread):
    """Runs one callable off the GUI thread and keeps whatever came back."""

    def __init__(self, fn: Callable) -> None:
        super().__init__()
        self._fn = fn
        self.value = None
        self.error: BaseException | None = None

    def run(self) -> None:                       # pragma: no cover - thread body
        try:
            self.value = self._fn()
        except BaseException as exc:             # noqa: BLE001 - re-raised on the GUI thread
            self.error = exc


def run_off_thread(overlay: BusyOverlay, message: str, fn: Callable):
    """Run `fn` on a worker thread with the spinner turning. Returns its result.

    Only for work that touches no widgets - Qt objects belong to the GUI thread.
    Anything `fn` raises is re-raised here, on the GUI thread, so callers handle
    errors exactly as they did when the call was a plain one.
    """
    overlay.start(message)
    worker = _Worker(fn)
    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    try:
        worker.start()
        # The local loop is what keeps the spinner alive; without it the GUI
        # thread would sit in wait() and paint nothing.
        loop.exec()
        worker.wait()
    finally:
        overlay.stop()
    if worker.error is not None:
        raise worker.error
    return worker.value


def run_steps(overlay: BusyOverlay, steps: Iterable[tuple[str, Callable]]) -> None:
    """Run GUI-thread steps in order, repainting between each one.

    Each step still blocks while it runs - it has to, it touches widgets - but
    the caption names what is happening and the spinner moves on, so a refresh
    that takes seconds reads as progress instead of a dead window.
    """
    steps = list(steps)
    if not steps:
        return
    overlay.start(steps[0][0])
    try:
        for message, fn in steps:
            overlay.set_message(message)
            overlay.pump()
            fn()
            overlay.pump()
    finally:
        overlay.stop()
