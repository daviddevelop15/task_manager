"""SQLite persistence for task links."""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import acc_crypto
import secure_store


DB_FILENAME = "task_manager.db"
ENC_FILENAME = "task_manager.db.enc"

TEMP_DB_FILENAME = "task_manager.plain.tmp.db"

# Encrypted mode keeps the database in RAM: a single persistent in-memory
# connection (the "keeper"), and _connect() returns it, so the existing
# `with _connect() as conn:` pattern keeps working while no plaintext touches
# disk. Some packaged builds cannot open the temp file SQLite needs for
# in-memory writes ("unable to open database file"); in that case we fall back
# to a decrypted temp file (file-based SQLite, reliable everywhere), removed on
# close. The encrypted file is always the source of truth at rest.
_dek: Optional[bytes] = None
_keeper: Optional[sqlite3.Connection] = None
_encrypted = False
_temp_plain_path: Optional[str] = None  # set only in the temp-file fallback
_flush_lock = threading.RLock()

# Writes coalesce into a single background flush after a short quiet period so a
# burst (e.g. a drag-reorder that commits once per card) re-encrypts the whole
# DB once instead of once per commit. flush()/close() force an immediate
# synchronous flush, so normal shutdown never loses data; a hard crash loses at
# most the edits made in the last _FLUSH_DEBOUNCE_SEC seconds.
_FLUSH_DEBOUNCE_SEC = 0.8
_flush_timer: Optional[threading.Timer] = None
_flush_dirty = False


def _base_dir() -> Path:
    return Path(__file__).resolve().parent


