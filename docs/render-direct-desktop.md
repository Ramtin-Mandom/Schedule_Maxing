# Render Postgres with the direct desktop mode

This guide connects the desktop app's **direct PostgreSQL storage**
([direct-postgres.md](direct-postgres.md)) to a **Render Postgres** database,
and verifies it with an opt-in command. It is a different setup from
[render-deployment.md](render-deployment.md), which deploys the HTTP API as a
Render web service (that one needs `JWT_SECRET` and a web service; this one does
not). Both use the same schema and services, so the same database can later be
served by the API.

> **Private development only.** The direct mode puts the database password on
> your computer (in `.env`). Anyone with that file can read and change every
> account's data. Never give it, or an app configured with it, to other people;
> other users should get the HTTP API instead.

What this milestone needs from Render: one Render Postgres instance and its
**external** connection URL. It does **not** need a web service, a JWT secret or
email verification.

## 1. Prerequisites

- A Render Postgres instance whose status is **Available**.
- Its **External Database URL** (dashboard → the database → **Connect** →
  External) in the project's `.env`, which is git-ignored:

  ```
  DATABASE_URL=postgresql://USER:PASSWORD@HOST.REGION-postgres.render.com/DATABASE
  ```

  Paste the full URL. Use the hostname, never an IP address it resolved to:
  Render routes external connections by the TLS server name (SNI) and refuses
  connections without it (`No SNI information found`).
- The optional packages: `python -m pip install -r requirements-direct.txt`.

## 2. How the app connects

- **TLS.** Render encrypts external connections with Render-managed TLS
  certificates and refuses `sslmode=disable`. The app requires TLS for any remote
  host: without an `sslmode` it uses `require`; `verify-ca`/`verify-full` (with
  `sslrootcert=...`) are kept; `disable`/`allow`/`prefer` are refused. The `.env`
  file is never rewritten.
- **Inbound IP rules.** By default a Render Postgres instance accepts external
  connections from any IP address (`0.0.0.0/0`) with valid credentials. Restrict
  it to your own public IPv4 address as a `/32` rule (dashboard → the database →
  **Networking** → **Inbound IP Restrictions**; only IPv4 ranges are supported),
  and update the rule when your address changes. A connection from an address
  that is not allowed fails with "The database server could not be reached".
- **Connection budget.** Each desktop window keeps at most 5 connections (3 plus
  2 overflow); the migration and verification commands use one at a time. Render
  allows 100 connections on instances with less than 8 GB of RAM, so this is far
  below the limit even with several windows open.
- **Free instances** expire 30 days after creation (then 14 days to upgrade
  before deletion), hold 1 GB, can be restarted by Render at any time, and have
  **no backups** of any kind. Use them only for trying things out.

## 3. Check the connection (writes nothing)

```bash
python -m app.persistence.verify_render --env-file .env --check-only
```

It reports where `DATABASE_URL` came from, the TLS mode, the schema revision and
the revision this version needs. It creates no table, account or row. Exit code
0 = schema current, 1 = migration needed, 2 = failure (with a safe message).

## 4. Migrate the schema explicitly

The app and the verifier never migrate. Check, back up, then upgrade:

```bash
python -m backend.migrate --env-file .env current
python -m backend.migrate --env-file .env upgrade
python -m backend.migrate --env-file .env check
```

`--env-file` goes before the action. Before `upgrade` on a database that
already holds data:

