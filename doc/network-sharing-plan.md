# Networked Task Manager: Server App (PM) + Client App (devs)

## Context

Task Manager is a **single-user PyQt6 desktop app** with one encrypted SQLite store
(`database.py`). It becomes a small team tool: the **project manager runs a server app** that
owns the database, and **developers run a client app** with the same native UI. The app shell is
reorganised at the same time. Confirmed setup:

- **One room, one LAN.** All PCs on the same trusted network; nothing leaves it.
- **The PM's machine is the server**, on while the team works, and owns all data.
- **Two desktop builds from one codebase** — `TaskManager-Server` (PM) and `TaskManager-Client`
  (devs); developers get the **full native UI**, not a web page.
- **Everyone logs in** with username + password; the server verifies the request's **source IP**
  against that user's registered IP.
- **The PM manages users** but **cannot reset a dev's password** — it can only issue a
  **recovery code** the dev redeems to set their own password.
- **New tab layout:** the **Tasks tab is removed entirely**; a new **Collabor Board** tab is added
  (empty placeholder). Order: **Daily · Meetings · Collabor · Notes · Acc**.
- Clients **self-update from the PM's server**, so daily changes reach everyone without manual
  reinstalls.
- **Stdlib only** (`http.server`, `json`, `hashlib`/scrypt, `urllib`, `zipfile`, `ssl` seam) →
  **no new dependencies**.

From exploration: DB access is synchronous `db.*` on the GUI thread; `daily_cards`/`meetings`
have `created_at`/`updated_at` but **no author column**; IDs are local `AUTOINCREMENT` (server
owns shared IDs); the whole DB is already encrypted at rest via `secure_store`; `build.ps1`
already preserves runtime data across rebuilds via a fixed `preserveNames` list.

## Data model & access control (the heart of the design)

Three tiers, enforced **server-side** from the caller's token on every request:

| Tier | Tables | Read | Create | Update | Delete |
|---|---|---|---|---|---|
| **Shared boards** | `daily_lists`, `daily_cards`, `meetings` | everyone | everyone | everyone | everyone |
| **Private (owner-only)** | `accs`, `stickies` | **owner only** | owner | owner only | owner only |
| **Shared resume library** | `resumes` | everyone | everyone (upload) | **PM any · dev own only** | **PM any · dev own only** |

- Shared records stamp **`created_by` / `updated_by`** — the creator is shown and filterable.
- Private records carry **`owner_user_id`**; the server hard-filters every query by it, so the app
  never displays or exposes another user's Acc/Notes.
- Resumes carry **`uploaded_by`**: everyone reads and uploads; **a dev can neither edit nor delete
  a PM-uploaded resume** — only their own; the **PM manages any**.

> ⚠️ **Accepted privacy trade-off.** Private data is protected by **application-level access
> control** on top of the existing whole-DB encryption. That encryption uses the **server's key,
> which the PM holds**, so it stops someone copying the `.enc` file but does not hide dev data
> from the PM inspecting the DB directly. Per your decision this is accepted: **the app will not
> display another user's Acc / Notes.**

## Architecture: one codebase, three modes, two builds

| Mode | Who | Data | Network |
|---|---|---|---|
| `standalone` | solo use (unchanged) | local encrypted DB | none |
| `server` | **PM** | **owns the DB**; reads/writes locally | hosts HTTP API, user admin, update host |
| `client` | **developers** | **none stored locally** — all data via the server | logs in, polls, self-updates |

**Key property of client mode: the client persists no data on disk.** Shared boards, the dev's own
private Acc/Notes, and the resume library all live on the PM's server; the client keeps them in
memory for the session only. So a dev PC holds **nothing at rest** (good if a laptop is lost), the
client needs no vault/unlock flow, and updates never have to preserve a local database.

### A. App shell: tab restructure
- **Remove the Tasks tab entirely** — the task table, link input, task search, date/daily filters
  and their QSettings keys come out of `main_window.py`.
  **Non-destructive:** the `tasks` table and existing rows are **left untouched** in the DB (UI
  only), so nothing is lost and it can be exported or purged later.
  `task_helpers.py` **stays** (its `normalize_search` / `fields_match_search` are used by the
  daily and meetings pages); `delegates.py` likely becomes unused and can be retired separately.
- **Add a `Collabor Board` tab** — `collabor_board.py` placeholder page (empty widget, honouring
  the same `shutdown()` / count-signal contract as the other pages).
- **Final order:** `Daily · Meetings · Collabor · Notes · Acc` ("Sticky notes" relabelled
  **Notes**). Remap `_refresh_status_bar_for_current_tab` to the new indices.
- Identical in both builds; only the server adds Users + Network config.