def _log(msg: str) -> None:
    """Append a diagnostic line to _internal/diag.log (best-effort)."""
    try:
        with open(_base_dir() / "diag.log", "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def log_diagnostic(msg: str) -> None:
    """Append a line to diag.log from another module.

    diag.log lives beside the database and this module already knows where that
    is, including the client's redirected cache folder - so anything else that
    needs to leave a trace comes through here rather than working the path out
    a second time.
    """
    _log(msg)


def _db_path() -> Path:
    return _base_dir() / DB_FILENAME


def _enc_path() -> Path:
    return _base_dir() / ENC_FILENAME


def _temp_db_path() -> Path:
    return _base_dir() / TEMP_DB_FILENAME


class _FlushingConnection(sqlite3.Connection):
    """Connection whose ``with`` block encrypts the DB to disk after a write.

    Used only in encrypted in-memory mode: when a transaction that changed rows
    commits successfully, the whole in-memory database is serialized, encrypted
    and atomically written - synchronous flush-per-commit, so an accidental
    shutdown loses at most the last action.
    """

    def __enter__(self):
        self._changes_baseline = self.total_changes
        return super().__enter__()

    def __exit__(self, exc_type, exc, tb):
        result = super().__exit__(exc_type, exc, tb)
        if exc_type is None and self.total_changes != self._changes_baseline:
            _schedule_flush()
        return result


def _connect() -> sqlite3.Connection:
    if _encrypted:
        if _temp_plain_path is not None:
            # Temp-file fallback: fresh file connection per call (its `with`
            # block flushes on a committed write, like the keeper).
            conn = sqlite3.connect(
                _temp_plain_path, factory=_FlushingConnection, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            return conn
        # The single persistent in-memory keeper.
        return _keeper
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def is_unlocked() -> bool:
    """True once :func:`unlock` has put the DB into encrypted in-memory mode."""
    return _encrypted


def unlock(dek: bytes, migrate_plaintext: bool = False) -> None:
    """Switch the database into encrypted in-memory mode using ``dek``.

    Loads the existing encrypted image (if any) into RAM via the backup API,
    ensures the schema exists, then writes an initial flush so the on-disk
    encrypted file is present.

    If no encrypted file exists yet and ``migrate_plaintext`` is set, the
    existing plaintext ``task_manager.db`` is imported first, so users upgrading
    from an unencrypted build keep all of their data.
    """
    global _dek
    _dek = dek
    _cleanup_temp_files(_temp_db_path())  # clear any plaintext left by a crash
    _log(f"unlock start (enc_exists={_enc_path().exists()}, migrate={migrate_plaintext})")
    try:
        _unlock_in_memory(dek, migrate_plaintext)
        _log("unlock mode = IN-MEMORY")
    except sqlite3.OperationalError as exc:
        # In-memory writes are unavailable in this environment (some packaged
        # builds cannot open the temp file SQLite needs). Fall back to a
        # decrypted temp file: file-based SQLite, reliable everywhere.
        _log(f"in-memory failed ({exc!r}); falling back to TEMP-FILE")
        _reset_state()
        _unlock_temp_file(dek, migrate_plaintext)
        _log("unlock mode = TEMP-FILE")
    _activate_account_key(dek)


def _activate_account_key(dek: bytes) -> None:
    """Fold account-password encryption into the vault DEK, re-encrypting any
    passwords still using the legacy standalone key file and removing it."""
    with _connect() as conn:
        rows = conn.execute("SELECT id, pwd_enc FROM accs").fetchall()
        updates, migrated = acc_crypto.fold_into_dek(
            dek, [(r["id"], r["pwd_enc"]) for r in rows]
        )
        for acc_id, blob in updates:
            conn.execute("UPDATE accs SET pwd_enc = ? WHERE id = ?", (blob, acc_id))
        if updates:
            conn.commit()
        nonempty = sum(1 for r in rows if r["pwd_enc"])
        _log(
            f"account-key fold: accounts={len(rows)} nonempty_pwd={nonempty} "
            f"reencrypted={len(updates)} all_readable={migrated}"
        )


def _unlock_in_memory(dek: bytes, migrate_plaintext: bool) -> None:
    global _keeper, _encrypted, _temp_plain_path
    keeper = sqlite3.connect(
        ":memory:", factory=_FlushingConnection, check_same_thread=False
    )
    keeper.row_factory = sqlite3.Row
    # Keep temp storage in RAM so in-memory writes never need a temp file.
    keeper.execute("PRAGMA temp_store=MEMORY")
    enc = _enc_path()
    if enc.exists():
        keeper.deserialize(secure_store.read_encrypted_file(enc, dek))
    elif migrate_plaintext and _db_path().exists():
        src = sqlite3.connect(_db_path())
        try:
            src.backup(keeper)
        finally:
            src.close()
    keeper.execute("PRAGMA journal_mode=MEMORY")
    # Probe an actual write to confirm this environment supports in-memory
    # writes; raises OperationalError to trigger the temp-file fallback if not.
    keeper.execute("CREATE TABLE IF NOT EXISTS _probe (x)")
    keeper.execute("DROP TABLE IF EXISTS _probe")
    _keeper = keeper
    _temp_plain_path = None
    _encrypted = True
    init_db()
    flush()  # write the encrypted file now, synchronously


def _unlock_temp_file(dek: bytes, migrate_plaintext: bool) -> None:
    global _keeper, _encrypted, _temp_plain_path
    _keeper = None
    tmp = _temp_db_path()
    _cleanup_temp_files(tmp)
    enc = _enc_path()
    if enc.exists():
        tmp.write_bytes(secure_store.read_encrypted_file(enc, dek))
    elif migrate_plaintext and _db_path().exists():
        shutil.copyfile(_db_path(), tmp)
    # Force rollback (DELETE) journaling, never WAL: the flush reads the main DB
    # file's bytes, so writes that went only to a -wal side file would be lost.
    # In DELETE mode every committed write lands in the main file.
    setup = sqlite3.connect(str(tmp))
    try:
        jm = setup.execute("PRAGMA journal_mode=DELETE").fetchone()
        _log(f"temp-file journal_mode now = {jm[0] if jm else '?'}")
        setup.commit()
    finally:
        setup.close()
    _temp_plain_path = str(tmp)
    _encrypted = True
    init_db()
    flush()  # write the encrypted file now, synchronously


def _cleanup_temp_files(tmp: Path) -> None:
    gced = False
    for suffix in ("", "-journal", "-wal", "-shm"):
        p = Path(str(tmp) + suffix)
        if not p.exists():
            continue
        try:
            p.unlink()
        except OSError:
            # Lingering sqlite file handles can block deletion on Windows; force
            # connections to be collected and retry once.
            if not gced:
                import gc

                gc.collect()
                gced = True
            try:
                p.unlink()
            except OSError:
                pass


def _reset_state() -> None:
    global _keeper, _encrypted, _temp_plain_path
    if _keeper is not None:
        try:
            _keeper.close()
        except sqlite3.Error:
            pass
    _keeper = None
    _encrypted = False
    _temp_plain_path = None


def _schedule_flush() -> None:
    """Mark the DB dirty and (re)arm a single coalesced background flush.

    Called after every committed write. Rapid writes reset the timer, so a
    burst collapses into one encrypt+write once the writes go quiet. Shutdown
    paths call :func:`flush` to force the pending data out synchronously.
    """
    global _flush_timer, _flush_dirty
    if not _encrypted or _dek is None:
        return
    with _flush_lock:
        _flush_dirty = True
        if _flush_timer is not None:
            _flush_timer.cancel()
        _flush_timer = threading.Timer(_FLUSH_DEBOUNCE_SEC, _flush_to_disk)
        _flush_timer.daemon = True
        _flush_timer.start()


def _flush_to_disk(force: bool = False) -> None:
    """Encrypt the current database image and atomically replace the file.

    ``force`` writes unconditionally (shutdown). Without it, the call is a
    no-op when nothing has changed since the last flush, so a stale debounce
    timer firing after a manual flush does not rewrite the file needlessly.
    """
    global _flush_timer, _flush_dirty
    if not _encrypted or _dek is None:
        return
    with _flush_lock:
        if not force and not _flush_dirty:
            return
        _flush_dirty = False
        _flush_timer = None
        try:
            if _temp_plain_path is not None:
                image = Path(_temp_plain_path).read_bytes()
                mode = "temp-file"
            elif _keeper is not None:
                image = _keeper.serialize()
                mode = "in-memory"
            else:
                _log("flush skipped: no keeper/temp")
                return
            secure_store.write_encrypted_file(_enc_path(), _dek, image, keep_backup=True)
            _log(f"flush OK ({mode}, {len(image)} bytes)")
        except Exception as exc:
            _flush_dirty = True  # keep the dirty mark so the data isn't dropped
            _log(f"flush FAILED: {exc!r}")
            raise


def flush() -> None:
    """Force an immediate synchronous flush (e.g. on app shutdown)."""
    global _flush_timer
    with _flush_lock:
        if _flush_timer is not None:
            _flush_timer.cancel()
            _flush_timer = None
    _flush_to_disk(force=True)


def close() -> None:
    """Flush, release the connection, and remove any plaintext temp file."""
    global _keeper, _encrypted, _temp_plain_path
    flush()  # cancel any pending debounce timer and write synchronously
    if _keeper is not None:
        _keeper.close()
        _keeper = None
    if _temp_plain_path is not None:
        _cleanup_temp_files(Path(_temp_plain_path))
        _temp_plain_path = None
    _encrypted = False
    acc_crypto.clear_key()


def current_dek() -> Optional[bytes]:
    """The active Data Encryption Key while unlocked (for re-wrapping in the
    Security settings), else ``None``."""
    return _dek


def plaintext_db_exists() -> bool:
    return _db_path().exists()


def archive_plaintext_db() -> Optional[Path]:
    """Rename the old plaintext DB aside after a successful migration so it is
    no longer used. It is kept (not deleted) so the user can remove it manually
    once they have confirmed the encrypted data is intact."""
    p = _db_path()
    if not p.exists():
        return None
    dest = p.with_name(p.name + ".pre-encryption")
    i = 1
    while dest.exists():
        dest = p.with_name(p.name + f".pre-encryption.{i}")
        i += 1
    try:
        os.replace(p, dest)
    except PermissionError:
        # A lingering sqlite handle can briefly lock the file on Windows; force
        # connections to be collected and retry once.
        import gc

        gc.collect()
        os.replace(p, dest)
    return dest


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'initial',
                description TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.commit()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stickies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL DEFAULT '',
                mail TEXT NOT NULL DEFAULT '',
                pwd_enc BLOB NOT NULL,
                other TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resumes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                fmt TEXT NOT NULL DEFAULT 'md',
                pdf BLOB,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        try:
            conn.execute("ALTER TABLE resumes ADD COLUMN pdf BLOB")
        except sqlite3.OperationalError:
            pass
        conn.commit()
        # Sections of a resume, entered by hand.
        #
        # Reading them out of the resume itself was tried and abandoned: a PDF
        # arrives as whatever its exporter felt like emitting - headings with no
        # markers, bullets dropped, titles absorbed into the line above - and a
        # guess that is right most of the time is still wrong on the resume you
        # are about to send. Typed once per resume, these are simply correct.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resume_sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resume_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_resume_sections_resume "
            "ON resume_sections (resume_id, position)"
        )
        conn.commit()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                column_key TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0,
                UNIQUE(day, column_key)
            )
            """
        )
        try:
            conn.execute(
                "ALTER TABLE daily_lists ADD COLUMN title TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE daily_lists ADD COLUMN always_shown INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass
        try:  # remembered per-column sort: '', 'newest', 'oldest', 'status'
            conn.execute(
                "ALTER TABLE daily_lists ADD COLUMN sort_mode TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass
        conn.execute(
            """
            UPDATE daily_lists SET title = CASE column_key
                WHEN 'todo' THEN 'To do'
                WHEN 'doing' THEN 'Doing'
                WHEN 'done' THEN 'Done'
                ELSE column_key
            END
            WHERE title = '' OR title IS NULL
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                list_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                body_hidden INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                updated_by TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (list_id) REFERENCES daily_lists(id) ON DELETE CASCADE
            )
            """
        )
        for column, ddl in (
            ("body_hidden", "ALTER TABLE daily_cards ADD COLUMN body_hidden INTEGER NOT NULL DEFAULT 0"),
            ("status", "ALTER TABLE daily_cards ADD COLUMN status TEXT NOT NULL DEFAULT ''"),
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        conn.commit()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                link TEXT NOT NULL DEFAULT '',
                start_at TEXT NOT NULL,
                alert_lead_minutes INTEGER NOT NULL DEFAULT 15,
                alert_interval_minutes INTEGER NOT NULL DEFAULT 5,
                alerts_enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                updated_by TEXT NOT NULL DEFAULT ''
            )
            """
        )
        try:  # migrate DBs created before the status column existed
            conn.execute("ALTER TABLE meetings ADD COLUMN status TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        try:  # link a meeting to the daily job card it belongs to (nullable)
            conn.execute("ALTER TABLE meetings ADD COLUMN linked_card_id INTEGER")
        except sqlite3.OperationalError:
            pass

        # --- team columns (server/client mode) ---------------------------------
        # Shared records record who created/last-updated them; private records
        # record who owns them; resumes record who uploaded them. All default to
        # '' so existing single-user data migrates untouched (a one-time backfill
        # attributes it to the PM once that account exists).
        for ddl in (
            "ALTER TABLE daily_lists ADD COLUMN created_by TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE daily_lists ADD COLUMN updated_by TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE daily_cards ADD COLUMN created_by TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE daily_cards ADD COLUMN updated_by TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE meetings ADD COLUMN created_by TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE meetings ADD COLUMN updated_by TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE accs ADD COLUMN owner_user_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE stickies ADD COLUMN owner_user_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE resumes ADD COLUMN uploaded_by TEXT NOT NULL DEFAULT ''",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already present

        # Team accounts. Only ever populated in server mode; harmless otherwise.
        # Passwords are scrypt hashes - never recoverable, even by the PM.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'dev',
                pw_hash TEXT NOT NULL DEFAULT '',
                pw_salt TEXT NOT NULL DEFAULT '',
                allowed_ips TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                recovery_hash TEXT NOT NULL DEFAULT '',
                recovery_salt TEXT NOT NULL DEFAULT '',
                recovery_expires_at TEXT NOT NULL DEFAULT '',
                migrated_at TEXT NOT NULL DEFAULT '',
                last_seen_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        # Indexes on the columns we filter/join on. Without these every board
        # render and cross-day lookup is a full table scan that grows with the
        # total amount of accumulated data.
        for index_ddl in (
            "CREATE INDEX IF NOT EXISTS idx_daily_cards_list_id ON daily_cards(list_id)",
            "CREATE INDEX IF NOT EXISTS idx_daily_lists_day ON daily_lists(day)",
            "CREATE INDEX IF NOT EXISTS idx_daily_lists_column_key ON daily_lists(column_key)",
            "CREATE INDEX IF NOT EXISTS idx_daily_lists_always_shown ON daily_lists(always_shown)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
            "CREATE INDEX IF NOT EXISTS idx_meetings_start_at ON meetings(start_at)",
        ):
            conn.execute(index_ddl)
        conn.commit()


@dataclass
class TaskRow:
    id: int
    link: str
    created_at: str
    status: str
    description: str


ALLOWED_STATUSES = (
    "initial",
    "invited or contacted",
    "closed",
    "created",
)


def normalize_link(url: str) -> str:
    return " ".join(url.strip().split())


def link_exists(link: str) -> bool:
    n = normalize_link(link)
    if not n:
        return False
    with _connect() as conn:
        cur = conn.execute(
            "SELECT 1 FROM tasks WHERE link = ? COLLATE NOCASE LIMIT 1",
            (n,),
        )
        return cur.fetchone() is not None


def add_task(link: str, status: str = "initial", description: str = "") -> int:
    n = normalize_link(link)
    if not n:
        raise ValueError("Link is empty")
    if status not in ALLOWED_STATUSES:
        raise ValueError("Invalid status")
    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks (link, created_at, status, description)
            VALUES (?, ?, ?, ?)
            """,
            (n, created, status, description or ""),
        )
        conn.commit()
        cur = conn.execute("SELECT last_insert_rowid()")
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("Failed to read new task id")
        return int(row[0])


def list_tasks() -> list[TaskRow]:
    with _connect() as conn:
        cur = conn.execute(
            "SELECT id, link, created_at, status, description FROM tasks ORDER BY id DESC"
        )
        return [TaskRow(**dict(r)) for r in cur.fetchall()]


def delete_task(task_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()


def update_task(task_id: int, status: str, description: str) -> None:
    if status not in ALLOWED_STATUSES:
        raise ValueError("Invalid status")
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET status = ?, description = ? WHERE id = ?",
            (status, description or "", task_id),
        )
        conn.commit()


def get_task(task_id: int) -> Optional[TaskRow]:
    with _connect() as conn:
        cur = conn.execute(
            "SELECT id, link, created_at, status, description FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return TaskRow(**dict(row))


@dataclass
class StickyRow:
    id: int
    title: str
    body: str
    created_at: str
    updated_at: str


def _now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _owner_clause(owner: str) -> tuple[str, tuple]:
    """SQL fragment restricting a private table to one person's rows.

    An empty ``owner`` means no restriction, which is what the single-user app
    wants - there is only one person, and their rows were written before anyone
    had a name. In team mode the caller always passes a name, so one dev can
    never read or change another's.
    """
    who = (owner or "").strip()
    if not who:
        return "", ()
    return " AND owner_user_id = ?", (who,)


def list_stickies(owner: str = "") -> list[StickyRow]:
    clause, args = _owner_clause(owner)
    with _connect() as conn:
        cur = conn.execute(
            f"""
            SELECT id, title, body, created_at, updated_at
            FROM stickies
            WHERE 1 = 1{clause}
            ORDER BY id ASC
            """,
            args,
        )
        return [StickyRow(**dict(r)) for r in cur.fetchall()]


def get_sticky(sticky_id: int, owner: str = "") -> Optional[StickyRow]:
    clause, args = _owner_clause(owner)
    with _connect() as conn:
        cur = conn.execute(
            f"""
            SELECT id, title, body, created_at, updated_at
            FROM stickies WHERE id = ?{clause}
            """,
            (sticky_id, *args),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return StickyRow(**dict(row))


def add_sticky(title: str = "", body: str = "", owner: str = "") -> int:
    ts = _now_ts()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO stickies (title, body, created_at, updated_at, owner_user_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (title or "", body or "", ts, ts, (owner or "").strip()),
        )
        conn.commit()
        cur = conn.execute("SELECT last_insert_rowid()")
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("Failed to read new sticky id")
        return int(row[0])


def update_sticky(sticky_id: int, title: str, body: str, owner: str = "") -> bool:
    """Update a note. False when ``owner`` does not own it, so nothing changed."""
    ts = _now_ts()
    clause, args = _owner_clause(owner)
    with _connect() as conn:
        cur = conn.execute(
            f"""
            UPDATE stickies SET title = ?, body = ?, updated_at = ?
            WHERE id = ?{clause}
            """,
            (title or "", body or "", ts, sticky_id, *args),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_sticky(sticky_id: int, owner: str = "") -> bool:
    """Delete a note. False when ``owner`` does not own it, so nothing changed."""
    clause, args = _owner_clause(owner)
    with _connect() as conn:
        cur = conn.execute(
            f"DELETE FROM stickies WHERE id = ?{clause}", (sticky_id, *args)
        )
        conn.commit()
        return cur.rowcount > 0


@dataclass
class AccRow:
    id: int
    url: str
    mail: str
    pwd: str
    other: str
    created_at: str
    updated_at: str


def list_accs(owner: str = "") -> list[AccRow]:
    clause, args = _owner_clause(owner)
    with _connect() as conn:
        cur = conn.execute(
            f"""
            SELECT id, url, mail, pwd_enc, other, created_at, updated_at
            FROM accs
            WHERE 1 = 1{clause}
            ORDER BY url, id ASC
            """,
            args,
        )
        out: list[AccRow] = []
        for r in cur.fetchall():
            d = dict(r)
            d["pwd"] = acc_crypto.decrypt_password(d.pop("pwd_enc"))
            out.append(AccRow(**d))
        return out


def get_acc(acc_id: int, owner: str = "") -> Optional[AccRow]:
    clause, args = _owner_clause(owner)
    with _connect() as conn:
        cur = conn.execute(
            f"""
            SELECT id, url, mail, pwd_enc, other, created_at, updated_at
            FROM accs WHERE id = ?{clause}
            """,
            (acc_id, *args),
        )
        row = cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        d["pwd"] = acc_crypto.decrypt_password(d.pop("pwd_enc"))
        return AccRow(**d)


def add_acc(url: str = "", mail: str = "", pwd: str = "", other: str = "",
            owner: str = "") -> int:
    ts = _now_ts()
    blob = acc_crypto.encrypt_password(pwd or "")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO accs (url, mail, pwd_enc, other, created_at, updated_at,
                              owner_user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (url or "", mail or "", blob, other or "", ts, ts, (owner or "").strip()),
        )
        conn.commit()
        cur = conn.execute("SELECT last_insert_rowid()")
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("Failed to read new acc id")
        return int(row[0])


def update_acc(
    acc_id: int,
    url: str,
    mail: str,
    pwd: str | None,
    other: str,
    owner: str = "",
) -> bool:
    """Update an account. False when ``owner`` does not own it."""
    ts = _now_ts()
    clause, args = _owner_clause(owner)
    with _connect() as conn:
        if pwd is None:
            cur = conn.execute(
                f"""
                UPDATE accs SET url = ?, mail = ?, other = ?, updated_at = ?
                WHERE id = ?{clause}
                """,
                (url or "", mail or "", other or "", ts, acc_id, *args),
            )
        else:
            blob = acc_crypto.encrypt_password(pwd)
            cur = conn.execute(
                f"""
                UPDATE accs SET url = ?, mail = ?, pwd_enc = ?, other = ?, updated_at = ?
                WHERE id = ?{clause}
                """,
                (url or "", mail or "", blob, other or "", ts, acc_id, *args),
            )
        conn.commit()
        return cur.rowcount > 0


def delete_acc(acc_id: int, owner: str = "") -> bool:
    """Delete an account. False when ``owner`` does not own it."""
    clause, args = _owner_clause(owner)
    with _connect() as conn:
        cur = conn.execute(f"DELETE FROM accs WHERE id = ?{clause}", (acc_id, *args))
        conn.commit()
        return cur.rowcount > 0


# --- Resumes (for AI match scoring / application answers) -------------------


@dataclass
class ResumeRow:
    id: int
    name: str
    content: str
    fmt: str  # "md" | "html" | "pdf"
    created_at: str
    updated_at: str
    pdf: Optional[bytes] = None  # populated only by get_resume()

    @property
    def is_pdf(self) -> bool:
        return self.fmt == "pdf"


def _parse_resume_row(r: sqlite3.Row) -> "ResumeRow":
    d = dict(r)
    pdf = d.get("pdf")
    return ResumeRow(
        id=int(d["id"]),
        name=str(d["name"]),
        content=str(d["content"]),
        fmt=str(d["fmt"] or "md"),
        created_at=str(d["created_at"]),
        updated_at=str(d["updated_at"]),
        pdf=bytes(pdf) if pdf is not None else None,
    )


def list_resumes() -> list[ResumeRow]:
    """Resumes (metadata only — no pdf bytes), most-recently-updated first."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT id, name, content, fmt, created_at, updated_at "
            "FROM resumes ORDER BY updated_at DESC, id DESC"
        )
        return [_parse_resume_row(r) for r in cur.fetchall()]


def get_resume(resume_id: int) -> Optional[ResumeRow]:
    """Full resume including the pdf blob when present."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT id, name, content, fmt, pdf, created_at, updated_at "
            "FROM resumes WHERE id = ?",
            (resume_id,),
        )
        row = cur.fetchone()
        return _parse_resume_row(row) if row is not None else None


def _resume_writer_clause(actor: str, is_pm: bool) -> tuple[str, tuple]:
    """SQL fragment limiting a resume write to one this person may change.

    The library is shared reading: everyone sees every resume and anyone may add
    one. Changing an existing one is narrower - the PM may change any, a
    developer only the ones they uploaded, so a dev can neither edit nor delete
    what the PM published. An empty ``actor`` is the single-user app, where the
    question does not arise.
    """
    who = (actor or "").strip()
    if is_pm or not who:
        return "", ()
    # Resumes that predate authorship belong to the PM after the backfill, so an
    # empty uploaded_by is not a free-for-all.
    return " AND uploaded_by = ?", (who,)


def add_resume(name: str, content: str, fmt: str = "md", uploaded_by: str = "") -> int:
    ts = _now_ts()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO resumes (name, content, fmt, created_at, updated_at, uploaded_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name or "Untitled", content or "", fmt or "md", ts, ts,
             (uploaded_by or "").strip()),
        )
        conn.commit()
        row = conn.execute("SELECT last_insert_rowid()").fetchone()
        return int(row[0]) if row else 0


def add_resume_pdf(name: str, pdf_bytes: bytes, uploaded_by: str = "") -> int:
    ts = _now_ts()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO resumes (name, content, fmt, pdf, created_at, updated_at, "
            "uploaded_by) VALUES (?, '', 'pdf', ?, ?, ?, ?)",
            (name or "Untitled", sqlite3.Binary(pdf_bytes), ts, ts,
             (uploaded_by or "").strip()),
        )
        conn.commit()
        row = conn.execute("SELECT last_insert_rowid()").fetchone()
        return int(row[0]) if row else 0


def update_resume(resume_id: int, name: str, content: str, fmt: str = "md",
                  actor: str = "", is_pm: bool = False) -> bool:
    """Update the name / text content / format. Leaves the pdf blob untouched
    (so renaming a PDF resume keeps its bytes).

    False when this person may not change that resume.
    """
    ts = _now_ts()
    clause, args = _resume_writer_clause(actor, is_pm)
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE resumes SET name = ?, content = ?, fmt = ?, updated_at = ? "
            f"WHERE id = ?{clause}",
            (name or "Untitled", content or "", fmt or "md", ts, resume_id, *args),
        )
        conn.commit()
        return cur.rowcount > 0


def set_resume_pdf(resume_id: int, pdf_bytes: bytes, actor: str = "",
                   is_pm: bool = False) -> bool:
    """Replace a resume's PDF (and mark it as a PDF resume)."""
    ts = _now_ts()
    clause, args = _resume_writer_clause(actor, is_pm)
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE resumes SET fmt = 'pdf', content = '', pdf = ?, updated_at = ? "
            f"WHERE id = ?{clause}",
            (sqlite3.Binary(pdf_bytes), ts, resume_id, *args),
        )
        conn.commit()
        return cur.rowcount > 0


