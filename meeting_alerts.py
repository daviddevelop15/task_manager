"""Meeting alarm engine: background polling, the alert popup, and sound.

The engine polls the ``meetings`` table on a short timer. For each meeting whose
alerts are enabled and whose start time is still in the future, alarms fire
inside the lead window ``[start - lead, start)`` every ``interval`` minutes. Each
alarm shows :class:`MeetingAlertDialog` (which loops an alarm sound) carrying two
required actions:

* **Pause** - disables that meeting's alarms until the user re-enables them
  (persisted via ``alerts_enabled = 0``).
* **Repeat** - snoozes one minute and fires a single extra alarm, then the normal
  every-``interval`` schedule resumes.

Sound uses the Windows stdlib ``winsound`` looping a small WAV synthesized at
runtime (no bundled asset). If unavailable it degrades to ``QApplication.beep``.
"""

from __future__ import annotations

import math
import os
import struct
import tempfile
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

import database as db

try:  # Windows only; the app already targets Windows (DPAPI etc.)
    import winsound
except Exception:  # pragma: no cover - non-Windows fallback
    winsound = None  # type: ignore

_alarm_wav_path: Optional[str] = None


def _ensure_alarm_wav() -> Optional[str]:
    """Synthesize (once) a short two-tone alarm WAV in the temp dir; loops cleanly."""
    global _alarm_wav_path
    if _alarm_wav_path and os.path.exists(_alarm_wav_path):
        return _alarm_wav_path
    try:
        framerate = 22050
        amplitude = 15000
        frames = bytearray()

        def tone(freq: float, ms: int) -> None:
            n = int(framerate * ms / 1000)
            for i in range(n):
                # short linear fade in/out so looping has no clicks
                env = min(1.0, i / 200.0, (n - i) / 200.0)
                val = amplitude * env * math.sin(2 * math.pi * freq * i / framerate)
                frames.extend(struct.pack("<h", int(val)))

        def silence(ms: int) -> None:
            frames.extend(b"\x00\x00" * int(framerate * ms / 1000))

        for _ in range(3):
            tone(880.0, 250)
            silence(110)
            tone(988.0, 250)
            silence(320)

        path = os.path.join(tempfile.gettempdir(), "taskmgr_meeting_alarm.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(framerate)
            w.writeframes(bytes(frames))
        _alarm_wav_path = path
        return path
    except Exception:
        return None


def start_alarm_sound() -> None:
    if winsound is None:
        try:
            QApplication.beep()
        except Exception:
            pass
        return
    path = _ensure_alarm_wav()
    try:
        if path:
            winsound.PlaySound(
                path,
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP,
            )
        else:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass


def stop_alarm_sound() -> None:
    if winsound is None:
        return
    try:
        winsound.PlaySound(None, winsound.SND_PURGE)
    except Exception:
        pass


def parse_meeting_dt(value: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _humanize_delta(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total <= 0:
        return "now"
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    if minutes:
        return f"{minutes} min"
    return "less than a minute"


class MeetingAlertDialog(QDialog):
    """Always-on-top alarm popup with Pause and Repeat actions."""

    paused = pyqtSignal(int)
    repeated = pyqtSignal(int)
    dismissed = pyqtSignal(int)

    def __init__(self, meeting: db.MeetingRow, parent=None) -> None:
        super().__init__(parent)
        self._mid = meeting.id
        self._link = (meeting.link or "").strip()
        self._choice: Optional[str] = None

        self.setWindowTitle("Meeting reminder")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(360)

        start = parse_meeting_dt(meeting.start_at)
        lay = QVBoxLayout(self)

        title = QLabel(meeting.title or "(untitled meeting)")
        tf = QFont()
        tf.setPointSize(13)
        tf.setBold(True)
        title.setFont(tf)
        title.setWordWrap(True)
        lay.addWidget(title)

        when = meeting.start_at
        if start is not None:
            delta = start - datetime.now()
            when = f"{start.strftime('%a %d %b %Y, %H:%M')}  —  starts in {_humanize_delta(delta)}"
        lay.addWidget(QLabel(when))

        body = (meeting.body or "").strip()
        if body:
            preview = body if len(body) <= 200 else body[:200] + "…"
            body_label = QLabel(preview)
            body_label.setWordWrap(True)
            lay.addWidget(body_label)

        btn_row = QHBoxLayout()
        if self._link:
            join = QPushButton("Join")
            join.clicked.connect(self._open_link)
            btn_row.addWidget(join)
        btn_row.addStretch(1)
        repeat = QPushButton("Repeat (1 min)")
        repeat.clicked.connect(self._do_repeat)
        btn_row.addWidget(repeat)
        pause = QPushButton("Pause alerts")
        pause.clicked.connect(self._do_pause)
        btn_row.addWidget(pause)
        lay.addLayout(btn_row)

    def _open_link(self) -> None:
        if self._link:
            QDesktopServices.openUrl(QUrl(self._link))

    def _do_pause(self) -> None:
        self._choice = "pause"
        self.paused.emit(self._mid)
        self.close()

    def _do_repeat(self) -> None:
        self._choice = "repeat"
        self.repeated.emit(self._mid)
        self.close()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._choice is None:
            self.dismissed.emit(self._mid)
        super().closeEvent(event)


@dataclass
class _AlertState:
    next_fire: datetime
    snooze_fire: Optional[datetime] = None
    dialog: Optional[MeetingAlertDialog] = None


class MeetingAlertEngine(QObject):
    """Polls the meetings table and raises alarms inside each lead window."""

    POLL_MS = 20000

    def __init__(self, parent_window) -> None:
        super().__init__(parent_window)
        self._window = parent_window
        self._states: dict[int, _AlertState] = {}
        self._active_dialogs: set[int] = set()
        self._timer = QTimer(self)
        self._timer.setInterval(self.POLL_MS)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._tick()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        for mid in list(self._states.keys()):
            self._close_dialog(mid)
        self._states.clear()
        self._active_dialogs.clear()
        stop_alarm_sound()

    def notify_changed(self) -> None:
        """Re-evaluate immediately (e.g. after a meeting is added/edited/deleted)."""
        self._tick()

    # --- internals ---

    def _tick(self) -> None:
        now = datetime.now()
        try:
            meetings = db.list_meetings()
        except Exception:
            return
        alive: set[int] = set()
        for m in meetings:
            start = parse_meeting_dt(m.start_at)
            if start is None:
                continue
            alive.add(m.id)

            if now >= start:
                # Meeting has started/passed: no more alarms.
                self._close_dialog(m.id)
                self._states.pop(m.id, None)
                continue
            if not m.alerts_enabled:
                # Paused for this meeting.
                self._close_dialog(m.id)
                self._states.pop(m.id, None)
                continue

            lead = max(0, int(m.alert_lead_minutes))
            interval = max(1, int(m.alert_interval_minutes))
            window_start = start - timedelta(minutes=lead)

            st = self._states.get(m.id)
            if st is None:
                st = _AlertState(next_fire=window_start)
                self._states[m.id] = st
            if st.next_fire < window_start:
                st.next_fire = window_start  # config changed; re-clamp

            if now < window_start:
                continue  # lead window not reached yet
            if st.dialog is not None:
                continue  # an alarm popup is already showing for this meeting

            snooze = st.snooze_fire is not None and now >= st.snooze_fire
            normal = now >= st.next_fire
            if not (snooze or normal):
                continue

            if snooze:
                st.snooze_fire = None
            else:
                nf = st.next_fire
                while nf <= now:
                    nf += timedelta(minutes=interval)
                st.next_fire = nf
            self._fire(m)

        for mid in list(self._states.keys()):
            if mid not in alive:
                self._close_dialog(mid)
                self._states.pop(mid, None)

    def _fire(self, meeting: db.MeetingRow) -> None:
        dlg = MeetingAlertDialog(meeting, self._window)
        dlg.paused.connect(self._on_paused)
        dlg.repeated.connect(self._on_repeated)
        dlg.dismissed.connect(self._on_dismissed)
        st = self._states.get(meeting.id)
        if st is not None:
            st.dialog = dlg
        self._active_dialogs.add(meeting.id)
        start_alarm_sound()
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_paused(self, mid: int) -> None:
        try:
            db.set_meeting_alerts_enabled(mid, False)
        except Exception:
            pass
        self._after_dialog(mid)

    def _on_repeated(self, mid: int) -> None:
        st = self._states.get(mid)
        if st is not None:
            st.snooze_fire = datetime.now() + timedelta(minutes=1)
        self._after_dialog(mid)

    def _on_dismissed(self, mid: int) -> None:
        self._after_dialog(mid)

    def _after_dialog(self, mid: int) -> None:
        st = self._states.get(mid)
        if st is not None:
            st.dialog = None
        self._active_dialogs.discard(mid)
        if not self._active_dialogs:
            stop_alarm_sound()

    def _close_dialog(self, mid: int) -> None:
        st = self._states.get(mid)
        if st is not None and st.dialog is not None:
            dlg = st.dialog
            st.dialog = None
            dlg.blockSignals(True)  # closing programmatically: don't re-enter handlers
            dlg.close()
            dlg.deleteLater()
        self._active_dialogs.discard(mid)
        if not self._active_dialogs:
            stop_alarm_sound()