### B. The data seam: `app_store.py`
Every page stops calling `db.*` directly and goes through one facade:
- **`LocalStore`** (`standalone` / `server`) — the existing `db.*` calls plus author stamping,
  owner scoping, and resume permissions; guarded by an `RLock` (HTTP + GUI threads).
- **`RemoteStore`** (`client`) — the same method surface over HTTP with a bearer token; returns
  the existing `DailyCardRow` / `MeetingRow` / `DailyListRow` / acc / sticky / resume dataclasses,
  so page code is unchanged downstream.
- `change_version()` — monotonic counter bumped on every shared write, for polling.

Call sites to migrate: `daily_sheet_page.py`, `daily_board_page.py` (DailyCardEditDialog),
`meetings_page.py`, `accounts_page.py`, `sticky_notes.py`, `resumes_ui.py`, plus `job_match.py`
and the similar-jobs detector (`list_all_daily_cards`).

### C. Shared boards (Daily + Meetings)
1. **Authorship columns** via the `try/except sqlite3.OperationalError` ALTER pattern in
   `database.py:init_db()` (kept in sync with CREATE bodies, `_MEETING_COLS`, parsers,
   dataclasses): `created_by`, `updated_by` on `daily_cards`, `meetings`, `daily_lists`. Stamped
   from the **token's user** on the server — unspoofable.
   **One-time backfill:** all pre-existing rows (the ~2,174 daily cards and current meetings)
   are attributed to the **PM**. It runs when the PM account is first created — the username
   isn't known at ALTER time — and fills only blanks
   (`UPDATE … SET created_by=?, updated_by=? WHERE created_by=''`), so it is idempotent and safe
   to re-run.
2. **Creator / Applier column + filter**
   - Daily board: a **"Creator / Applier" column at the front of every row**, from `created_by`
     — aligns with the existing "Applier" concept.
   - Daily **and** Meetings get a **Creator filter** (dropdown of users).

### D. Private data (Acc + Notes) and E. Resume library
`accs`/`stickies` gain `owner_user_id`; `resumes` gains `uploaded_by` (ALTER-migrated). The server
scopes every private query to the caller (`WHERE owner_user_id = :token_user`) and enforces the
resume matrix above; the client UI additionally hides edit/delete on resumes the user may not
touch. Existing account password-field encryption (`acc_crypto`) is unchanged.

### F. Authentication, IP restriction, user management
- **`sync_users`:** `id, username UNIQUE, display_name, role (pm|dev), pw_hash, pw_salt,
  allowed_ips TEXT, enabled, created_at, last_seen_at, recovery_hash, recovery_expires_at`.
  Passwords are **scrypt hashes** — the PM cannot read them.
- **Login:** `POST /api/login {username, password}` → verify hash, **then verify the source IP** ∈
  `allowed_ips`. The IP comes from the **TCP connection**
  (`BaseHTTPRequestHandler.client_address`) — **never** from a client-supplied field or header, so
  a client cannot claim an IP. (Assumes direct LAN connections, no reverse proxy.) Returns a bearer
  token bound to `{user, source_ip}`, re-checked on every request.
- **Forgotten password:** the PM issues a **one-time, expiring recovery code** (stored hashed); the
  dev redeems it via `POST /api/recover {username, code, new_password}`.
- **Users admin (PM only):** CRUD users — username, display name, role, allowed IP(s),
  enable/disable, issue recovery code — plus last-seen/online. Settings menu → "Users…".
- **Client login UI:** on startup the client prompts for server address + username + password
  (address/username remembered in QSettings; password optionally remembered), shows
  "connected as X" or a clear failure reason (bad credentials / unregistered IP / server down).

### G. Server service: `sync_server.py`
`ThreadingHTTPServer` on a background thread, started by `MainWindow` in server mode. JSON
endpoints mirroring `app_store` for shared boards, own-private data, and resumes; `GET
/api/changes?since=<v>` for cheap polling (also refreshes `last_seen`); auth + IP enforcement on
every request; all handlers take the `LocalStore` `RLock` and stamp author from the token.
A **Network / Sharing** section in `AppSettingsDialog` sets mode, bind host/port, display name,
service on/off, and shows the LAN URL to hand out.

### H. Client behaviour: polling + transient offline
- **Polling** — a `QTimer` (~3–5 s) hits `/api/changes`; when the version advances the client calls
  the existing `refresh_board()` / meetings `refresh()` entry points.
- **Transient disconnects** — the last-fetched data stays visible **read-only** with a clear
  "disconnected" indicator; writes attempted while offline are held in an **in-memory queue** and
  replayed in order on reconnect, then a refresh. Because nothing persists on disk, closing the
  client while offline discards the queue (documented, acceptable in one room).
- Conflicts: **server is the source of truth, last-write-wins** (it stamps `updated_at`/`updated_by`).

