# CheckN Go — Platform Administrator Guide

This is the guide for the **Django admin** at `/admin/`. It is the platform
operator interface, deliberately separate from the farm-facing application.

There is **no in-app administrator role**. Owners, managers, and workers all
operate inside a farm through `FarmMembership`; none of them can create
accounts, move farm ownership, or read another farm's data. Those are
operator tasks, and this interface is how they get done.

Access requires a Django account with `is_staff=True` (and, in practice,
`is_superuser=True`). Those accounts are created with
`manage.py createsuperuser` on the server, never through the app.

---

## 1. Creating a new farm owner account

A new customer is onboarded by creating their OWNER account here. They then
log in to the app, are forced to rotate the PIN you set, and — because they
belong to no farm yet — are routed automatically into the setup wizard,
where they register their own farm.

**Steps**

1. Go to **Accounts → Users → Add user**.
2. Fill in:
   - **Phone number** — E.164 format, e.g. `+639171234567`. This is the
     login identifier; there is no username.
   - **Full name**.
   - **Role** — set to **Farm Owner (`OWNER`)**. This is the field that
     later lets the account hold a `FarmMembership` and own a `Farm`; a
     `SUPPLIER`/`CONSUMER` account cannot.
   - **Password / password confirmation** — this is the **initial PIN** you
     will read out to the owner. Use a 6-digit number to match what the app
     expects. It is transmitted to them out of band (call, SMS); they
     replace it on first login.
3. Save. You land on the full change form.
4. On the change form, confirm:
   - **Must change credential** is **still ticked**. Leave it. This is the
     forced-rotation gate — every API endpoint except *view me* and *change
     credential* returns `403` until the owner rotates the PIN themselves.
     That rotation is the non-repudiation event: from then on the
     credential is known only to them. **Do not untick this box by hand**;
     doing so silently defeats the guarantee.
   - **Role** is `OWNER`.
   - **Active** is ticked.
   - Leave **Staff status** and **Superuser status** unticked. An owner is
     not a platform operator.
5. Do **not** create a `Farm` or `FarmMembership` for them here. Leave the
   account with **no membership**. When the owner logs in, rotates the PIN,
   and the app finds zero memberships, it sends them to `/setup`, where
   they register their farm. Registering the farm creates the `Farm`, the
   owner's `OWNER` `FarmMembership`, and the first `FarmOwnershipHistory`
   row (the "registration" entry, with no `from_owner`).

**How to tell it worked** — find the user again (next section). While they
are still in the wizard: `Credential` column shows *Not yet rotated* or,
after rotation, *Rotated*; `Active farms` column shows `0` and `Member of`
shows *— none —*. Once they finish setup, `Active farms` becomes `1` and
`Member of` names their farm.

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

## Reference — what each admin section is for

| Section | Editable? | Notes |
|---|---|---|
| Accounts → Users | Yes | Membership inline is read-only. |
| Accounts → Invitations | Read-only | Add disabled; revoke action available. |
| Farms → Farms | Yes | Membership + ownership-history inlines. |
| Farms → Farm memberships | Yes | Grant/revoke farm access here. |
| Farms → Farm ownership history | Read-only | Append-only ledger. |
| Partners → Farm partner links | Yes | Supplier/consumer relationships. |
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
