"""Accounts, passwords, IP policy and recovery codes for team mode.

`database.py` stores the rows; everything that *decides* something lives here,
so the rules are in one place and testable without touching a database.

Three deliberate choices:

* **Passwords are scrypt hashes.** The PM administers accounts but cannot read
  anybody's password, and cannot set one either - a forgotten password is
  resolved with a one-time recovery code the person redeems themselves.
* **The source IP is checked by the server**, never sent by the client. This
  module only decides whether a given IP is acceptable; the caller must pass the
  IP it read off the TCP connection.
* **Nothing here trusts the caller's claim of who they are.** `authenticate()`
  takes a username, a password and an observed IP, and returns a single verdict.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from fnmatch import fnmatch
from typing import Optional

import database as db
from database import ROLE_DEV, ROLE_PM, SyncUserRow

# scrypt parameters. n=2**14 with r=8 needs ~16 MB and a few hundred ms - ample
# against offline cracking, and only paid on login, not per request.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32
_SALT_BYTES = 16

# Recovery codes are read aloud or typed, so the alphabet leaves out characters
# that look alike (0/O, 1/I/L).
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_GROUPS = 3
_CODE_GROUP_LEN = 4
RECOVERY_TTL_HOURS = 24

# authenticate() verdicts
AUTH_OK = "ok"
AUTH_UNKNOWN_USER = "unknown_user"
AUTH_BAD_PASSWORD = "bad_password"
AUTH_DISABLED = "disabled"
AUTH_IP_NOT_ALLOWED = "ip_not_allowed"
AUTH_NO_PASSWORD = "no_password"

_AUTH_MESSAGES = {
    AUTH_UNKNOWN_USER: "Unknown username or password.",
    AUTH_BAD_PASSWORD: "Unknown username or password.",
    AUTH_DISABLED: "That account is disabled. Ask the PM to re-enable it.",
    AUTH_IP_NOT_ALLOWED: (
        "This PC's IP address is not registered for that account. "
        "Ask the PM to add it."
    ),
    AUTH_NO_PASSWORD: (
        "That account has no password yet. Ask the PM for a recovery code and "
        "set one."
    ),
}


# --------------------------------------------------------------- principals
#
# Records store *who* made them as a principal - "u:<id>" - not as a name.
# Names change: rename yourself and every row you created would stop being
# yours, because the permission check compares the stored author with the
# caller. The id never changes, so the link survives a rename.
#
# The prefix keeps a principal from ever being mistaken for a display name,
# including on databases written before this existed, where these columns hold
# plain names. Those legacy values still resolve - see :func:`label_for` - so a
# row created by an account that no longer exists still shows its old name
# rather than going blank.

PRINCIPAL_PREFIX = "u:"

_LABEL_CACHE: dict[str, str] = {}


def principal(user: SyncUserRow | int) -> str:
    """The stable identifier stored on records this user creates."""
    return f"{PRINCIPAL_PREFIX}{user if isinstance(user, int) else user.id}"


def principal_id(value: str) -> Optional[int]:
    """The account id inside a principal, or None if this is a legacy name."""
    text = (value or "").strip()
    if not text.startswith(PRINCIPAL_PREFIX):
        return None
    try:
        return int(text[len(PRINCIPAL_PREFIX):])
    except ValueError:
        return None


def invalidate_label_cache() -> None:
    _LABEL_CACHE.clear()


def label_for(value: str) -> str:
    """Human name for a stored author value.

    Resolves a principal through the account table, and passes anything else
    through unchanged - that is a name written before principals existed, or one
    belonging to an account since deleted, and showing it is better than showing
    nothing. A principal whose account is gone falls back to its id so the row
    is still attributable to *someone* rather than silently blank.
    """
    text = (value or "").strip()
    if not text:
        return ""
    uid = principal_id(text)
    if uid is None:
        return text                      # legacy plain name
    if text in _LABEL_CACHE:
        return _LABEL_CACHE[text]
    user = db.get_sync_user(uid)
    label = user.label if user is not None else f"(removed user {uid})"
    _LABEL_CACHE[text] = label
    return label


ROLE_LABELS = {ROLE_PM: "PM", ROLE_DEV: "Developer"}


def role_label_for(value: str) -> str:
    """Human role for a stored author value, or '' when it cannot be known.

    Empty for a legacy plain name or a deleted account - there is no account
    left to ask, and guessing would be worse than saying nothing.
    """
    uid = principal_id(value)
    if uid is None:
        return ""
    user = db.get_sync_user(uid)
    return ROLE_LABELS.get(user.role, "") if user is not None else ""


def migrate_authorship_to_ids() -> int:
    """Rewrite author columns that still hold names as principals.

    One-time, idempotent, and matched case-insensitively against both the
    display name and the username, because either could have been stored. Names
    with no matching account are left alone: there is nobody to point them at,
    and the name is the only record of who it was.

    Matching is against names as they are *now*, and nothing records what an
    account used to be called - so this runs at startup, before anyone has a
    chance to rename themselves in the new build. A row left holding a name
    still displays and still belongs to that person in the Creator column; it
    is only the permission check that can no longer connect it to an account,
    and the PM can edit it regardless.
    """
    updated = 0
    columns = (
        ("daily_lists", "created_by"), ("daily_lists", "updated_by"),
        ("daily_cards", "created_by"), ("daily_cards", "updated_by"),
        ("meetings", "created_by"), ("meetings", "updated_by"),
        ("accs", "owner_user_id"), ("stickies", "owner_user_id"),
        ("resumes", "uploaded_by"),
    )
    users = db.list_sync_users()
    if not users:
        return 0
    with db._connect() as conn:
        for user in users:
            names = {n.strip().lower() for n in (user.display_name, user.username) if n.strip()}
            if not names:
                continue
            for table, column in columns:
                for name in names:
                    try:
                        cur = conn.execute(
                            f"UPDATE {table} SET {column} = ? "
                            f"WHERE lower(trim({column})) = ?",
                            (principal(user), name),
                        )
                    except Exception:            # noqa: BLE001 - older schema
                        continue
                    updated += max(0, cur.rowcount)
        conn.commit()
    invalidate_label_cache()
    return updated


# --------------------------------------------------------------- passwords

def hash_password(password: str, salt_hex: str = "") -> tuple[str, str]:
    """``(hash_hex, salt_hex)``. A fresh salt is generated unless one is given."""
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        (password or "").encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_DKLEN,
    )
    return digest.hex(), salt.hex()


def verify_password(password: str, pw_hash: str, pw_salt: str) -> bool:
    """Constant-time check. An account with no stored hash never verifies."""
    if not pw_hash or not pw_salt:
        return False
    try:
        candidate, _ = hash_password(password, pw_salt)
    except ValueError:
        return False  # unparseable salt
    return hmac.compare_digest(candidate, pw_hash)


# --------------------------------------------------------------- IP policy

def normalize_ip(ip: str) -> str:
    """Comparable form of an observed address.

    IPv4-mapped IPv6 (``::ffff:192.168.1.5``) reduces to the IPv4 address, and
    the IPv6 loopback to ``127.0.0.1``, so the PM registers the address people
    actually read off ``ipconfig`` rather than whatever shape the socket used.
    """
    s = (ip or "").strip().lower()
    if not s:
        return ""
    if s.startswith("["):                      # [::1]:port
        s = s[1:].split("]", 1)[0]
    if s.startswith("::ffff:"):
        s = s[len("::ffff:"):]
    if s in ("::1", "localhost"):
        return "127.0.0.1"
    if s.count(":") == 1 and "." in s:         # 192.168.1.5:51234
        s = s.split(":", 1)[0]
    return s


def parse_allowed_ips(allowed: str) -> list[str]:
    """Split the stored list. Commas, spaces and newlines all separate."""
    out: list[str] = []
    for chunk in (allowed or "").replace("\n", ",").replace(";", ",").split(","):
        for part in chunk.split():
            p = part.strip()
            if p and p not in out:
                out.append(p)
    return out


def ip_allowed(allowed: str, source_ip: str) -> bool:
    """Whether ``source_ip`` may be used by an account registered to ``allowed``.

    An empty list means unrestricted. That is deliberate: it lets the PM create
    the first account and get in before any address is known, and lets a user be
    left unrestricted on purpose. The admin UI flags it, because it is the one
    setting that turns the IP check off.

    Entries may be exact addresses or glob patterns (``192.168.1.*``).
    """
    entries = parse_allowed_ips(allowed)
    if not entries or "*" in entries:
        return True
    ip = normalize_ip(source_ip)
    if not ip:
        return False
    return any(ip == normalize_ip(e) or fnmatch(ip, e.strip().lower()) for e in entries)


# --------------------------------------------------------------- accounts

def create_user(
    username: str,
    password: str,
    role: str = ROLE_DEV,
    display_name: str = "",
    allowed_ips: str = "",
    enabled: bool = True,
) -> SyncUserRow:
    """Create an account. The first PM also inherits the existing data.

    Everything in a single-user database was made by the person now becoming the
    PM, so their creation is the moment those rows can finally be attributed -
    see :func:`database.backfill_authorship`.
    """
    pw_hash, pw_salt = hash_password(password) if password else ("", "")
    first_pm = role == ROLE_PM and db.count_sync_users(ROLE_PM) == 0
    user_id = db.insert_sync_user(
        username=username,
        display_name=display_name,
        role=role,
        pw_hash=pw_hash,
        pw_salt=pw_salt,
        allowed_ips=allowed_ips,
        enabled=enabled,
    )
    user = db.get_sync_user(user_id)
    if user is None:                       # pragma: no cover - just created it
        raise RuntimeError("user vanished immediately after creation")
    invalidate_label_cache()
    # Names already written by a previous release become this account's
    # principal, so a rename from here on cannot orphan them.
    migrate_authorship_to_ids()
    if first_pm:
        db.backfill_authorship(principal(user))
    return user


def set_password(user_id: int, password: str) -> None:
    """Set a password directly. Only the account's owner may reach this - the
    PM's path to a locked-out dev is a recovery code, never a new password."""
    if not password:
        raise ValueError("password is empty")
    pw_hash, pw_salt = hash_password(password)
    db.set_sync_user_password(user_id, pw_hash, pw_salt)