1. **Take a backup.** Paid instances: dashboard → **Recovery** → **Create
   export** (kept for seven days). Free instances cannot export from the
   dashboard; use `pg_dump` (its major version must be at least the server's).
   In PowerShell, without printing the URL:

   ```powershell
   $env:PGDUMP_URL = ((Select-String -Path .env -Pattern '^DATABASE_URL=').Line -replace '^DATABASE_URL=', '')
   pg_dump --format=custom --file=schedule_maxing_backup.dump --dbname=$env:PGDUMP_URL
   Remove-Item Env:PGDUMP_URL
   ```

   (The URL is briefly visible to local process listings while `pg_dump` runs.
   Keep the dump file private: it contains the data and password hashes.)
2. **Stop older writers.** Revisions 0004-0006 replace JSON columns that older
   application versions use, so suspend any Render web service running an older
   version of this code before upgrading (see
   [backend.md](backend.md#normalized-storage-0004-0006)). A new, empty database
   needs neither step: `upgrade` simply creates the schema.

The whole upgrade runs in one transaction: if any existing row cannot be
converted exactly, nothing changes and the error names the row and field.

## 5. Verify with a sample account (opt-in writes)

```bash
python -m app.persistence.verify_render --env-file .env --write-sample --email Ramtin1383.5@gmail.com --display-name Ramtin --anchor-date 2026-09-28 --timezone America/Vancouver
```

The password is asked with a hidden prompt (twice when the account is new); it is
never an argument, a default or a file. Then run the **same command again**: the
second run must report `seed already seeded`, `generation already_current` and
`0 new change-log entries`.

What it does, in short transactions (none open while you type the password or
while the optimizer runs):

1. **Account.** Created if absent (normalized email, Argon2id hash). If it exists,
   the password must match; a wrong password stops everything, and an existing
   password is never reset.
2. **Sample.** `samples/inputs/valid_single_day_basic.csv` -- checked to contain
   3 flexible tasks and 4 fixed blocks on day 1 with **no dependencies** -- is
   converted by the ordinary legacy importer onto `--anchor-date` in
   `--timezone` (category, tag, duration, priority, date preference and preferred
   window kept) and given stable ids derived from the fixture version, the
   account, the date, the time zone and the CSV line. Missing records are
   created; identical ones are left alone; records you edited or deleted are
   reported as a conflict and never overwritten. A date that already holds other
   tasks or fixed blocks of the account (including undated tasks, which
   generation would schedule there) is refused: pick another `--anchor-date`.
3. **Preferences.** A neutral layer for that date only (the Normal engine, study
   multiplier 1.0); an existing, different layer is reported, not replaced.
4. **Schedule.** Generated with the shared planning workflow (fingerprints,
   allocation id, engine mode, freshness); an unchanged date is "already
   current" and nothing is written.
5. **Execution.** One *scheduled* execution for the Study Session placement --
   no started or completed work is invented (completed work is tested with an
   injected clock on throwaway databases, never in your account).
6. **Read-back.** The connection pool is closed and a new one signs in again and
   reads everything through the app's services: ids, owner, values, placement
   validity (inside the day, no overlaps, dependencies in order) and freshness.
7. **Password scope.** The stored credential is a valid Argon2id hash that
   verifies; the schema has no plaintext credential column; the change-log
   snapshots of the seeded records and this run's log messages do not contain the
   password. This covers this account and this run -- it is not a database-wide
   audit, and the password is never sent in a search query.

Optional dependency fixture, on a different date:

```bash
python -m app.persistence.verify_render --env-file .env --write-sample --email Ramtin1383.5@gmail.com --display-name Ramtin --anchor-date 2026-09-28 --timezone America/Vancouver --dependency-date 2026-09-29
```

It seeds `samples/inputs/dependency_chain_linear.csv` (Task A -> B -> C -> D)
with the same protections.

**Output** is a safe summary: `SUCCESS`/`FAILURE`, the account name and
normalized email, TLS mode and revision, the seed date and time zone, the task,
fixed-block, placement and execution counts, dependency applicability, the
preference/freshness checks, the reopened-connection result, idempotence and the
password-scope result -- never a URL, host, password, hash or SQL. Every step is
listed as `ok`, `FAILED` or `not run`; if a run fails after earlier steps
committed, running it again resumes safely.

Exit codes: 0 success; 1 a conflict (nothing overwritten); 2 a failure.

## 6. Use the desktop app

```bash
python -m app.app --storage postgres --env-file .env
```

Sign in on the Account page with the same account; the seeded day appears on
the Day page. See [direct-postgres.md](direct-postgres.md#the-desktop-app-in-direct-mode).

## Messages you may see

| Message | Meaning |
| --- | --- |
| `DATABASE_URL asks for sslmode=disable ...` | Remove `sslmode` or use `require`/`verify-full`. |
| `The database server could not be reached. Check the network connection, the server's inbound IP allowlist, TLS settings and DATABASE_URL ...` | Network, IP rule, a stale host, or the instance is not available. |
| `The database refused the credentials in DATABASE_URL.` | Wrong user or password in the URL (it was rotated?). |
| `The database has no schema yet` / `... is at schema revision X` | Run the migration commands of section 4. |
| `The password does not match the existing account ...` | Use that account's password; nothing was changed. |
| `... already holds N other task(s)/fixed block(s) ...` | Choose another `--anchor-date`. |
| `The seeded records changed since they were created ...` | You edited sample records; they were kept. |

## Sources (Render documentation, checked 2026-09-27)

- [Create and Connect to Render Postgres](https://render.com/docs/postgresql-creating-connecting) -- external URLs, TLS, `sslmode`, SNI, connection limits.
- [Inbound IP Rules](https://render.com/docs/inbound-ip-rules) -- default rule, `/32` CIDR, IPv4 only.
- [Recovery and Backups](https://render.com/docs/postgresql-backups) -- exports, PITR, `pg_dump`.
- [Deploy for Free](https://render.com/docs/free) -- free instance expiry, storage, no backups.
