"""Minimal iCalendar (.ics) reader — extracts VEVENTs into meeting fields.

No external dependency. Handles RFC 5545 line unfolding, property parameters
(``ORGANIZER;CN=Jane Doe:mailto:jane@x.com``), TEXT unescaping, and the common
DTSTART value forms (UTC 'Z', floating local, TZID wall-clock, and date-only).
Times are returned as local wall-clock ``YYYY-MM-DD HH:MM:SS`` strings so they
drop straight into the meetings table.

The meeting link is taken from the first property that carries one: URL,
CONFERENCE (RFC 7986), the Google/Microsoft conferencing extensions, then any
http(s) URL found in LOCATION or DESCRIPTION.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

# A URL inside free text. Trailing punctuation/brackets are trimmed separately.
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_URL_TRAILING = ">,.;:)]}'\"\\"

# Properties that may carry the conferencing link, in priority order.
_LINK_PROPS = (
    "X-GOOGLE-CONFERENCE",
    "X-MICROSOFT-SKYPETEAMSMEETINGURL",
    "X-MICROSOFT-ONLINEMEETINGEXTERNALLINK",
    "CONFERENCE",
    "URL",
)


@dataclass
class IcsEvent:
    title: str
    body: str
    link: str
    start_at: str  # 'YYYY-MM-DD HH:MM:SS'
    organizer: str = ""            # "Name <mail>" or "mail"
    attendees: list[str] = field(default_factory=list)


def _unescape(text: str) -> str:
    """Undo RFC 5545 TEXT escaping (\\n, \\,, \\;, \\\\)."""
    out: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append({"n": "\n", "N": "\n"}.get(nxt, nxt))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _unfold(text: str) -> list[str]:
    """Normalize newlines and join folded continuation lines (leading space/tab)."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for ln in raw:
        if ln[:1] in (" ", "\t") and lines:
            lines[-1] += ln[1:]
        else:
            lines.append(ln)
    return lines


def _split_line(line: str) -> tuple[str, dict[str, str], str]:
    """('NAME', {params}, 'value') for one unfolded content line.

    Splits on the first colon that is not inside a quoted parameter value, so
    values like ``mailto:a@b.c`` and params like ``ALTREP="http://x"`` survive.
    """
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            return (*_split_params(line[:i]), line[i + 1:])
    return "", {}, ""


def _split_params(name_part: str) -> tuple[str, dict[str, str]]:
    """('NAME', {PARAM: value}) from the part before the colon."""
    parts: list[str] = []
    cur: list[str] = []
    in_quotes = False
    for ch in name_part:
        if ch == '"':
            in_quotes = not in_quotes
            cur.append(ch)
        elif ch == ";" and not in_quotes:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    params: dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip().upper()] = v.strip().strip('"')
    return parts[0].strip().upper(), params


def _clean_url(url: str) -> str:
    return url.strip().rstrip(_URL_TRAILING)


def _find_url(*texts: str) -> str:
    for t in texts:
        m = _URL_RE.search(t or "")
        if m:
            return _clean_url(m.group(0))
    return ""


_OFFSET_RE = re.compile(r"([+-])(\d{2})(\d{2})")


def _parse_offset(text: str) -> Optional[timezone]:
    """A '+0530'/'-0800' UTC-offset string as a datetime timezone."""
    m = _OFFSET_RE.match((text or "").strip())
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))


# iCalendar weekday tokens -> Python weekday() (Mon=0 .. Sun=6).
_RRULE_WEEKDAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_BYDAY_RE = re.compile(r"^(-?\d+)?([A-Z]{2})$")


@dataclass
class _TzObservance:
    """One STANDARD or DAYLIGHT block of a VTIMEZONE.

    ``offset_to`` is the UTC offset once this observance is in effect. When the
    block carries a yearly ``RRULE`` (e.g. "2nd Sunday of March at 02:00"),
    ``has_rule`` is set and month/ordinal/weekday/time describe that onset, so
    the offset in effect on any given date can be worked out.
    """

    offset_to: timezone
    month: int = 0        # BYMONTH 1-12
    ordinal: int = 0      # nth weekday; -1 = last
    weekday: int = -1     # Python weekday, Mon=0
    hour: int = 0
    minute: int = 0
    second: int = 0
    has_rule: bool = False


def _parse_rrule(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in value.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().upper()] = v.strip().upper()
    return out