def list_users() -> list[SyncUserRow]:
    return db.list_sync_users()


def get_user(user_id: int) -> Optional[SyncUserRow]:
    return db.get_sync_user(user_id)


def find_user(username: str) -> Optional[SyncUserRow]:
    return db.get_sync_user_by_username(username)


def delete_user(user_id: int) -> None:
    """Delete an account, refusing to remove the last PM.

    Without a PM nobody can administer users, and the server would be
    unmanageable - so this is a hard rule, not a UI convenience.
    """
    user = db.get_sync_user(user_id)
    if user is None:
        return
    if user.is_pm and db.count_sync_users(ROLE_PM) <= 1:
        raise ValueError("cannot delete the only PM account")
    # Leave the person's name on everything they made, before the account that
    # resolves it disappears. Records point at an id, so without this the
    # history they authored would read "(removed user 7)" for ever.
    _stamp_name_over_principal(user)
    db.delete_sync_user(user_id)
    invalidate_label_cache()


def _stamp_name_over_principal(user: SyncUserRow) -> int:
    """Replace this account's principal with its label, everywhere.

    Used when an account is deleted: the rows stop being editable by anybody but
    the PM - correct, the person is gone - while still saying who made them.
    """
    label = user.label
    if not label:
        return 0
    changed = 0
    with db._connect() as conn:
        for table, column in (
            ("daily_lists", "created_by"), ("daily_lists", "updated_by"),
            ("daily_cards", "created_by"), ("daily_cards", "updated_by"),
            ("meetings", "created_by"), ("meetings", "updated_by"),
            ("accs", "owner_user_id"), ("stickies", "owner_user_id"),
            ("resumes", "uploaded_by"),
        ):
            try:
                cur = conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                    (label, principal(user)),
                )
            except Exception:            # noqa: BLE001 - older schema
                continue
            changed += max(0, cur.rowcount)
        conn.commit()
    return changed


