"""Match a meeting to the daily job-application card it belongs to.

A meeting (often an ICS-imported calendar invite) carries a company, a job
title, and one or more people (organizer / attendees = usually the recruiter).
Each daily-board card is one applied job. This module finds the card a meeting
refers to, two ways:

* :func:`recommend` — automatic: rank all cards by how well their company,
  title, and recruiter details line up with the meeting.
* :func:`search` — manual: filter cards by any combination of company name,
  recruiter/person name, and position, combined with AND.

Pure/parsing-only (no Qt), so it is cheap to unit-test. Cards and meetings are
duck-typed: any object with the expected attributes works (``db.DailyCardRow``,
``db.MeetingRow``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape

# Only real HTML element tags (<p>, <br/>, <span …>, </div>). Deliberately does
# NOT match <email@x.com> or <https://…>, which ICS invites wrap in angle
# brackets as plain text — those must survive so recruiter emails are found.
_TAG_RE = re.compile(r"(?s)</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?/?>")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.IGNORECASE)

_COMPANY_NOISE = frozenset(
    {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
     "gmbh", "srl", "sa", "plc", "pvt", "llp", "the", "group", "holdings",
     "technologies", "technology", "labs", "software", "solutions", "en",
     "de", "cv", "sapi", "s", "a"}
)

# Words that look like a name token but are just calendar/meeting filler, so
# they are never treated as a recruiter name pulled from a meeting title.
_TITLE_STOPWORDS = frozenset(
    {"and", "with", "meeting", "interview", "call", "sync", "chat", "screen",
     "screening", "technical", "tech", "hr", "phone", "intro", "introduction",
     "the", "for", "at", "on", "re", "hiring", "recruiter", "team", "1st",
     "2nd", "3rd", "first", "second", "final", "round", "step", "position",
     "role", "zoom", "meet", "teams", "google", "microsoft"}
)


def _plain(body: str) -> str:
    b = body or ""
    if "<" in b and ">" in b:
        b = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", b)
        b = re.sub(r"(?is)<br\s*/?>", "\n", b)
        b = re.sub(r"(?is)</(p|div|li|tr|h[1-6])>", "\n", b)
        b = _TAG_RE.sub(" ", b)
        b = unescape(b)
    return b


def _labeled(text: str, *labels: str) -> str:
    for label in labels:
        pat = re.compile(rf"^\s*{re.escape(label)}\s*:\s*(.*)$",
                         re.IGNORECASE | re.MULTILINE)
        m = pat.search(text or "")
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def _role_from_title(title: str) -> str:
    t = (title or "").strip()
    m = re.match(r"^.+?\s[-–—]\s+(.+)$", t)
    return (m.group(1) if m else t).strip()


def _norm(s: str) -> str:
    s = re.sub(r"[^\w\s]", " ", (s or "").lower())
    return " ".join(t for t in s.split() if t and t not in _COMPANY_NOISE)


def _norm_plain(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (s or "").lower()).split())


def _overlap(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _person_names(text: str) -> set[str]:
    """Candidate person names from 'Organizer:'/'Attendees:' lines: the part of
    each ``Name <email>`` before the angle bracket."""
    names: set[str] = set()
    for label in ("Organizer", "Attendees", "Attendee", "Recruiter"):
        val = _labeled(text, label)
        if not val:
            continue
        for chunk in re.split(r",(?![^<]*>)", val):  # split on commas outside <>
            name = chunk.split("<", 1)[0].strip().strip('"').strip()
            name = _norm_plain(name)
            if name and not _EMAIL_RE.match(name) and len(name.split()) >= 1:
                names.add(name)
    return names


def _names_from_title(title: str) -> set[str]:
    """Person-ish tokens in a meeting title (e.g. 'Felipe Rivera and Vinay
    Lakhani' -> the two names), excluding meeting/calendar filler words."""
    out: set[str] = set()
    # split on ' and ', '&', ',', '/', '|', '-'
    for part in re.split(r"\s+and\s+|\s*[&,/|]\s*|\s+[-–—]\s+", title or ""):
        toks = [t for t in _norm_plain(part).split() if t not in _TITLE_STOPWORDS]
        # keep two-plus-token fragments (First Last), the usual name shape
        if len(toks) >= 2:
            out.add(" ".join(toks))
    return out


@dataclass
class MeetingContext:
    company: str
    role: str
    n_company: str
    n_role: str
    names: set[str] = field(default_factory=set)   # normalized person names
    emails: set[str] = field(default_factory=set)


def parse_meeting(meeting) -> MeetingContext:
    title = getattr(meeting, "title", "") or ""
    body = _plain(getattr(meeting, "body", "") or "")
    link = getattr(meeting, "link", "") or ""
    company = _labeled(body, "Company")
    role = _labeled(body, "Role", "Position") or _role_from_title(title)
    names = _person_names(body) | _names_from_title(title)
    emails = {m.group(0).lower() for m in _EMAIL_RE.finditer(body)}
    emails |= {m.group(0).lower() for m in _EMAIL_RE.finditer(link)}
    return MeetingContext(
        company=company,
        role=role,
        n_company=_norm(company),
        n_role=_norm_plain(role),
        names=names,
        emails=emails,
    )


@dataclass
class CardInfo:
    card_id: int
    title: str
    company: str
    role: str
    status: str
    created_at: str
    text: str        # lowercase title + body, for name/email lookup
    n_company: str
    n_role: str


def index_cards(cards) -> list[CardInfo]:
    out: list[CardInfo] = []
    for c in cards:
        title = getattr(c, "title", "") or ""
        body = _plain(getattr(c, "body", "") or "")
        if not (title.strip() or body.strip()):
            continue
        company = _labeled(body, "Company")
        role = _role_from_title(title)
        out.append(CardInfo(
            card_id=getattr(c, "id"),
            title=title,
            company=company,
            role=role,
            status=(getattr(c, "status", "") or ""),
            created_at=(getattr(c, "created_at", "") or ""),
            text=f"{title}\n{body}".lower(),
            n_company=_norm(company),
            n_role=_norm_plain(role),
        ))
    return out


@dataclass
class Suggestion:
    card: CardInfo
    score: int
    reasons: list[str]


def _name_in_text(name: str, text: str) -> int:
    """0 = no, 1 = surname only, 2 = full name — text is already lowercase."""
    if not name:
        return 0
    if name in text:
        return 2
    toks = name.split()
    if len(toks) >= 2 and len(toks[-1]) >= 4 and re.search(rf"\b{re.escape(toks[-1])}\b", text):
        return 1
    return 0


def recommend(ctx: MeetingContext, cards: list[CardInfo], limit: int = 12) -> list[Suggestion]:
    """Cards most likely to be the meeting's job, best first.

    Company match is the strongest signal, a shared recruiter email next, then
    the recruiter's name appearing in the card, then the job title."""
    role_toks = set(ctx.n_role.split())
    out: list[Suggestion] = []
    for card in cards:
        score = 0
        reasons: list[str] = []
        if ctx.n_company and card.n_company:
            if ctx.n_company == card.n_company:
                score += 100
                reasons.append("same company")
            elif ctx.n_company in card.n_company or card.n_company in ctx.n_company:
                score += 45
                reasons.append("similar company")
        if ctx.emails:
            if any(e in card.text for e in ctx.emails):
                score += 80
                reasons.append("recruiter email")
        best_name = 0
        for name in ctx.names:
            best_name = max(best_name, _name_in_text(name, card.text))
        if best_name == 2:
            score += 40
            reasons.append("recruiter name")
        elif best_name == 1:
            score += 15
            reasons.append("recruiter surname")
        if ctx.n_role and card.n_role:
            if ctx.n_role == card.n_role:
                score += 40
                reasons.append("same title")
            elif _overlap(role_toks, set(card.n_role.split())) >= 0.5:
                score += 22
                reasons.append("similar title")
        if score > 0:
            out.append(Suggestion(card=card, score=score, reasons=reasons))
    out.sort(key=lambda s: (s.score, s.card.created_at), reverse=True)
    return out[:limit]


def search(
    cards: list[CardInfo],
    company: str = "",
    recruiter: str = "",
    position: str = "",
    limit: int = 100,
) -> list[CardInfo]:
    """Cards matching every non-empty field (AND), case-insensitive substrings.

    * ``company`` matches the card's Company field (raw or normalized).
    * ``recruiter`` matches anywhere in the card (name/email noted in the body).
    * ``position`` matches the card's role or title.
    Returns [] when no criteria are given."""
    company = (company or "").strip().lower()
    recruiter = (recruiter or "").strip().lower()
    position = (position or "").strip().lower()
    if not (company or recruiter or position):
        return []
    n_company = _norm(company)
    results: list[tuple[int, str, CardInfo]] = []
    for card in cards:
        if company:
            if company not in card.company.lower() and not (
                n_company and n_company in card.n_company
            ):
                continue
        if recruiter and recruiter not in card.text:
            continue
        if position and position not in card.role.lower() and position not in card.title.lower():
            continue
        # rank: exact company-field hits above partials, then most recent
        exact = 1 if company and card.company.strip().lower() == company else 0
        results.append((exact, card.created_at, card))
    results.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [c for _e, _d, c in results[:limit]]


def card_label(card: CardInfo) -> str:
    company = card.company or "—"
    role = card.role or card.title or "—"
    date = (card.created_at or "")[:10] or "—"
    status = card.status or "no status"
    return f"{company} — {role}   ·   {date}   ·   {status}"
