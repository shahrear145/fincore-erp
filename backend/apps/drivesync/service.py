"""Export / import of the user's data as a JSON snapshot.

Scope:
  Personal   → per-user rows (only this user)
  Business   → rows of businesses the user is a member of

Restore modes:
  Personal                       → wipe + re-import
  Exclusive business (sole owner)→ wipe cascade + re-import
  Shared business                → upsert by pk (never deletes other members' rows)

User references (created_by, recorded_by, …) are set to NULL on restore;
the logged-in user id is never restored.
"""
import json
from decimal import Decimal
from datetime import date, datetime, time, timezone
from uuid import UUID

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.db.models import ForeignKey


# ---------------------------------------------------------------- value helpers

from django.db.models.fields.files import FieldFile

def _clean(v):
    if v is None or isinstance(v, FieldFile):
        if isinstance(v, FieldFile):
            return v.name or None
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, Decimal):
        return str(v.quantize(Decimal("0.01")))
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, time):
        return v.isoformat()
    return v


def _pk(value, model):
    if value is None:
        return None
    pk_type = model._meta.pk.get_internal_type()
    if pk_type in ("AutoField", "BigAutoField"):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if pk_type == "UUIDField":
        try:
            return UUID(str(value))
        except (TypeError, ValueError):
            return None
    return value


def _convert_row(row: dict, model):
    for f in model._meta.fields:
        name = f.name
        if name not in row:
            continue
        v = row[name]
        itype = f.get_internal_type()
        if f.is_relation:
            row[name] = _pk(v, f.related_model) if v is not None else None
        elif itype == "DecimalField":
            row[name] = Decimal(str(v)) if v is not None else None
        elif itype == "DateField":
            row[name] = date.fromisoformat(str(v)[:10]) if v else None
        elif itype == "DateTimeField":
            row[name] = datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None
        elif itype == "TimeField":
            row[name] = time.fromisoformat(str(v)[:8]) if v else None
        elif itype in ("PositiveIntegerField", "PositiveSmallIntegerField"):
            row[name] = int(v) if v is not None else 0
        elif itype == "BooleanField":
            row[name] = bool(v)
    return row


def serialize_qs(qs):
    rows = []
    fields = list(qs.model._meta.fields)
    for obj in qs:
        row = {}
        for f in fields:
            row[f.attname] = _clean(getattr(obj, f.attname))
        rows.append(row)
    return rows


def _null_user_fks(model, row):
    for f in model._meta.fields:
        try:
            rel = f.related_model
        except Exception:
            continue
        if rel is not None and rel._meta.label == "auth.User":
            row[f.attname] = None


def insert_rows(model, rows):
    """Insert rows verbatim — used by the overwrite path."""
    created = 0
    failed = 0
    first_error = ""
    for raw in rows:
        row = dict(raw)
        _null_user_fks(model, row)
        try:
            _convert_row(row, model)
        except Exception as exc:
            failed += 1
            if not first_error:
                first_error = f"{model._meta.label}: {exc}"
            continue
        try:
            # own savepoint: one bad row must not poison the surrounding
            # atomic block (every later insert would fail otherwise)
            with transaction.atomic():
                obj = model(**row)
                obj.save(force_insert=True)
            created += 1
        except Exception as exc:
            failed += 1
            if not first_error:
                first_error = f"{model._meta.label}: {exc}"
    return {"created": created, "failed": failed, "first_error": first_error}


