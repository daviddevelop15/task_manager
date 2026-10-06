# Task Manager

A small **PyQt6** desktop app for tracking **links** (URLs or short notes) with a **status workflow**, **multiline descriptions**, and **SQLite** storage. Window layout, filters, and “always on top” are remembered via **Qt settings** on your machine.

## Requirements

- **Python 3.10+** (uses modern union type syntax)
- Windows is the primary target; the UI and build script assume Windows paths.

## Run from source

```powershell
cd path\to\Task-manager
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

## Data files

| What | Where |
|------|--------|
| Tasks (links, created time, status, description) | `task_manager.db` in the project directory (same folder as `database.py`) |
| Window geometry, column widths, filter toggles, always-on-top | OS user settings (`QSettings` org/app: `TaskManager` / `TaskManager`) |

`task_manager.db` is listed in `.gitignore` so your local database is not committed.

## Using the app

- **Add a link**: Type or paste into the field, then **Add link** or press **Enter**. **Paste** fills the field from the clipboard.
- **Edit in the table**: Change **status** from the combo, edit **description** (double-click for a multiline editor). **Link** and **Date / time** are read-only after creation.
- **Search**: Filters rows by partial text across link, description, status, date/time, or numeric id.
- **Created date filters** (mutually exclusive):
  - **Filter by created date**: Custom **From** / **To** range.
  - **Daily window filter**: Tasks created from **previous day 17:00** through **selected day 13:00** (useful for a fixed “business day” slice).
- **Remove**: Select one or more rows, then **Remove selected row(s)**.
- **View → Always on top**: Keeps the window above others when enabled.

### Status values

Allowed statuses (enforced in the database): `initial`, `invited or contacted`, `closed`, `created`. Rows are tinted by status for quick scanning.

## Build a Windows executable

The release build uses **PyInstaller**. You need an app icon at **`resources\app_icon.png`** (the build script fails fast if it is missing).

```powershell
.\build.ps1
```

Output: `dist\TaskManager\TaskManager.exe` (folder distribution with bundled `resources`).

`requirements-build.txt` adds PyInstaller; `TaskManager.spec` is available if you prefer editing a spec and running `pyinstaller TaskManager.spec` directly.

## Project layout

- `main.py` — UI and filters
- `database.py` — SQLite schema and CRUD
- `build.ps1` — install deps and run PyInstaller
- `resources/` — `app_icon.png` for dev icon and packaging (add locally if absent)
