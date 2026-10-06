"""Symmetric encryption for account passwords stored in SQLite.

The Fernet key is derived from the vault's Data Encryption Key (DEK) via HKDF,
so there is no separate key file to lose. (Older builds kept the key in a
standalone ``task_manager_acc.key`` file; :func:`fold_into_dek` migrates those
passwords to the DEK-derived key once and removes the file.)

While the database is locked / in legacy plaintext mode (no DEK), it falls back
to the standalone key file for backward compatibility.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_KEY_FILENAME = "task_manager_acc.key"

# The active Fernet (DEK-derived) used for new encryption, set after unlock.
# _legacy_fernet is the standalone-file key kept as a DECRYPT fallback so
# passwords written under either key always decrypt. Both None => legacy mode.
_active_fernet: Optional[Fernet] = None
_legacy_fernet: Optional[Fernet] = None


def _key_file_path() -> Path:
    return Path(__file__).resolve().parent / _KEY_FILENAME


def derive_key(dek: bytes) -> bytes:
    """A url-safe base64 Fernet key derived from the vault DEK."""
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"task-manager/account-key/v1",
    ).derive(dek)
    return base64.urlsafe_b64encode(raw)


def use_derived_key(dek: bytes) -> None:
    """Switch account encryption to the DEK-derived key (called after unlock),
    keeping the standalone-file key (if any) as a decrypt fallback."""
    global _active_fernet, _legacy_fernet
    _active_fernet = Fernet(derive_key(dek))
    _legacy_fernet = _file_fernet_or_none()


def clear_key() -> None:
    global _active_fernet, _legacy_fernet
    _active_fernet = None
    _legacy_fernet = None


def _decrypt_fernets():
    """Fernets to try when decrypting, most-likely first."""
    if _active_fernet is not None:
        yield _active_fernet
    if _legacy_fernet is not None:
        yield _legacy_fernet


def _file_fernet_or_none() -> Optional[Fernet]:
    """The legacy standalone-file Fernet, if that key file exists."""
    p = _key_file_path()
    if not p.exists():
        return None
    key = p.read_bytes().strip().splitlines()[0].strip()
    return Fernet(key)


def _remove_key_file() -> None:
    p = _key_file_path()
    try:
        if p.exists():
            p.unlink()
    except OSError:
        pass


def _fernet() -> Fernet:
    if _active_fernet is not None:
        return _active_fernet
    # Legacy / no-DEK fallback: read the standalone key file, creating it once
    # if absent (only reached when running without an unlocked vault).
    p = _key_file_path()
    if p.exists():
        key = p.read_bytes().strip().splitlines()[0].strip()
    else:
        key = Fernet.generate_key()
        p.write_bytes(key + b"\n")
    return Fernet(key)


def fold_into_dek(
    dek: bytes, items: list[tuple[int, Optional[bytes]]]
) -> tuple[list[tuple[int, bytes]], bool]:
    """Re-encrypt every readable account password under the DEK-derived key.

    ``items`` is ``[(account_id, pwd_enc), ...]``. The derived key becomes the
    active key (with the standalone-file key kept as a decrypt fallback). Each
    blob that decrypts with EITHER key is re-encrypted under the derived key.
    Returns ``(updates, all_readable)``; the legacy key file is removed only when
    every non-empty blob was readable, so a blob under an unknown key is never
    cut off and nothing is destroyed.
    """
    use_derived_key(dek)  # active = derived, legacy = file key (if present)
    updates: list[tuple[int, bytes]] = []
    all_readable = True
    for acc_id, blob in items:
        if not blob:
            continue
        plain = _decrypt_raw(blob)
        if plain is None:
            all_readable = False  # unknown/lost key — leave the blob as-is
            continue
        updates.append((acc_id, _active_fernet.encrypt(plain)))
    if all_readable:
        # Every password is now (or will be) under the derived key; the
        # standalone file is no longer needed.
        global _legacy_fernet
        _remove_key_file()
        _legacy_fernet = None
    return updates, all_readable


def encrypt_password(plain: str) -> bytes:
    return _fernet().encrypt((plain or "").encode("utf-8"))


def _decrypt_raw(blob: bytes) -> Optional[bytes]:
    """Plaintext bytes for ``blob`` trying the active then legacy key, else the
    legacy-mode file key; None if nothing can read it."""
    for fernet in _decrypt_fernets():
        try:
            return fernet.decrypt(blob)
        except InvalidToken:
            continue
    if _active_fernet is None:  # legacy / no-DEK mode
        try:
            return _fernet().decrypt(blob)
        except InvalidToken:
            return None
    return None


def decrypt_password(blob: bytes | None) -> str:
    if not blob:
        return ""
    plain = _decrypt_raw(blob)
    return plain.decode("utf-8") if plain is not None else ""
