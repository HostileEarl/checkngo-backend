# CheckN Go — Frontend Integration Guide

Interactive reference: `/api/docs/`
Schema: `/api/schema/`

---

## 1. Authentication

### Login
`POST /api/auth/login/`
```json
{ "phone_number": "+639171112222", "pin": "111111" }
```

The field is **`pin`**, not `password`. Phone numbers are normalized server-side,
so `+63 917 111 2222` works.

Response:
```json
{
  "access": "eyJ...",
  "refresh": "eyJ...",
  "must_change_credential": false,
  "user": { "id": 1, "full_name": "Maria Santos", "role": "OWNER" },
  "memberships": [
    { "farm_id": 1, "farm_name": "Santos Broiler Farm", "role": "OWNER" }
  ]
}
```

Send `Authorization: Bearer <access>` on every subsequent request.

### The rotation gate — handle this first

If `must_change_credential` is `true`, **route straight to a change-PIN screen.**
Every other endpoint returns `403` until it's cleared. Only these work:

- `GET /api/auth/me/`
- `POST /api/auth/credential/change/`
- `POST /api/auth/logout/`

```json
POST /api/auth/credential/change/
{ "current_pin": "483920", "new_pin": "112233", "confirm_pin": "112233" }
```

**This kills every existing session.** Discard stored tokens and log in again.

### Token lifetimes

| Token | Lifetime |
|---|---|
| access | 8 hours (one shift) |
| refresh | 30 days |

`POST /api/auth/refresh/` with `{"refresh": "..."}`. Refresh tokens rotate — the
old one is blacklisted immediately, so **always store the new one**.

---

## 2. Farm scoping

Everything operational is nested: `/api/farms/<farm_id>/...`

The farm id comes from `memberships` in the login response. If a user belongs to
several farms, they pick one, and it goes in the URL — not in app state.

`403` means no active membership on that farm. Not retryable.

---

## 3. Roles

| | Owner | Manager | Worker |
|---|:---:|:---:|:---:|
| View production data | ✅ | ✅ | ✅ |
| Record daily / weights | ✅ | ✅ | ✅ |
| Create batches, harvest | ✅ | ✅ | ❌ |
| Feed deliveries | ✅ | ✅ | ❌ |
| Correct locked records | ✅ | ✅ | ❌ |
| Invite workers | ✅ | ✅ | ❌ |
| Invite managers | ✅ | ❌ | ❌ |
| Financial analytics | ✅ | ✅ | ❌ |
| Transfer ownership | ✅ | ❌ | ❌ |

Hide the controls a role can't use — but the server enforces it regardless.

---

## 4. Offline sync

### The contract

1. **Generate a UUID v4 on the device** for every record, before saving locally.
2. Store `recorded_at` as the local timestamp at entry.
3. Queue locally.
4. On reconnect, `POST` the queue to `bulk-sync/`.

`POST /api/farms/<farm_id>/batches/<batch_id>/daily-records/bulk-sync/`

```json
{
  "records": [
    {
      "id": "9f1c2e4a-1b3d-4f5a-8c9e-0a1b2c3d4e5f",
      "record_date": "2026-08-19",
      "mortality_disease": 4,
      "mortality_heat": 0,
      "mortality_culled": 2,
      "mortality_unknown": 0,
      "feed_kg": "312.50",
      "recorded_at": "2026-08-19T06:15:00+08:00"
    }
  ]
}
```

### Reading the response

| Status | Meaning | Action |
|---|---|---|
| `200` | All records landed | Clear the queue |
| `207` | Some failed | Clear only `created` + `updated`; keep `failed` |
| `400` | Payload rejected entirely | Malformed — do not retry blindly |

```json
{
  "created": ["9f1c2e4a-..."],
  "updated": [],
  "failed": [{ "record_date": "2026-08-15", "error": "Locked — older than 24 hours." }],
  "batch_totals": { "total_mortality": 198, "current_bird_count": 3202 }
}
```

Use `batch_totals` to refresh the local cache — saves a follow-up GET.

**Limits:** 60 records per request; no duplicate dates within one payload.

---

## 5. The 24-hour lock

Every daily record and weight sample carries `is_editable`.

- `true` → the worker can `PATCH` it normally
- `false` → `PATCH` returns `400`; only a manager can correct it

**Correction flow** (manager/owner):

`POST /api/farms/<farm_id>/batches/<batch_id>/daily-records/<record_id>/correct/`
```json
{ "mortality_heat": 45, "reason": "Transposed digits, verified against paper log" }
```

`reason` requires **15+ characters**. Shorter returns `400`.

Corrections are blocked once the batch is harvested — the books are closed.

Show `is_editable` in the UI. A worker tapping edit on a locked record should
see "ask your manager", not a failed request.

---

## 6. Charts

### Chart 1 — mortality over time
`GET /api/farms/<farm_id>/analytics/batches/<batch_id>/mortality/`

Stacked area or multi-line on `disease` / `heat` / `culled` / `unknown`,
x-axis `age_days`.

- **Gaps are real.** A missing `age_days` means nobody recorded that day.
  Break the line — do not interpolate to zero.
- **`was_corrected: true`** — mark the point. This is the audit trail
  reaching the UI.

### Chart 2 — FCR
`GET /api/farms/<farm_id>/analytics/fcr/`

Bar chart on `rows[].fcr`. Lower is better — consider inverting the visual
sense so shorter bars don't read as worse.

**Always display the `excluded` block.** "3 active batches excluded — FCR
requires a harvest weight" prevents a user reading 5 bars as the whole farm.

### Chart 3 — feed margin
`GET /api/farms/<farm_id>/analytics/profitability/` *(owner/manager)*

**Label this "Feed Margin", never "Profit".** The `methodology` block lists
what's excluded — surface it as a tooltip or footnote.

`revenue: null` means no sale price was recorded. Render as "—", never ₱0.

### Dashboard
`GET /api/farms/<farm_id>/analytics/dashboard/`

One call for the landing screen. Use it instead of four separate requests.

---

## 7. Numbers

All money and weight values arrive as **strings** (`"312.50"`), not floats.
This is deliberate — float arithmetic on currency introduces rounding errors.

Parse with a decimal library. In Dart, `Decimal.parse()` from the `decimal`
package, not `double.parse()`.

---

## 8. Errors

| Code | Meaning |
|---|---|
| `400` | Validation failed — read the field-keyed body |
| `401` | Token missing, expired, or blacklisted → refresh |
| `403` | Not permitted — role, farm scope, or the rotation gate |
| `404` | Not found, or outside this farm's scope |
| `207` | Partial success (bulk sync only) |
| `429` | Throttled — read `Retry-After` |

`403` bodies carry a `detail` string worth showing verbatim; the messages are
written for end users.

---

## 9. Demo credentials

After `py manage.py seed_demo_data --reset`:

| Role | Phone | PIN |
|---|---|---|
| Owner | `+639171112222` | `111111` |
| Manager | `+639172223333` | `222222` |
| Worker | `+639173334444` | `333333` |
| Supplier | `+639175556666` | `555555` |