def _collect_timezones(lines: list[str]) -> dict[str, list[_TzObservance]]:
    """Map each VTIMEZONE's TZID to its STANDARD/DAYLIGHT observances.

    Used only when Qt can't resolve the named zone (e.g. Microsoft's
    "Customized Time Zone"). Keeping the observances - not just one offset -
    lets the fallback follow the zone's own daylight-saving rules instead of
    pinning every date to the first offset seen.
    """
    zones: dict[str, list[_TzObservance]] = {}
    tzid: Optional[str] = None
    in_tz = False
    cur: Optional[dict] = None
    for line in lines:
        upper = line.upper()
        if upper.startswith("BEGIN:VTIMEZONE"):
            in_tz, tzid = True, None
            continue
        if upper.startswith("END:VTIMEZONE"):
            in_tz, tzid = False, None
            continue
        if not in_tz:
            continue
        if upper.startswith("BEGIN:STANDARD") or upper.startswith("BEGIN:DAYLIGHT"):
            cur = {"off": None, "month": 0, "ordinal": 0, "weekday": -1,
                   "h": 0, "m": 0, "s": 0, "rule": False}
            continue
        if upper.startswith("END:STANDARD") or upper.startswith("END:DAYLIGHT"):
            if tzid and cur and cur["off"] is not None:
                off = _parse_offset(cur["off"])
                if off is not None:
                    zones.setdefault(tzid, []).append(_TzObservance(
                        offset_to=off, month=cur["month"], ordinal=cur["ordinal"],
                        weekday=cur["weekday"], hour=cur["h"], minute=cur["m"],
                        second=cur["s"], has_rule=cur["rule"]))
            cur = None
            continue
        name, _params, value = _split_line(line)
        if name == "TZID" and not tzid:
            tzid = value.strip()
        elif cur is not None:
            if name == "TZOFFSETTO":
                cur["off"] = value.strip()
            elif name == "DTSTART":              # the onset time-of-day
                try:
                    d = datetime.strptime(value.strip()[:15], "%Y%m%dT%H%M%S")
                    cur["h"], cur["m"], cur["s"] = d.hour, d.minute, d.second
                except ValueError:
                    pass
            elif name == "RRULE":
                r = _parse_rrule(value)
                m = _BYDAY_RE.match(r.get("BYDAY", ""))
                if m and r.get("BYMONTH", "").isdigit():
                    cur["month"] = int(r["BYMONTH"])
                    cur["ordinal"] = int(m.group(1)) if m.group(1) else 1
                    cur["weekday"] = _RRULE_WEEKDAY.get(m.group(2), -1)
                    cur["rule"] = cur["weekday"] >= 0
    return zones


def _onset_in_year(year: int, obs: _TzObservance) -> Optional[datetime]:
    """The concrete date/time this observance's rule fires in ``year``."""
    if not obs.has_rule or not 1 <= obs.month <= 12:
        return None
    if obs.ordinal == -1:                        # last <weekday> of the month
        nxt = (datetime(year + 1, 1, 1) if obs.month == 12
               else datetime(year, obs.month + 1, 1))
        last = nxt - timedelta(days=1)
        day = last.day - ((last.weekday() - obs.weekday) % 7)
    else:                                        # nth <weekday> of the month
        first = datetime(year, obs.month, 1)
        day = 1 + ((obs.weekday - first.weekday()) % 7) + (obs.ordinal - 1) * 7
    try:
        return datetime(year, obs.month, day, obs.hour, obs.minute, obs.second)
    except ValueError:
        return None


def _offset_for(naive: datetime, observances: list[_TzObservance]) -> Optional[timezone]:
    """The offset in effect at ``naive`` per the zone's daylight-saving rules.

    Returns None when the zone doesn't carry two dated transition rules (i.e.
    there is no DST to resolve), so the caller can use a fixed offset instead.
    """
    ruled = [o for o in observances if o.has_rule]
    if len(ruled) < 2:
        return None
    events: list[tuple[datetime, timezone]] = []
    for yr in (naive.year - 1, naive.year, naive.year + 1):
        for obs in ruled:
            when = _onset_in_year(yr, obs)
            if when is not None:
                events.append((when, obs.offset_to))
    events.sort(key=lambda e: e[0])
    chosen: Optional[timezone] = None
    for when, off in events:
        if when <= naive:
            chosen = off
        else:
            break
    if chosen is None and events:                # before the earliest transition
        chosen = events[0][1]
    return chosen


def _wallclock_in_zone_to_local(
    naive: datetime, tzid: str, zones: Optional[dict[str, list[_TzObservance]]]
) -> Optional[str]:
    """A wall-clock reading in the named zone, expressed as local wall-clock.

    Prefers Qt's QTimeZone (handles DST for IANA ids like 'Asia/Kolkata'). When
    Qt can't resolve the id, follows the VTIMEZONE's own STANDARD/DAYLIGHT rules
    so daylight saving is applied for the event's actual date; only a zone with
    no such rules falls back to its single fixed offset.
    """
    try:  # Qt path — QtCore only, no QApplication needed
        from PyQt6.QtCore import QDate, QDateTime, QTime, QTimeZone

        tz = QTimeZone(tzid.encode())
        if tz.isValid():
            qdt = QDateTime(
                QDate(naive.year, naive.month, naive.day),
                QTime(naive.hour, naive.minute, naive.second),
            )
            qdt.setTimeZone(tz)
            local = QTimeZone(QTimeZone.systemTimeZoneId())
            shown = qdt.toTimeZone(local).toString("yyyy-MM-dd HH:mm:ss")
            if shown:
                return shown
    except Exception:
        pass
    observances = (zones or {}).get(tzid) or []
    off = _offset_for(naive, observances)
    if off is None and observances:              # non-DST zone: single offset
        off = observances[0].offset_to
    if off is not None:
        return naive.replace(tzinfo=off).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return None


