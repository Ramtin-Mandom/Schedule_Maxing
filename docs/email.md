# Email: verification and transactional delivery

How an account's email address is proved, how the server sends email, and
what an operator must set up. Code: `backend/verification.py`,
`backend/verification_api.py`, `backend/outbox.py`, `backend/mail.py`,
`backend/email_webhooks.py`, `backend/email_diagnostic.py`.

## Signing up

```
register (email only) -> verification email -> confirm + choose password -> account -> one welcome email
```

| Step | Request | Result |
| --- | --- | --- |
| Ask | `POST /auth/register {email}` | `202`, one generic message for every address. A pending registration (address + expiry) is recorded. No account, no password, no session. |
| Again | `POST /auth/verify/resend {email}` | Same answer. Earlier links stay valid. |
| Open the link | `GET /auth/verify` | A page. Loading it changes nothing (mail scanners open links). |
| Confirm | `POST /auth/verify/confirm {token, password, display_name?, username?}` | `201` the account, verified, with the password given now. The welcome email is queued once. |

Why the password is chosen at confirmation: if it were taken at registration,
someone could register a victim's address with a password of their own and
wait for the victim to click the link ("pre-hijacking"). Here a registration
holds no credentials at all, so there is nothing to hijack; whoever receives
the email sets the password. A `password` sent to `/auth/register` by an
older client is accepted and ignored.

Other rules:

- **Generic answers.** New, already pending, already an account, over an
  email limit: the same `202` body. An existing account's owner is mailed a
  notice that changes nothing. Only text that is not an email address at all
  is refused (`422`) -- that says nothing about any account.
- **Expiry.** A pending registration is forgotten after
  `PENDING_REGISTRATION_TTL_HOURS` (48); it reserves nothing meanwhile, since
  anyone may ask again and the email only ever goes to the address itself.
- **Tokens.** 256 random bits; only `SHA-256("<purpose>:<token>")` is stored,
  so a registration token is not a recovery or account token. Bound to the
  registration and the address it was mailed to, valid
  `VERIFICATION_TOKEN_TTL_MINUTES` (1440), consumed by one conditional
  `UPDATE` (of concurrent confirmations exactly one wins). A resend does not
  invalidate earlier links; at most 5 are outstanding (the oldest is revoked
  beyond that) and a successful confirmation ends them all.
- **Limits** (`RATE_LIMIT_ENABLED`): requests 10 per client address per hour
  (`429`); emails 4 per destination per hour with a 60 s cooldown, and 300 per
  hour for the whole service -- over an email limit nothing is queued and the
  answer does not change; confirmations 20 per client address per hour.
- **Addresses.** One mailbox, validated with the `email-validator` library:
  no recipient lists, display names, quoted local parts, IP literals, control
  characters or line breaks. Stored NFKC-normalized, trimmed and case-folded;
  plus tags and dots are kept as typed. Sign-in and recovery look addresses up
  with the same normalization. `EMAIL_DNS_CHECK=true` additionally refuses a
  domain DNS says cannot receive mail; a DNS timeout refuses nothing, and
  passing proves nothing about the mailbox or its owner -- only the emailed
  link does.

### Existing accounts

Accounts created before verification existed (and accounts created directly
in the database by the development-only direct mode) have
`users.email_verified_at = NULL`. Migration 0017 does **not** mark them
verified and deletes nothing. They keep signing in and keep their data; the
profile reports `email_verified: false` and the desktop shows "Confirm email
address...".

Such an account becomes verified in one of two ways:

1. Signed in, the owner asks for a code
   (`POST /auth/email/verification/request`) and confirms it signed in
   (`POST /auth/email/verification/confirm {token}`). Both the session (the
   password) and the code (the mailbox) are needed, so a mailbox owner who
   merely receives the email cannot bless a password somebody else set for
   their address.
2. A password reset through an emailed recovery link: the mailbox owner
   replaces the credentials, and every earlier session ends.

`REQUIRE_VERIFIED_EMAIL=true` makes the server refuse record access
(`403 email_unverified`) to unverified accounts, leaving `/me` and `/auth/...`
so they can verify. It is off by default so existing users are not locked out
on deploy; turn it on after announcing a date.

### Client compatibility

Desktop builds older than this change send a password to `/auth/register`
and then sign in at once; that sign-in now fails, because no account exists
until the emailed link is confirmed. Those users finish in the browser (the
link in the email) or update the app.

## Delivery

