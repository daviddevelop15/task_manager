"""The PM's machine serving the shared boards to the team over the LAN.

Runs inside the normal desktop app on a background thread: `ThreadingHTTPServer`
plus a small JSON API. Stdlib only.

**Shape of the API.** Rather than one endpoint per operation, clients POST to
``/api/call`` naming a method on the store. That keeps the HTTP surface from
drifting out of step with `app_store.LocalStore` as it grows, and lets the
client be a thin proxy. The safety of that arrangement rests entirely on
:data:`SHARED_METHODS` - a call to anything not on that list is refused, so the
API cannot be used to reach a method simply because it exists.

**What the client is never trusted for.**

* *Who it is* - the author stamped on every write comes from the token's user;
  any ``author`` the client sends is discarded.
* *Where it is* - the source IP is read off the TCP connection. A client cannot
  put an address in a header or body and be believed. (Direct LAN connections
  are assumed: behind a reverse proxy every client would look like the proxy.)

**Tokens** live in memory only, bound to the user *and* the IP they logged in
from, and expire when idle. A server restart invalidates them all, which is
correct - clients simply log in again.
"""

from __future__ import annotations

import inspect
import json
import secrets
import socket
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

import app_store
import sync_users
import sync_wire

DEFAULT_PORT = 8765
DEFAULT_HOST = "0.0.0.0"        # every interface, so the LAN can reach it

# A token idle for this long stops working; the client logs in again.
TOKEN_IDLE_SECONDS = 12 * 60 * 60
# Cap on a request body. Generous for a board write, small enough that a stray
# client cannot exhaust the PM's memory.
MAX_BODY_BYTES = 16 * 1024 * 1024
# Login throttle: this many failures from one IP inside the window and further
# attempts are refused for the rest of it.
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 300
# last_seen is a database write, and the database is encrypted and flushed on
# commit - so a 3-second poll must not write. Persist at most this often.
SEEN_PERSIST_SECONDS = 300

# Methods a logged-in client may call on the store. Everything on the shared
# boards; nothing else. Private data (Acc, Notes) and resumes are owner-scoped
# and get their own endpoints rather than passing through this list.
SHARED_METHODS = frozenset({
    # daily sheets
    "list_daily_lists_for_board", "list_daily_lists_for_day", "list_all_daily_lists",
    "list_daily_lists_ordered",
    "get_daily_list", "add_daily_list", "update_daily_list", "delete_daily_list",
    "reorder_daily_lists", "set_daily_list_always_shown", "get_daily_list_sort_mode",
    "set_daily_list_sort_mode", "ensure_daily_job_bids_list",
    "find_daily_job_bids_list_id", "delete_daily_job_bids_list_if_empty",
    "job_bid_card_titles",
    # daily cards
    "list_daily_cards_for_day", "list_daily_cards_for_list", "list_all_daily_cards",
    "list_daily_card_summaries",
    # Company/title pairs for the duplicate check, computed here so a client
    # never downloads every card body just to compare two fields.
    "list_applied_job_index",
    "get_daily_card", "count_daily_cards_for_list", "add_daily_card",
    "may_edit_card", "may_edit_meeting",
    "update_daily_card", "update_daily_card_status", "update_daily_card_body_hidden",
    "move_daily_card", "delete_daily_card", "reorder_daily_cards",
    # meetings
    "list_meetings", "list_meetings_between", "get_meeting", "add_meeting",
    "update_meeting", "delete_meeting", "set_meeting_linked_card",
    "set_meeting_alerts_enabled",
})

# Owner-scoped and permission-checked, but reached through the same door. They
# are safe here only because the identity arguments below are taken from the
# token and never from the request: a client can ask for "my notes" and nothing
# else, because it cannot say whose notes it means.
PRIVATE_METHODS = frozenset({
    # Who is who: id -> display name, so a client can render a Creator column.
    # Names only; the account table itself never leaves the server.
    "list_user_directory",
    # a person's own Acc and Notes
    "list_stickies", "get_sticky", "add_sticky", "update_sticky", "delete_sticky",
    "list_accs", "get_acc", "add_acc", "update_acc", "delete_acc",
    # the shared resume library
    "list_resumes", "get_resume", "resume_uploader", "may_edit_resume",
    "add_resume", "add_resume_pdf", "update_resume", "set_resume_pdf",
    "touch_resume", "delete_resume",
    # Sections of a resume, typed in by hand. Same ownership rules as the
    # resume they hang off - the store checks each one.
    "list_resume_sections", "add_resume_section", "update_resume_section",
    "delete_resume_section", "reorder_resume_sections",
})

