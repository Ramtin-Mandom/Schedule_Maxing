# Accounts and local-first synchronization in the desktop app

The desktop app is local-first. Every page reads from and saves to the SQLite
database on this computer, with or without an account and with or without a
connection. An account adds synchronization through the authenticated backend
API (`app/sync`, [sync-protocol.md](sync-protocol.md)); the backend is the
only program that talks to PostgreSQL, and the desktop never holds a database
password in this (default) mode.

- **Guest mode** (no account): your work is saved on this device and survives
  restarts. It belongs to nobody yet -- it is waiting for an account, not a
  permanent "local owner".
- **Create an account** inside the app: the workspace you were using stays
  exactly where it is, becomes the new account's, and is uploaded.
- **Signed in:** each save is one local transaction that also records the
  change for upload, so a change is never reported saved without being queued.
  Pending changes are sent automatically and the server's changes are fetched;
  offline, you simply keep working and it catches up later.

## Set up the API-backed workflow

1. **Run the backend** (once, on a server or on this computer). It needs
   `DATABASE_URL` (its PostgreSQL database) and `JWT_SECRET` (at least 32
   characters); see [backend.md](backend.md) and `.env.example`:

   ```bash
   python -m backend.migrate upgrade
   ```

   ```bash
   uvicorn --factory backend.app:create_app --host 127.0.0.1 --port 8000
   ```

   [render-deployment.md](render-deployment.md) describes a hosted backend.
   End users never receive `DATABASE_URL`; they only need the backend's address.
2. **Run the desktop** as usual:

   ```bash
   python -m app.app
   ```

3. **Nothing to point at:** the app already knows its API address
   (`DEFAULT_BACKEND_URL` in `config/settings.py`). Only for development, set
   `SCHEDULE_MAXING_BACKEND_URL` before starting to use another server (for
   example `http://127.0.0.1:8000`), or to `off` for no backend at all.
   **Check connection** only asks the backend whether it answers; it never
   reads, writes or uploads a record.
4. **Create an account** (or sign in) on the same page. From then on nothing
   else is needed: saving is local, and uploading and downloading are automatic.

The desktop never runs, and never needs, the local web service.

**Direct PostgreSQL storage** (`python -m app.app --storage postgres --env-file
.env`, [direct-postgres.md](direct-postgres.md)) uses the same Account page
differently: registering, signing in, the profile and signing out go straight to
the PostgreSQL database with the shared account rules (no backend address, HTTP
or JWT). The app starts signed out on the Account page and checks the database
and its schema revision in the background; **Check database** repeats that. Until
someone signs in, the other pages load nothing. Signing in binds every page to
that account; signing out ends that access and rebuilds the pages, so nothing of
the previous account stays visible and results of work it started are dropped.
The password field is cleared after every attempt and no password is kept. The
backend address, association, synchronization and conflict cards are hidden:
PostgreSQL is the only copy, so there is nothing to associate or synchronize.

## The Account page (sidebar → Account, or **Account** in the status bar)

**Status**

- Shows whether you are signed in and whether the server can be reached.
  There is no address to enter: the app uses its own API.
- **Check connection** tests whether the server answers.
- Nothing is sent until you sign in, so the app still works fully offline.

**Keep me signed in**

- Tick the box above **Sign in** (or **Create account**) to stay signed in
  after closing the app, until you choose **Sign out**.
- What is kept is the session's renewable credential, in the operating
  system's credential store (Windows Credential Manager, macOS Keychain) --
  never your password, and never in the app's database or a file.
- **Sign out** removes it from this computer and ends the session on the
  server. A password reset, or the server's session lifetime
  (`REFRESH_TOKEN_EXPIRE_DAYS`, 30 days by default), also ends it; then you
  sign in once more.
- Without the box, the session lasts while the app is open and is renewed
  in the background, so you are not asked again every few minutes.
- The option needs the `keyring` package (in `requirements-desktop.txt`);
  where no credential store exists the box is disabled.

**Create an account**

- The fields are checked before anything is sent: a valid email and a password
  of 8 to 1024 characters (the server enforces the same rules).