def touch_resume(resume_id: int) -> None:
    """Bump updated_at so a just-used resume sorts first next time."""
    with _connect() as conn:
        conn.execute(
            "UPDATE resumes SET updated_at = ? WHERE id = ?", (_now_ts(), resume_id)
        )
        conn.commit()


def resume_uploader(resume_id: int) -> str:
    """Who put this resume in the library ('' for ones that predate authorship)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT uploaded_by FROM resumes WHERE id = ?", (resume_id,)
        ).fetchone()
        return str(row[0] or "") if row is not None else ""


def delete_resume(resume_id: int, actor: str = "", is_pm: bool = False) -> bool:
    """Delete a resume. False when this person may not remove that one."""
    clause, args = _resume_writer_clause(actor, is_pm)
    with _connect() as conn:
        cur = conn.execute(f"DELETE FROM resumes WHERE id = ?{clause}", (resume_id, *args))
        removed = cur.rowcount > 0
        if removed:
            # The sections belong to the resume and outlive nothing.
            conn.execute(
                "DELETE FROM resume_sections WHERE resume_id = ?", (resume_id,)
            )
        conn.commit()
        return removed


# --- resume sections (entered by hand) --------------------------------------


@dataclass
class ResumeSectionRow:
    id: int
    resume_id: int
    title: str
    content: str
    position: int


def _parse_resume_section(r: sqlite3.Row) -> "ResumeSectionRow":
    return ResumeSectionRow(
        id=int(r["id"]),
        resume_id=int(r["resume_id"]),
        title=r["title"] or "",
        content=r["content"] or "",
        position=int(r["position"] or 0),
    )


def list_resume_sections(resume_id: int) -> list[ResumeSectionRow]:
    """A resume's sections in the order they were arranged."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT id, resume_id, title, content, position FROM resume_sections "
            "WHERE resume_id = ? ORDER BY position ASC, id ASC",
            (resume_id,),
        )
        return [_parse_resume_section(r) for r in cur.fetchall()]


def add_resume_section(resume_id: int, title: str, content: str) -> int:
    """Append a section to a resume. Returns its id."""
    now = _now_ts()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM resume_sections "
            "WHERE resume_id = ?",
            (resume_id,),
        ).fetchone()
        pos = int(row[0]) if row else 0
        conn.execute(
            "INSERT INTO resume_sections "
            "(resume_id, title, content, position, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (resume_id, title or "", content or "", pos, now, now),
        )
        conn.commit()
        return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def update_resume_section(section_id: int, title: str, content: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE resume_sections SET title = ?, content = ?, updated_at = ? "
            "WHERE id = ?",
            (title or "", content or "", _now_ts(), section_id),
        )
        conn.commit()


def delete_resume_section(section_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM resume_sections WHERE id = ?", (section_id,))
        conn.commit()


def reorder_resume_sections(resume_id: int, ordered_ids: list[int]) -> None:
    """Persist the order the sections are shown in (position 0, 1, ...)."""
    if not ordered_ids:
        return
    with _connect() as conn:
        for pos, section_id in enumerate(ordered_ids):
            conn.execute(
                "UPDATE resume_sections SET position = ? WHERE id = ? AND resume_id = ?",
                (pos, section_id, resume_id),
            )
        conn.commit()


def resume_section_owner(section_id: int) -> Optional[int]:
    """The resume a section belongs to, so a write can be permission-checked."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT resume_id FROM resume_sections WHERE id = ?", (section_id,)
        ).fetchone()
        return int(row[0]) if row is not None else None




# --- Daily board (per calendar day, Kanban columns) -------------------------


@dataclass
class DailyListRow:
    id: int
    day: str
    column_key: str
    title: str
    position: int
    always_shown: bool = False
    created_by: str = ""
    updated_by: str = ""


def _parse_daily_list_row(row: sqlite3.Row) -> DailyListRow:
    d = dict(row)
    return DailyListRow(
        id=int(d["id"]),
        day=str(d["day"]),
        column_key=str(d["column_key"]),
        title=str(d["title"]),
        position=int(d["position"]),
        always_shown=bool(d.get("always_shown", 0)),
        created_by=str(d.get("created_by", "") or ""),
        updated_by=str(d.get("updated_by", "") or ""),
    )


@dataclass
class DailyCardRow:
    id: int
    list_id: int
    title: str
    body: str
    body_hidden: bool
    status: str
    position: int
    created_at: str
    updated_at: str
    created_by: str = ""
    updated_by: str = ""


def _parse_daily_card_row(row: sqlite3.Row) -> DailyCardRow:
    d = dict(row)
    return DailyCardRow(
        id=int(d["id"]),
        list_id=int(d["list_id"]),
        title=str(d["title"]),
        body=str(d["body"]),
        body_hidden=bool(d.get("body_hidden", 0)),
        status=str(d.get("status", "") or ""),
        position=int(d["position"]),
        created_at=str(d["created_at"]),
        updated_at=str(d["updated_at"]),
        created_by=str(d.get("created_by", "") or ""),
        updated_by=str(d.get("updated_by", "") or ""),
    )


def ensure_daily_job_bids_list() -> int:
    """Ensure the special Job bids column exists and is visible on every board day."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id FROM daily_lists
            WHERE column_key = 'job_bids'
            ORDER BY always_shown DESC, id ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT id FROM daily_lists
                WHERE lower(title) = lower('Job bids')
                ORDER BY always_shown DESC, id ASC
                LIMIT 1
                """
            ).fetchone()
        if row is not None:
            list_id = int(row[0])
            conn.execute(
                """
                UPDATE daily_lists
                SET title = 'Job bids', column_key = 'job_bids', always_shown = 1
                WHERE id = ?
                """,
                (list_id,),
            )
            conn.commit()
            return list_id

        pos_row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM daily_lists"
        ).fetchone()
        pos = int(pos_row[0]) if pos_row else 0
        conn.execute(
            """
            INSERT INTO daily_lists (day, column_key, title, position, always_shown)
            VALUES (?, 'job_bids', 'Job bids', ?, 1)
            """,
            ("__always__", pos),
        )
        conn.commit()
        new_row = conn.execute("SELECT last_insert_rowid()").fetchone()
        if new_row is None:
            raise RuntimeError("Failed to read Job bids list id")
        return int(new_row[0])


def find_daily_job_bids_list_id() -> Optional[int]:
    """The existing Job bids list id, or None — never creates one."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM daily_lists WHERE column_key = 'job_bids' "
            "ORDER BY always_shown DESC, id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id FROM daily_lists WHERE lower(title) = lower('Job bids') "
                "ORDER BY always_shown DESC, id ASC LIMIT 1"
            ).fetchone()
        return int(row[0]) if row is not None else None