A request never talks to the mail provider. It adds a row to `email_outbox`
in its own transaction and answers; the row is sent right after the response,
and a background worker (`OUTBOX_WORKER_ENABLED`, every 15 s) sends whatever
is left, on every worker process -- a row is claimed with a conditional
`UPDATE`, so it is never sent by two at once.

- **Retries.** A failed send waits 1 min, 5 min, 15 min, 1 h, 4 h; after 6
  attempts the row is `dead`. Only the failure's class name is stored.
- **No token in the queue.** A row holds the kind of message, the recipient
  and the id of what it is about. The link is composed, and its token minted,
  at the moment of sending; only the token's digest is stored. A queue dump
  cannot verify or reset anything, and there is nothing to purge.
- **Duplicates.** `dedupe_key` is unique for ever (`welcome:<user id>`), so
  the welcome email is queued once whatever is retried or restarted. Sending
  is at-least-once: a process that dies between the provider's answer and the
  commit sends that row again when its 120 s lock expires. With Resend the
  row id is the request's `Idempotency-Key`, which closes that gap; plain
  SMTP has no equivalent.
- **Acceptance is not delivery.** `accepted_at` is the provider taking the
  message. `delivered_at`, `bounced_at` and `complained_at` are written only
  by the provider's signed webhook. Nothing records that a person read it.
- **Links** are built from `APP_PUBLIC_URL` only -- never from a request's
  `Host` or forwarded headers -- with the token in the URL fragment.
- **Logs** never contain an address, a link, a token or a credential.

### Webhook

`POST /webhooks/email` (404 unless `EMAIL_WEBHOOK_SECRET` is set) follows the
Standard Webhooks scheme that Resend's Svix-signed webhooks use: `webhook-id`
/ `webhook-timestamp` / `webhook-signature` headers (or `svix-*`), HMAC-SHA256
over `id.timestamp.body` with the `whsec_...` secret, compared in constant
time; the timestamp must be within 5 minutes; every processed id is stored, so
a replayed event answers `200 duplicate` and changes nothing. Handled events:
`email.delivered`, `email.bounced`, `email.complained`.

## Provider and domain setup

Nothing below was done or verified from this repository: no provider account,
API key or DNS zone was available. The Resend adapter and the webhook were
tested only against fakes; check the request and event shapes against the
provider's current documentation during the staging test.

1. **Sending domain.** Use a subdomain you control (for example
   `mail.example.com`); add it in the provider's dashboard and publish the DNS
   records it lists.
2. **SPF.** A TXT record on the sending (envelope) domain authorizing the
   provider, e.g. `v=spf1 include:<provider's include> -all`. One SPF record
   per name.
3. **DKIM.** The provider's CNAME or TXT selector records, so messages are
   signed for your domain.
4. **DMARC.** `_dmarc.<domain>` TXT, starting with
   `v=DMARC1; p=none; rua=mailto:<reports address>`; move to `quarantine` and
   then `reject` once the reports show SPF/DKIM aligned.
5. **Environment** (the server's secret settings; never the desktop app, the
   repository or a client):

   ```
   APP_PUBLIC_URL=https://<the API's public host>
   SMTP_SENDER=Schedule Maxing <no-reply@mail.example.com>
   # either SMTP ...
   SMTP_HOST=<host>   SMTP_PORT=587   SMTP_USERNAME=<user>   SMTP_PASSWORD=<secret>
   # ... or the HTTPS API
   EMAIL_PROVIDER=resend
   RESEND_API_KEY=<secret>
   EMAIL_WEBHOOK_SECRET=<whsec_... from the provider's webhook settings>
   EMAIL_DIAGNOSTIC_RECIPIENT=<one inbox you control>
   ```

   Point the provider's webhook at `https://<the API's public host>/webhooks/email`
   for delivered, bounced and complained events. Delivery evidence needs the
   API provider: plain SMTP reports acceptance only.
6. **Run migration 0017** (`python -m backend.migrate upgrade`).

### Controlled live test

On the server (it reads the server's environment; there is no API route for
it and no way to name another recipient):

```
python -m backend.email_diagnostic send
python -m backend.email_diagnostic status <id printed by send>
```

The report keeps four facts apart: configuration passed; the provider
accepted the message; delivery or bounce evidence (only from a signed
webhook, otherwise "none recorded"); and whether the recipient received it,
which the tool never claims -- a person checks the inbox for the test code it
printed. Then register a throwaway address you control end to end
(register, receive, confirm, welcome) before announcing signup.