def _upsert_rows(model, rows):
    """Merge rows by primary key.

    Rows are looked up through the *all objects* manager, so a record the
    user deleted from the UI (soft delete) is found, un-deleted and updated
    instead of being skipped as a duplicate primary key. Without this a
    restore could report "0 rows" and quietly leave deleted data deleted.
    """
    created = 0
    failed = 0
    first_error = ""
    pk_name = model._meta.pk.name
    for raw in rows:
        row = dict(raw)
        _null_user_fks(model, row)
        try:
            _convert_row(row, model)
        except Exception as exc:
            failed += 1
            if not first_error:
                first_error = f"{model._meta.label}: {exc}"
            continue
        pk_val = row.get(pk_name)
        try:
            existing = None
            if pk_val is not None:
                existing = _mgr(model).filter(pk=pk_val).first()
            if existing is not None:
                try:
                    with transaction.atomic():
                        for f in model._meta.fields:
                            if f.attname == pk_name:
                                continue
                            setattr(existing, f.attname, row.get(f.attname))
                        if hasattr(existing, "deleted_at"):
                            existing.deleted_at = None
                        existing.save()
                    created += 1
                except Exception as exc:
                    failed += 1
                    if not first_error:
                        first_error = f"{model._meta.label}: {exc}"
                continue
            with transaction.atomic():
                obj = model(**row)
                obj.save(force_insert=True)
            created += 1
        except Exception as exc:
            failed += 1
            if not first_error:
                first_error = f"{model._meta.label}: {exc}"
    return {"created": created, "failed": failed, "first_error": first_error}


# ---------------------------------------------------------------- model lists

def _m(app_label, model_name):
    return apps.get_model(app_label, model_name)


# children first (delete order); insert goes reversed
PERSONAL_MODELS = [
    ("personal", "PersonalTransaction", "personal_transactions"),
    ("personal", "PersonalBudget", "personal_budgets"),
    ("personal", "SavingsGoal", "savings_goals"),
    ("personal", "PersonalLoan", "personal_loans"),
    ("personal", "PersonalAccount", "personal_accounts"),
    ("personal", "PersonalCategory", "personal_categories"),
]

BUSINESS_MODELS = [
    ("transactions", "Transaction", "transactions"),
    ("transactions", "TransactionDateMemory", "transaction_date_memory"),
    ("transactions", "RecurringTransaction", "recurring_transactions"),
    ("customers", "DueEntry", "due_entries"),
    ("customers", "PaymentAllocation", "payment_allocations"),
    ("customers", "CustomerDocument", "customer_documents"),
    ("customers", "DueReminder", "due_reminders"),
    ("customers", "CustomerAssignment", "customer_assignments"),
    ("customers", "Customer", "customers"),
    ("partners", "Partner", "partners"),
    ("accounting", "JournalLine", "journal_lines"),
    ("accounting", "JournalEntry", "journal_entries"),
    ("accounting", "Account", "accounting_accounts"),
    ("loans", "LoanEntry", "loan_entries"),
    ("loans", "LoanParty", "loan_parties"),
    ("cashbook", "CashCount", "cash_counts"),
    ("cashbook", "StockSnapshot", "stock_snapshots"),
    ("organizations", "FinancialPeriod", "financial_periods"),
    ("organizations", "BusinessMembership", "memberships"),
    ("organizations", "Business", "businesses"),
]


def _mgr(model):
    return getattr(model, "all_objects", model.objects)


def personal_qs(model, user):
    has_user = any(f.name == "user" for f in model._meta.fields)
    return _mgr(model).filter(user=user) if has_user else _mgr(model).none()


def membership_list(user):
    from apps.organizations.models import BusinessMembership
    return (BusinessMembership.objects.filter(user=user, is_active=True)
            .select_related("business"))