def delete_daily_job_bids_list_if_empty() -> bool:
    """Remove the auto-created Job bids column when it holds no cards. Returns
    True if it was deleted. A Job bids column that has cards is left untouched."""
    lid = find_daily_job_bids_list_id()
    if lid is None:
        return False
    with _connect() as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM daily_cards WHERE list_id = ?", (lid,)
        ).fetchone()
        if cnt and int(cnt[0]) > 0:
            return False
        conn.execute("DELETE FROM daily_lists WHERE id = ?", (lid,))
        conn.commit()
        return True


def delete_empty_daily_lists() -> list[tuple[str, str]]:
    """Remove every sheet that holds no cards. Returns ``(day, title)`` of each.

    Cleanup for boards carrying years of auto-created To do / Doing / Done
    sheets from the Kanban version, which are pure clutter now that every sheet
    is on the tab bar. Only sheets with zero cards go, so no card can be lost by
    running this - and running it twice does nothing the second time.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT l.id, l.day, l.title
            FROM daily_lists l
            WHERE NOT EXISTS (SELECT 1 FROM daily_cards c WHERE c.list_id = l.id)
            ORDER BY l.day, l.title
            """
        ).fetchall()
        removed = [(str(r[1]), str(r[2] or "")) for r in rows]
        if rows:
            conn.executemany(
                "DELETE FROM daily_lists WHERE id = ?", [(int(r[0]),) for r in rows]
            )
            conn.commit()
        return removed


def list_daily_lists_for_day(day: str) -> list[DailyListRow]:
    """Sheets for a calendar day, ordered by position.

    Days start empty — sheets are created only when the user adds one (via the
    board's "+ Sheet"). No To do/Doing/Done columns are auto-created anymore.
    """
    day = (day or "").strip()
    if not day:
        raise ValueError("day is empty")
    with _connect() as conn:
        cur = conn.execute(
            """
            SELECT id, day, column_key, title, position, always_shown, created_by, updated_by
            FROM daily_lists
            WHERE day = ?
            ORDER BY position ASC, id ASC
            """,
            (day,),
        )
        return [_parse_daily_list_row(r) for r in cur.fetchall()]


def list_daily_lists_for_board(day: str) -> list[DailyListRow]:
    """Sheets for the board: this calendar day's sheets plus any always-shown
    sheets, ordered by stored ``position`` so a dragged/reordered sheet order is
    preserved (always-shown sheets are no longer force-sorted to the front)."""
    day = (day or "").strip()
    if not day:
        raise ValueError("day is empty")
    day_lists = list_daily_lists_for_day(day)
    with _connect() as conn:
        cur = conn.execute(
            """
            SELECT id, day, column_key, title, position, always_shown, created_by, updated_by
            FROM daily_lists
            WHERE always_shown = 1
            """
        )
        always_rows = [_parse_daily_list_row(r) for r in cur.fetchall()]
    by_id: dict[int, DailyListRow] = {r.id: r for r in day_lists}
    for r in always_rows:
        by_id.setdefault(r.id, r)
    return sorted(by_id.values(), key=lambda r: (r.position, r.id))


def add_daily_list(day: str, title: str = "New column", author: str = "") -> int:
    day = (day or "").strip()
    if not day:
        raise ValueError("day is empty")
    key = f"col_{uuid.uuid4().hex[:10]}"
    with _connect() as conn:
        # Position is global, not per-day: the board shows every sheet at once
        # (spreadsheet-style). A per-day max used to collide across days and
        # left the board unable to hold a dragged order - see
        # list_daily_lists_ordered.
        #
        # One below the current minimum, so a new sheet lands FIRST in the tab
        # bar rather than last. A sheet is made to be worked in straight away,
        # and appending put it off the right-hand edge of a long tab bar, where
        # it had to be scrolled to. Positions may go negative; only their order
        # is ever read, and the first drag renumbers everything from zero.
        row = conn.execute(
            "SELECT COALESCE(MIN(position), 0) - 1 FROM daily_lists"
        ).fetchone()
        pos = int(row[0]) if row else 0
        conn.execute(
            """
            INSERT INTO daily_lists
                (day, column_key, title, position, created_by, updated_by)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (day, key, title or "New column", pos, author or "", author or ""),
        )
        conn.commit()
        cur = conn.execute("SELECT last_insert_rowid()")
        r = cur.fetchone()
        if r is None:
            raise RuntimeError("Failed to read new daily list id")
        return int(r[0])


def _creator_write_clause(actor: str, is_pm: bool) -> tuple[str, tuple]:
    """SQL fragment limiting a shared-board write to rows this person may change.

    The daily board and meetings are shared to *read* - everyone sees the whole
    team's applications and interviews. Changing them is narrower: the PM may
    change anything, a developer only what they created. So a dev cannot delete
    or rewrite the PM's rows, or another dev's.

    An empty ``actor`` is the single-user app, where the question does not arise.
    Rows that predate authorship have an empty created_by and belong to the PM
    after the backfill, so they are not left editable by everyone.
    """
    who = (actor or "").strip()
    if is_pm or not who:
        return "", ()
    return " AND created_by = ?", (who,)


def update_daily_list(list_id: int, title: str, author: str = "",
                      is_pm: bool = False) -> bool:
    """Rename a sheet. False when this person did not create it."""
    clause, args = _creator_write_clause(author, is_pm)
    with _connect() as conn:
        if author:
            cur = conn.execute(
                f"UPDATE daily_lists SET title = ?, updated_by = ? WHERE id = ?{clause}",
                (title or "", author, list_id, *args),
            )
        else:
            cur = conn.execute(
                "UPDATE daily_lists SET title = ? WHERE id = ?",
                (title or "", list_id),
            )
        conn.commit()
        return cur.rowcount > 0


def set_daily_list_always_shown(list_id: int, always_shown: bool) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE daily_lists SET always_shown = ? WHERE id = ?",
            (1 if always_shown else 0, list_id),
        )
        conn.commit()


def delete_daily_list(list_id: int, author: str = "", is_pm: bool = False) -> bool:
    """Delete a sheet and everything on it. False when not allowed.

    Deleting a sheet takes its cards with it, so a developer needs more than
    having created the sheet: every card on it must be theirs too. Otherwise
    making a sheet would be a way to delete other people's applications.
    """
    who = (author or "").strip()
    restricted = bool(who) and not is_pm
    with _connect() as conn:
        if restricted:
            owner = conn.execute(
                "SELECT created_by FROM daily_lists WHERE id = ?", (list_id,)
            ).fetchone()
            if owner is None or (owner[0] or "") != who:
                return False
            foreign = conn.execute(
                "SELECT COUNT(*) FROM daily_cards WHERE list_id = ? "
                "AND COALESCE(created_by, '') <> ?",
                (list_id, who),
            ).fetchone()
            if foreign and int(foreign[0]) > 0:
                return False
        conn.execute("DELETE FROM daily_cards WHERE list_id = ?", (list_id,))
        cur = conn.execute("DELETE FROM daily_lists WHERE id = ?", (list_id,))
        conn.commit()
        return cur.rowcount > 0


def reorder_daily_lists(day: str, ordered_list_ids: list[int]) -> None:
    """Persist visible column order on the board (position 0, 1, …)."""
    del day  # board order is stored per list id
    if not ordered_list_ids:
        return
    with _connect() as conn:
        for pos, list_id in enumerate(ordered_list_ids):
            conn.execute(
                "UPDATE daily_lists SET position = ? WHERE id = ?",
                (pos, list_id),
            )
        conn.commit()


def get_daily_list(list_id: int) -> Optional[DailyListRow]:
    with _connect() as conn:
        cur = conn.execute(
            """
            SELECT id, day, column_key, title, position, always_shown, created_by, updated_by
            FROM daily_lists WHERE id = ?
            """,
            (list_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _parse_daily_list_row(row)


def list_daily_cards_for_day(day: str) -> list[DailyCardRow]:
    lists = list_daily_lists_for_board(day)
    if not lists:
        return []
    placeholders = ",".join("?" * len(lists))
    ids = [lst.id for lst in lists]
    with _connect() as conn:
        cur = conn.execute(
            f"""
            SELECT c.id, c.list_id, c.title, c.body, c.body_hidden, c.status, c.position,
                   c.created_at, c.updated_at, c.created_by, c.updated_by
            FROM daily_cards c
            WHERE c.list_id IN ({placeholders})
            ORDER BY c.list_id, c.position ASC, c.id ASC
            """,
            ids,
        )
        return [_parse_daily_card_row(r) for r in cur.fetchall()]


def get_daily_card(card_id: int) -> Optional[DailyCardRow]:
    with _connect() as conn:
        cur = conn.execute(
            """
            SELECT id, list_id, title, body, body_hidden, status, position, created_at, updated_at,
                   created_by, updated_by
            FROM daily_cards WHERE id = ?
            """,
            (card_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _parse_daily_card_row(row)


def add_daily_card(list_id: int, title: str = "", author: str = "") -> int:
    """Create a card. ``author`` records who made it (team mode); in
    single-user mode it stays blank."""
    ts = _now_ts()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM daily_cards WHERE list_id = ?",
            (list_id,),
        ).fetchone()
        pos = int(row[0]) if row else 0
        conn.execute(
            """
            INSERT INTO daily_cards
                (list_id, title, body, position, created_at, updated_at,
                 created_by, updated_by)
            VALUES (?, ?, '', ?, ?, ?, ?, ?)
            """,
            (list_id, title or "", pos, ts, ts, author or "", author or ""),
        )
        conn.commit()
        cur = conn.execute("SELECT last_insert_rowid()")
        r = cur.fetchone()
        if r is None:
            raise RuntimeError("Failed to read new daily card id")
        return int(r[0])


def update_daily_card(card_id: int, title: str, body: str, author: str = "",
                      is_pm: bool = False) -> bool:
    """Edit a card. False when this person did not apply to that job."""
    ts = _now_ts()
    clause, args = _creator_write_clause(author, is_pm)
    with _connect() as conn:
        if author:  # team mode: record who touched it last
            cur = conn.execute(
                f"""
                UPDATE daily_cards SET title = ?, body = ?, updated_at = ?, updated_by = ?
                WHERE id = ?{clause}
                """,
                (title or "", body or "", ts, author, card_id, *args),
            )
        else:  # single-user: leave any existing author untouched
            cur = conn.execute(
                """
                UPDATE daily_cards SET title = ?, body = ?, updated_at = ?
                WHERE id = ?
                """,
                (title or "", body or "", ts, card_id),
            )
        conn.commit()
        return cur.rowcount > 0


def update_daily_card_body_hidden(card_id: int, body_hidden: bool) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE daily_cards SET body_hidden = ?
            WHERE id = ?
            """,
            (1 if body_hidden else 0, card_id),
        )
        conn.commit()


