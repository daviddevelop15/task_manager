"""Task search, status colors, and date parsing helpers."""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import QDateTime, Qt
from PyQt6.QtGui import QColor

import database as db


def status_background(status: str) -> QColor:
    """Row tint by workflow stage; closed uses a light alert red."""
    return {
        "initial": QColor(227, 242, 252),
        "invited or contacted": QColor(179, 229, 252),
        "closed": QColor(255, 228, 225),
        "created": QColor(200, 230, 201),
    }.get(status, QColor(245, 245, 245))


def status_foreground(status: str) -> QColor:
    """Near-black text for strong contrast on pastel / alert backgrounds."""
    _near_black = QColor(12, 12, 14)
    return {
        "initial": _near_black,
        "invited or contacted": _near_black,
        "closed": _near_black,
        "created": _near_black,
    }.get(status, _near_black)


def status_label(status: str) -> str:
    return {
        "initial": "Initial",
        "invited or contacted": "Invited or contacted",
        "closed": "Closed",
        "created": "Created",
    }.get(status, status)


def normalize_search(text: str) -> str:
    return " ".join(text.strip().split()).casefold()


def fields_match_search(needle: str, *parts: str) -> bool:
    """Substring match (partial or whole), case-insensitive, across given field strings."""
    if not needle:
        return True
    return any(needle in (p or "").casefold() for p in parts)


def task_matches_search(task: db.TaskRow, needle: str) -> bool:
    """Substring match (partial or whole), case-insensitive, across common fields."""
    return fields_match_search(
        needle,
        task.link,
        task.description,
        task.status,
        status_label(task.status),
        task.created_at,
        str(task.id),
    )


def parse_created_at(value: str) -> datetime | None:
    s = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def qdatetime_to_naive_local(qdt: QDateTime) -> datetime:
    qd = qdt.date()
    qt = qdt.time()
    return datetime(qd.year(), qd.month(), qd.day(), qt.hour(), qt.minute(), qt.second())