def update_user(
    user_id: int,
    display_name: Optional[str] = None,
    role: Optional[str] = None,
    allowed_ips: Optional[str] = None,
    enabled: Optional[bool] = None,
    username: Optional[str] = None,
) -> None:
    """Update account fields, refusing to demote or disable the last PM."""
    user = db.get_sync_user(user_id)
    if user is None:
        raise ValueError("no such user")
    losing_pm = user.is_pm and (
        (role is not None and role != ROLE_PM) or enabled is False
    )
    if losing_pm and db.count_sync_users(ROLE_PM) <= 1:
        raise ValueError("cannot demote or disable the only PM account")
    db.update_sync_user(
        user_id,
        display_name=display_name,
        role=role,
        allowed_ips=allowed_ips,
        enabled=enabled,
        username=username,
    )
    # A rename changes only what is displayed - records point at the id - but
    # the cached names have to go.
    invalidate_label_cache()


def has_any_users() -> bool:
    return db.count_sync_users() > 0


# --------------------------------------------------------------- recovery

def _format_code(raw: str) -> str:
    return "-".join(
        raw[i:i + _CODE_GROUP_LEN] for i in range(0, len(raw), _CODE_GROUP_LEN)
    )


def normalize_code(code: str) -> str:
    """Strip formatting so 'abcd efgh-jkmn' matches 'ABCD-EFGH-JKMN'."""
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def issue_recovery_code(user_id: int, ttl_hours: int = RECOVERY_TTL_HOURS) -> str:
    """Generate a one-time code and store only its hash.

    Returned in the clear exactly once, for the PM to hand over. It replaces any
    outstanding code, so issuing a second one invalidates the first.
    """
    raw = "".join(secrets.choice(_CODE_ALPHABET)
                  for _ in range(_CODE_GROUPS * _CODE_GROUP_LEN))
    code_hash, code_salt = hash_password(raw)
    expires = (datetime.now() + timedelta(hours=max(1, ttl_hours))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    db.set_sync_user_recovery(user_id, code_hash, code_salt, expires)
    return _format_code(raw)


def recovery_is_live(user: SyncUserRow, now: Optional[str] = None) -> bool:
    """Whether the account has an unredeemed code that has not expired."""
    if not user.recovery_hash or not user.recovery_expires_at:
        return False
    stamp = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return stamp <= user.recovery_expires_at   # both 'YYYY-MM-DD HH:MM:SS'


def redeem_recovery_code(username: str, code: str, new_password: str) -> bool:
    """Consume a valid code and set the new password. False if it doesn't match.

    Single use: the code is cleared by ``set_sync_user_password`` as part of
    setting the password, so a replayed code finds nothing to match.
    """
    if not new_password:
        raise ValueError("password is empty")
    user = db.get_sync_user_by_username(username)
    if user is None or not user.enabled or not recovery_is_live(user):
        return False
    if not verify_password(normalize_code(code), user.recovery_hash, user.recovery_salt):
        return False
    set_password(user.id, new_password)
    return True


# --------------------------------------------------------------- login

@dataclass
class AuthResult:
    ok: bool
    reason: str
    user: Optional[SyncUserRow] = None

    @property
    def message(self) -> str:
        """What to show the person trying to log in."""
        if self.ok:
            return ""
        return _AUTH_MESSAGES.get(self.reason, "Login failed.")


def authenticate(username: str, password: str, source_ip: str) -> AuthResult:
    """Decide whether this login attempt succeeds.

    ``source_ip`` must be the address the server read off the connection. A
    client-supplied address is worthless here - it is exactly what the check
    exists to catch - so callers must not pass a value from the request body or
    a header.

    The order matters: password before IP, so an attacker who guesses an IP
    learns nothing about whether the password was right, and a legitimate user
    on the wrong PC gets told what is actually wrong.
    """
    user = db.get_sync_user_by_username(username)
    if user is None:
        return AuthResult(False, AUTH_UNKNOWN_USER)
    if not user.enabled:
        return AuthResult(False, AUTH_DISABLED, user)
    if not user.pw_hash:
        return AuthResult(False, AUTH_NO_PASSWORD, user)
    if not verify_password(password, user.pw_hash, user.pw_salt):
        return AuthResult(False, AUTH_BAD_PASSWORD, user)
    if not ip_allowed(user.allowed_ips, source_ip):
        return AuthResult(False, AUTH_IP_NOT_ALLOWED, user)
    db.touch_sync_user_seen(user.id)
    return AuthResult(True, AUTH_OK, db.get_sync_user(user.id) or user)


__all__ = [
    "ROLE_PM", "ROLE_DEV", "SyncUserRow", "AuthResult",
    "AUTH_OK", "AUTH_UNKNOWN_USER", "AUTH_BAD_PASSWORD", "AUTH_DISABLED",
    "AUTH_IP_NOT_ALLOWED", "AUTH_NO_PASSWORD",
    "hash_password", "verify_password", "normalize_ip", "parse_allowed_ips",
    "ip_allowed", "create_user", "set_password", "list_users", "get_user",
    "find_user", "delete_user", "update_user", "has_any_users",
    "issue_recovery_code", "recovery_is_live", "redeem_recovery_code",
    "normalize_code", "authenticate",
]