ALLOWED_METHODS = SHARED_METHODS | PRIVATE_METHODS

# Arguments that say *who is asking*. Whatever the client sends for these is
# discarded and replaced from the session, so no request can act as anyone else.
_IDENTITY_ARGS = ("author", "owner", "uploaded_by", "actor", "is_pm")


class ApiError(Exception):
    """An error to report to the client, with the status it deserves."""

    def __init__(self, status: HTTPStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class Session:
    """A logged-in client. Pinned to the IP the login came from."""
    token: str
    user_id: int
    username: str
    label: str          # what gets stamped on their writes
    role: str
    ip: str
    created_at: float
    last_used: float
    last_persisted_seen: float = 0.0

    @property
    def is_pm(self) -> bool:
        return self.role == sync_users.ROLE_PM

    @property
    def principal(self) -> str:
        """What gets stamped on this user's records - stable across renames."""
        return sync_users.principal(self.user_id)


@dataclass
class ServerEvent:
    """One line of the activity log the PM's Users screen shows."""
    at: float
    kind: str
    detail: str


class _Sessions:
    """In-memory token table. Tokens never touch disk."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_token: dict[str, Session] = {}

    def create(self, user, ip: str) -> Session:
        now = time.time()
        sess = Session(
            token=secrets.token_urlsafe(32),
            user_id=user.id,
            username=user.username,
            label=user.label,
            role=user.role,
            ip=sync_users.normalize_ip(ip),
            created_at=now,
            last_used=now,
            last_persisted_seen=now,
        )
        with self._lock:
            self._by_token[sess.token] = sess
        return sess

    def resolve(self, token: str, ip: str) -> Session:
        """The session for this token, or raise. Also enforces the IP pin."""
        with self._lock:
            sess = self._by_token.get(token or "")
            if sess is None:
                raise ApiError(HTTPStatus.UNAUTHORIZED, "no_token",
                               "Not logged in.")
            if time.time() - sess.last_used > TOKEN_IDLE_SECONDS:
                self._by_token.pop(sess.token, None)
                raise ApiError(HTTPStatus.UNAUTHORIZED, "expired",
                               "Session expired - log in again.")
            if sess.ip != sync_users.normalize_ip(ip):
                # The token was stolen, or the PC's address changed. Either way
                # this connection is not the one that logged in.
                raise ApiError(HTTPStatus.FORBIDDEN, "ip_changed",
                               "This session belongs to a different PC.")
            sess.last_used = time.time()
            return sess

    def drop(self, token: str) -> None:
        with self._lock:
            self._by_token.pop(token or "", None)

    def drop_user(self, user_id: int) -> int:
        """Log a user out everywhere - used when the PM disables an account."""
        with self._lock:
            gone = [t for t, s in self._by_token.items() if s.user_id == user_id]
            for t in gone:
                self._by_token.pop(t, None)
            return len(gone)

    def active(self) -> list[Session]:
        cutoff = time.time() - TOKEN_IDLE_SECONDS
        with self._lock:
            return [s for s in self._by_token.values() if s.last_used > cutoff]

    def clear(self) -> None:
        with self._lock:
            self._by_token.clear()


class _LoginThrottle:
    """Slows down password guessing from a single address."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._failures: dict[str, list[float]] = {}

    def blocked(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            recent = [t for t in self._failures.get(ip, [])
                      if now - t < LOGIN_WINDOW_SECONDS]
            self._failures[ip] = recent
            return len(recent) >= LOGIN_MAX_FAILURES

    def record_failure(self, ip: str) -> None:
        with self._lock:
            self._failures.setdefault(ip, []).append(time.time())

    def clear(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)


def _public_user(user) -> dict[str, Any]:
    """The safe half of a user row. Never includes hashes or salts."""
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "label": user.label,
        "role": user.role,
        "enabled": bool(user.enabled),
        "allowed_ips": user.allowed_ips,
        "migrated_at": user.migrated_at,
        "last_seen_at": user.last_seen_at,
    }


class SyncService:
    """The API's behaviour, independent of HTTP.

    Kept separate from the request handler so every rule below can be tested by
    calling a method, without sockets in the way.
    """

    def __init__(self, store=None) -> None:
        self._store = store
        self.sessions = _Sessions()
        self.throttle = _LoginThrottle()
        self._events: list[ServerEvent] = []
        self._events_lock = threading.RLock()

    @property
    def store(self):
        return self._store if self._store is not None else app_store.get_store()

    # --- activity log ------------------------------------------------------

    def log(self, kind: str, detail: str) -> None:
        with self._events_lock:
            self._events.append(ServerEvent(time.time(), kind, detail))
            del self._events[:-200]        # keep the tail only

    def recent_events(self, limit: int = 50) -> list[ServerEvent]:
        with self._events_lock:
            return self._events[-limit:][::-1]

    # --- endpoints ---------------------------------------------------------

    def ping(self) -> dict[str, Any]:
        """Reachability check, callable without a token.

        Deliberately says nothing about who exists - only that a server is here
        and whether it has been set up yet, which the client needs to show a
        useful message before anyone can log in.
        """
        return {
            "ok": True,
            "service": "task-manager-sync",
            "configured": sync_users.has_any_users(),
        }

    def login(self, body: dict, source_ip: str) -> dict[str, Any]:
        ip = sync_users.normalize_ip(source_ip)
        if self.throttle.blocked(ip):
            self.log("login-blocked", f"{ip} - too many failures")
            raise ApiError(HTTPStatus.TOO_MANY_REQUESTS, "throttled",
                           "Too many failed attempts. Wait a few minutes.")
        username = str(body.get("username") or "")
        password = str(body.get("password") or "")
        # Note what is NOT read from the body: any client-supplied address. The
        # IP comes from the connection, which is the whole point of the check.
        result = sync_users.authenticate(username, password, ip)
        if not result.ok:
            self.throttle.record_failure(ip)
            self.log("login-failed", f"{username or '?'}@{ip} - {result.reason}")
            status = (HTTPStatus.FORBIDDEN
                      if result.reason == sync_users.AUTH_IP_NOT_ALLOWED
                      else HTTPStatus.UNAUTHORIZED)
            raise ApiError(status, result.reason, result.message)
        self.throttle.clear(ip)
        sess = self.sessions.create(result.user, ip)
        self.log("login", f"{sess.username}@{ip}")
        return {"token": sess.token, "user": _public_user(result.user),
                "change_version": self.store.change_version()}

    def logout(self, session: Session) -> dict[str, Any]:
        self.sessions.drop(session.token)
        self.log("logout", session.username)
        return {"ok": True}

    def recover(self, body: dict, source_ip: str) -> dict[str, Any]:
        """Redeem a recovery code and set a new password.

        Open without a token by necessity - the point is that the person cannot
        log in. It is still throttled per IP, and the code itself is one-use and
        expiring.
        """
        ip = sync_users.normalize_ip(source_ip)
        if self.throttle.blocked(ip):
            raise ApiError(HTTPStatus.TOO_MANY_REQUESTS, "throttled",
                           "Too many failed attempts. Wait a few minutes.")
        username = str(body.get("username") or "")
        code = str(body.get("code") or "")
        new_password = str(body.get("new_password") or "")
        if not new_password:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request",
                           "A new password is required.")
        user = sync_users.find_user(username)
        # The IP is checked here too: a leaked code is useless from an
        # unregistered PC.
        if user is not None and not sync_users.ip_allowed(user.allowed_ips, ip):
            self.throttle.record_failure(ip)
            raise ApiError(HTTPStatus.FORBIDDEN, sync_users.AUTH_IP_NOT_ALLOWED,
                           "This PC's IP address is not registered for that account.")
        if not sync_users.redeem_recovery_code(username, code, new_password):
            self.throttle.record_failure(ip)
            self.log("recover-failed", f"{username or '?'}@{ip}")
            raise ApiError(HTTPStatus.UNAUTHORIZED, "bad_code",
                           "That recovery code is not valid or has expired.")
        self.throttle.clear(ip)
        self.log("recover", f"{username}@{ip}")
        return {"ok": True}

    def changes(self, session: Session, since: int) -> dict[str, Any]:
        """Cheap poll: has anything shared changed since version ``since``?"""
        self._maybe_persist_seen(session)
        version = self.store.change_version()
        return {"version": version, "changed": version != since}

    def call(self, session: Session, body: dict) -> dict[str, Any]:
        """Invoke one allow-listed store method on the caller's behalf."""
        method = str(body.get("method") or "")
        if method not in ALLOWED_METHODS:
            # Same answer whether the method is unknown or merely not shared:
            # the client has no business learning the difference.
            raise ApiError(HTTPStatus.FORBIDDEN, "not_allowed",
                           f"{method!r} is not available over the network.")
        args = body.get("args") or []
        kwargs = body.get("kwargs") or {}
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request",
                           "args must be a list and kwargs an object.")
        fn: Optional[Callable] = getattr(self.store, method, None)
        if fn is None or not callable(fn):
            raise ApiError(HTTPStatus.NOT_FOUND, "no_method",
                           f"This server has no {method!r}.")
        call_args = [sync_wire.decode(a) for a in args]
        call_kwargs = {str(k): sync_wire.decode(v) for k, v in kwargs.items()}
        # Identity is the server's to decide, always. Whatever the client sent
        # for these is dropped and replaced from the session, so "my notes"
        # cannot be made to mean somebody else's.
        params = _parameters(fn)
        for name in _IDENTITY_ARGS:
            call_kwargs.pop(name, None)
            if name in params:
                # The principal, not the name: an account that gets renamed must
                # keep the rows it created.
                call_kwargs[name] = (session.is_pm if name == "is_pm"
                                     else session.principal)
        try:
            result = fn(*call_args, **call_kwargs)
        except app_store.PermissionDenied as exc:
            raise ApiError(HTTPStatus.FORBIDDEN, "denied", str(exc)) from exc
        except TypeError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_args", str(exc)) from exc
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "rejected", str(exc)) from exc
        return {"result": sync_wire.encode(result),
                "change_version": self.store.change_version()}

    # --- helpers -----------------------------------------------------------

    def _maybe_persist_seen(self, session: Session) -> None:
        """Record last-seen at most every few minutes.

        Clients poll every few seconds, and this database is encrypted and
        re-flushed on each committed write - writing per poll would rewrite the
        whole file continuously for no benefit.
        """
        now = time.time()
        if now - session.last_persisted_seen < SEEN_PERSIST_SECONDS:
            return
        session.last_persisted_seen = now
        try:
            import database as db
            db.touch_sync_user_seen(session.user_id)
        except Exception:
            pass          # a missed heartbeat must never break a client's poll


