# CheckN Go — Platform Administrator Guide

This is the guide for the **Django admin**. It is the platform operator
interface, deliberately separate from the farm-facing application.

The admin is **not** at `/admin/`. Its path is read from the `ADMIN_URL`
environment variable and is unguessable in every real environment (see
*Deployment notes* at the end). `/admin/` itself returns 404.

There is **no in-app administrator role**. Owners, managers, and workers all
operate inside a farm through `FarmMembership`; none of them can create
accounts, move farm ownership, or read another farm's data. Those are
operator tasks, and this interface is how they get done.

Access requires a Django account with `is_staff=True` (and, in practice,
`is_superuser=True`). Those accounts are created with
`manage.py createsuperuser` on the server, never through the app.

The admin login is rate-limited (django-axes): **5 failed attempts** for a
given username + IP within an hour locks that pair out for **1 hour**. A
successful login clears the count. See *If you are locked out* below.

---

## 1. Creating a new farm owner account

A new customer is onboarded by creating their OWNER account here. They then
log in to the app, are forced to rotate the PIN the system issued, and —
because they belong to no farm yet — are routed automatically into the setup
wizard, where they register their own farm.

**Steps**

1. Go to **Accounts → Farm owner accounts → Add farm owner account**.
2. Fill in:
   - **Full name**.
   - **Phone number** — E.164 format, e.g. `+639171234567`. Display spacing
     (`+63 917 123 4567`) is accepted and stripped. This is the login
     identifier; there is no username.
   - **Email** — optional.
   There is **no password field**. The account role is set to `OWNER`
   automatically, and the login PIN is generated for you.
3. Save. A yellow banner shows the **one-time PIN**:
   > One-time PIN for *Name*: `481920` — give it to the owner now. It is not
   > stored and cannot be shown again; they must change it on first login.
   Copy it out immediately and pass it to the owner out of band (spoken, or
   SMS). It is only the password hash from here on — there is no way to read
   it back, and no "resend".
4. That is the whole flow. The account is created with `role=OWNER`,
   `must_change_credential=True` (the forced-rotation gate), `is_staff=False`,
   and **no farm or membership**. When the owner logs in, rotates the PIN,
   and the app finds zero memberships, it sends them to `/setup`, where they
   register their farm — which creates the `Farm`, their `OWNER`
   `FarmMembership`, and the first `FarmOwnershipHistory` row (the
   "registration" entry, with no `from_owner`).

This is the same issuance path as an invitee accepting an invitation and as
the `make_test_owner` management command — one code path
(`accounts.services.issue_owner_account`), so a hand-created owner is
identical to an invited one.

**Why there is no password field.** `AUTH_PASSWORD_VALIDATORS` (which also
guards every superuser and staff password) rejects a 6-digit numeric PIN on
both length and "all numeric". Rather than weaken those globally, this flow
generates the PIN in code and stores only its hash — exactly what the
invitation flow already does. Typing a PIN into the stock **Users → Add
user** form will fail validation, by design.

**Lost PIN.** There is no reveal and no resend. Deactivate the account under
**Accounts → Users** (§3) and create a fresh one here — same rule as
"revoke and re-invite" for invitations.

**How to tell it worked** — the owner now appears under **Accounts → Farm
owner accounts** with **Credential** = *Not yet rotated* and **In setup
wizard** = ✓. After they rotate their PIN it flips to *Rotated*; once they
finish `/setup` the wizard flag clears. The same account under **Accounts →
Users** shows `Active farms` = `0` and `Member of` = *— none —* until then.

---

## 2. Finding a user and seeing which farms they belong to

1. **Accounts → Users**.
2. Search by name, phone number, or email (top search box).
3. The changelist carries the scope at a glance:
   - **Active farms** — count of active `FarmMembership` rows.
   - **Member of** — each farm name with the role held there
     (`Santos Layer Farm (Farm Manager)`, …).
   - **Credential** — whether they have rotated their issued PIN.
4. Open the user. The **Farm memberships** inline at the bottom lists every
   membership row — farm, role, active flag, join date, and who invited
   them. It is **read-only**: memberships are granted and revoked from the
   **Farms** section and the invite flow, not typed in on the user page.
5. To find owners still stuck in onboarding, use the **Farm membership**
   filter in the right-hand sidebar → **No active membership**.

---

## 3. Deactivating a user (preferred over deletion)

To remove someone's access — a worker who left, a compromised account:

1. Open the user in **Accounts → Users**.
2. Untick **Active**. Save.

A deactivated user cannot log in and cannot refresh a token. Their
`recorded_by` links on every daily record, weight sample, sale event, and
correction stay intact, so the audit trail still reads correctly.

**Do not delete the account.** `Farm.owner` is a `PROTECT` foreign key and
much production data references the user; a delete would either be blocked
or would blank out authorship (`SET_NULL`) across historical records,
turning "recorded by Ana Reyes" into "recorded by —". Deactivation is
reversible and keeps the evidence. To also cut existing sessions
immediately, blacklist the user's tokens in
**Token Blacklist → Outstanding tokens**, or have them rotate their
credential (which revokes all sessions).