### I. Auto-update (client pulls from the PM's server)
The PM's build **is** the client build, so the PM's machine distributes updates — no hosting.
- **`app_version.py`** holds `APP_VERSION`, bumped per release and embedded in the build.
- **Server:** `GET /api/version` → `{version, sha256, size}`; `GET /api/update/download` → a zip of
  the server's own app folder **excluding** the runtime-data files (reuse `build.ps1`'s
  `preserveNames`), built lazily and cached per version.
- **Client flow** (gated by an "auto-update" setting): on launch (and on the poll heartbeat) compare
  versions; if different, download to a temp staging dir and **verify the SHA-256**; launch
  **`updater.py` as a detached process** (from outside the app folder) and quit so file locks
  release; the updater **waits for the exe to exit**, swaps the folder, and relaunches.
  Because the client stores no database, the swap is a plain folder replace — but it still
  preserves any `preserveNames` files if present. Any failure aborts safely, leaving the existing
  install intact, and surfaces a "download ready — restart to finish" fallback.
- Windows file locks (Defender/indexer — hit repeatedly this session) are handled with waits and
  retries, mirroring `build.ps1`.

### J. Developer migration: one-time upload of existing local data
Each developer already runs the standalone app with their **own local encrypted DB**. Moving to
the client build must carry that data to the server, attributed to them.
- **Trigger:** after the dev's **first successful login** in client mode, if a legacy local DB is
  found, the client offers **"Import my existing data"** (also re-runnable from Settings).
- **Unlock:** the dev unlocks their **legacy local vault once** (password or DPAPI, exactly as the
  old app did) so the client can read the old DB read-only.
- **Preview + selection:** show per-type counts (daily cards, meetings, Acc, Notes, resumes) and
  let the dev choose what to bring.
- **Upload:** batched `POST /api/migrate/import` (chunked — a dev may have thousands of cards),
  with `GET /api/migrate/status` for progress/idempotency.
- **Attribution:** the server stamps `created_by` / `owner_user_id` / `uploaded_by` **from the
  authenticated token's user** (looked up in `sync_users` by the username+password+IP login) —
  the client cannot set or spoof the author. This is the same rule as normal writes.
- **ID remapping (important):** local `AUTOINCREMENT` ids will collide with server ids. Import in
  order — sheets first, keeping an **old→new id map**, then cards with remapped `list_id`, then
  meetings with remapped `linked_card_id` — so relationships survive.
- **Sheet merging:** the daily board is now shared, so an imported sheet **merges into an existing
  shared sheet matching (day, title)** and is created only if absent; cards keep their own
  creator, so the Creator/Applier column keeps everything attributable without duplicate sheets.
- **Idempotency:** a per-user `migrated_at` on `sync_users` plus a client-side marker; re-running
  reports "already imported" rather than duplicating rows.
- **Not migrated:** `tasks` (the tab is removed). The legacy local DB is **archived, never
  deleted**, so the dev keeps a full backup of their pre-migration state.

## Files
- **New:** `app_store.py` (Local/RemoteStore facade), `migration_ui.py` (dev import wizard),
  `sync_server.py`, `sync_users.py`
  (accounts, IP check, recovery codes), `user_admin_ui.py` (Users dialog), `client_login_ui.py`,
  `collabor_board.py` (placeholder), `app_version.py`, `updater.py`.
- **Modify:** `database.py` (`created_by`/`updated_by` on shared tables; `owner_user_id` on
  `accs`/`stickies`; `uploaded_by` on `resumes`; migrations kept in sync; access lock);
  `main_window.py` (**remove Tasks**, add Collabor, reorder/relabel tabs, remap status bar, mode
  branch, start server/poll/update-check, Users + Network UI, connection indicator);
  `daily_sheet_page.py`, `daily_board_page.py`, `meetings_page.py` (route through `app_store`;
  Creator/Applier column + Creator filter; authorship display); `accounts_page.py`,
  `sticky_notes.py`, `resumes_ui.py` (route through `app_store`; permission-aware controls);
  `main.py` (mode branch, client login before MainWindow); `job_match.py` + similar-jobs detector.
- **Build:** `build.ps1` emits **two labeled variants** (`TaskManager-Server`,
  `TaskManager-Client`) from the same code, differing only by a bundled default-mode marker;
  ensure `app_version.py` and `updater.py` ship in both. No new dependencies.

## Risks / notes
- **Server availability:** clients can work only while the PM app is running (chosen topology);
  `sync_server` is structured so it could run headless later.
- **Self-replace on Windows** is the highest-risk piece; mitigated by a detached updater, waits +
  retries, SHA-256 verification, and a safe abort that never corrupts an install.
- **Concurrency:** HTTP threads + GUI thread serialize on the `LocalStore` `RLock`; the SQLite
  keeper already uses `check_same_thread=False`.
