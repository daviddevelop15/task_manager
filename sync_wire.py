"""JSON encoding of store values, shared by the server and the client.

The pages downstream expect the real ``database`` dataclasses - `DailyCardRow`,
`MeetingRow` and friends - so a client must get those back, not dictionaries.
Rows therefore travel tagged with their type and are rebuilt on arrival:

    DailyCardRow(id=7, ...)  ->  {"_t": "DailyCardRow", "v": {"id": 7, ...}}

Tuples are tagged too, because ``job_bid_card_titles`` returns one and JSON has
no tuple. Everything else (numbers, strings, booleans, None, lists, plain dicts)
travels as itself.

Both sides import this module, so the format cannot drift between them.
"""

from __future__ import annotations

import base64
from dataclasses import fields, is_dataclass
from typing import Any

import database as db

# Every row type that may cross the wire. A type missing here is a bug, not a
# silent degradation - encode() raises rather than shipping an untyped dict the
# client would fail to rebuild.
ROW_TYPES: dict[str, type] = {
    "DailyListRow": db.DailyListRow,
    "DailyCardRow": db.DailyCardRow,
    "MeetingRow": db.MeetingRow,
    "StickyRow": db.StickyRow,
    "AccRow": db.AccRow,
    "ResumeRow": db.ResumeRow,
    "ResumeSectionRow": db.ResumeSectionRow,
}
# SyncUserRow is deliberately absent: it carries password and recovery-code
# hashes. User information is sent as a curated dict instead, so a hash can
# never leave the server by accident.

_TYPE_NAMES = {t: name for name, t in ROW_TYPES.items()}

_TAG = "_t"
_VAL = "v"
_TUPLE = "tuple"
_BYTES = "bytes"


class WireError(ValueError):
    """A value could not be encoded or decoded."""


def encode(value: Any) -> Any:
    """Turn a store return value into JSON-safe data."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        name = _TYPE_NAMES.get(type(value))
        if name is None:
            raise WireError(f"{type(value).__name__} is not a known wire row type")
        return {_TAG: name, _VAL: {f.name: encode(getattr(value, f.name))
                                   for f in fields(value)}}
    if isinstance(value, tuple):
        return {_TAG: _TUPLE, _VAL: [encode(v) for v in value]}
    if isinstance(value, (bytes, bytearray)):
        # PDF resumes travel as bytes. Base64 costs a third more size and is the
        # only thing JSON can carry losslessly.
        return {_TAG: _BYTES, _VAL: base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (list, set)):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    raise WireError(f"cannot encode {type(value).__name__}")


def decode(value: Any) -> Any:
    """Rebuild what :func:`encode` produced, dataclasses included."""
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    tag = value.get(_TAG)
    if tag is None:
        return {k: decode(v) for k, v in value.items()}
    payload = value.get(_VAL)
    if tag == _TUPLE:
        return tuple(decode(v) for v in (payload or []))
    if tag == _BYTES:
        try:
            return base64.b64decode(payload or "")
        except (ValueError, TypeError) as exc:
            raise WireError(f"corrupt bytes payload: {exc}") from exc
    row_type = ROW_TYPES.get(tag)
    if row_type is None:
        raise WireError(f"unknown row type {tag!r}")
    if not isinstance(payload, dict):
        raise WireError(f"row {tag} has no fields")
    kwargs = {f.name: decode(payload.get(f.name)) for f in fields(row_type)
              if f.name in payload}
    try:
        return row_type(**kwargs)
    except TypeError as exc:                     # a field the server did not send
        raise WireError(f"cannot rebuild {tag}: {exc}") from exc