To end just one farm relationship rather than the whole account, deactivate
the specific **Farm membership** row instead (**Farms → Farm memberships**).

---

## 4. Reading the audit trails

### Correction ledger — `Production → Record corrections`

Every time a manager overrides the 24-hour edit lock on a daily record or
weight sample, a `RecordCorrection` row is written. Each row holds:

- **Fields changed** — which values were altered.
- **`previous_values` / `new_values`** — full before and after snapshots.
- **`reason`** — the written justification (minimum 15 characters, enforced
  at entry).
- **`corrected_by_name`** — the manager's name, copied in as permanent text
  so it survives the account being removed.
- **`corrected_at`** — when.

Filter by farm, by batch, or by date with the date drilldown at the top.
Search covers the reason text, the corrector's name, and the batch code.
The rows are **completely read-only** — no add, no edit, no delete, no bulk
actions.

### Ownership ledger — `Farms → Farm ownership history`

One row per ownership event for every farm: the original registration
(`from_owner` empty) and each subsequent transfer. A transfer deletes the
outgoing owner's membership, so this table is the only record of who held a
farm at a given time. Each row snapshots `from_owner_name` and
`to_owner_name` as text, plus `transferred_at`, `performed_by`, and an
optional note.

You can also read a single farm's history without leaving its page: the
**Ownership history** inline is shown, read-only, on the farm's change form
in **Farms → Farms**.

---

## 5. What an administrator deliberately cannot do

These are not missing features. They are the guarantees that make the data
evidence, and they are enforced in this admin, not just in the API.

- **Edit a locked daily record.** `DailyRecord`, `WeightSample`,
  `SaleEvent`, and `InventoryUsageLog` are editable only for 24 hours after
  the server received them. After that the admin change form is
  permission-denied for that row, exactly as the API is. There is no
  override switch here — a manager who needs a later fix does it through
  the correction flow in the app, which writes a `RecordCorrection`.
- **Delete a daily record** (or weight sample, sale event, usage log).
  Delete is disabled on all of them. A recorded day is not removable.
- **Alter or delete a correction.** `RecordCorrection` has no add, no
  change, and no delete. The model itself raises on any update. Its whole
  value is that it cannot be walked back.
- **Change ownership history.** `FarmOwnershipHistory` is add/change/delete
  disabled, and the model raises on update. The ledger is append-only, and
  entries are written by the transfer flow, never by hand.
- **Edit a closed harvest.** `Harvest` is read-only. Closing a batch fixes
  its FCR and feed margin permanently.
- **Hand-create an invitation.** `Invitation` add is disabled — an
  invitation made outside the API would carry no valid PIN hash. Issue
  invites from the app; revoke pending ones with the admin action if
  needed.
- **Clear a user's forced PIN rotation.** Technically the checkbox exists,
  but unticking `must_change_credential` by hand breaks non-repudiation for
  that account. Let the user rotate their own PIN.

The limits are the feature.

---

## 6. If you are locked out of the admin login

After **5 failed login attempts** for one username + IP address within an
hour, django-axes locks that combination out and every further attempt —
**including one with the correct password** — returns **HTTP 429 Too Many
Requests** with a "too many login attempts" message.

The lock is on the pair, not the account: the same superuser logging in
from a different network is unaffected, and other users on your network are
unaffected unless they were also failing against the same username.

Your options, in order of preference:

1. **Wait one hour.** The lock clears itself — no cooloff to configure, no
   record to clean up. This is the intended path.
2. **Log in successfully from another network** (phone hotspot, home).
   A successful login anywhere resets the counter for that username, which
   also clears the locked pair.
3. **Clear it from the server**, if you cannot wait and have shell access:

   ```
   python manage.py axes_reset                # clears every lockout
   python manage.py axes_reset_username "+639171234567"
   python manage.py axes_reset_ip 203.0.113.7
   ```

4. **Clear it from the admin**, if another operator still has a session:
   django-axes registers its own models. Open **Axes → Access attempts**,
   find the row for the username/IP, and delete it.

There is deliberately **no self-service unlock** and no way to raise the
limit for your own account from inside the app. If lockouts are a recurring
problem for a legitimate operator, the fix is a second superuser account
they can fall back to, not a looser limit.

Do not respond to a lockout by widening `AXES_FAILURE_LIMIT` or adding an
IP allowlist. The tight limit is the point, and an operator who travels
would be locked to a stale allowlist within a week.

---

## 7. Deployment notes

**`ADMIN_URL` must be set to a non-default value in production.** It is read
from the environment in `checkngo/urls.py`:

```
ADMIN_URL = config("ADMIN_URL", default="admin/")
```

- Set it in the deployment environment (Render dashboard / `.env`) to an
  unguessable path **with a trailing slash**, e.g. `farm-admin-8f3k/`.
  Unguessable, not clever — it is not a secret and not security on its own,
  it just keeps automated `/admin/` scanners from finding the login form.
- The value never appears in the repository. `.env.example` ships only the
  `admin/` default with a comment.
- The path differs between local and production; that is the intent.
- After changing it, the admin is reachable **only** at the new path and
  `/admin/` returns 404. Update any bookmarks and the operator runbook.

