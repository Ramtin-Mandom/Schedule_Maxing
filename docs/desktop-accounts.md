# Accounts, association and synchronization in the desktop app (Milestone 4, Prompt 2)

The desktop app works without an account and without internet. Connecting to
a backend is optional. The app then signs you in and keeps your account's
records synchronized through the existing sync client (`app/sync`,
[sync-protocol.md](sync-protocol.md)).

The desktop never runs, and never needs, the local web service. In the
default (local) storage mode it never holds a database password.

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

**Connection**

- Enter the backend address (`https://…`, or `http://` for a server on this
  computer), then **Save address**. The address is kept in the database's
  `local_settings`. `SCHEDULE_MAXING_BACKEND_URL` still overrides it.
- **Check connection** tests whether the backend answers.
- **Work offline** removes the backend. This ends the session but changes no
  records.
- Switching to another backend also ends the current session first.

**Create an account / Sign in**

- The fields are checked before anything is sent: a valid email, and a
  password of 8 to 1024 characters for a new account (the server enforces the
  same rules).
- Errors appear next to each field and in words ("The email or password is
  incorrect.", "The backend could not be reached …").
- The submit button is disabled while a request runs, so a double click sends
  only once.
- Passwords are never stored. The field is cleared after every attempt, and
  the session token lives only in the app's memory.
- Bearer tokens expire after 60 minutes by default (server setting
  `ACCESS_TOKEN_TTL_MINUTES`). Sign in again when the session expires or after
  restarting; the active account's data remains available offline.

**Sign out**

- Signing out forgets the session on this device. Your records stay on the
  device.
- Desktop bearer tokens have no revocation endpoint, so the token simply expires there. The
  app does not claim otherwise.

## Whose records you see (workspaces)

The desktop works on one owner's records at a time. The rule is
`SyncService.workspace_scope()`, described in
[desktop-web-boundaries.md](desktop-web-boundaries.md):

- **Signed in:** that account's records.
- **Not signed in, but an account is active on this device:** that account's
  records. An account stays active after association, even across a restart,
  until you sign out. You keep working offline, and changes are sent after you
  sign in again.
- **Otherwise:** the records on this device without an account (the ownerless
  workspace).

Signing in, signing out, associating and switching backends all rebuild the
schedule and productivity pages for the new workspace. Each page keeps its
date. A result from work started for the previous workspace is dropped, so it
never appears in, or changes, the new one.

Records you create while signed in belong to your account. Signing in **never**
uploads or claims the older ownerless records.

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

The status bar above every page says, in words:

- whether a backend is configured, whether you are signed out or signed in,
  whether the session has ended, and whether the backend is unreachable;
- how many changes are waiting to be sent;
- how many conflicts are open;
- when the last successful sync happened.

These numbers come from the durable sync store and outbox, not from a counter
in the window. They are therefore correct after a restart, before you sign in
again. The status bar refreshes every few seconds. When a sync brings changes
from the server, the visible page re-reads them.

**Sync now** (in the status bar or on the Account page) runs the same
`SyncService.sync_now()` as the background loop. If a sync is already
running, it waits for it; two syncs never overlap. Failures keep your changes
queued, and the next sync retries them with bounded backoff. The server
recognizes a resent change, so a lost answer never creates a duplicate.

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
- `tests/sync/test_desktop_account_page.py` covers the same flows as real
  widgets, plus closing during a network call.
