"""Envelope encryption for the Task Manager database and backups.

A single random Data Encryption Key (DEK) protects all data with AES-256-GCM.
The DEK itself is "wrapped" (encrypted) one of two interchangeable ways, chosen
by the user:

* ``dpapi``    - wrapped with the Windows logged-in user's credentials, so the
                 app unlocks at startup with no password prompt.
* ``password`` - wrapped with a key derived from the user's passphrase via
                 scrypt (memory-hard KDF).

Because the DEK never changes, switching modes or changing the password only
re-wraps the 32-byte DEK - the (potentially large) database is never
re-encrypted. An optional recovery code provides a second password-style wrap
so a forgotten passphrase does not mean lost data.

Pure standard library + ``cryptography`` (already a project dependency).
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

import win_dpapi

VAULT_FORMAT = "TaskManagerVault"
VAULT_VERSION = 1

DEK_BYTES = 32
NONCE_BYTES = 12
SALT_BYTES = 16

# scrypt cost parameters (~100-300 ms per derivation on a modern CPU). ``n``
# must be a power of two. Stored in the vault so future tuning stays readable.
SCRYPT_N = 2 ** 16
SCRYPT_R = 8
SCRYPT_P = 1

MODE_DPAPI = "dpapi"
MODE_PASSWORD = "password"

# Associated-data tags keep ciphertexts from being swapped between contexts.
_AAD_DEK = b"taskmanager:dek"
_AAD_VERIFY = b"taskmanager:verify"
_AAD_DB = b"taskmanager:db"

_VERIFIER = b"TaskManagerVault:verify:v1"

# Recovery code: groups of unambiguous characters, e.g. ABCDE-FGHJK-...
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_RECOVERY_GROUPS = 6
_RECOVERY_GROUP_LEN = 5


class VaultError(Exception):
    """Base class for vault errors."""


class UnlockError(VaultError):
    """The DEK could not be unwrapped (wrong password, wrong user, tampering)."""


# --- base64 helpers ---------------------------------------------------------

def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


# --- AES-GCM helpers --------------------------------------------------------

def aesgcm_encrypt(key: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """Return ``nonce || ciphertext`` for ``plaintext`` under ``key``."""
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def aesgcm_decrypt(key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    if len(blob) < NONCE_BYTES + 16:
        raise UnlockError("ciphertext too short")
    nonce, ct = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, ct, aad)
    except InvalidTag as exc:
        raise UnlockError("authentication failed (wrong key or corrupted data)") from exc


def _derive_kek(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    kdf = Scrypt(salt=salt, length=DEK_BYTES, n=n, r=r, p=p)
    return kdf.derive(password.encode("utf-8"))


# --- atomic file writes -----------------------------------------------------

def atomic_write_bytes(path: Path, data: bytes, keep_backup: bool = False) -> None:
    """Write ``data`` to ``path`` atomically (write temp + fsync + os.replace).

    With ``keep_backup`` the previous file is copied to ``path.bak`` first, so a
    power loss never leaves you without at least the prior good copy.
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    if keep_backup and path.exists():
        try:
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        except OSError:
            pass
    os.replace(tmp, path)  # atomic on Windows and POSIX


# --- DEK lifecycle ----------------------------------------------------------

def generate_dek() -> bytes:
    return os.urandom(DEK_BYTES)


