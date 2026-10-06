"""The developer's app talking to the PM's server.

`RemoteStore` implements the same surface as `app_store.LocalStore` over HTTP,
so page code does not know which one it is using. Nothing is stored on disk: a
dev's PC holds no database, only whatever is in memory for the session.

**What happens when the server goes away.** The PM's machine is an ordinary
desktop that gets closed, sleeps, or loses Wi-Fi, so a client has to survive it
without losing the user's work or lying to them:

* *Reads* fall back to the last value fetched for that exact call, so the board
  stays on screen and readable instead of emptying out. Anything never fetched
  raises, rather than inventing a plausible empty answer.
* *Writes that change existing data* (edits, status, moves, deletes) queue in
  order and replay on reconnect.
* *Writes that create something* refuse immediately. The new row's id is
  assigned by the server and the UI needs it straight away - queueing would mean
  handing back a fake id and reconciling later, which is worse than saying "not
  connected" while the person can still see it happen.

Because nothing persists, closing the app while offline discards the queue. In
one room, with the server on a machine in that room, that is the right trade.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import sync_wire
from sync_server import ALLOWED_METHODS

DEFAULT_TIMEOUT = 10.0        # seconds; a LAN hop should never come near this
POLL_TIMEOUT = 5.0            # the heartbeat gives up sooner than a real call

# Calls that only read. Safe to serve from cache when the server is unreachable.
READ_METHODS = frozenset({
    "list_daily_lists_for_board", "list_daily_lists_for_day", "list_all_daily_lists",
    "list_daily_lists_ordered",
    "get_daily_list", "get_daily_list_sort_mode", "find_daily_job_bids_list_id",
    "job_bid_card_titles", "list_daily_cards_for_day", "list_daily_cards_for_list",
    "list_all_daily_cards", "list_daily_card_summaries", "list_applied_job_index",
    "get_daily_card", "count_daily_cards_for_list",
    "list_meetings", "list_meetings_between", "get_meeting",
    "may_edit_card", "may_edit_meeting",
    "list_stickies", "get_sticky", "list_accs", "get_acc",
    "list_resumes", "get_resume", "resume_uploader", "may_edit_resume",
    "list_resume_sections",
    "list_user_directory",
})

# How long a fetched name map is trusted before an unknown id is worth asking
# about again. Names change rarely; a miss is what triggers a refetch, not a
# timer, so a quiet board makes no requests at all.
DIRECTORY_RETRY_SECONDS = 30.0
# A rename does not change any board data, so nothing else would ever prompt a
# refetch - an id already in the map keeps resolving to the old name. The
# heartbeat refreshes it this often so a rename reaches clients on its own.
DIRECTORY_MAX_AGE_SECONDS = 300.0

# Writes that create a row and hand back its id. These cannot be queued: the id
# comes from the server and the caller needs it now.
CREATE_METHODS = frozenset({
    "add_daily_list", "add_daily_card", "add_meeting", "ensure_daily_job_bids_list",
    "add_sticky", "add_acc", "add_resume", "add_resume_pdf",
})

# Everything else changes existing data and returns nothing useful, so it can
# wait in a queue until the server is back.
QUEUEABLE_METHODS = ALLOWED_METHODS - READ_METHODS - CREATE_METHODS


class RemoteError(Exception):
    """The server answered, and the answer was no."""

    def __init__(self, code: str, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class AuthError(RemoteError):
    """The session is gone - the user has to log in again."""


class OfflineError(Exception):
    """The server could not be reached."""

    def __init__(self, message: str = "Not connected to the server.") -> None:
        super().__init__(message)
        self.message = message


@dataclass
class LoginResult:
    base_url: str
    token: str
    user: dict
    change_version: int

    @property
    def label(self) -> str:
        return str(self.user.get("label") or self.user.get("username") or "")

    @property
    def is_pm(self) -> bool:
        return self.user.get("role") == "pm"


@dataclass
class QueuedWrite:
    method: str
    args: list
    kwargs: dict


def _normalize_base_url(server: str, default_port: int = 8765) -> str:
    """Accept what people actually type: '192.168.1.10', with or without a port
    or scheme, and turn it into a base URL."""
    s = (server or "").strip().rstrip("/")
    if not s:
        raise ValueError("server address is empty")
    if "://" not in s:
        s = "http://" + s
    scheme, _, rest = s.partition("://")
    host = rest.split("/", 1)[0]
    if ":" not in host.strip("[]"):        # no port given
        s = f"{scheme}://{host}:{default_port}"
    return s


class _Http:
    """Minimal JSON client over one kept-alive connection.

    Every store call is a separate request, and the board makes several per
    action, so a fresh TCP (and, on HTTPS, TLS) handshake each time - which is
    what ``urllib`` does - is pure latency. This holds a single
    ``http.client.HTTPConnection`` open and reuses it, reconnecting only when
    the server drops it. A lock serialises requests because a connection object
    is not safe to share between overlapping calls.
    """

    def __init__(self, base_url: str, token: str = "",
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        parts = urllib.parse.urlsplit(self.base_url)
        self._secure = parts.scheme == "https"
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or (443 if self._secure else 80)
        self._prefix = parts.path.rstrip("/")     # usually empty
        self._conn: Optional[http.client.HTTPConnection] = None
        self._lock = threading.RLock()

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        conn = self._conn
        if conn is None:
            cls = http.client.HTTPSConnection if self._secure else http.client.HTTPConnection
            conn = cls(self._host, self._port, timeout=timeout)
            self._conn = conn
        else:
            conn.timeout = timeout
        return conn

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:              # noqa: BLE001 - already broken
                pass

    def request(self, path: str, body: Optional[dict] = None,
                method: Optional[str] = None,
                timeout: Optional[float] = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        verb = method or ("POST" if data is not None else "GET")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        raw, status, reason = self._send(self._prefix + path, verb, data, headers,
                                         timeout or self.timeout)
        if status >= 400:
            raise _error_from_response(status, reason, raw)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteError("bad_response",
                              "The server sent something that is not JSON.") from exc
        if not isinstance(parsed, dict):
            raise RemoteError("bad_response", "The server sent an unexpected reply.")
        return parsed

    def _send(self, path: str, verb: str, data, headers: dict, timeout: float):
        """One request over the kept-alive connection, retried once on a drop.

        A reused HTTP/1.1 connection can be closed by the server between calls;
        the reader then raises before any bytes are sent, so retrying once on a
        fresh connection is safe (the request never reached the server).
        """
        with self._lock:
            for attempt in (1, 2):
                conn = self._connection(timeout)
                try:
                    conn.request(verb, path or "/", body=data, headers=headers)
                    resp = conn.getresponse()
                    raw = resp.read()          # drains body so the socket can be reused
                    return raw, resp.status, resp.reason
                except (http.client.HTTPException, ConnectionError, OSError) as exc:
                    self._drop()
                    if attempt == 2:
                        raise OfflineError(
                            f"Cannot reach {self.base_url}: {exc}"
                        ) from exc
        # unreachable - the loop either returns or raises
        raise OfflineError(f"Cannot reach {self.base_url}")

    def close(self) -> None:
        with self._lock:
            self._drop()


def _error_from_response(status: int, reason: str, raw: bytes) -> Exception:
    try:
        payload = json.loads(raw.decode("utf-8"))
        err = payload.get("error") or {}
        code = str(err.get("code") or "error")
        message = str(err.get("message") or reason)
    except Exception:                      # noqa: BLE001 - a non-JSON error body
        code, message = "error", str(reason)
    if status == 401:
        return AuthError(code, message, status)
    if code == "denied":
        # Same exception the local store raises, so page code catches one thing
        # and does not need to know whether it is talking to a server.
        import app_store

        return app_store.PermissionDenied(message)
    return RemoteError(code, message, status)


# --------------------------------------------------------------- login

def ping(server: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Is a server there, and has it been set up? Raises OfflineError if not."""
    http = _Http(_normalize_base_url(server), timeout=timeout)
    return http.request("/api/ping")