def business_qs(model, business_id):
    meta = model._meta
    if model.__name__ == "Business":
        # include a soft-deleted business too: otherwise the snapshot has the
        # business's rows but not the business itself, and a restore can
        # never put them back (every insert fails on the missing parent)
        return _mgr(model).filter(pk=business_id)
    if any(f.name == "business" for f in meta.fields):
        # include soft-deleted rows: without them a backup silently loses
        # everything that was deleted from the UI (e.g. a deleted customer's
        # dues history), and a restore can never bring it back
        return _mgr(model).filter(business_id=business_id)
    # models WITHOUT a direct business FK — resolve through the parent.
    # Without these the backup is missing rows entirely (journal lines!),
    # and the ledger — every ledger-derived figure — reads 0 after restore.
    name = model.__name__
    if name == "JournalLine":
        return _mgr(model).filter(entry__business_id=business_id)
    if name == "DueEntry":
        return _mgr(model).filter(
            assignment__customer__business_id=business_id)
    if name == "PaymentAllocation":
        from django.db.models import Q
        return _mgr(model).filter(
            Q(collection_transaction__business_id=business_id)
            | Q(credit_entry__assignment__customer__business_id=business_id))
    if name == "CustomerDocument":
        return _mgr(model).filter(customer__business_id=business_id)
    if name == "CustomerAssignment":
        return _mgr(model).filter(customer__business_id=business_id)
    return _mgr(model).none()


def sole_owner_ids(user):
    ids = []
    for m in membership_list(user):
        if m.business.owner_id != user.id:
            continue
        count = m.business.memberships.filter(is_active=True).count()
        if count == 1:
            ids.append(m.business.id)
    return ids


# ---------------------------------------------------------------- snapshot

def build_snapshot(user) -> dict:
    personal = {}
    for app_label, model_name, key in PERSONAL_MODELS:
        model = _m(app_label, model_name)
        personal[key] = serialize_qs(personal_qs(model, user))

    businesses = []
    for member in membership_list(user):
        biz = member.business
        rows = {}
        for app_label, model_name, key in BUSINESS_MODELS:
            model = _m(app_label, model_name)
            rows[key] = serialize_qs(business_qs(model, biz.id))
        sole = (biz.owner_id == user.id and biz.memberships.filter(
            is_active=True).count() == 1)
        businesses.append({
            "id": str(biz.id),
            "name": biz.name,
            "sole_owner": bool(sole),
            "rows": rows,
        })
    return {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "copyright": "FinCore ERP — personal data export",
        "user": user.username,
        "personal": personal,
        "businesses": businesses,
    }