def _parse_dt(
    value: str,
    tzid: Optional[str] = None,
    zones: Optional[dict[str, list[_TzObservance]]] = None,
) -> Optional[str]:
    """Convert an ICS DTSTART value to a local 'YYYY-MM-DD HH:MM:SS' string."""
    v = value.strip()
    try:
        if len(v) == 8 and v.isdigit():  # date only: YYYYMMDD
            return datetime.strptime(v, "%Y%m%d").strftime("%Y-%m-%d %H:%M:%S")
        if v.endswith("Z"):  # UTC -> local
            dt = (
                datetime.strptime(v, "%Y%m%dT%H%M%SZ")
                .replace(tzinfo=timezone.utc)
                .astimezone()
            )
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        naive = datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
        if tzid:  # TZID wall-clock: convert from the named zone to local
            converted = _wallclock_in_zone_to_local(naive, tzid, zones)
            if converted:
                return converted
        # floating local (or an unresolvable zone): keep the wall-clock as-is
        return naive.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _person(params: dict[str, str], value: str) -> str:
    """'Name <mail>' (or just the mail/value) from an ORGANIZER/ATTENDEE line."""
    raw = (value or "").strip()
    mail = raw[7:].strip() if raw[:7].lower() == "mailto:" else raw
    name = (params.get("CN") or "").strip()
    if name and mail and name.lower() != mail.lower():
        return f"{name} <{mail}>"
    return mail or name


def parse_ics_events(text: str) -> list[IcsEvent]:
    """Parse every VEVENT that has a start time into an :class:`IcsEvent`."""
    events: list[IcsEvent] = []
    cur: Optional[dict] = None
    lines = _unfold(text)
    zones = _collect_timezones(lines)
    for line in lines:
        upper = line.upper()
        if upper.startswith("BEGIN:VEVENT"):
            cur = {"attendees": [], "links": {}}
            continue
        if upper.startswith("END:VEVENT"):
            if cur is not None and cur.get("start"):
                events.append(_build_event(cur))
            cur = None
            continue
        if cur is None:
            continue
        name, params, value = _split_line(line)
        if not name:
            continue
        if name == "SUMMARY":
            cur["summary"] = _unescape(value)
        elif name == "DESCRIPTION":
            cur["description"] = _unescape(value)
        elif name == "LOCATION":
            cur["location"] = _unescape(value)
        elif name == "ORGANIZER":
            who = _person(params, value)
            if who:
                cur["organizer"] = who
        elif name == "ATTENDEE":
            who = _person(params, value)
            if who and who not in cur["attendees"]:
                cur["attendees"].append(who)
        elif name in _LINK_PROPS:
            url = _clean_url(_unescape(value))
            if url:
                cur["links"][name] = url
        elif name == "DTSTART":
            dt = _parse_dt(value, tzid=params.get("TZID"), zones=zones)
            if dt:
                cur["start"] = dt
    return events


def _build_event(cur: dict) -> IcsEvent:
    description = (cur.get("description") or "").strip()
    location = (cur.get("location") or "").strip()
    organizer = (cur.get("organizer") or "").strip()
    attendees: list[str] = cur.get("attendees") or []

    # Meeting link: first conferencing property that has one, else a URL found
    # in LOCATION, else one in DESCRIPTION.
    link = ""
    for prop in _LINK_PROPS:
        if cur["links"].get(prop):
            link = cur["links"][prop]
            break
    if not link:
        link = _find_url(location, description)

    body_parts: list[str] = []
    if organizer:
        body_parts.append(f"Organizer: {organizer}")
    if attendees:
        body_parts.append("Attendees: " + ", ".join(attendees))
    if location and location != link:
        body_parts.append(f"Location: {location}")
    if description:
        if body_parts:
            body_parts.append("")  # blank line before the free-text description
        body_parts.append(description)
    return IcsEvent(
        title=(cur.get("summary") or "").strip() or "(no title)",
        body="\n".join(body_parts),
        link=link,
        start_at=cur["start"],
        organizer=organizer,
        attendees=list(attendees),
    )