def login(server: str, username: str, password: str,
          timeout: float = DEFAULT_TIMEOUT) -> LoginResult:
    """Log in and get a token.

    Note what is not sent: this machine's IP address. The server reads it off
    the connection - a client that could state its own address would make the
    restriction meaningless.
    """
    base = _normalize_base_url(server)
    http = _Http(base, timeout=timeout)
    payload = http.request("/api/login",
                           {"username": username, "password": password})
    token = str(payload.get("token") or "")
    if not token:
        raise RemoteError("no_token", "The server did not return a session token.")
    return LoginResult(
        base_url=base,
        token=token,
        user=payload.get("user") or {},
        change_version=int(payload.get("change_version") or 0),
    )


def recover(server: str, username: str, code: str, new_password: str,
            timeout: float = DEFAULT_TIMEOUT) -> None:
    """Redeem a recovery code from the PM and set a new password."""
    http = _Http(_normalize_base_url(server), timeout=timeout)
    http.request("/api/recover", {"username": username, "code": code,
                                  "new_password": new_password})


# --------------------------------------------------------------- the store

class RemoteStore:
    """`app_store.LocalStore`'s surface, served over HTTP.

    Methods are proxied rather than written out one by one, so the client cannot
    fall behind the store as it grows - and a method the server does not share
    is refused here too, before it ever reaches the network.
    """

    def __init__(self, login_result: LoginResult,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self._http = _Http(login_result.base_url, login_result.token, timeout)
        self._user = login_result.user
        self._lock = threading.RLock()
        self._version = login_result.change_version
        self._cache: dict[str, Any] = {}
        self._queue: list[QueuedWrite] = []
        self._online = True
        self._listeners: list[Callable[[bool], None]] = []
        # id -> display name, fetched from the server. Records carry ids, and a
        # client has no account table, so without this every Creator cell would
        # read "(removed user 2)".
        self._directory: dict[str, str] = {}
        self._directory_roles: dict[str, str] = {}
        self._directory_checked = 0.0

    # --- identity ----------------------------------------------------------

    @property
    def author(self) -> str:
        """Only for display. The server stamps writes from the token."""
        return str(self._user.get("label") or self._user.get("username") or "")

    @property
    def user(self) -> dict:
        return dict(self._user)

    @property
    def is_pm(self) -> bool:
        return self._user.get("role") == "pm"

    def set_author(self, author: str) -> None:
        """Ignored on purpose: on a client, who you are is the server's call."""

    # --- who is who --------------------------------------------------------

    def author_label(self, value: str) -> str:
        """Resolve a stored author value to a name, using the server's map.

        A value that is not an account id is a plain name from before ids were
        stored, or from an account since deleted, and passes through unchanged.
        """
        text = (value or "").strip()
        if not text or not text.startswith("u:"):
            return text
        with self._lock:
            known = self._directory.get(text)
            due = time.monotonic() - self._directory_checked > DIRECTORY_RETRY_SECONDS
        if known is not None:
            return known
        if due:
            self.refresh_directory()
            with self._lock:
                known = self._directory.get(text)
        # Still unknown: the account really is gone. Say so rather than showing
        # a raw id, and remember it so this does not refetch on every row.
        if known is None:
            known = f"(removed user {text[2:]})"
            with self._lock:
                self._directory[text] = known
        return known

    def author_role(self, value: str) -> str:
        """The role of whoever a stored author value refers to, or ''."""
        text = (value or "").strip()
        if not text.startswith("u:"):
            return ""
        with self._lock:
            known = self._directory_roles.get(text)
        if known is None:
            self.author_label(text)          # may fetch the map as a side effect
            with self._lock:
                known = self._directory_roles.get(text)
        return known or ""

    def refresh_directory(self) -> bool:
        """Refetch the id -> name map. False when the server was unreachable."""
        try:
            entries = self._invoke("list_user_directory", [], {})
        except (OfflineError, RemoteError, AuthError):
            with self._lock:
                self._directory_checked = time.monotonic()
            return False
        mapping, roles = {}, {}
        for row in entries or []:
            if isinstance(row, dict) and row.get("principal"):
                key = str(row["principal"])
                mapping[key] = str(row.get("label") or "")
                roles[key] = str(row.get("role") or "")
        with self._lock:
            # Replace rather than merge, so a rename cannot leave the old name
            # behind, and drop any "(removed user N)" guesses made meanwhile.
            self._directory = mapping
            self._directory_roles = roles
            self._directory_checked = time.monotonic()
        return True

    # --- connection state --------------------------------------------------

    @property
    def online(self) -> bool:
        with self._lock:
            return self._online

    @property
    def pending_writes(self) -> int:
        with self._lock:
            return len(self._queue)

    def on_state_change(self, callback: Callable[[bool], None]) -> None:
        """Register a listener for connected/disconnected transitions.

        A plain callable rather than a Qt signal, so the store stays usable (and
        testable) without a GUI.
        """
        with self._lock:
            self._listeners.append(callback)

    def _set_online(self, online: bool) -> None:
        with self._lock:
            if self._online == online:
                return
            self._online = online
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(online)
            except Exception:              # noqa: BLE001 - a bad listener must
                pass                       # not take the connection down

    # --- change tracking ---------------------------------------------------

    def change_version(self) -> int:
        with self._lock:
            return self._version

    def poll_changes(self) -> bool:
        """Heartbeat. True when the server has data we have not seen.

        Also drives reconnection: a poll that succeeds after a failure flushes
        the queued writes, so recovery needs no separate machinery.
        """
        with self._lock:
            since = self._version
        try:
            payload = self._http.request(f"/api/changes?since={since}",
                                         timeout=POLL_TIMEOUT)
        except OfflineError:
            self._set_online(False)
            return False
        except AuthError:
            self._set_online(False)
            raise
        was_offline = not self.online
        self._set_online(True)
        with self._lock:
            self._version = int(payload.get("version") or 0)
            stale = (time.monotonic() - self._directory_checked
                     > DIRECTORY_MAX_AGE_SECONDS)
        renamed = False
        if was_offline or stale:
            before = dict(self._directory)
            if self.refresh_directory():
                renamed = self._directory != before
        replayed = self.flush_queue() if was_offline else 0
        return bool(payload.get("changed")) or replayed > 0 or renamed

    # --- offline queue -----------------------------------------------------

    def flush_queue(self) -> int:
        """Replay queued writes in order. Returns how many went through.

        A write the server rejects outright (the card was deleted meanwhile, or
        it turned out to belong to somebody else) is dropped rather than retried
        forever - it can never succeed. A write that fails because the server
        vanished again stays at the head of the queue with everything after it,
        preserving order.
        """
        import app_store

        sent = 0
        while True:
            with self._lock:
                if not self._queue:
                    return sent
                item = self._queue[0]
            try:
                self._invoke(item.method, item.args, item.kwargs)
            except OfflineError:
                self._set_online(False)
                return sent
            except (RemoteError, app_store.PermissionDenied):
                # Unreplayable: drop it and carry on, or one refused edit would
                # block every queued change behind it forever.
                pass
            with self._lock:
                if self._queue and self._queue[0] is item:
                    self._queue.pop(0)
            sent += 1

    def discard_queue(self) -> int:
        with self._lock:
            n = len(self._queue)
            self._queue.clear()
            return n

    # --- the proxy ---------------------------------------------------------

    def __getattr__(self, name: str):
        if name.startswith("_") or name not in ALLOWED_METHODS:
            raise AttributeError(
                f"{name!r} is not available on a client - the server does not "
                f"share it."
            )

        def proxy(*args, **kwargs):
            return self._dispatch(name, list(args), dict(kwargs))

        proxy.__name__ = name
        return proxy

    def _dispatch(self, method: str, args: list, kwargs: dict):
        # The server decides who is asking, from the token. Sending these is
        # pointless - it discards them - and would only be confusing to read in
        # a request log.
        for name in ("author", "owner", "uploaded_by", "actor", "is_pm"):
            kwargs.pop(name, None)
        if method in READ_METHODS:
            return self._read(method, args, kwargs)
        return self._write(method, args, kwargs)

    def _read(self, method: str, args: list, kwargs: dict):
        # Serve a repeated read from memory while nothing has changed. Cache
        # entries are tagged with the change_version they were fetched at; a
        # write (ours or another user's, seen via the poll) advances the version
        # and every older entry is skipped. Without this, every read hit the
        # network even when the board had not moved - the reason switching tabs
        # and reopening a card stalled.
        key = _cache_key(method, args, kwargs)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[0] == self._version:
                return hit[1]
        try:
            result = self._invoke(method, args, kwargs)
        except OfflineError:
            self._set_online(False)
            with self._lock:
                hit = self._cache.get(key)
                if hit is not None:
                    return hit[1]     # stale, but better than an empty board
            raise
        self._set_online(True)
        with self._lock:
            self._cache[key] = (self._version, result)
        return result

    def _write(self, method: str, args: list, kwargs: dict):
        try:
            result = self._invoke(method, args, kwargs)
        except OfflineError:
            self._set_online(False)
            if method in CREATE_METHODS:
                raise OfflineError(
                    "Not connected to the server, so this cannot be created "
                    "yet. Your existing rows are still shown, and edits you "
                    "make will be sent when the connection returns."
                ) from None
            with self._lock:
                self._queue.append(QueuedWrite(method, list(args), dict(kwargs)))
            return None
        self._set_online(True)
        with self._lock:
            # Our own write invalidates every read (some writes don't advance the
            # version, e.g. a sort-mode change); remote changes are handled by
            # the version tag above.
            self._cache.clear()
        return result

    def _invoke(self, method: str, args: list, kwargs: dict):
        payload = self._http.request("/api/call", {
            "method": method,
            "args": [sync_wire.encode(a) for a in args],
            "kwargs": {k: sync_wire.encode(v) for k, v in kwargs.items()},
        })
        with self._lock:
            self._version = int(payload.get("change_version") or self._version)
        return sync_wire.decode(payload.get("result"))

    # --- session -----------------------------------------------------------

    def logout(self) -> None:
        try:
            self._http.request("/api/logout", {})
        except (OfflineError, RemoteError):
            pass                          # the token dies with the server anyway
        self._http.close()


def _cache_key(method: str, args: list, kwargs: dict) -> str:
    return json.dumps([method, args, sorted(kwargs.items())], default=str)
