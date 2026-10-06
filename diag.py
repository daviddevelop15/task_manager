"""Read-only diagnostic for account-password recovery.

Run from the project folder:  py -3 diag.py
It prompts for your vault password (hidden), reads the packaged app's encrypted
database, and reports ONLY counts (no passwords or keys are printed).
"""

from __future__ import annotations

import getpass
import sqlite3
from pathlib import Path

import secure_store as ss
import acc_crypto
from cryptography.fernet import Fernet, InvalidToken

BASE = Path(r"E:\Task-manager\dist\TaskManager\_internal")
VAULT = BASE / "task_manager.vault"
ENC = BASE / "task_manager.db.enc"


def _load_fernet(path: Path):
    if not path.exists():
        return None
    try:
        return Fernet(path.read_bytes().strip().splitlines()[0].strip())
    except Exception:
        return None


def _ok(fernet, blob) -> bool:
    if fernet is None or not blob:
        return False
    try:
        fernet.decrypt(blob)
        return True
    except InvalidToken:
        return False


def main() -> None:
    print("Vault mode:", ss.vault_mode(VAULT))
    pw = getpass.getpass("Enter your vault password: ")
    try:
        dek = ss.unlock(VAULT, pw)
    except Exception as exc:
        print("UNLOCK FAILED:", type(exc).__name__, exc)
        return
    print("Unlock OK.")

    image = ss.read_encrypted_file(ENC, dek)
    con = sqlite3.connect(":memory:")
    con.deserialize(image)
    rows = con.execute("SELECT id, pwd_enc FROM accs").fetchall()
    nonempty = [(i, b) for (i, b) in rows if b]
    print(f"accounts: {len(rows)}  | non-empty pwd_enc: {len(nonempty)}")

    derived = Fernet(acc_crypto.derive_key(dek))
    dist_file = _load_fernet(BASE / "task_manager_acc.key")
    root_file = _load_fernet(Path(r"E:\Task-manager\task_manager_acc.key"))

    d = sum(1 for _, b in nonempty if _ok(derived, b))
    f1 = sum(1 for _, b in nonempty if _ok(dist_file, b))
    f2 = sum(1 for _, b in nonempty if _ok(root_file, b))
    lost = sum(
        1 for _, b in nonempty
        if not _ok(derived, b) and not _ok(dist_file, b) and not _ok(root_file, b)
    )
    print(f"decrypt with DERIVED(DEK) key : {d}")
    print(f"decrypt with DIST file key    : {f1}")
    print(f"decrypt with ROOT file key    : {f2}")
    print(f"decrypt with NONE (lost key)  : {lost}")


if __name__ == "__main__":
    main()
