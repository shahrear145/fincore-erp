# FinCore ERP — System Architecture

## 1. Platform layers

| Layer      | Technology                                    | Notes |
|------------|----------------------------------------------|-------|
| Android    | Flutter + Riverpod + Dio + drift (SQLite)    | offline-first, background sync |
| Web        | Flutter Web (same codebase)                  | served by Nginx |
| API        | Django 5.2 LTS + DRF 3.16                    | REST, `api/` prefix |
| Auth       | SimpleJWT (access + refresh, rotation + blacklist) | email or username login |
| Jobs       | Celery + Redis                               | reminders, recurring, backups |
| Primary DB | PostgreSQL 16 (UUIDs, decimal money, FKs, constraints, indexes) | accounting authority |
| Local DB   | SQLite via drift in Flutter                  | device mirror, sync queue |
| Files      | S3-compatible (MinIO on VPS / AWS S3)        | statements, backups |
| Deploy     | Docker Compose on a VPS, Nginx reverse proxy, HTTPS via Caddy | |

## 2. Tenancy & data separation model

- `Business` is the tenant root. Every business-scoped financial row carries
  a `business_id` FK. All list endpoints filter by the resolved business and
  an active `BusinessMembership`.
- **Personal finance (Phase 6)** is a separate ledger owned by `User` with
  `business_id = NULL`; it never joins into business queries. Two accounting
  environments guaranteed by schema, not UI.
- Multi-business support falls out of the same structure (Section 2/3 of spec).

## 3. Roles and permissions (backend-enforced)

ADMIN (owner or granted):
- everything inside that business: partners, customers, transactions,
  approvals, period locking, reports.

PARTNER:
- only their own platform account, mapped through `Partner.linked_user`;
  records scoped by partner attribution (`partner_id`) everywhere.
- `BusinessMembership.partner_permissions` holds granular flags
  (create transactions, edit own records, view reports …). The flag
  `can_edit_other_partners_dues` is permanently false for partners;
  cross-partner due changes go through an ADMIN approval flow (Phase 3).

Enforcement lives in `apps.core.permissions` (permission classes +
scope querysets). The Flutter app merely mirrors what the API returns.

## 4. Time & date policy

- DB stores UTC instants (`USE_TZ=True`); displayed in `Asia/Dhaka`.
- Every financial record keeps **two dates**:
  - `transaction_date` (business/effective date — report slicing)
  - `created_at` (record creation timestamp)
- Balance-sheet style reports take an *as-of date*; income statements take a
  range. Period locking (`FinancialPeriod`) blocks edits in closed periods.
- The Flutter layer persists per-partner last-used transaction dates for fast
  repeat entry; server never alters saved transaction dates.

## 5. Money policy

- All monetary columns are `numeric(18,2)` DecimalFields.
- Allocation algorithm (Phase 3) allocates collections across open due
  entries oldest-first with exact decimal splitting.
- Journal engine posts every financial event as balanced journal lines
  (Σdebit = Σcredit) inside a DB transaction (Phase 5).

## 6. Offline & sync (Android)

- Flutter writes to local SQLite with UUID keys; a sync-queue pushes
  ops to `/api/sync/` when online. Server side rebases and replies with
  per-item status (applied / conflict / rejected). Conflicts open a manual
  resolution UI; PostgreSQL remains authoritative. (Phase 8.)

## 7. Security architecture

- JWT short access (30 min default), rotating refresh with blacklist.
- Password policy via Django validators; optional app PIN + biometric gate
  in the client; optional TOTP second factor (Phase 7 hook prepared).
- Everything mutating important records writes to `AuditLog` with
  previous/new values, actor, IP, reason.
- Posted financial records are never hard-deleted: soft delete/reversal.

## 8. Phase roadmap

Phases 1-8 as defined in the master spec; current status in
`docs/PROJECT_CHECKLIST.md`.
