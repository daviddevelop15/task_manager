"""Windows DPAPI (CryptProtectData / CryptUnprotectData) via ctypes.

Used to wrap a random key with the logged-in Windows user's credentials so the
app can decrypt its database at startup without prompting for a password
(the "no password" unlock mode). Protection is bound to the current Windows
user account, so the wrapped key cannot be unwrapped by another user or on a
different machine.

Pure standard library (ctypes) - no third-party dependency, bundles cleanly
with PyInstaller.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

# Secondary "entropy" mixed into the protection so the wrapped blob is bound to
# this application's namespace, not just the Windows user.
APP_ENTROPY = b"TaskManager/DPAPI/v1"

# CryptProtectData flag: never show a UI prompt (fail instead).
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _is_available() -> bool:
    return hasattr(ctypes, "windll")


if _is_available():  # pragma: no cover - Windows only
    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),  # pDataIn
        wintypes.LPCWSTR,            # szDataDescr
        ctypes.POINTER(_DATA_BLOB),  # pOptionalEntropy
        ctypes.c_void_p,             # pvReserved
        ctypes.c_void_p,             # pPromptStruct
        wintypes.DWORD,              # dwFlags
        ctypes.POINTER(_DATA_BLOB),  # pDataOut
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL

    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),  # pDataIn
        ctypes.POINTER(wintypes.LPWSTR),  # ppszDataDescr
        ctypes.POINTER(_DATA_BLOB),  # pOptionalEntropy
        ctypes.c_void_p,             # pvReserved
        ctypes.c_void_p,             # pPromptStruct
        wintypes.DWORD,              # dwFlags
        ctypes.POINTER(_DATA_BLOB),  # pDataOut
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL

    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    _kernel32.LocalFree.restype = wintypes.HLOCAL


def is_available() -> bool:
    """True when the DPAPI functions can be called (i.e. running on Windows)."""
    return _is_available()


def _blob_in(data: bytes):
    """Build a _DATA_BLOB plus the backing buffer (which the caller must keep
    alive for the duration of the API call)."""
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def _blob_out_bytes(blob: _DATA_BLOB) -> bytes:
    return ctypes.string_at(blob.pbData, int(blob.cbData))


def protect(data: bytes, entropy: bytes = APP_ENTROPY) -> bytes:
    """Encrypt ``data`` with the current Windows user's credentials."""
    if not _is_available():
        raise OSError("DPAPI is only available on Windows")
    blob_in, _buf_in = _blob_in(data)
    ent_blob, _buf_ent = _blob_in(entropy) if entropy else (None, None)
    blob_out = _DATA_BLOB()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(ent_blob) if ent_blob is not None else None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("CryptProtectData failed (error %d)" % ctypes.GetLastError())
    try:
        return _blob_out_bytes(blob_out)
    finally:
        _kernel32.LocalFree(blob_out.pbData)


def unprotect(data: bytes, entropy: bytes = APP_ENTROPY) -> bytes:
    """Decrypt data produced by :func:`protect` for the current Windows user."""
    if not _is_available():
        raise OSError("DPAPI is only available on Windows")
    blob_in, _buf_in = _blob_in(data)
    ent_blob, _buf_ent = _blob_in(entropy) if entropy else (None, None)
    blob_out = _DATA_BLOB()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(ent_blob) if ent_blob is not None else None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("CryptUnprotectData failed (error %d)" % ctypes.GetLastError())
    try:
        return _blob_out_bytes(blob_out)
    finally:
        _kernel32.LocalFree(blob_out.pbData)