- Creating the account also signs you in and **adopts the guest workspace**:
  every record on this device without an account becomes the new account's, in
  one local transaction (`SyncService.create_account`). Ids, relationships
  (projects, dependencies, series), schedules, preferences and execution
  history are unchanged; each record is queued for upload in that same
  transaction, and the next automatic sync sends them.
- Nothing is cleared, replaced or hidden: the pages show the same work, now
  under the account.
- If it fails, nothing is lost or half-assigned:
  - the registration is refused or the backend is unreachable: nothing changed;
  - the account was created but the sign-in did not complete, or the records
    could not be adopted: they are all still guest records, still visible, and
    nobody is signed in. Sign in and choose to add them (below) to finish.

**Sign in (an existing account)**

- Signing in never changes work done without an account. If this device has
  guest records, the app asks -- in plain words -- what to do with them:
  - **Keep them separate** (preselected): nothing changes. They stay on this
    device outside the account, you see them again whenever you sign out, and
    you can still add them later from "Records on this device without an
    account".
  - **Add them to this account**: exactly those records become the account's
    (same ids and history) and are uploaded. Nothing already in the account is
    overwritten; a record that cannot be added as it is (for example a
    preference for a date the account already has one for) blocks the merge and
    is named, with nothing changed.
- Errors appear next to each field and in words ("The email or password is
  incorrect.", "The backend could not be reached ...").
- The submit button is disabled while a request runs, so a double click sends
  only once.
- Passwords are never stored. The field is cleared after every attempt, and
  the session token lives only in the app's memory.
- Bearer tokens expire after 60 minutes by default (server setting
  `ACCESS_TOKEN_TTL_MINUTES`). When the session ends -- or after a restart --
  the app says so and asks you to sign in again; meanwhile you keep working in
  the account's records, and everything you change is kept and sent afterwards.

**Sign out**

- Signing out is the only thing that leaves an account. It hides that
  account's records on this device and opens the guest workspace (separate,
  and empty unless you kept guest records there).
- Changes that were not sent yet are kept safely with the account and are sent
  the next time you sign in to it. They are never uploaded into another
  account.
- Desktop bearer tokens have no revocation endpoint, so the token simply expires there. The
  app does not claim otherwise.

## Whose records you see (workspaces)

The desktop works on one owner's records at a time. The rule is
`SyncService.workspace_scope()`, described in
[desktop-web-boundaries.md](desktop-web-boundaries.md):

- **Signed in:** that account's records.
- **Not signed in, but an account is active on this device:** that account's
  records. An account becomes active when you sign in (or create it) and stays
  active -- offline, after the session ends, across restarts -- until you sign
  out. Losing the connection is never a sign-out: records keep their owner and
  changes are sent after you sign in again.
- **Otherwise:** guest mode, the records on this device without an account.

While an account is active, every new record is that account's.

Signing in, signing out, associating and switching backends all rebuild the
schedule and productivity pages for the new workspace. Each page keeps its
date. A result from work started for the previous workspace is dropped, so it
never appears in, or changes, the new one.

Signing in to an existing account **never** uploads or claims guest records
by itself; creating a new account adopts them, as described above.

## Associating the records on this device

After you sign in, **Records on this device without an account** shows what
exists: the number of records by type, any deleted ones, and anything that
cannot be associated as it is.

1. Click **Associate these records...** to open the confirmation.
2. **Cancel** changes nothing: no owner, version or queued change.
3. **Associate** makes exactly the previewed records your account's. They
   keep the same ids and history, and they are synchronized from then on.

If the local records changed since the preview, the app refuses, shows the
updated preview, and you confirm again. Another account's records are never
claimed.

## Synchronization status and Sync now

The status bar above every page, and the Synchronization card on the Account
page, say in words:

- the mode: guest, signed in as an account, or an account whose session ended;
- whether the backend is online, unreachable or not checked yet;
- how many changes are waiting to be sent, and whether a sync is running;
- how many conflicts are open;
- when the last successful sync happened, and the last problem if there is one.