- **IP restriction caveats:** LAN IPs are spoofable at the network layer and **DHCP can reassign
  them** — use **static IPs / DHCP reservations**; support multiple IPs per user.
- **Tasks removal** is UI-only and reversible; no task data is deleted.
- **Dev migration is a one-way data merge** into the shared board — hence the idempotency guard,
  the (day, title) sheet-merge rule, and keeping each dev's legacy DB archived as a backup so a
  bad import can be re-done from a known state.
- **Migration / backfill:** columns are added in place (no data moved, nothing rewritten
  destructively) and default to `''`/NULL. Then a **single one-time backfill, run when the PM
  account is created, attributes every pre-existing row to the PM**: `created_by`/`updated_by` on
  all existing daily cards, meetings and sheets, `owner_user_id` on existing Acc/Notes, and
  `uploaded_by` on existing resumes. It only fills blanks, so it is idempotent. Result: the
  Creator/Applier column and Creator filter are meaningful across all history from day one.
- LAN plain HTTP per your choice; an `ssl` seam is left if this ever leaves the room.

## Development setup (before any code changes)

1. **Secure the current work first.** This session's finished features (meeting↔job linking,
   similar-jobs detector, ICS timezone fix, Sunday-first calendar, F2 paste, richer AI extraction,
   `analyze_jobs.py`, `job_match.py`, `doc/`) are **uncommitted** — commit them to `main` so it
   holds the working single-user app.
2. **Tag that commit** (e.g. `v1-standalone`) as a known-good restore point to return to at any time.
3. **Create the feature branch** (e.g. `feature/network-server-client`) and do all rewrite work there.
   `dist/` is already gitignored, so **branch switching never touches the live 31 MB database**.
4. **Build the branch to a separate output folder.** `build.ps1` gains a `-DistPath` parameter
   (default `dist`); dev builds use `-DistPath dist-dev`, so `dist\TaskManager` — the live app and
   its real data — stays untouched and usable for daily work. Add `dist-dev/` to `.gitignore`.
   The dev build starts with its **own empty database**, which is ideal: server/client testing
   never risks the real 2,174-record dataset. For two-instance testing, build the two variants
   into `dist-dev\Server\` and `dist-dev\Client\`.
5. **Suggested build order** (each step independently testable): schema + `app_store` facade →
   tab restructure → users/auth + server API → client mode + polling → resume/private permissions →
   dev migration import → auto-updater.

## Verification
- **Unit:** owner-scoped queries (User B cannot read/update/delete User A's Acc or Note); resume
  permissions (dev edits/deletes own ✓, dev edits/deletes PM's ✗, PM any ✓); author stamping +
  `change_version` bumps; scrypt hash/verify; recovery code (single-use, expiring).
- **API (in-process):** `sync_server` on `127.0.0.1:<port>` — login from a **wrong IP is rejected**;
  a **client-supplied IP header is ignored**; shared CRUD stamps `created_by` = token user; private
  endpoints return only the caller's rows; `/api/changes` advances.
- **RemoteStore:** drive the client store against the in-process server for every page's data;
  test the transient-offline queue (writes while the server is down → replay + refresh on reconnect).
- **Auto-update:** point a client at a server with a bumped `APP_VERSION`; assert download, SHA-256
  verification, that `updater.py` (temp dirs) swaps correctly and relaunches, and that a simulated
  lock aborts safely without damaging the install.
- **Migration/backfill (on a copy of the real DB):** after adding the columns and creating the PM
  account, every existing daily card, meeting, sheet, Acc, Note and resume is attributed to the
  PM; re-running the backfill changes nothing (idempotent); rows created afterwards keep their
  own author, not the PM's.
- **Dev migration:** import a legacy dev DB through the client — every imported row lands with
  **that dev** as creator/owner (not the PM); `list_id` / `linked_card_id` relationships survive
  the id remap; sheets merge by (day, title) instead of duplicating; re-running imports nothing;
  the dev's legacy DB is left intact as a backup.
- **App shell (offscreen):** tabs are exactly **Daily, Meetings, Collabor, Notes, Acc** in order;
  **no Tasks tab**; status bar counts per tab; Collabor opens empty; task rows still in the DB.
- **Negative tests:** forged token / other-user id → denied; unregistered IP → denied; dev editing
  or deleting a PM resume → denied.
- **End-to-end (manual, two instances on the LAN):** PM server + dev client; create/edit shared
  records both ways and confirm creator/updater within a poll; confirm each user sees only their
  own Acc/Notes; bump the server version and watch the client self-update and relaunch; stop the
  server mid-edit to see the offline indicator and reconnect sync.
- Rebuild via `.\build.ps1`; confirm both variants launch and **standalone still works**.
