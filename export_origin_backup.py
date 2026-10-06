"""Write a full backup of the live app's database using the current code.

The live app is an older build whose export has no meetings in it, so exporting
from inside it would produce another meetings-free file. This reads its database
directly - nothing is written to it - and builds the file with today's format,
meetings included.

Usage:
    py -3 export_origin_backup.py                 # drops sheets holding no cards
    py -3 export_origin_backup.py --keep-empty    # keeps every sheet
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import database as db
import secure_store

SOURCE = Path(__file__).resolve().parent / "dist" / "TaskManager" / "_internal"
OUT_DIR = Path(__file__).resolve().parent / "db_backups"
DROP_EMPTY_SHEETS = "--keep-empty" not in sys.argv


def password_from_env_file() -> str:
    path = Path(__file__).resolve().parent / "doc" / ".env.var"
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == "TM_DB_PASSWORD":
            return val.strip().strip('"').strip("'")
    return ""


db._base_dir = lambda: SOURCE
vault = SOURCE / "task_manager.vault"
mode = secure_store.vault_mode(vault)
pw = password_from_env_file() if mode == "password" else None
if mode == "password" and not pw:
    import getpass

    pw = getpass.getpass("Live app password (not stored): ")
db.unlock(secure_store.unlock(vault, pw))

doc = db.build_backup_document()
sheets = doc["daily"]["lists"]
cards = doc["daily"]["cards"]
meetings = doc["meetings"]
print("read from the live app:")
print(f"  {len(sheets)} sheets, {len(cards)} cards, {len(meetings)} meetings, "
      f"{len(doc['accounts'])} accounts, {len(doc['stickies'])} notes, "
      f"{len(doc['tasks'])} tasks")

if DROP_EMPTY_SHEETS:
    used = {c["list_id"] for c in cards}
    kept = [s for s in sheets if s["id"] in used]
    dropped = len(sheets) - len(kept)
    doc["daily"]["lists"] = kept
    print(f"  dropped {dropped} sheet(s) holding no cards "
          f"(pass --keep-empty to keep them)")
    sheets = kept

# The file carries account passwords in the clear, so it is always encrypted.
if not pw:
    raise SystemExit("A password is required to encrypt the backup file.")
raw = (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
blob = secure_store.encrypt_backup(raw, pw)

OUT_DIR.mkdir(parents=True, exist_ok=True)
out = OUT_DIR / f"origin_full_{datetime.now().strftime('%Y%m%d_%H%M%S')}.tmbk"
out.write_bytes(blob)

# Read it back the way the app will, so a bad file is caught here and not later.
check = db.parse_backup_payload(json.loads(secure_store.decrypt_backup(
    out.read_bytes(), pw).decode("utf-8")))
t, s, a, dl, dc, mt = check
linked = sum(1 for m in mt if m.get("linked_card_id"))
print(f"\nwrote {out}")
print(f"  {out.stat().st_size / 1024 / 1024:.1f} MB, encrypted")
print(f"  verified by re-reading: {len(dl)} sheets, {len(dc)} cards, "
      f"{len(mt)} meetings ({linked} linked to a job), {len(a)} accounts, "
      f"{len(s)} notes, {len(t)} tasks")
print("\nImport it in the PM app with Data backup > Import from...")
print("Choose REPLACE ALL: it preserves ids, so every meeting keeps its link to")
print("the right job, and your already-imported cards are not duplicated.")