def _parameters(fn: Callable) -> frozenset:
    """Argument names a store method takes, for deciding what to inject."""
    try:
        return frozenset(inspect.signature(fn).parameters)
    except (TypeError, ValueError):       # builtins / C functions
        return frozenset()


class _Handler(BaseHTTPRequestHandler):
    """Maps HTTP onto :class:`SyncService`. Holds no rules of its own."""

    server_version = "TaskManagerSync/1.0"
    protocol_version = "HTTP/1.1"

    # --- plumbing ----------------------------------------------------------

    @property
    def service(self) -> SyncService:
        return self.server.service            # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        """Silence stderr access logs - this runs inside a GUI app."""

    def _source_ip(self) -> str:
        """The address of the machine at the other end of this socket.

        Read from the connection, never from a header: `X-Forwarded-For` and
        friends are client-writable and would defeat the IP restriction.
        """
        return sync_users.normalize_ip(self.client_address[0])

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_request",
                           "Invalid Content-Length.")
        if length < 0 or length > MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large",
                           "Request body is too large.")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_json",
                           f"Invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_json",
                           "Request body must be a JSON object.")
        return data

    def _session(self) -> Session:
        auth = self.headers.get("Authorization") or ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "no_token", "Not logged in.")
        return self.service.sessions.resolve(token, self._source_ip())

    def _send(self, status: HTTPStatus, payload: dict) -> bool:
        """Write a JSON reply. False if the client had already gone.

        A client that quits, sleeps or loses Wi-Fi mid-reply is routine here,
        and on Windows it surfaces as ConnectionAbortedError from any of the
        header writes as well as the body - so the whole send is guarded, and
        the caller is told not to try again on a socket that is already dead.
        """
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return True
        except OSError:
            self.close_connection = True
            return False

    def _fail(self, err: ApiError) -> None:
        self._send(err.status, {"error": {"code": err.code, "message": err.message}})

    # --- routing -----------------------------------------------------------

    def do_GET(self) -> None:       # noqa: N802  (stdlib naming)
        self._dispatch("GET")

    def do_POST(self) -> None:      # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if method == "GET" and path in ("/api/ping", "/"):
                self._send(HTTPStatus.OK, self.service.ping())
            elif method == "POST" and path == "/api/login":
                self._send(HTTPStatus.OK,
                           self.service.login(self._read_json(), self._source_ip()))
            elif method == "POST" and path == "/api/recover":
                self._send(HTTPStatus.OK,
                           self.service.recover(self._read_json(), self._source_ip()))
            elif method == "POST" and path == "/api/logout":
                self._send(HTTPStatus.OK, self.service.logout(self._session()))
            elif method == "GET" and path == "/api/changes":
                q = parse_qs(parsed.query)
                try:
                    since = int((q.get("since") or ["-1"])[0])
                except ValueError:
                    since = -1
                self._send(HTTPStatus.OK, self.service.changes(self._session(), since))
            elif method == "POST" and path == "/api/call":
                self._send(HTTPStatus.OK,
                           self.service.call(self._session(), self._read_json()))
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, "no_route",
                               f"No {method} {path}.")
        except ApiError as err:
            self._fail(err)
        except OSError:
            # The connection died mid-reply. Not an error worth logging, and
            # there is nowhere to send an error to.
            self.close_connection = True
        except Exception as exc:                        # never kill the thread
            self.service.log("error", f"{method} {path}: {exc!r}")
            self._fail(ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "server_error",
                                "The server hit an unexpected error."))