@transaction.atomic
def restore_snapshot(user, snapshot: dict) -> dict:
    if snapshot.get("version") != 1:
        raise ValueError("অসমর্থিত ব্যাকআপ ভার্সন।")

    # The personal reset must HARD delete. `qs.delete()` is a soft delete on
    # models such as PersonalAccount — the row stays in the table, and
    # re-inserting it then violates UNIQUE (user, name). That IntegrityError
    # poisons the surrounding atomic block, and every row after it fails with
    # "You can't execute queries until the end of the 'atomic' block".
    # Reuse the reset module's dependency-ordered purge (transactions before
    # the accounts they PROTECT, retries for self-referencing PROTECT).
    from apps.core.reset import _personal_tasks, _run_tasks
    deleted = _run_tasks(_personal_tasks(user))
    p_created = p_failed = 0
    p_first_error = ""
    for app_label, model_name, key in reversed(PERSONAL_MODELS):
        model = apps.get_model(app_label, model_name)
        result = insert_rows(model, snapshot.get("personal", {}).get(key, []))
        p_created += result["created"]
        p_failed += result["failed"]
        if result["first_error"] and not p_first_error:
            p_first_error = result["first_error"]

    report = {
        "personal_deleted": deleted,
        "personal_created": p_created,
        "personal_failed": p_failed,
        "personal_first_error": p_first_error,
        "businesses": [],
    }

    for biz_data in snapshot.get("businesses", []):
        biz_id = biz_data.get("id")
        sole = bool(biz_data.get("sole_owner"))
        business = None
        try:
            # all_objects: a soft-deleted business must still be found, so a
            # restore can bring it (and its rows) back instead of failing
            business = apps.get_model("organizations", "Business").all_objects.filter(
                pk=UUID(str(biz_id))).first()
        except Exception:
            business = None

        from apps.organizations.models import BusinessMembership
        member_count = 0
        if business is not None:
            member_count = BusinessMembership.objects.filter(
                business=business, is_active=True).count()

        overwrite = bool(sole and business is not None and member_count == 1)

        rows = biz_data.get("rows", {})
        biz_rows = rows.get("businesses") or []

        # A snapshot taken before the soft-delete fix can be missing the
        # business row. Never hard-delete in that case; rebuild the row when
        # the business no longer exists at all.
        if not biz_rows:
            overwrite = False
            if business is not None:
                if getattr(business, "deleted_at", None):
                    business.deleted_at = None
                    business.save(update_fields=["deleted_at"])
            else:
                biz_rows = [{
                    "id": str(biz_id),
                    "name": biz_data.get("name") or "Business",
                    "slug": f"restored-{str(biz_id)[:8]}",
                    "currency": "BDT",
                    "timezone": settings.PRIMARY_TZ,
                    "owner_id": user.id,
                    "contact_phone": "",
                    "contact_email": "",
                    "address": "",
                    "logo": None,
                    "is_active": True,
                    "deleted_at": None,
                }]
                rows = dict(rows, businesses=biz_rows)

        if overwrite and business is not None:
            # Delete the business's rows in dependency order first: a plain
            # cascade hits PROTECT foreign keys (LoanParty ← LoanEntry,
            # Partner, …) and aborts the whole restore.
            from apps.core.reset import _business_tasks, _run_tasks
            _run_tasks(_business_tasks([business.id]))
            business.hard_delete()
        created_total = 0
        failed_total = 0
        created_first_error = ""
        for app_label, model_name, key in reversed(BUSINESS_MODELS):
            model = apps.get_model(app_label, model_name)
            if overwrite:
                result = insert_rows(model, rows.get(key, []))
            else:
                result = _upsert_rows(model, rows.get(key, []))
            created_total += result["created"]
            failed_total += result["failed"]
            if result["first_error"] and not created_first_error:
                created_first_error = result["first_error"]

        # ট্রanzাকশন ফিরে এসেছে কিন্তু তার জার্নাল পোস্টিং নেই — লেজার-এ
        # দৃশ্যমান হবে না, তাই "হাতে নগদ" ০ দেখায়। রিস্টোরের পর ஒவ்வொரு
        # লেনদেনের জার্নাল যেন পোস্ট থাকে, সেজন্য স্বয়ংক্রিয় বিল্ড:
        if business is not None:
            try:
                from apps.cashbook.services import (
                    rebuild_ledger_from_transactions,
                )
                                # overwrite-এর পর business অবজেক্টটি পুরনো (ডিলিট করা) থাকে,
                # তাই ডিবি থেকে নতুনটি আনো।
                restored_business = apps.get_model("organizations", "Business").objects.filter(
                    pk=UUID(str(biz_id))).first()
                if restored_business is not None:
                    rebuild_ledger_from_transactions(restored_business, user=user)
            except Exception:
                pass   # রিস্টোর ব্যর্থ হতে দিলে না — লক্ষণ அள意思是

        report["businesses"].append({
            "name": biz_data.get("name"),
            "overwrite": overwrite,
            "inserted": created_total,
            "failed": failed_total,
            "first_error": created_first_error,
        })

    return report


def summarize_snapshot(snapshot: dict) -> dict:
    """What a backup file contains — for the pre-restore preview."""
    info = {
        "created_at": snapshot.get("created_at"),
        "user": snapshot.get("user"),
        "personal": {k: len(v) for k, v in (snapshot.get("personal") or {}).items()
                     if v},
        "businesses": [],
    }
    for biz in snapshot.get("businesses") or []:
        info["businesses"].append({
            "name": biz.get("name"),
            "sole_owner": bool(biz.get("sole_owner")),
            "rows": {k: len(v) for k, v in (biz.get("rows") or {}).items() if v},
        })
    return info