def update_daily_card_status(card_id: int, status: str, author: str = "",
                             is_pm: bool = False) -> bool:
    """Set a card's status. False when this person did not apply to that job."""
    ts = _now_ts()
    clause, args = _creator_write_clause(author, is_pm)
    with _connect() as conn:
        if author:
            cur = conn.execute(
                f"""
                UPDATE daily_cards SET status = ?, updated_at = ?, updated_by = ?
                WHERE id = ?{clause}
                """,
                (status or "", ts, author, card_id, *args),
            )
        else:
            cur = conn.execute(
                """
                UPDATE daily_cards SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status or "", ts, card_id),
            )
        conn.commit()
        return cur.rowcount > 0


def move_daily_card(card_id: int, list_id: int, position: int | None = None,
                    author: str = "", is_pm: bool = False) -> bool:
    """Move a card to another sheet. False when it is not this person's."""
    ts = _now_ts()
    clause, args = _creator_write_clause(author, is_pm)
    with _connect() as conn:
        if position is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM daily_cards WHERE list_id = ?",
                (list_id,),
            ).fetchone()
            position = int(row[0]) if row else 0
        cur = conn.execute(
            f"""
            UPDATE daily_cards SET list_id = ?, position = ?, updated_at = ?
            WHERE id = ?{clause}
            """,
            (list_id, position, ts, card_id, *args),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_daily_card(card_id: int, author: str = "", is_pm: bool = False) -> bool:
    """Delete a card. False when this person did not apply to that job."""
    clause, args = _creator_write_clause(author, is_pm)
    with _connect() as conn:
        cur = conn.execute(
            f"DELETE FROM daily_cards WHERE id = ?{clause}", (card_id, *args)
        )
        conn.commit()
        return cur.rowcount > 0


def count_daily_cards_for_list(list_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM daily_cards WHERE list_id = ?",
            (list_id,),
        ).fetchone()
        return int(row[0]) if row else 0


# --- Meetings -------------------------------------------------------------

@dataclass
class MeetingRow:
    id: int
    title: str
    body: str
    link: str
    start_at: str  # 'YYYY-MM-DD HH:MM:SS'
    alert_lead_minutes: int  # how long before start_at alerts begin
    alert_interval_minutes: int  # how often alerts repeat within the lead window
    alerts_enabled: bool
    status: str
    created_at: str
    updated_at: str
    linked_card_id: Optional[int] = None  # the daily job card this meeting is for
    created_by: str = ""
    updated_by: str = ""


_MEETING_COLS = (
    "id, title, body, link, start_at, alert_lead_minutes, "
    "alert_interval_minutes, alerts_enabled, status, created_at, updated_at, "
    "linked_card_id, created_by, updated_by"
)


def _parse_meeting_row(row: sqlite3.Row) -> MeetingRow:
    d = dict(row)
    linked = d.get("linked_card_id")
    return MeetingRow(
        id=int(d["id"]),
        title=str(d["title"]),
        body=str(d["body"]),
        link=str(d["link"]),
        start_at=str(d["start_at"]),
        alert_lead_minutes=int(d["alert_lead_minutes"]),
        alert_interval_minutes=max(1, int(d["alert_interval_minutes"])),
        alerts_enabled=bool(d.get("alerts_enabled", 1)),
        status=str(d.get("status", "") or ""),
        created_at=str(d["created_at"]),
        updated_at=str(d["updated_at"]),
        linked_card_id=int(linked) if linked is not None else None,
        created_by=str(d.get("created_by", "") or ""),
        updated_by=str(d.get("updated_by", "") or ""),
    )


def list_meetings() -> list[MeetingRow]:
    with _connect() as conn:
        cur = conn.execute(
            f"SELECT {_MEETING_COLS} FROM meetings ORDER BY start_at ASC, id ASC"
        )
        return [_parse_meeting_row(r) for r in cur.fetchall()]


def list_meetings_between(start_iso: str, end_iso: str) -> list[MeetingRow]:
    """Meetings whose start_at falls in [start_iso, end_iso) - used by the
    month calendar so it only loads the visible range."""
    with _connect() as conn:
        cur = conn.execute(
            f"""
            SELECT {_MEETING_COLS} FROM meetings
            WHERE start_at >= ? AND start_at < ?
            ORDER BY start_at ASC, id ASC
            """,
            (start_iso, end_iso),
        )
        return [_parse_meeting_row(r) for r in cur.fetchall()]


def get_meeting(meeting_id: int) -> Optional[MeetingRow]:
    with _connect() as conn:
        cur = conn.execute(
            f"SELECT {_MEETING_COLS} FROM meetings WHERE id = ?", (meeting_id,)
        )
        row = cur.fetchone()
        return _parse_meeting_row(row) if row is not None else None


def add_meeting(
    title: str,
    body: str,
    link: str,
    start_at: str,
    alert_lead_minutes: int,
    alert_interval_minutes: int,
    alerts_enabled: bool = True,
    status: str = "",
    linked_card_id: Optional[int] = None,
    author: str = "",
) -> int:
    ts = _now_ts()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO meetings
                (title, body, link, start_at, alert_lead_minutes,
                 alert_interval_minutes, alerts_enabled, status, created_at,
                 updated_at, linked_card_id, created_by, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title or "",
                body or "",
                link or "",
                start_at,
                int(alert_lead_minutes),
                max(1, int(alert_interval_minutes)),
                1 if alerts_enabled else 0,
                status or "",
                ts,
                ts,
                int(linked_card_id) if linked_card_id is not None else None,
                author or "",
                author or "",
            ),
        )
        conn.commit()
        r = conn.execute("SELECT last_insert_rowid()").fetchone()
        if r is None:
            raise RuntimeError("Failed to read new meeting id")
        return int(r[0])


def update_meeting(
    meeting_id: int,
    title: str,
    body: str,
    link: str,
    start_at: str,
    alert_lead_minutes: int,
    alert_interval_minutes: int,
    alerts_enabled: bool,
    status: str = "",
    linked_card_id: Optional[int] = None,
    author: str = "",
    is_pm: bool = False,
) -> bool:
    """Edit a meeting. False when this person did not schedule it."""
    ts = _now_ts()
    author_sql = ", updated_by = ?" if author else ""
    clause, guard = _creator_write_clause(author, is_pm)
    with _connect() as conn:
        params = [
            title or "",
            body or "",
            link or "",
            start_at,
            int(alert_lead_minutes),
            max(1, int(alert_interval_minutes)),
            1 if alerts_enabled else 0,
            status or "",
            ts,
            int(linked_card_id) if linked_card_id is not None else None,
        ]
        if author:
            params.append(author)
        params.append(meeting_id)
        params.extend(guard)
        cur = conn.execute(
            f"""
            UPDATE meetings SET
                title = ?, body = ?, link = ?, start_at = ?,
                alert_lead_minutes = ?, alert_interval_minutes = ?,
                alerts_enabled = ?, status = ?, updated_at = ?,
                linked_card_id = ?{author_sql}
            WHERE id = ?{clause}
            """,
            params,
        )
        conn.commit()
        return cur.rowcount > 0


def set_meeting_linked_card(meeting_id: int, card_id: Optional[int],
                            author: str = "", is_pm: bool = False) -> bool:
    """Link (or, with ``None``, unlink) a meeting to a daily job card."""
    ts = _now_ts()
    clause, args = _creator_write_clause(author, is_pm)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE meetings SET linked_card_id = ?, updated_at = ? WHERE id = ?{clause}",
            (int(card_id) if card_id is not None else None, ts, meeting_id, *args),
        )
        conn.commit()
        return cur.rowcount > 0


def set_meeting_alerts_enabled(meeting_id: int, enabled: bool,
                               author: str = "", is_pm: bool = False) -> bool:
    """Pause (False) or resume (True) a single meeting's alarms."""
    ts = _now_ts()
    clause, args = _creator_write_clause(author, is_pm)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE meetings SET alerts_enabled = ?, updated_at = ? WHERE id = ?{clause}",
            (1 if enabled else 0, ts, meeting_id, *args),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_meeting(meeting_id: int, author: str = "", is_pm: bool = False) -> bool:
    """Delete a meeting. False when this person did not schedule it."""
    clause, args = _creator_write_clause(author, is_pm)
    with _connect() as conn:
        cur = conn.execute(
            f"DELETE FROM meetings WHERE id = ?{clause}", (meeting_id, *args)
        )
        conn.commit()
        return cur.rowcount > 0


def list_daily_cards_for_list(list_id: int) -> list[DailyCardRow]:
    with _connect() as conn:
        cur = conn.execute(
            """
            SELECT id, list_id, title, body, body_hidden, status, position, created_at, updated_at,
                   created_by, updated_by
            FROM daily_cards
            WHERE list_id = ?
            ORDER BY position ASC, id ASC
            """,
            (list_id,),
        )
        return [_parse_daily_card_row(r) for r in cur.fetchall()]


def reorder_daily_cards(list_id: int, ordered_card_ids: list[int]) -> None:
    ts = _now_ts()
    with _connect() as conn:
        for pos, cid in enumerate(ordered_card_ids):
            conn.execute(
                """
                UPDATE daily_cards SET position = ?, updated_at = ?
                WHERE id = ? AND list_id = ?
                """,
                (pos, ts, cid, list_id),
            )
        conn.commit()