class _ThreadingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, service: SyncService) -> None:
        self.service = service
        # Track accepted client sockets so a shutdown can close them. With
        # keep-alive a worker thread sits blocked reading the next request on an
        # idle connection; closing the listener alone leaves those threads
        # answering on lingering sockets. A stopped server must serve nothing.
        self._active: set = set()
        self._active_lock = threading.Lock()
        super().__init__(addr, handler)

    def get_request(self):
        sock, addr = super().get_request()
        with self._active_lock:
            self._active.add(sock)
        return sock, addr

    def shutdown_request(self, request) -> None:
        with self._active_lock:
            self._active.discard(request)
        super().shutdown_request(request)

    def close_active(self) -> None:
        """Force-close every open client connection, unblocking idle keep-alive
        readers so a stopped server stops answering at once."""
        with self._active_lock:
            socks = list(self._active)
            self._active.clear()
        for sock in socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


class SyncServer:
    """Starts and stops the API on a background thread."""

    def __init__(self, store=None, host: str = DEFAULT_HOST,
                 port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = port
        self.service = SyncService(store)
        self._httpd: Optional[_ThreadingServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def is_running(self) -> bool:
        return self._httpd is not None

    @property
    def bound_port(self) -> int:
        """The port actually in use - meaningful when 0 was requested."""
        if self._httpd is None:
            return self.port
        return int(self._httpd.server_address[1])

    def start(self) -> None:
        """Bind and serve. Raises OSError if the port is taken."""
        if self._httpd is not None:
            return
        httpd = _ThreadingServer((self.host, self.port), _Handler, self.service)
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="sync-server", daemon=True
        )
        self._thread.start()
        self.service.log("start", f"listening on {self.host}:{self.bound_port}")

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        thread, self._thread = self._thread, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
            httpd.close_active()
        if thread is not None:
            thread.join(timeout=5)
        self.service.sessions.clear()
        self.service.log("stop", "stopped")

    def client_urls(self) -> list[str]:
        """Addresses to hand out to the team."""
        return [f"http://{host}:{self.bound_port}" for host in lan_addresses()]


def lan_addresses() -> list[str]:
    """This machine's likely LAN addresses, best first.

    Asking a UDP socket which interface would reach the outside world names the
    real LAN adapter, where the hostname lookup alone often answers 127.0.0.1 on
    a multi-homed Windows PC.
    """
    found: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))      # no packets are sent by connect()
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for addr in socket.gethostbyname_ex(socket.gethostname())[2]:
            if addr not in found and not addr.startswith("127."):
                found.append(addr)
    except OSError:
        pass
    if not found:
        found.append("127.0.0.1")
    return found
