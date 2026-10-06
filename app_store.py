"""Single data seam between the UI pages and wherever the data actually lives.

The app runs in one of three modes (QSettings key ``network_mode``):

* ``standalone`` - today's single-user app; data is the local encrypted DB.
* ``server``     - the project manager's app; owns the DB and serves it to
                   clients over HTTP. Reads/writes go straight to the local DB.
* ``client``     - a developer's app; keeps no data of its own and talks to the
                   PM's server.

Pages call :func:`get_store` and use the returned object instead of importing
``database`` directly, so the same page code works in every mode. Rows are the
existing ``database`` dataclasses in all modes, so nothing downstream changes.

``LocalStore`` (standalone/server) is implemented here. ``RemoteStore``
(client, HTTP) plugs into the same surface and is registered by
``sync_client`` when the app starts in client mode.
"""

from __future__ import annotations

import threading
from typing import Optional

from PyQt6.QtCore import QSettings

import app_variant
import database as db

MODE_STANDALONE = "standalone"
MODE_SERVER = "server"
MODE_CLIENT = "client"

NETWORK_MODE_SETTINGS_KEY = "network_mode"
DISPLAY_NAME_SETTINGS_KEY = "display_name"

_SETTINGS_ORG = app_variant.SETTINGS_ORG
_SETTINGS_APP = app_variant.SETTINGS_APP

# Shown when somebody tries to change a shared-board row that is not theirs.
_NOT_YOURS = (
    "That {what} was added by somebody else. Only they, or the PM, can change it."
)


class PermissionDenied(Exception):
    """This person may not touch that record.

    Raised rather than returned so a caller cannot ignore it by accident: a
    silent no-op would look to the user like the edit worked.
    """


def _settings() -> QSettings:
    return QSettings(_SETTINGS_ORG, _SETTINGS_APP)


def network_mode() -> str:
    """The configured run mode; anything unrecognised means standalone.

    A Server or Client build starts in its own mode so it works out of the box,
    but only until someone chooses otherwise - the saved setting always wins,
    so a build can be repurposed without rebuilding it.
    """
    fallback = app_variant.default_network_mode()
    val = _settings().value(NETWORK_MODE_SETTINGS_KEY, fallback)
    val = val.strip().lower() if isinstance(val, str) else fallback
    return val if val in (MODE_STANDALONE, MODE_SERVER, MODE_CLIENT) else MODE_STANDALONE


def set_network_mode(mode: str) -> None:
    _settings().setValue(NETWORK_MODE_SETTINGS_KEY, mode)


