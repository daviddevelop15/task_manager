"""Shared table column indices and item roles."""

from __future__ import annotations

from PyQt6.QtCore import Qt

COL_INDEX = 0
COL_DATETIME = 1
COL_LINK = 2
COL_STATUS = 3
COL_DESC = 4
ROLE_TASK_ID = Qt.ItemDataRole.UserRole
ROLE_STICKY_ID = Qt.ItemDataRole.UserRole + 100
ROLE_ACC_ID = Qt.ItemDataRole.UserRole + 101
ROLE_DAILY_CARD_ID = Qt.ItemDataRole.UserRole + 102

DAILY_MIME_CARD = "application/x-taskmanager-daily-card"
DAILY_MIME_LIST = "application/x-taskmanager-daily-list"
DAILY_COLUMNS = ("todo", "doing", "done")
DAILY_COLUMN_LABELS = {
    "todo": "To do",
    "doing": "Doing",
    "done": "Done",
}
STICKY_COL_CHECK = 0
STICKY_COL_NUM = 1
STICKY_COL_TITLE = 2
STICKY_COL_PREVIEW = 3
ACC_COL_CHECK = 0
ACC_COL_NUM = 1
ACC_COL_URL = 2
ACC_COL_MAIL = 3
ACC_COL_PWD = 4
ACC_COL_OTHER = 5