These numbers come from the durable sync store and outbox, not from a counter
in the window. They are therefore correct after a restart, before you sign in
again. The status bar refreshes every few seconds. When a sync brings changes
from the server, the visible page re-reads them.

**Automatic sync.** While you are signed in, a background loop
(`SyncService.start`) pushes the outbox and pulls the server's changes:

- every minute when nothing is happening (one small request pair);
- within a few seconds of a local change: the loop looks at the durable count
  of pending records every 5 seconds -- a local query, never a request -- and
  runs when it differs from what the last sync left;
- after a failure, only by bounded exponential backoff (5 s doubling up to 10
  minutes), so an unreachable backend is not hammered. Your changes stay
  queued, also across a restart, and are sent when it answers again.

Only changed records are transferred, each as an idempotent operation with the
version it was based on; deletions travel as tombstones, in dependency order.
A resent operation is recognized by the server, so a lost answer never creates
a duplicate, and a pull never overwrites a record you changed and have not
sent (that becomes a conflict instead).

**Sync now** (in the status bar or on the Account page) runs the same
`SyncService.sync_now()` immediately -- a manual retry, never a requirement.
If a sync is already running, it waits for it; two syncs never overlap.

## Conflicts

A conflict happens when a record changed both here and on the server, or when
the server refused this device's change. It is listed under **Conflicts**.
Select a conflict to compare the two versions field by field (**This device**
against **Server**), with versions and change times. Only the decisions the
sync service supports are offered:

- **Use the server's version** (accept remote).
- **Keep this device's version** (keep local). This is unavailable, with the
  reason shown, when the server deleted the record, because keeping it would
  silently bring it back. It is also unavailable when another record owns the
  same scope on the server.

There is no merge, and nothing is overwritten silently (no last-write-wins).

## Recovery

| What you see | What to do |
| --- | --- |
| "Session ended — sign in again to synchronize" | Sign in again. Queued changes are sent afterwards |
| Forgot your password | **Forgot password?** under Sign in: request a recovery link, then paste the link (or its code) and choose a new password. The answer never says whether the account exists. Every older session ends; sign in again, and your unsynced work is still here. A device that only works offline has no account password to recover |
| "backend unreachable" | Keep working offline. Changes are queued, and **Check connection** or **Sync now** retries later |
| "The backend refused the request: …" | The server rejected that request. The message names the reason |
| "Your local records changed since this preview" | Review the updated preview and confirm again |

Closing the window while a network request is still running is safe. The app
waits (bounded) for the request to finish, then closes the database. Nothing
is delivered to the closed window.

## For developers

- `app/ui/account_controller.py`: the Tk-free `AccountController`. It
  validates input, turns failures into readable messages, and builds the
  `ConnectionView` and `ConflictView` view models.
- `app/ui/account_page.py`: the Account page widgets.
- `app/ui/shell.py`: the `StatusBar`.

Tests:

- `tests/sync/test_desktop_account_controller.py` covers:
  - registration, sign-in and sign-out, profile, and an unreachable backend;
  - association preview, cancel, stale preview and confirm;
  - durable pending changes and last sync across a restart;
  - lost-response retry and session expiry;
  - two devices, deletion, and the allowed conflict decisions;
  - backend and account isolation.
- `tests/sync/test_local_first_accounts.py` covers the local-first workflow:
  - guest work across a restart, and a new account adopting and uploading it
    (same ids, schedule included; a second client sees it; no duplicates);
  - a refused registration, a sign-in lost after the account was created, and
    an adoption interrupted mid-transaction: the guest workspace is unchanged;
  - an existing account with guest data: kept separate by default, merged only
    when chosen;
  - offline edits across a restart, a lost answer, and upload exactly once;
  - sign-out hiding an account, its pending changes kept, and nothing uploaded
    into another account;
  - a connection check changing no record, and the idle loop noticing new
    pending work without a request.
- `tests/sync/test_desktop_account_page.py` covers the same flows as real
  widgets (creating an account, the guest-data choice), plus closing during a
  network call.