class LocalStore:
    """Reads and writes the local encrypted database.

    Used by ``standalone`` (no author recorded) and by ``server``, where every
    write is stamped with the acting user's name. A re-entrant lock serialises
    access because in server mode the HTTP worker threads and the Qt GUI thread
    both come through here, while ``database`` keeps a single shared
    connection.
    """

    def __init__(self, author: str = "", is_pm: Optional[bool] = None) -> None:
        self._author = author or ""
        # Who this store acts as for resume permissions. Left None it is derived
        # from the mode; the server passes it per call from the caller's token,
        # because one LocalStore serves every connected user and must not lend
        # the PM's rights to whoever happens to be asking.
        self._is_pm_identity = is_pm
        self._lock = threading.RLock()
        self._version = 0  # bumped on every shared write, for client polling
        # Duplicate-match index, held against the version it was built at (see
        # list_applied_job_index). Rebuilt lazily after any write.
        self._applied_index = None
        self._applied_index_at = -1

    # --- identity / change tracking ---------------------------------------

    @property
    def author(self) -> str:
        return self._author

    def set_author(self, author: str) -> None:
        with self._lock:
            self._author = author or ""

    def set_identity(self, author: str, is_pm: Optional[bool] = None) -> None:
        """Say who is using this store, and whether they administer the team.

        The server calls this once it knows which account owns the machine, so
        the window's own reads and writes are scoped and stamped like anyone
        else's rather than running as nobody.
        """
        with self._lock:
            self._author = author or ""
            self._is_pm_identity = is_pm

    def change_version(self) -> int:
        """Monotonic counter of shared writes. Clients poll this to learn that
        something changed without refetching the whole board."""
        with self._lock:
            return self._version

    def _bump(self) -> None:
        self._version += 1

    def _who(self, author: Optional[str]) -> str:
        """The author to stamp: an explicit one (the server passing the
        authenticated caller) wins over this store's own identity."""
        return (author if author is not None else self._author) or ""

    # --- private data (Acc, Notes) ----------------------------------------
    #
    # These never leave their owner. The scoping is done in SQL rather than by
    # filtering afterwards, so another person's row is not merely hidden - it
    # is never read, and an update or delete aimed at one changes nothing.

    def _owner(self, owner: Optional[str]) -> str:
        """Whose private data a call is about.

        Empty in the single-user app: there is one person, and their existing
        rows were written before anyone had a name, so scoping them to a display
        name they might set later would hide their own notes from them.

        In team mode an empty name would mean *no* scoping, which is how the PM
        ended up seeing developers' notes: the server's own window had no display
        name set, so every query ran unrestricted. It now falls back to the PM
        account, which is who is sitting at the server.
        """
        if owner is not None:
            return owner or ""
        if network_mode() == MODE_STANDALONE:
            return ""
        return self._author or _pm_principal()

    def _is_pm(self, is_pm: Optional[bool]) -> bool:
        """Whether the caller administers the resume library.

        A per-call value wins - that is how the server states who is asking,
        from the token. Failing that, the identity this store was built with.
        Only when neither is given is it derived from the mode: the single-user
        app and the PM's own window each own everything they can see, while a
        client is never assumed to be the PM.
        """
        if is_pm is not None:
            return bool(is_pm)
        if self._is_pm_identity is not None:
            return bool(self._is_pm_identity)
        return network_mode() != MODE_CLIENT

    # --- who is who --------------------------------------------------------

    def list_user_directory(self) -> list[dict]:
        """``[{"principal": "u:2", "label": "sebastian"}, ...]``.

        Records store account ids, so anything that shows a name needs this map.
        A client has no account table of its own - the accounts live on the
        server - so it fetches this instead of guessing, which is why it exists
        as a store method rather than a direct lookup.

        Deliberately just the id and the name: no password material, no IP, no
        role. Everyone needs to read a Creator column; nobody needs the rest.
        """
        with self._lock:
            try:
                import sync_users

                return [{"principal": sync_users.principal(u),
                         "label": u.label,
                         "role": sync_users.ROLE_LABELS.get(u.role, "")}
                        for u in sync_users.list_users()]
            except Exception:            # noqa: BLE001 - never break a board render
                return []

    def author_label(self, value: str) -> str:
        """Resolve one stored author value to a name."""
        try:
            import sync_users

            return sync_users.label_for(value)
        except Exception:                # noqa: BLE001
            return (value or "").strip()

    def author_role(self, value: str) -> str:
        """Resolve one stored author value to a role ('PM' / 'Developer')."""
        try:
            import sync_users

            return sync_users.role_label_for(value)
        except Exception:                # noqa: BLE001
            return ""

    def list_stickies(self, owner: Optional[str] = None):
        with self._lock:
            return db.list_stickies(owner=self._owner(owner))

    def get_sticky(self, sticky_id: int, owner: Optional[str] = None):
        with self._lock:
            return db.get_sticky(sticky_id, owner=self._owner(owner))

    def add_sticky(self, title: str = "", body: str = "",
                   owner: Optional[str] = None) -> int:
        with self._lock:
            return db.add_sticky(title, body, owner=self._owner(owner))

    def update_sticky(self, sticky_id: int, title: str, body: str,
                      owner: Optional[str] = None) -> None:
        with self._lock:
            if not db.update_sticky(sticky_id, title, body, owner=self._owner(owner)):
                raise PermissionDenied("That note belongs to somebody else.")

    def delete_sticky(self, sticky_id: int, owner: Optional[str] = None) -> None:
        with self._lock:
            if not db.delete_sticky(sticky_id, owner=self._owner(owner)):
                raise PermissionDenied("That note belongs to somebody else.")

    def list_accs(self, owner: Optional[str] = None):
        with self._lock:
            return db.list_accs(owner=self._owner(owner))

    def get_acc(self, acc_id: int, owner: Optional[str] = None):
        with self._lock:
            return db.get_acc(acc_id, owner=self._owner(owner))

    def add_acc(self, url: str = "", mail: str = "", pwd: str = "",
                other: str = "", owner: Optional[str] = None) -> int:
        with self._lock:
            return db.add_acc(url, mail, pwd, other, owner=self._owner(owner))

    def update_acc(self, acc_id: int, url: str, mail: str, pwd: Optional[str],
                   other: str, owner: Optional[str] = None) -> None:
        with self._lock:
            if not db.update_acc(acc_id, url, mail, pwd, other,
                                 owner=self._owner(owner)):
                raise PermissionDenied("That account belongs to somebody else.")

    def delete_acc(self, acc_id: int, owner: Optional[str] = None) -> None:
        with self._lock:
            if not db.delete_acc(acc_id, owner=self._owner(owner)):
                raise PermissionDenied("That account belongs to somebody else.")

    # --- the resume library ------------------------------------------------
    #
    # Shared reading: everyone sees every resume and anyone may add one. Only
    # the PM may change or remove someone else's, so a dev cannot edit or delete
    # what the PM published.

    def list_resumes(self):
        with self._lock:
            return db.list_resumes()

    def get_resume(self, resume_id: int):
        with self._lock:
            return db.get_resume(resume_id)

    def resume_uploader(self, resume_id: int) -> str:
        with self._lock:
            return db.resume_uploader(resume_id)

    def may_edit_resume(self, resume_id: int, actor: Optional[str] = None,
                        is_pm: Optional[bool] = None) -> bool:
        """Whether this person may change that resume - for greying out buttons.

        The answer is advisory only. The write itself is checked again, so a UI
        that gets this wrong cannot turn into a permission hole.
        """
        with self._lock:
            if self._is_pm(is_pm):
                return True
            who = (actor if actor is not None else self._author) or ""
            return bool(who) and db.resume_uploader(resume_id) == who

    def add_resume(self, name: str, content: str, fmt: str = "md",
                   uploaded_by: Optional[str] = None) -> int:
        with self._lock:
            return db.add_resume(name, content, fmt,
                                 uploaded_by=self._who(uploaded_by))

    def add_resume_pdf(self, name: str, pdf_bytes: bytes,
                       uploaded_by: Optional[str] = None) -> int:
        with self._lock:
            return db.add_resume_pdf(name, pdf_bytes,
                                     uploaded_by=self._who(uploaded_by))

    # --- resume sections (entered by hand) --------------------------------

    _SECTION_DENIED = (
        "Only the person who uploaded this resume, or the PM, can change its "
        "sections."
    )

    def list_resume_sections(self, resume_id: int):
        with self._lock:
            return db.list_resume_sections(resume_id)

    def add_resume_section(self, resume_id: int, title: str, content: str,
                           actor: Optional[str] = None,
                           is_pm: Optional[bool] = None) -> int:
        with self._lock:
            if not self.may_edit_resume(resume_id, actor, is_pm):
                raise PermissionDenied(self._SECTION_DENIED)
            return db.add_resume_section(resume_id, title, content)

    def update_resume_section(self, section_id: int, title: str, content: str,
                              actor: Optional[str] = None,
                              is_pm: Optional[bool] = None) -> None:
        with self._lock:
            # A section is only as protected as the resume it belongs to, so
            # the check follows the section home rather than trusting the id.
            owner = db.resume_section_owner(section_id)
            if owner is None or not self.may_edit_resume(owner, actor, is_pm):
                raise PermissionDenied(self._SECTION_DENIED)
            db.update_resume_section(section_id, title, content)

    def delete_resume_section(self, section_id: int,
                              actor: Optional[str] = None,
                              is_pm: Optional[bool] = None) -> None:
        with self._lock:
            owner = db.resume_section_owner(section_id)
            if owner is None or not self.may_edit_resume(owner, actor, is_pm):
                raise PermissionDenied(self._SECTION_DENIED)
            db.delete_resume_section(section_id)

    def reorder_resume_sections(self, resume_id: int, ordered_ids: list,
                                actor: Optional[str] = None,
                                is_pm: Optional[bool] = None) -> None:
        with self._lock:
            if not self.may_edit_resume(resume_id, actor, is_pm):
                raise PermissionDenied(self._SECTION_DENIED)
            db.reorder_resume_sections(resume_id, [int(i) for i in ordered_ids])

    def update_resume(self, resume_id: int, name: str, content: str,
                      fmt: str = "md", actor: Optional[str] = None,
                      is_pm: Optional[bool] = None) -> None:
        with self._lock:
            if not db.update_resume(resume_id, name, content, fmt,
                                    actor=self._who(actor), is_pm=self._is_pm(is_pm)):
                raise PermissionDenied(
                    "Only the person who uploaded this resume, or the PM, can change it."
                )

    def set_resume_pdf(self, resume_id: int, pdf_bytes: bytes,
                       actor: Optional[str] = None,
                       is_pm: Optional[bool] = None) -> None:
        with self._lock:
            if not db.set_resume_pdf(resume_id, pdf_bytes, actor=self._who(actor),
                                     is_pm=self._is_pm(is_pm)):
                raise PermissionDenied(
                    "Only the person who uploaded this resume, or the PM, can change it."
                )

    def touch_resume(self, resume_id: int) -> None:
        with self._lock:
            db.touch_resume(resume_id)

    def delete_resume(self, resume_id: int, actor: Optional[str] = None,
                      is_pm: Optional[bool] = None) -> None:
        with self._lock:
            if not db.delete_resume(resume_id, actor=self._who(actor),
                                    is_pm=self._is_pm(is_pm)):
                raise PermissionDenied(
                    "Only the person who uploaded this resume, or the PM, can delete it."
                )

    # --- daily lists (sheets) ---------------------------------------------

    def list_daily_lists_for_board(self, day: str):
        with self._lock:
            return db.list_daily_lists_for_board(day)

    def list_daily_lists_for_day(self, day: str):
        with self._lock:
            return db.list_daily_lists_for_day(day)

    def list_all_daily_lists(self):
        with self._lock:
            return db.list_all_daily_lists()

    def list_daily_lists_ordered(self):
        with self._lock:
            return db.list_daily_lists_ordered()

    def get_daily_list(self, list_id: int):
        with self._lock:
            return db.get_daily_list(list_id)

    def add_daily_list(self, day: str, title: str = "New column",
                       author: Optional[str] = None) -> int:
        with self._lock:
            new_id = db.add_daily_list(day, title, author=self._who(author))
            self._bump()
            return new_id

    def update_daily_list(self, list_id: int, title: str,
                          author: Optional[str] = None,
                          is_pm: Optional[bool] = None) -> None:
        with self._lock:
            if not db.update_daily_list(list_id, title, author=self._who(author),
                                        is_pm=self._is_pm(is_pm)):
                raise PermissionDenied(_NOT_YOURS.format(what="sheet"))
            self._bump()

    def delete_daily_list(self, list_id: int, author: Optional[str] = None,
                          is_pm: Optional[bool] = None) -> None:
        with self._lock:
            if not db.delete_daily_list(list_id, author=self._who(author),
                                        is_pm=self._is_pm(is_pm)):
                raise PermissionDenied(
                    "You can only delete a sheet you created, and only while "
                    "everything on it is yours."
                )
            self._bump()

    def reorder_daily_lists(self, day: str, ordered_list_ids: list[int]) -> None:
        with self._lock:
            db.reorder_daily_lists(day, ordered_list_ids)
            self._bump()

    def set_daily_list_always_shown(self, list_id: int, always_shown: bool) -> None:
        with self._lock:
            db.set_daily_list_always_shown(list_id, always_shown)
            self._bump()

    def get_daily_list_sort_mode(self, list_id: int) -> str:
        with self._lock:
            return db.get_daily_list_sort_mode(list_id)

    def set_daily_list_sort_mode(self, list_id: int, mode: str) -> None:
        with self._lock:
            db.set_daily_list_sort_mode(list_id, mode)

    def ensure_daily_job_bids_list(self) -> int:
        with self._lock:
            return db.ensure_daily_job_bids_list()

    def find_daily_job_bids_list_id(self) -> Optional[int]:
        with self._lock:
            return db.find_daily_job_bids_list_id()

    def delete_daily_job_bids_list_if_empty(self) -> bool:
        with self._lock:
            return db.delete_daily_job_bids_list_if_empty()

    def job_bid_card_titles(self, prefix: str):
        with self._lock:
            return db.job_bid_card_titles(prefix)

    # --- daily cards -------------------------------------------------------

    def list_daily_cards_for_day(self, day: str):
        with self._lock:
            return db.list_daily_cards_for_day(day)

    def list_daily_cards_for_list(self, list_id: int):
        with self._lock:
            return db.list_daily_cards_for_list(list_id)

    def list_all_daily_cards(self):
        with self._lock:
            return db.list_all_daily_cards()

    def list_applied_job_index(self):
        """Every card reduced to what duplicate-matching needs, and nothing else.

        The "already applied to this job" check compares company and title. Read
        naively that means every card's full body - the whole job description -
        which is most of the database. A client was downloading all of it each
        time a card was opened, because creating the card had just invalidated
        its read cache: several seconds of frozen window on a LAN that should
        answer in milliseconds.

        Computing the index here, where the rows already are, turns that into a
        few kilobytes of company/title pairs. The result is cached against the
        change counter, so repeated asks cost nothing until somebody writes.
        """
        with self._lock:
            if self._applied_index_at == self._version and self._applied_index is not None:
                return self._applied_index
            # Imported late: the parsing lives with the board page, which
            # imports this module - at module scope that would be a cycle.
            import daily_board_page

            rows = [
                daily_board_page.applied_index_row(card)
                for card in db.list_all_daily_cards()
            ]
            rows = [r for r in rows if r is not None]
            self._applied_index = rows
            self._applied_index_at = self._version
            return rows

    def list_daily_card_summaries(self):
        with self._lock:
            return db.list_daily_card_summaries()

    def get_daily_card(self, card_id: int):
        with self._lock:
            return db.get_daily_card(card_id)

    def count_daily_cards_for_list(self, list_id: int) -> int:
        with self._lock:
            return db.count_daily_cards_for_list(list_id)

    def may_edit_card(self, card_id: int, actor: Optional[str] = None,
                      is_pm: Optional[bool] = None) -> bool:
        """Whether this person may change that card - for greying out an editor.

        Advisory only: the write is checked again underneath, so a UI that gets
        this wrong is a cosmetic bug and not a permission hole.
        """
        with self._lock:
            if self._is_pm(is_pm):
                return True
            who = (actor if actor is not None else self._author) or ""
            if not who:
                return True                 # single-user app: everything is theirs
            card = db.get_daily_card(card_id)
            return card is None or (card.created_by or "") == who

    def may_edit_meeting(self, meeting_id: int, actor: Optional[str] = None,
                         is_pm: Optional[bool] = None) -> bool:
        """The same question for a meeting."""
        with self._lock:
            if self._is_pm(is_pm):
                return True
            who = (actor if actor is not None else self._author) or ""
            if not who:
                return True
            meeting = db.get_meeting(meeting_id)
            return meeting is None or (meeting.created_by or "") == who

    def add_daily_card(self, list_id: int, title: str = "",
                       author: Optional[str] = None) -> int:
        with self._lock:
            new_id = db.add_daily_card(list_id, title, author=self._who(author))
            self._bump()
            return new_id

    def update_daily_card(self, card_id: int, title: str, body: str,
                          author: Optional[str] = None,
                          is_pm: Optional[bool] = None) -> None:
        with self._lock:
            if not db.update_daily_card(card_id, title, body,
                                        author=self._who(author),
                                        is_pm=self._is_pm(is_pm)):
                raise PermissionDenied(_NOT_YOURS.format(what="job"))
            self._bump()

    def update_daily_card_status(self, card_id: int, status: str,
                                 author: Optional[str] = None,
                                 is_pm: Optional[bool] = None) -> None:
        with self._lock:
            if not db.update_daily_card_status(card_id, status,
                                               author=self._who(author),
                                               is_pm=self._is_pm(is_pm)):
                raise PermissionDenied(_NOT_YOURS.format(what="job"))
            self._bump()

    def update_daily_card_body_hidden(self, card_id: int, body_hidden: bool) -> None:
        with self._lock:
            db.update_daily_card_body_hidden(card_id, body_hidden)

    def move_daily_card(self, card_id: int, list_id: int,
                        position: int | None = None,
                        author: Optional[str] = None,
                        is_pm: Optional[bool] = None) -> None:
        with self._lock:
            if not db.move_daily_card(card_id, list_id, position,
                                      author=self._who(author),
                                      is_pm=self._is_pm(is_pm)):
                raise PermissionDenied(_NOT_YOURS.format(what="job"))
            self._bump()

    def delete_daily_card(self, card_id: int, author: Optional[str] = None,
                          is_pm: Optional[bool] = None) -> None:
        with self._lock:
            if not db.delete_daily_card(card_id, author=self._who(author),
                                        is_pm=self._is_pm(is_pm)):
                raise PermissionDenied(_NOT_YOURS.format(what="job"))
            self._bump()

    def reorder_daily_cards(self, list_id: int, ordered_card_ids: list[int]) -> None:
        with self._lock:
            db.reorder_daily_cards(list_id, ordered_card_ids)
            self._bump()

    # --- meetings ----------------------------------------------------------

    def list_meetings(self):
        with self._lock:
            return db.list_meetings()

    def list_meetings_between(self, start_iso: str, end_iso: str):
        with self._lock:
            return db.list_meetings_between(start_iso, end_iso)

    def get_meeting(self, meeting_id: int):
        with self._lock:
            return db.get_meeting(meeting_id)

    def add_meeting(self, title: str, body: str, link: str, start_at: str,
                    alert_lead_minutes: int, alert_interval_minutes: int,
                    alerts_enabled: bool = True, status: str = "",
                    linked_card_id: Optional[int] = None,
                    author: Optional[str] = None) -> int:
        with self._lock:
            new_id = db.add_meeting(
                title, body, link, start_at, alert_lead_minutes,
                alert_interval_minutes, alerts_enabled, status, linked_card_id,
                author=self._who(author),
            )
            self._bump()
            return new_id

    def update_meeting(self, meeting_id: int, title: str, body: str, link: str,
                       start_at: str, alert_lead_minutes: int,
                       alert_interval_minutes: int, alerts_enabled: bool,
                       status: str = "", linked_card_id: Optional[int] = None,
                       author: Optional[str] = None,
                       is_pm: Optional[bool] = None) -> None:
        with self._lock:
            if not db.update_meeting(
                meeting_id, title, body, link, start_at, alert_lead_minutes,
                alert_interval_minutes, alerts_enabled, status, linked_card_id,
                author=self._who(author), is_pm=self._is_pm(is_pm),
            ):
                raise PermissionDenied(_NOT_YOURS.format(what="meeting"))
            self._bump()

    def delete_meeting(self, meeting_id: int, author: Optional[str] = None,
                       is_pm: Optional[bool] = None) -> None:
        with self._lock:
            if not db.delete_meeting(meeting_id, author=self._who(author),
                                     is_pm=self._is_pm(is_pm)):
                raise PermissionDenied(_NOT_YOURS.format(what="meeting"))
            self._bump()

    def set_meeting_linked_card(self, meeting_id: int, card_id: Optional[int],
                                author: Optional[str] = None,
                                is_pm: Optional[bool] = None) -> None:
        with self._lock:
            if not db.set_meeting_linked_card(meeting_id, card_id,
                                              author=self._who(author),
                                              is_pm=self._is_pm(is_pm)):
                raise PermissionDenied(_NOT_YOURS.format(what="meeting"))
            self._bump()

    def set_meeting_alerts_enabled(self, meeting_id: int, enabled: bool,
                                   author: Optional[str] = None,
                                   is_pm: Optional[bool] = None) -> None:
        with self._lock:
            if not db.set_meeting_alerts_enabled(meeting_id, enabled,
                                                 author=self._who(author),
                                                 is_pm=self._is_pm(is_pm)):
                raise PermissionDenied(_NOT_YOURS.format(what="meeting"))
            self._bump()