**django-axes** stores its counters in the database (`AccessAttempt` rows),
not the cache, so it behaves identically whether `REDIS_URL` is set or not.
`manage.py migrate` creates its tables — already part of `build.sh`. The
lockout parameters (5 attempts, 1 hour, username + IP combined, reset on
success) live in `checkngo/settings.py` under the `AXES_*` block.

**SMS (Semaphore)** sends invitation PINs and low-stock alerts. Three
environment variables, all optional locally:

- `SEMAPHORE_API_KEY` — from the Semaphore dashboard. Unset means every SMS
  is skipped (logged, not sent) regardless of `SMS_ENABLED`.
- `SEMAPHORE_SENDER_NAME` — optional; blank uses the account's registered
  default sender name.
- `SMS_ENABLED` — must be explicitly `True` in production. Kept separate
  from the key so SMS can be switched off (e.g. for a demo) without
  removing credentials.
- `ACCESS_REQUEST_NOTIFY_PHONE` — optional; the number texted when the
  landing page's "Request access" form is submitted. The text names only
  the farm and location ("Open the admin to reply"), at most 10 per
  rolling 24 hours. Blank means no text; requests are still saved under
  **People → Access requests**.

Low-stock alerts are **not** sent automatically — `analytics/alerts.py`
only computes the condition on read. Schedule
`python manage.py send_stock_alerts` to run periodically (a Render Cron Job
is the natural fit); it dedupes itself so running it more often than needed
just means an idle no-op most of the time, not extra texts.

---

## 8. Evaluation farms (guided test sessions)

**System → Evaluation farms** (superusers only; everyone else gets a 403)
creates one clean farm per guided test session. It only adds data: it
never edits, renames or deletes an existing farm, user or record, sends
no SMS and creates no invitation.

Each farm is called **Demo Farm – Session NN** (Atimonan, Quezon) and comes
with:

- four accounts that log in straight away, with no PIN change on first
  login: Owner, Manager, Worker (House 1 only) and Buyer (a linked buyer).
  Their numbers are `+63999` + `NN` + `0000` + `1`/`2`/`3`/`4`;
- House 1 and House 2, each with a flock placed 21 days ago and a daily
  record for every day up to **yesterday** — today is left for the
  respondent to record;
- a closed flock in House 1 from about two months ago, with sales to the
  linked buyer (one paid, one with a returned cheque), a walk-in sale and
  a parts sale;
- feed and supplies stock (Vitamins sits just under its reorder level, so
  a low-stock alert shows) and the daily routine ticked on past days.

**Create farms:** choose how many (1–10). They take the next free session
numbers. The next page lists each farm's four accounts with their PINs
**once** — copy them into the private session sheet before leaving the
page. They are stored hashed and cannot be shown again; reloading the page
does not create more farms. Each farm takes a few seconds (four PINs are
hashed), so on the live site create two or three at a time. If a request
times out, nothing was created — try again with fewer.

Create each farm close to its session: the records end "yesterday"
relative to the day the farm is created.

**After the session:** tick the farm and use **Archive selected evaluation
farms**. The farm becomes inactive and its four accounts are deactivated
(they can no longer log in). Nothing is deleted.

Locally the same is available as
`python manage.py create_evaluation_farms --count N`.

---

## Reference — what each admin section is for

| Section | Editable? | Notes |
|---|---|---|
| People → Access requests | Status actions only | Landing-page "Request access" submissions. Never creates an account — create the owner by hand. Mark contacted / approved / declined; only handled requests can be deleted. |
| Accounts → Users | Yes | Membership inline is read-only. |
| Accounts → Farm owner accounts | Add only | Owner onboarding funnel; PIN generated and shown once. No edit/delete. |
| Accounts → Invitations | Read-only | Add disabled; revoke action available. |
| Farms → Farms | Yes | Membership + ownership-history inlines. |
| Farms → Farm memberships | Yes | Grant/revoke farm access here. |
| Farms → Farm ownership history | Read-only | Append-only ledger. |
| System → Evaluation farms | Create / archive | Superusers only. Guided-test farms; PINs shown once. See section 8. |
| Partners → Farm partner links | Yes | Consumer (buyer) relationships. |
| Production → Houses | Yes | Physical sheds. |
| Production → Batches | Yes | Totals are denormalized; recalculate action provided. |
| Production → Daily records | 24h window | Then permission-denied. Never deletable. |
| Production → Weight samples | 24h window | Never deletable. |
| Production → Sale events | 24h window | Never deletable. |
| Production → Harvests | Read-only | Closes a batch permanently. |
| Production → Feed deliveries | Yes | Stock-in stream. |
| Production → Inventory items | Yes | Configuration; on-hand level is computed. |
| Production → Inventory stock-in | Yes | |
| Production → Inventory usage logs | 24h window | Never deletable. |
| Production → Record corrections | Read-only | The correction ledger. |
| Production → Task templates | Yes | Configuration — the daily routine. |
| Production → Task completions | Read-only | Records that a routine item was done. |
| Axes → Access attempts | Delete only | Failed-login counters; delete a row to lift a lockout. |
| Axes → Access logs | Read-only | Login history. |