def get_daily_list_sort_mode(list_id: int) -> str:
    """Remembered sort for a column: '' (manual), 'newest', 'oldest', 'status'."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT sort_mode FROM daily_lists WHERE id = ?", (list_id,)
        ).fetchone()
        return str(row[0]) if row and row[0] else ""


def set_daily_list_sort_mode(list_id: int, mode: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE daily_lists SET sort_mode = ? WHERE id = ?", (mode or "", list_id)
        )
        conn.commit()


def list_all_daily_lists() -> list[DailyListRow]:
    with _connect() as conn:
        cur = conn.execute(
            """
            SELECT id, day, column_key, title, position, always_shown, created_by, updated_by
            FROM daily_lists
            ORDER BY day ASC, position ASC, id ASC
            """
        )
        return [_parse_daily_list_row(r) for r in cur.fetchall()]


def list_daily_lists_ordered() -> list[DailyListRow]:
    """Every sheet in the board's tab order.

    Ordered by ``position`` first so a dragged reorder survives a refresh. This
    is the board read; ``list_all_daily_lists`` keeps its day-grouped order for
    exports and reports. Before a sheet has ever been dragged, positions can
    collide across days (older rows were numbered per-day), so day and id break
    ties into the same familiar order the board used to show - the first drag
    renumbers everything globally and the collisions are gone for good.
    """
    with _connect() as conn:
        cur = conn.execute(
            """
            SELECT id, day, column_key, title, position, always_shown, created_by, updated_by
            FROM daily_lists
            ORDER BY position ASC, day ASC, id ASC
            """
        )
        return [_parse_daily_list_row(r) for r in cur.fetchall()]


def list_all_daily_cards() -> list[DailyCardRow]:
    with _connect() as conn:
        cur = conn.execute(
            """
            SELECT id, list_id, title, body, body_hidden, status, position, created_at, updated_at,
                   created_by, updated_by
            FROM daily_cards
            ORDER BY list_id ASC, position ASC, id ASC
            """
        )
        return [_parse_daily_card_row(r) for r in cur.fetchall()]


def list_daily_card_summaries() -> list[DailyCardRow]:
    """Every card, but with an empty ``body`` — a lightweight board index.

    The board needs card bodies only for the sheet on screen (its
    Site/Company/Role/Link/Description columns are parsed from the body). Tab
    counts, the Creator filter and status/date grouping need only the metadata.
    Over the network the full-body list of ~2300 cards is several MB; this drops
    it roughly tenfold. Rows are ordinary ``DailyCardRow`` objects with
    ``body=""`` so nothing downstream needs a new type - callers that want the
    real body fetch the active sheet with ``list_daily_cards_for_list``.
    """
    with _connect() as conn:
        cur = conn.execute(
            """
            SELECT id, list_id, title, '' AS body, body_hidden, status, position,
                   created_at, updated_at, created_by, updated_by
            FROM daily_cards
            ORDER BY list_id ASC, position ASC, id ASC
            """
        )
        return [_parse_daily_card_row(r) for r in cur.fetchall()]


def job_bid_card_titles(prefix: str) -> tuple[int, list[str]]:
    """Aggregate across every column whose title begins with ``prefix``.

    Returns ``(matching_column_count, [card_title, ...])`` spanning all days, so
    callers can summarise job-site statistics over every "JobBid…" column at once.
    SQLite LIKE is case-insensitive for ASCII; the prefix is matched literally
    (``%`` / ``_`` in it are escaped)."""
    like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with _connect() as conn:
        col_rows = conn.execute(
            "SELECT id FROM daily_lists WHERE title LIKE ? ESCAPE '\\'",
            (like,),
        ).fetchall()
        col_ids = [int(r[0]) for r in col_rows]
        if not col_ids:
            return 0, []
        placeholders = ",".join("?" * len(col_ids))
        card_rows = conn.execute(
            f"SELECT title FROM daily_cards WHERE list_id IN ({placeholders})",
            col_ids,
        ).fetchall()
    return len(col_ids), [str(r[0] or "") for r in card_rows]


# --- JSON backup (portable, human-readable) ---------------------------------

BACKUP_FORMAT = "TaskManagerBackup"
BACKUP_VERSION = 3
BACKUP_VERSION_LEGACY = 1
BACKUP_VERSION_WITH_ACCOUNTS = 2


def build_backup_document() -> dict[str, object]:
    """Snapshot of all tasks, sticky notes, and accounts for JSON export."""
    tasks = [
        {
            "id": t.id,
            "link": t.link,
            "created_at": t.created_at,
            "status": t.status,
            "description": t.description,
        }
        for t in list_tasks()
    ]
    stickies = [
        {
            "id": s.id,
            "title": s.title,
            "body": s.body,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }
        for s in list_stickies()
    ]
    accounts = [
        {
            "id": a.id,
            "url": a.url,
            "mail": a.mail,
            "pwd": a.pwd,
            "other": a.other,
            "created_at": a.created_at,
            "updated_at": a.updated_at,
        }
        for a in list_accs()
    ]
    # created_by/updated_by ride along so a backup keeps who applied to what.
    # Older readers ignore unknown keys and older files simply lack them, so no
    # version bump is needed either way.
    daily_lists = [
        {
            "id": row.id,
            "day": row.day,
            "column_key": row.column_key,
            "title": row.title,
            "position": row.position,
            "always_shown": row.always_shown,
            "created_by": row.created_by,
            "updated_by": row.updated_by,
        }
        for row in list_all_daily_lists()
    ]
    daily_cards = [
        {
            "id": c.id,
            "list_id": c.list_id,
            "title": c.title,
            "body": c.body,
            "body_hidden": c.body_hidden,
            "status": c.status,
            "position": c.position,
            "created_at": c.created_at,
            "updated_at": c.updated_at,
            "created_by": c.created_by,
            "updated_by": c.updated_by,
        }
        for c in list_all_daily_cards()
    ]
    # Meetings were missing from backups entirely, so an export could not carry
    # the interview side of the process at all. They ride along under an
    # optional key rather than a new version number: older releases ignore keys
    # they do not know, and they never backed meetings up in the first place, so
    # nothing they used to restore is lost by reading one of these files.
    meetings = [
        {
            "id": m.id,
            "title": m.title,
            "body": m.body,
            "link": m.link,
            "start_at": m.start_at,
            "alert_lead_minutes": m.alert_lead_minutes,
            "alert_interval_minutes": m.alert_interval_minutes,
            "alerts_enabled": bool(m.alerts_enabled),
            "status": m.status,
            "created_at": m.created_at,
            "updated_at": m.updated_at,
            "linked_card_id": m.linked_card_id,
            "created_by": m.created_by,
            "updated_by": m.updated_by,
        }
        for m in list_meetings()
    ]
    return {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "meetings": meetings,
        "exported_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "tasks": tasks,
        "stickies": stickies,
        "accounts": accounts,
        "daily": {"lists": daily_lists, "cards": daily_cards},
    }


def parse_backup_payload(
    data: object,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    """``(tasks, stickies, accounts, daily_lists, daily_cards, meetings)``."""
    if not isinstance(data, dict):
        raise ValueError("The file is not a JSON object.")
    if data.get("format") != BACKUP_FORMAT:
        raise ValueError("This file is not a Task Manager backup (wrong format field).")
    ver = data.get("version")
    if ver not in (BACKUP_VERSION_LEGACY, BACKUP_VERSION_WITH_ACCOUNTS, BACKUP_VERSION):
        raise ValueError(
            f"Unsupported backup version: {ver!r} "
            f"(expected {BACKUP_VERSION_LEGACY}, {BACKUP_VERSION_WITH_ACCOUNTS}, or {BACKUP_VERSION})."
        )
    tasks = data.get("tasks")
    stickies = data.get("stickies")
    if not isinstance(tasks, list) or not isinstance(stickies, list):
        raise ValueError("Backup must contain JSON arrays tasks and stickies.")
    accounts_raw = data.get("accounts")
    if ver == BACKUP_VERSION_LEGACY:
        accounts = []
    elif accounts_raw is None:
        accounts = []
    elif isinstance(accounts_raw, list):
        accounts = [x for x in accounts_raw if isinstance(x, dict)]
    else:
        raise ValueError("Backup accounts must be a JSON array (version 2+).")
    daily_lists: list[dict[str, object]] = []
    daily_cards: list[dict[str, object]] = []
    if ver >= BACKUP_VERSION_WITH_ACCOUNTS:
        daily_raw = data.get("daily")
        if isinstance(daily_raw, dict):
            lists_raw = daily_raw.get("lists")
            cards_raw = daily_raw.get("cards")
            if lists_raw is not None:
                if not isinstance(lists_raw, list):
                    raise ValueError("Backup daily.lists must be a JSON array.")
                daily_lists = [x for x in lists_raw if isinstance(x, dict)]
            if cards_raw is not None:
                if not isinstance(cards_raw, list):
                    raise ValueError("Backup daily.cards must be a JSON array.")
                daily_cards = [x for x in cards_raw if isinstance(x, dict)]
    # Optional: files written before meetings were included simply lack the key.
    meetings_raw = data.get("meetings")
    if meetings_raw is None:
        meetings: list[dict[str, object]] = []
    elif isinstance(meetings_raw, list):
        meetings = [x for x in meetings_raw if isinstance(x, dict)]
    else:
        raise ValueError("Backup meetings must be a JSON array.")
    return tasks, stickies, accounts, daily_lists, daily_cards, meetings


def add_task_if_new_from_backup(
    link: str, created_at: str, status: str, description: str
) -> bool:
    """Insert a task if the normalized link is new. Returns False on duplicate or invalid row."""
    n = normalize_link(link or "")
    if not n:
        return False
    st = (status or "").strip() or "initial"
    if st not in ALLOWED_STATUSES:
        return False
    ca = (created_at or "").strip() or _now_ts()
    desc = description or ""
    with _connect() as conn:
        try:
            conn.execute(
                """
                INSERT INTO tasks (link, created_at, status, description)
                VALUES (?, ?, ?, ?)
                """,
                (n, ca, st, desc),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def add_sticky_from_backup(
    title: str, body: str, created_at: str, updated_at: str
) -> int:
    ca = (created_at or "").strip() or _now_ts()
    ua = (updated_at or "").strip() or ca
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO stickies (title, body, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (title or "", body or "", ca, ua),
        )
        conn.commit()
        cur = conn.execute("SELECT last_insert_rowid()")
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("Failed to read new sticky id")
        return int(row[0])


def add_acc_from_backup(
    url: str,
    mail: str,
    pwd: str,
    other: str,
    created_at: str,
    updated_at: str,
) -> int:
    ca = (created_at or "").strip() or _now_ts()
    ua = (updated_at or "").strip() or ca
    blob = acc_crypto.encrypt_password(pwd or "")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO accs (url, mail, pwd_enc, other, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (url or "", mail or "", blob, other or "", ca, ua),
        )
        conn.commit()
        cur = conn.execute("SELECT last_insert_rowid()")
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("Failed to read new acc id")
        return int(row[0])


def _find_mergeable_list(conn: sqlite3.Connection, day: str, column_key: str):
    """The sheet already here that this imported one belongs in, or None.

    Sheets are identified by ``(day, column_key)`` - the table's own unique key.
    Job bids is the exception: there is deliberately only ever one of it, and an
    older install may hold it against a real date rather than ``__always__``, so
    it is matched on the key alone to avoid ending up with two.
    """
    if column_key == "job_bids":
        row = conn.execute(
            "SELECT id FROM daily_lists WHERE column_key = 'job_bids' "
            "ORDER BY always_shown DESC, id ASC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM daily_lists WHERE day = ? AND column_key = ?",
            (day, column_key),
        ).fetchone()
    return int(row[0]) if row is not None else None


def _import_daily_merge(
    list_dicts: list[dict[str, object]],
    card_dicts: list[dict[str, object]],
) -> tuple[int, int, int, dict[int, int]]:
    """Append the file's daily board to what is already stored.

    Returns ``(sheets_added, cards_added, cards_skipped, card_id_map)``. The map
    is old card id -> new one, so a meeting can find the job it was linked to
    after every id has been reassigned.

    A sheet already present is *reused* and the file's cards are added to it,
    rather than inserted a second time. `daily_lists` has UNIQUE(day,
    column_key), and every app creates the Job bids sheet for itself on first
    run - so importing any backup into a fresh app used to hit that constraint
    and roll back the whole daily import, losing every sheet and card while the
    tasks and notes from the same file went in.

    Cards whose sheet is genuinely absent are counted rather than dropped in
    silence, so an import that could not place everything says so.
    """
    list_id_map: dict[int, int] = {}
    lists_added = 0
    with _connect() as conn:
        for d in list_dicts:
            if not isinstance(d, dict):
                continue
            day = str(d.get("day", "") or "").strip()
            if not day:
                continue
            column_key = str(d.get("column_key", "") or "").strip()
            if not column_key:
                # Sheets from old versions predate column keys. Give it one so
                # the sheet - and everything in it - still imports.
                column_key = f"col_{uuid.uuid4().hex[:10]}"
            title = str(d.get("title", "") or column_key)
            pos = int(d.get("position", 0) or 0)
            always = 1 if d.get("always_shown") else 0
            new_id = _find_mergeable_list(conn, day, column_key)
            if new_id is None:
                conn.execute(
                    """
                    INSERT INTO daily_lists (day, column_key, title, position, always_shown)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (day, column_key, title, pos, always),
                )
                new_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                lists_added += 1
            old_id = d.get("id")
            try:
                list_id_map[int(old_id)] = new_id  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass
        cards_added = 0
        cards_skipped = 0
        card_id_map: dict[int, int] = {}
        for d in card_dicts:
            if not isinstance(d, dict):
                cards_skipped += 1
                continue
            try:
                old_lid = int(d.get("list_id"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                cards_skipped += 1
                continue
            new_lid = list_id_map.get(old_lid)
            if new_lid is None:
                cards_skipped += 1
                continue
            title = str(d.get("title", "") or "")
            body = str(d.get("body", "") or "")
            body_hidden = 1 if d.get("body_hidden") else 0
            status = str(d.get("status", "") or "")
            pos = int(d.get("position", 0) or 0)
            ca = str(d.get("created_at", "") or "").strip() or _now_ts()
            ua = str(d.get("updated_at", "") or "").strip() or ca
            cb = str(d.get("created_by", "") or "").strip()
            ub = str(d.get("updated_by", "") or "").strip()
            conn.execute(
                """
                INSERT INTO daily_cards (list_id, title, body, body_hidden, status,
                                         position, created_at, updated_at,
                                         created_by, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (new_lid, title, body, body_hidden, status, pos, ca, ua, cb, ub),
            )
            try:
                card_id_map[int(d.get("id"))] = int(  # type: ignore[arg-type]
                    conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                )
            except (TypeError, ValueError):
                pass          # a card with no id in the file; nothing can link to it
            cards_added += 1
        conn.commit()
    return lists_added, cards_added, cards_skipped, card_id_map


def _import_meetings(
    meeting_dicts: list[dict[str, object]],
    card_id_map: Optional[dict[int, int]] = None,
) -> int:
    """Append meetings from a backup, repointing each at its job application.

    ``card_id_map`` translates the file's card ids to the ones just inserted.
    Without it a meeting would keep an id that now belongs to a different job -
    worse than no link at all - so an unmappable link is dropped rather than
    guessed.
    """
    added = 0
    with _connect() as conn:
        for d in meeting_dicts:
            if not isinstance(d, dict):
                continue
            start_at = str(d.get("start_at", "") or "").strip()
            if not start_at:
                continue          # a meeting with no time cannot be shown anywhere
            ts = _now_ts()
            linked = d.get("linked_card_id")
            try:
                linked_i: Optional[int] = int(linked)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                linked_i = None
            if linked_i is not None and card_id_map is not None:
                linked_i = card_id_map.get(linked_i)
            conn.execute(
                """
                INSERT INTO meetings
                    (title, body, link, start_at, alert_lead_minutes,
                     alert_interval_minutes, alerts_enabled, status,
                     created_at, updated_at, linked_card_id, created_by, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(d.get("title", "") or ""),
                    str(d.get("body", "") or ""),
                    str(d.get("link", "") or ""),
                    start_at,
                    int(d.get("alert_lead_minutes", 15) or 0),
                    max(1, int(d.get("alert_interval_minutes", 5) or 1)),
                    1 if d.get("alerts_enabled", True) else 0,
                    str(d.get("status", "") or ""),
                    str(d.get("created_at", "") or "").strip() or ts,
                    str(d.get("updated_at", "") or "").strip() or ts,
                    linked_i,
                    str(d.get("created_by", "") or "").strip(),
                    str(d.get("updated_by", "") or "").strip(),
                ),
            )
            added += 1
        conn.commit()
    return added


def import_backup_merge(
    task_dicts: list[dict[str, object]],
    sticky_dicts: list[dict[str, object]],
    account_dicts: list[dict[str, object]],
    daily_list_dicts: list[dict[str, object]],
    daily_card_dicts: list[dict[str, object]],
    meeting_dicts: Optional[list[dict[str, object]]] = None,
) -> tuple[int, int, int, int, int, int, int, int]:
    """Add new tasks by link; append stickies, accounts, daily board and meetings.

    Returns ``(tasks_added, tasks_skipped, stickies_added, accounts_added,
    daily_lists_added, daily_cards_added, daily_cards_skipped, meetings_added)``.
    """
    tasks_added = 0
    tasks_skipped = 0
    for d in task_dicts:
        if not isinstance(d, dict):
            tasks_skipped += 1
            continue
        ok = add_task_if_new_from_backup(
            str(d.get("link", "")),
            str(d.get("created_at", "")),
            str(d.get("status", "")),
            str(d.get("description", "")),
        )
        if ok:
            tasks_added += 1
        else:
            tasks_skipped += 1
    stickies_added = 0
    for d in sticky_dicts:
        if not isinstance(d, dict):
            continue
        add_sticky_from_backup(
            str(d.get("title", "")),
            str(d.get("body", "")),
            str(d.get("created_at", "")),
            str(d.get("updated_at", "")),
        )
        stickies_added += 1
    accounts_added = 0
    for d in account_dicts:
        if not isinstance(d, dict):
            continue
        add_acc_from_backup(
            str(d.get("url", "")),
            str(d.get("mail", "")),
            str(d.get("pwd", "")),
            str(d.get("other", "")),
            str(d.get("created_at", "")),
            str(d.get("updated_at", "")),
        )
        accounts_added += 1
    (daily_lists_added, daily_cards_added, daily_cards_skipped,
     card_id_map) = _import_daily_merge(daily_list_dicts, daily_card_dicts)
    # After the cards, so meetings can be repointed at their new ids.
    meetings_added = _import_meetings(meeting_dicts or [], card_id_map)
    return (
        tasks_added,
        tasks_skipped,
        stickies_added,
        accounts_added,
        daily_lists_added,
        daily_cards_added,
        daily_cards_skipped,
        meetings_added,
    )


def _sqlite_set_autoincrement_seq(conn: sqlite3.Connection, table: str) -> None:
    if table not in ("tasks", "stickies", "accs", "daily_lists", "daily_cards"):
        raise ValueError("invalid table")
    row = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()
    mx = row[0] if row else None
    if mx is None:
        conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
        return
    cur = conn.execute("SELECT 1 FROM sqlite_sequence WHERE name = ?", (table,))
    if cur.fetchone():
        conn.execute(
            "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
            (int(mx), table),
        )
    else:
        conn.execute(
            "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
            (table, int(mx)),
        )


def initialize_database() -> None:
    """Permanently delete ALL user data (tasks, sticky notes, accounts, and the
    whole daily board), leaving an empty database."""
    with _connect() as conn:
        conn.execute("DELETE FROM daily_cards")
        conn.execute("DELETE FROM daily_lists")
        conn.execute("DELETE FROM accs")
        conn.execute("DELETE FROM stickies")
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM resumes")
        conn.execute("DELETE FROM resume_sections")
        conn.commit()


def import_backup_replace(
    task_dicts: list[dict[str, object]],
    sticky_dicts: list[dict[str, object]],
    account_dicts: list[dict[str, object]],
    daily_list_dicts: list[dict[str, object]],
    daily_card_dicts: list[dict[str, object]],
    meeting_dicts: Optional[list[dict[str, object]]] = None,
) -> tuple[int, int, int, int, int, int]:
    """Replace all data with backup contents. Preserves ids when present in the file.

    Returns ``(tasks, stickies, accounts, daily_lists, daily_cards, meetings)``.
    """
    with _connect() as conn:
        conn.execute("DELETE FROM daily_cards")
        conn.execute("DELETE FROM daily_lists")
        conn.execute("DELETE FROM accs")
        conn.execute("DELETE FROM stickies")
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM meetings")
        task_count = 0
        for d in task_dicts:
            if not isinstance(d, dict):
                continue
            link = normalize_link(str(d.get("link", "")))
            if not link:
                continue
            st = str(d.get("status", "initial")).strip()
            if st not in ALLOWED_STATUSES:
                st = "initial"
            created_at = str(d.get("created_at", "") or "").strip() or _now_ts()
            desc = str(d.get("description", "") or "")
            tid = d.get("id")
            try:
                tid_i = int(tid)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                tid_i = None
            if tid_i is not None:
                conn.execute(
                    """
                    INSERT INTO tasks (id, link, created_at, status, description)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (tid_i, link, created_at, st, desc),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO tasks (link, created_at, status, description)
                    VALUES (?, ?, ?, ?)
                    """,
                    (link, created_at, st, desc),
                )
            task_count += 1
        sticky_count = 0
        for d in sticky_dicts:
            if not isinstance(d, dict):
                continue
            title = str(d.get("title", "") or "")
            body = str(d.get("body", "") or "")
            ca = str(d.get("created_at", "") or "").strip() or _now_ts()
            ua = str(d.get("updated_at", "") or "").strip() or ca
            sid = d.get("id")
            try:
                sid_i = int(sid)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                sid_i = None
            if sid_i is not None:
                conn.execute(
                    """
                    INSERT INTO stickies (id, title, body, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (sid_i, title, body, ca, ua),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO stickies (title, body, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (title, body, ca, ua),
                )
            sticky_count += 1
        acc_count = 0
        for d in account_dicts:
            if not isinstance(d, dict):
                continue
            url = str(d.get("url", "") or "")
            mail = str(d.get("mail", "") or "")
            pwd = str(d.get("pwd", "") or "")
            other = str(d.get("other", "") or "")
            ca = str(d.get("created_at", "") or "").strip() or _now_ts()
            ua = str(d.get("updated_at", "") or "").strip() or ca
            aid = d.get("id")
            try:
                aid_i = int(aid)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                aid_i = None
            blob = acc_crypto.encrypt_password(pwd)
            if aid_i is not None:
                conn.execute(
                    """
                    INSERT INTO accs (id, url, mail, pwd_enc, other, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (aid_i, url, mail, blob, other, ca, ua),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO accs (url, mail, pwd_enc, other, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (url, mail, blob, other, ca, ua),
                )
            acc_count += 1
        daily_list_count = 0
        for d in daily_list_dicts:
            if not isinstance(d, dict):
                continue
            day = str(d.get("day", "") or "").strip()
            column_key = str(d.get("column_key", "") or "").strip()
            if not day or not column_key:
                continue
            title = str(d.get("title", "") or column_key)
            pos = int(d.get("position", 0) or 0)
            always = 1 if d.get("always_shown") else 0
            lid = d.get("id")
            try:
                lid_i = int(lid)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                lid_i = None
            cb = str(d.get("created_by", "") or "").strip()
            ub = str(d.get("updated_by", "") or "").strip()
            if lid_i is not None:
                conn.execute(
                    """
                    INSERT INTO daily_lists (id, day, column_key, title, position,
                                             always_shown, created_by, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (lid_i, day, column_key, title, pos, always, cb, ub),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO daily_lists (day, column_key, title, position,
                                             always_shown, created_by, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (day, column_key, title, pos, always, cb, ub),
                )
            daily_list_count += 1
        daily_card_count = 0
        for d in daily_card_dicts:
            if not isinstance(d, dict):
                continue
            try:
                list_id = int(d.get("list_id"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            title = str(d.get("title", "") or "")
            body = str(d.get("body", "") or "")
            body_hidden = 1 if d.get("body_hidden") else 0
            status = str(d.get("status", "") or "")
            pos = int(d.get("position", 0) or 0)
            ca = str(d.get("created_at", "") or "").strip() or _now_ts()
            ua = str(d.get("updated_at", "") or "").strip() or ca
            cid = d.get("id")
            try:
                cid_i = int(cid)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                cid_i = None
            cb = str(d.get("created_by", "") or "").strip()
            ub = str(d.get("updated_by", "") or "").strip()
            if cid_i is not None:
                conn.execute(
                    """
                    INSERT INTO daily_cards (id, list_id, title, body, body_hidden,
                                             status, position, created_at, updated_at,
                                             created_by, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (cid_i, list_id, title, body, body_hidden, status, pos, ca, ua, cb, ub),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO daily_cards (list_id, title, body, body_hidden, status,
                                             position, created_at, updated_at,
                                             created_by, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (list_id, title, body, body_hidden, status, pos, ca, ua, cb, ub),
                )
            daily_card_count += 1
        _sqlite_set_autoincrement_seq(conn, "tasks")
        _sqlite_set_autoincrement_seq(conn, "stickies")
        _sqlite_set_autoincrement_seq(conn, "accs")
        _sqlite_set_autoincrement_seq(conn, "daily_lists")
        _sqlite_set_autoincrement_seq(conn, "daily_cards")
        conn.commit()
    # Card ids were preserved above, so each meeting's linked_card_id still
    # points at the same job - no remapping needed on this path.
    meeting_count = _import_meetings(meeting_dicts or [], None)
    return (task_count, sticky_count, acc_count, daily_list_count,
            daily_card_count, meeting_count)


# ---------------------------------------------------------------- sync users
#
# Team mode: the server owns one row per person who may connect. This layer is
# storage only - password hashing, IP policy and recovery codes live in
# sync_users.py so the rules stay in one place and are testable without a DB.

ROLE_PM = "pm"
ROLE_DEV = "dev"

_SYNC_USER_COLS = (
    "id, username, display_name, role, pw_hash, pw_salt, allowed_ips, enabled, "
    "recovery_hash, recovery_salt, recovery_expires_at, migrated_at, "
    "last_seen_at, created_at, updated_at"
)


@dataclass
class SyncUserRow:
    id: int
    username: str
    display_name: str
    role: str
    pw_hash: str
    pw_salt: str
    allowed_ips: str
    enabled: bool
    recovery_hash: str
    recovery_salt: str
    recovery_expires_at: str
    migrated_at: str
    last_seen_at: str
    created_at: str
    updated_at: str

    @property
    def label(self) -> str:
        """What to show in the UI and stamp on records."""
        return self.display_name.strip() or self.username

    @property
    def is_pm(self) -> bool:
        return self.role == ROLE_PM


def _parse_sync_user_row(r) -> SyncUserRow:
    return SyncUserRow(
        id=int(r[0]),
        username=r[1] or "",
        display_name=r[2] or "",
        role=r[3] or ROLE_DEV,
        pw_hash=r[4] or "",
        pw_salt=r[5] or "",
        allowed_ips=r[6] or "",
        enabled=bool(r[7]),
        recovery_hash=r[8] or "",
        recovery_salt=r[9] or "",
        recovery_expires_at=r[10] or "",
        migrated_at=r[11] or "",
        last_seen_at=r[12] or "",
        created_at=r[13] or "",
        updated_at=r[14] or "",
    )


def list_sync_users() -> list[SyncUserRow]:
    with _connect() as conn:
        cur = conn.execute(
            f"SELECT {_SYNC_USER_COLS} FROM sync_users "
            "ORDER BY role DESC, username COLLATE NOCASE ASC"
        )
        return [_parse_sync_user_row(r) for r in cur.fetchall()]


def get_sync_user(user_id: int) -> Optional[SyncUserRow]:
    with _connect() as conn:
        cur = conn.execute(
            f"SELECT {_SYNC_USER_COLS} FROM sync_users WHERE id = ?", (user_id,)
        )
        row = cur.fetchone()
        return _parse_sync_user_row(row) if row is not None else None


def get_sync_user_by_username(username: str) -> Optional[SyncUserRow]:
    """Case-insensitive lookup (the column is COLLATE NOCASE)."""
    with _connect() as conn:
        cur = conn.execute(
            f"SELECT {_SYNC_USER_COLS} FROM sync_users WHERE username = ?",
            ((username or "").strip(),),
        )
        row = cur.fetchone()
        return _parse_sync_user_row(row) if row is not None else None


def count_sync_users(role: str = "") -> int:
    with _connect() as conn:
        if role:
            row = conn.execute(
                "SELECT COUNT(*) FROM sync_users WHERE role = ?", (role,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM sync_users").fetchone()
        return int(row[0]) if row else 0


def insert_sync_user(
    username: str,
    display_name: str,
    role: str,
    pw_hash: str,
    pw_salt: str,
    allowed_ips: str = "",
    enabled: bool = True,
) -> int:
    """Create a user. Raises ValueError when the username is taken or blank."""
    name = (username or "").strip()
    if not name:
        raise ValueError("username is empty")
    ts = _now_ts()
    with _connect() as conn:
        taken = conn.execute(
            "SELECT 1 FROM sync_users WHERE username = ?", (name,)
        ).fetchone()
        if taken is not None:
            raise ValueError(f"username {name!r} is already taken")
        conn.execute(
            """
            INSERT INTO sync_users
                (username, display_name, role, pw_hash, pw_salt, allowed_ips,
                 enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, display_name or "", role or ROLE_DEV, pw_hash, pw_salt,
             allowed_ips or "", 1 if enabled else 0, ts, ts),
        )
        conn.commit()
        r = conn.execute("SELECT last_insert_rowid()").fetchone()
        if r is None:
            raise RuntimeError("Failed to read new user id")
        return int(r[0])


def update_sync_user(
    user_id: int,
    display_name: Optional[str] = None,
    role: Optional[str] = None,
    allowed_ips: Optional[str] = None,
    enabled: Optional[bool] = None,
    username: Optional[str] = None,
) -> None:
    """Update only the fields passed; None means 'leave alone'."""
    sets: list[str] = []
    args: list[object] = []
    if username is not None:
        name = username.strip()
        if not name:
            raise ValueError("username is empty")
        with _connect() as conn:
            taken = conn.execute(
                "SELECT 1 FROM sync_users WHERE username = ? AND id <> ?",
                (name, user_id),
            ).fetchone()
        if taken is not None:
            raise ValueError(f"username {name!r} is already taken")
        sets.append("username = ?")
        args.append(name)
    if display_name is not None:
        sets.append("display_name = ?")
        args.append(display_name)
    if role is not None:
        sets.append("role = ?")
        args.append(role)
    if allowed_ips is not None:
        sets.append("allowed_ips = ?")
        args.append(allowed_ips)
    if enabled is not None:
        sets.append("enabled = ?")
        args.append(1 if enabled else 0)
    if not sets:
        return
    sets.append("updated_at = ?")
    args.append(_now_ts())
    args.append(user_id)
    with _connect() as conn:
        conn.execute(
            f"UPDATE sync_users SET {', '.join(sets)} WHERE id = ?", tuple(args)
        )
        conn.commit()


def set_sync_user_password(user_id: int, pw_hash: str, pw_salt: str) -> None:
    """Set the password hash and consume any outstanding recovery code."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE sync_users
               SET pw_hash = ?, pw_salt = ?, recovery_hash = '', recovery_salt = '',
                   recovery_expires_at = '', updated_at = ?
             WHERE id = ?
            """,
            (pw_hash, pw_salt, _now_ts(), user_id),
        )
        conn.commit()


def set_sync_user_recovery(
    user_id: int, recovery_hash: str, recovery_salt: str, expires_at: str
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE sync_users
               SET recovery_hash = ?, recovery_salt = ?, recovery_expires_at = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (recovery_hash, recovery_salt, expires_at, _now_ts(), user_id),
        )
        conn.commit()


def clear_sync_user_recovery(user_id: int) -> None:
    set_sync_user_recovery(user_id, "", "", "")


def touch_sync_user_seen(user_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE sync_users SET last_seen_at = ? WHERE id = ?",
            (_now_ts(), user_id),
        )
        conn.commit()


def set_sync_user_migrated(user_id: int, when: str = "") -> None:
    """Stamp the one-time legacy-data import so it cannot run twice."""
    with _connect() as conn:
        conn.execute(
            "UPDATE sync_users SET migrated_at = ?, updated_at = ? WHERE id = ?",
            (when or _now_ts(), _now_ts(), user_id),
        )
        conn.commit()


def delete_sync_user(user_id: int) -> None:
    """Remove the account. Records the person authored keep their name - the
    author columns are plain text, not a foreign key, so history survives."""
    with _connect() as conn:
        conn.execute("DELETE FROM sync_users WHERE id = ?", (user_id,))
        conn.commit()


def backfill_authorship(author: str) -> dict[str, int]:
    """Attribute every pre-existing record to ``author``.

    Run once, when the PM account is first created: everything in the database
    up to that point was made by the PM on the single-user app, but the author
    columns were added blank because no username existed yet. Only blanks are
    filled, so re-running changes nothing and rows created afterwards keep their
    own author. Returns the number of rows touched per table.
    """
    who = (author or "").strip()
    if not who:
        return {}
    counts: dict[str, int] = {}
    updates = (
        ("daily_lists", "created_by"), ("daily_lists", "updated_by"),
        ("daily_cards", "created_by"), ("daily_cards", "updated_by"),
        ("meetings", "created_by"), ("meetings", "updated_by"),
        ("accs", "owner_user_id"), ("stickies", "owner_user_id"),
        ("resumes", "uploaded_by"),
    )
    with _connect() as conn:
        for table, column in updates:
            try:
                cur = conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE {column} = '' OR {column} IS NULL",
                    (who,),
                )
            except sqlite3.OperationalError:
                continue  # table or column absent in an older database
            counts[f"{table}.{column}"] = cur.rowcount if cur.rowcount > 0 else 0
        conn.commit()
    return counts