def _wrap_password(dek: bytes, password: str) -> tuple[dict, str]:
    salt = os.urandom(SALT_BYTES)
    kek = _derive_kek(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    wrapped = aesgcm_encrypt(kek, dek, aad=_AAD_DEK)
    kdf = {"name": "scrypt", "salt": _b64e(salt), "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P}
    return kdf, _b64e(wrapped)


def _unwrap_password(wrapped_b64: str, password: str, kdf: dict) -> bytes:
    salt = _b64d(kdf["salt"])
    kek = _derive_kek(
        password, salt, int(kdf.get("n", SCRYPT_N)), int(kdf.get("r", SCRYPT_R)),
        int(kdf.get("p", SCRYPT_P)),
    )
    return aesgcm_decrypt(kek, _b64d(wrapped_b64), aad=_AAD_DEK)


def _wrap_dpapi(dek: bytes) -> str:
    return _b64e(win_dpapi.protect(dek))


def _unwrap_dpapi(wrapped_b64: str) -> bytes:
    try:
        return win_dpapi.unprotect(_b64d(wrapped_b64))
    except OSError as exc:
        raise UnlockError("Windows could not unprotect the key (different user or machine)") from exc


# --- vault file -------------------------------------------------------------

def _build_vault(mode: str, dek: bytes, password: Optional[str]) -> dict:
    vault = {
        "format": VAULT_FORMAT,
        "version": VAULT_VERSION,
        "mode": mode,
        "verifier": _b64e(aesgcm_encrypt(dek, _VERIFIER, aad=_AAD_VERIFY)),
    }
    if mode == MODE_PASSWORD:
        if not password:
            raise VaultError("a password is required for password mode")
        kdf, wrapped = _wrap_password(dek, password)
        vault["kdf"] = kdf
        vault["wrapped_dek"] = wrapped
    elif mode == MODE_DPAPI:
        vault["wrapped_dek"] = _wrap_dpapi(dek)
    else:
        raise VaultError(f"unknown mode {mode!r}")
    return vault


def read_vault(path) -> Optional[dict]:
    p = Path(path)
    if not p.exists():
        return None
    vault = json.loads(p.read_text("utf-8"))
    if vault.get("format") != VAULT_FORMAT:
        raise VaultError("not a Task Manager vault file")
    return vault


def write_vault(path, vault: dict) -> None:
    atomic_write_bytes(Path(path), json.dumps(vault, indent=2).encode("utf-8"), keep_backup=True)


def vault_exists(path) -> bool:
    return Path(path).exists()


def vault_mode(path) -> Optional[str]:
    vault = read_vault(path)
    return vault.get("mode") if vault else None


def create_vault(path, mode: str, password: Optional[str] = None) -> bytes:
    """Create a new vault with a fresh DEK and return that DEK."""
    if read_vault(path) is not None:
        raise VaultError("vault already exists")
    dek = generate_dek()
    write_vault(path, _build_vault(mode, dek, password))
    return dek


def _verify(vault: dict, dek: bytes) -> None:
    if aesgcm_decrypt(dek, _b64d(vault["verifier"]), aad=_AAD_VERIFY) != _VERIFIER:
        raise UnlockError("vault verifier mismatch")


def unlock(path, password: Optional[str] = None) -> bytes:
    """Unwrap and return the DEK. Raises :class:`UnlockError` on failure."""
    vault = read_vault(path)
    if vault is None:
        raise VaultError("no vault to unlock")
    mode = vault.get("mode")
    if mode == MODE_PASSWORD:
        if password is None:
            raise UnlockError("password required")
        dek = _unwrap_password(vault["wrapped_dek"], password, vault.get("kdf", {}))
    elif mode == MODE_DPAPI:
        dek = _unwrap_dpapi(vault["wrapped_dek"])
    else:
        raise VaultError(f"unknown mode {mode!r}")
    _verify(vault, dek)
    return dek


def rewrap(path, dek: bytes, new_mode: str, new_password: Optional[str] = None) -> None:
    """Re-protect an already-unlocked DEK under a new mode / password.

    Preserves any existing recovery wrap (it protects the same DEK).
    """
    old = read_vault(path)
    new_vault = _build_vault(new_mode, dek, new_password)
    if old and "recovery" in old:
        new_vault["recovery"] = old["recovery"]
    write_vault(path, new_vault)


def switch_to_dpapi(path, dek: bytes) -> None:
    rewrap(path, dek, MODE_DPAPI)


def switch_to_password(path, dek: bytes, password: str) -> None:
    rewrap(path, dek, MODE_PASSWORD, password)


def change_password(path, dek: bytes, new_password: str) -> None:
    rewrap(path, dek, MODE_PASSWORD, new_password)


# --- recovery code ----------------------------------------------------------

def generate_recovery_code() -> str:
    raw = "".join(
        secrets.choice(_RECOVERY_ALPHABET)
        for _ in range(_RECOVERY_GROUPS * _RECOVERY_GROUP_LEN)
    )
    return "-".join(
        raw[i : i + _RECOVERY_GROUP_LEN]
        for i in range(0, len(raw), _RECOVERY_GROUP_LEN)
    )


def _normalize_recovery(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch in _RECOVERY_ALPHABET)


def set_recovery(path, dek: bytes, recovery_code: str) -> None:
    vault = read_vault(path)
    if vault is None:
        raise VaultError("no vault")
    kdf, wrapped = _wrap_password(dek, _normalize_recovery(recovery_code))
    vault["recovery"] = {"kdf": kdf, "wrapped_dek": wrapped}
    write_vault(path, vault)


def has_recovery(path) -> bool:
    vault = read_vault(path)
    return bool(vault and vault.get("recovery"))


def unlock_with_recovery(path, recovery_code: str) -> bytes:
    vault = read_vault(path)
    if vault is None:
        raise VaultError("no vault")
    rec = vault.get("recovery")
    if not rec:
        raise UnlockError("no recovery code is set for this vault")
    dek = _unwrap_password(rec["wrapped_dek"], _normalize_recovery(recovery_code), rec["kdf"])
    _verify(vault, dek)
    return dek


# --- encrypted data blobs (used by the database layer) ----------------------

def encrypt_blob(dek: bytes, plaintext: bytes) -> bytes:
    return aesgcm_encrypt(dek, plaintext, aad=_AAD_DB)


def decrypt_blob(dek: bytes, blob: bytes) -> bytes:
    return aesgcm_decrypt(dek, blob, aad=_AAD_DB)


def write_encrypted_file(path, dek: bytes, plaintext: bytes, keep_backup: bool = True) -> None:
    atomic_write_bytes(Path(path), encrypt_blob(dek, plaintext), keep_backup=keep_backup)


def read_encrypted_file(path, dek: bytes) -> bytes:
    return decrypt_blob(dek, Path(path).read_bytes())


# --- portable encrypted backups (.tmbk) -------------------------------------
#
# Backups are protected by a passphrase (independent of the app's unlock mode)
# so the file is portable to any machine. The container is self-describing JSON:
# it carries the scrypt parameters needed to re-derive the key from the
# passphrase, plus the AES-GCM ciphertext of the backup payload.

BACKUP_ENC_FORMAT = "TaskManagerBackupEnc"
BACKUP_ENC_VERSION = 1
_AAD_BACKUP = b"taskmanager:backup"


def encrypt_backup(plaintext: bytes, passphrase: str) -> bytes:
    salt = os.urandom(SALT_BYTES)
    key = _derive_kek(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    blob = aesgcm_encrypt(key, plaintext, aad=_AAD_BACKUP)
    doc = {
        "format": BACKUP_ENC_FORMAT,
        "version": BACKUP_ENC_VERSION,
        "kdf": {"name": "scrypt", "salt": _b64e(salt), "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P},
        "data": _b64e(blob),
    }
    return json.dumps(doc, indent=2).encode("utf-8")


def is_encrypted_backup(raw: bytes) -> bool:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(doc, dict) and doc.get("format") == BACKUP_ENC_FORMAT


def decrypt_backup(raw: bytes, passphrase: str) -> bytes:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise VaultError("not a valid encrypted backup file") from exc
    if not isinstance(doc, dict) or doc.get("format") != BACKUP_ENC_FORMAT:
        raise VaultError("not an encrypted Task Manager backup")
    kdf = doc.get("kdf", {})
    salt = _b64d(kdf["salt"])
    key = _derive_kek(
        passphrase, salt, int(kdf.get("n", SCRYPT_N)), int(kdf.get("r", SCRYPT_R)),
        int(kdf.get("p", SCRYPT_P)),
    )
    return aesgcm_decrypt(key, _b64d(doc["data"]), aad=_AAD_BACKUP)