# --- the active store -------------------------------------------------------

_store: Optional[object] = None
_local: Optional[LocalStore] = None


def local_store() -> LocalStore:
    """This machine's own database, whatever mode the app is in.

    Kept separate from :func:`get_store` because the server must serve the local
    database and nothing else. Reading the *active* store would be subtly wrong:
    on a machine that is also a client, the server would end up proxying to
    another server instead of answering from disk.
    """
    global _local
    if _local is None:
        _local = LocalStore(author=display_name())
    return _local


def get_store():
    """The store the UI should use for shared data.

    Client mode installs a ``RemoteStore`` via :func:`set_store` during startup;
    until that happens this is the local database, which keeps standalone and
    server behaviour identical to today.
    """
    global _store
    if _store is None:
        _store = local_store()
    return _store


def set_store(store) -> None:
    """Install a store explicitly (used by client mode)."""
    global _store
    _store = store


def reset_store() -> None:
    """Drop the cached stores so the next call rebuilds them (tests, mode change)."""
    global _store, _local
    _store = None
    _local = None


def _pm_principal() -> str:
    """The PM account's principal, or '' when the server has no accounts yet.

    Only used as a fallback for the server's own window. Before any account
    exists there is nobody to scope to and the data belongs to whoever set the
    machine up, so '' (unscoped) is the right answer then.
    """
    try:
        import sync_users

        pms = [u for u in sync_users.list_users() if u.is_pm]
        return sync_users.principal(pms[0]) if len(pms) == 1 else ""
    except Exception:            # noqa: BLE001 - never break a read over this
        return ""


