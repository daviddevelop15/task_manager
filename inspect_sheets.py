"""Read-only look at a build's database: which sheets exist, and on which day.

Uses the password from doc/.env.var (TM_DB_PASSWORD), the same file
analyze_jobs.py reads. Nothing is written.
"""
import sys
from pathlib import Path
sys.path.insert(0, r"e:\Task-manager")

import database as db
import secure_store

WHICH = sys.argv[1] if len(sys.argv) > 1 else r"dist-dev\Server"


def find_internal(dist: Path) -> Path:
    """The app folder inside a dist directory, whatever the build is called.

    The variants ship as TaskManager_PM and TaskManager_Dev, so this looks for
    whichever folder actually holds a database rather than assuming a name.
    """
    for candidate in sorted(dist.glob("*")):
        internal = candidate / "_internal"
        if (internal / "task_manager.vault").exists():
            return internal
    return dist / "TaskManager" / "_internal"      # nothing found; report on this


APP = find_internal(Path(r"e:\Task-manager") / WHICH)


def password_from_env_file() -> str:
    path = Path(r"e:\Task-manager\doc\.env.var")
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


db._base_dir = lambda: APP
vault = APP / "task_manager.vault"
mode = secure_store.vault_mode(vault)
print(f"{WHICH}: vault mode = {mode}")
pw = None
if mode == "password":
    pw = password_from_env_file()
    if not pw:
        import getpass

        pw = getpass.getpass(f"Password for the {WHICH} database (not stored): ")
try:
    dek = secure_store.unlock(vault, pw)
except Exception as exc:
    print(f"  cannot unlock: {exc}")
    raise SystemExit(1)
db.unlock(dek)

lists = db.list_all_daily_lists()
cards = db.list_all_daily_cards()
print(f"\nTOTAL IN THE DATABASE: {len(lists)} sheets, {len(cards)} cards\n")

per_list = {}
for c in cards:
    per_list[c.list_id] = per_list.get(c.list_id, 0) + 1

print(f"{'sheet day':<14} {'always':<7} {'cards':>6}  title")
print("-" * 72)
for l in sorted(lists, key=lambda x: (x.day, x.title)):
    print(f"{l.day:<14} {'yes' if l.always_shown else '-':<7} "
          f"{per_list.get(l.id, 0):>6}  {l.title}")

days = sorted({l.day for l in lists})
print(f"\ndistinct days that hold sheets ({len(days)}): {days}")

board_day = "2026-07-22"
shown = db.list_daily_lists_for_board(board_day)
visible_cards = sum(per_list.get(l.id, 0) for l in shown)
print(f"\nOn the {board_day} board the app shows {len(shown)} sheet(s) "
      f"/ {visible_cards} cards:")
for l in shown:
    print(f"   {l.title}  (day={l.day}, always_shown={bool(l.always_shown)}, "
          f"{per_list.get(l.id, 0)} cards)")
print(f"\nNOT visible from that day: {len(lists) - len(shown)} sheet(s) / "
      f"{len(cards) - visible_cards} cards")