def author_label(value: str) -> str:
    """Turn a stored author value into a name for display.

    Page code calls this rather than printing the column, because what is stored
    is an account id, not a name - that is what lets someone rename themselves
    without losing their rows.

    Asks the active store, because only it knows where the accounts are: on a
    client they are on the server, not in the local database, and resolving
    locally there produced "(removed user 2)" against every row.
    """
    text = (value or "").strip()
    if not text:
        return ""
    resolver = getattr(get_store(), "author_label", None)
    if callable(resolver):
        try:
            return resolver(text)
        except Exception:        # noqa: BLE001 - fall back to the raw value
            pass
    return text


def author_role(value: str) -> str:
    """The role of whoever a stored author value refers to, or ''.

    Same routing as :func:`author_label`: only the active store knows where the
    accounts are.
    """
    text = (value or "").strip()
    if not text:
        return ""
    resolver = getattr(get_store(), "author_role", None)
    if callable(resolver):
        try:
            return resolver(text)
        except Exception:        # noqa: BLE001
            pass
    return ""


def author_display(value: str) -> str:
    """``"name (Role)"`` for a stored author value, or just the name.

    The role is dropped when it cannot be known - a legacy plain name, or an
    account since deleted - rather than shown as an empty pair of brackets.
    """
    name = author_label(value)
    if not name:
        return ""
    role = author_role(value)
    return f"{name} ({role})" if role else name


def display_name() -> str:
    """This user's name as recorded on the data they create."""
    val = _settings().value(DISPLAY_NAME_SETTINGS_KEY, "")
    return val.strip() if isinstance(val, str) else ""


def set_display_name(name: str) -> None:
    _settings().setValue(DISPLAY_NAME_SETTINGS_KEY, (name or "").strip())
    for store in (_store, _local):
        if isinstance(store, LocalStore):
            store.set_author((name or "").strip())
