"""Delete all accounting data that belongs to one user — completely.

Why this module exists
----------------------
The reset page used to do the wipe inline and wrapped every section in
``try/except Exception: pass``. That hid two classes of failure:

* wrong model names — it imported ``customers.DueAllocation`` (the real model
  is ``PaymentAllocation``), ``personal.Transfer``, ``personal.Budget``,
  ``personal.Debt``, ``sync.SyncOperation``, ``ops.Notification`` … so the
  whole customers/dues and personal-finance sections were *skipped on every
  reset*;
* PROTECT foreign keys — rows that still referenced a business (customers,
  loan entries, allocations) made the final business delete fail, and the
  exception was swallowed. The result: the membership rows were gone, the
  business and all of its records stayed, and the owner could no longer see
  any of it. The data looked deleted but was not.

This module fixes both:

* it deletes in dependency order and retries until nothing is left, so
  PROTECT chains cannot strand a row;
* it only touches the businesses the user belongs to (plus their own
  personal finance / assistant / sync / session rows) — other users' data is
  never at risk;
* if anything survives it raises :class:`ResetFailed` so the caller rolls the
  whole transaction back and tells the user, instead of reporting success on
  a half wipe.
"""
from django.apps import apps
from django.db import transaction
from django.db.models import Q


class ResetFailed(RuntimeError):
    """Some rows could not be deleted; the transaction must be rolled back."""


def businesses_for(user):
    """Every business this user can reach — owned or joined, soft-deleted too."""
    from apps.organizations.models import Business

    return Business.all_objects.filter(
        Q(owner=user) | Q(memberships__user=user)
    ).distinct()


def _model(label):
    return apps.get_model(label)


def _business_tasks(biz_ids):
    """(label, queryset factory) in dependency order.

    Children before parents: allocations before the transactions/entries they
    point at, journal lines before accounts, transactions before the customers
    and partners they PROTECT, and memberships before the business itself.
    """
    from apps.accounting.models import JournalLine
    from apps.customers.models import (
        CustomerAssignment,
        CustomerDocument,
        DueEntry,
        PaymentAllocation,
    )

    return [
        # --- leaves that reach the business through a parent ---------------
        ("customers.PaymentAllocation", lambda: PaymentAllocation.objects.filter(
            Q(collection_transaction__business_id__in=biz_ids)
            | Q(credit_entry__assignment__customer__business_id__in=biz_ids))),
        ("accounting.JournalLine",
         lambda: JournalLine.objects.filter(account__business_id__in=biz_ids)),
        ("customers.DueEntry",
         lambda: DueEntry.objects.filter(assignment__customer__business_id__in=biz_ids)),
        ("customers.CustomerDocument",
         lambda: CustomerDocument.objects.filter(customer__business_id__in=biz_ids)),
        ("customers.CustomerAssignment",
         lambda: CustomerAssignment.objects.filter(customer__business_id__in=biz_ids)),
        # --- rows that carry the business FK --------------------------------
        ("transactions.Transaction",
         lambda: _model("transactions.Transaction").all_objects.filter(
             business_id__in=biz_ids)),
        ("loans.LoanEntry",
         lambda: _model("loans.LoanEntry").all_objects.filter(
             business_id__in=biz_ids)),
        ("loans.LoanParty",
         lambda: _model("loans.LoanParty").all_objects.filter(
             business_id__in=biz_ids)),
        ("accounting.JournalEntry",
         lambda: _model("accounting.JournalEntry").objects.filter(
             business_id__in=biz_ids)),
        ("accounting.Account",
         lambda: _model("accounting.Account").objects.filter(
             business_id__in=biz_ids)),
        ("partners.Partner",
         lambda: _model("partners.Partner").all_objects.filter(
             business_id__in=biz_ids)),
        ("customers.Customer",
         lambda: _model("customers.Customer").all_objects.filter(
             business_id__in=biz_ids)),
        ("customers.DueReminder",
         lambda: _model("customers.DueReminder").objects.filter(
             business_id__in=biz_ids)),
        ("transactions.RecurringTransaction",
         lambda: _model("transactions.RecurringTransaction").all_objects.filter(
             business_id__in=biz_ids)),
        ("transactions.TransactionDateMemory",
         lambda: _model("transactions.TransactionDateMemory").objects.filter(
             business_id__in=biz_ids)),
        ("cashbook.CashCount",
         lambda: _model("cashbook.CashCount").objects.filter(
             business_id__in=biz_ids)),
        ("cashbook.StockSnapshot",
         lambda: _model("cashbook.StockSnapshot").objects.filter(
             business_id__in=biz_ids)),
        ("assistant.AssistantQuery",
         lambda: _model("assistant.AssistantQuery").objects.filter(
             business_id__in=biz_ids)),
        ("organizations.FinancialPeriod",
         lambda: _model("organizations.FinancialPeriod").objects.filter(
             business_id__in=biz_ids)),
        ("core.AuditLog",
         lambda: _model("core.AuditLog").objects.filter(
             business_id__in=biz_ids)),
        ("organizations.BusinessMembership",
         lambda: _model("organizations.BusinessMembership").objects.filter(
             business_id__in=biz_ids)),
    ]


def _personal_tasks(user):
    from apps.personal.models import (
        PersonalAccount,
        PersonalBudget,
        PersonalCategory,
        PersonalLoan,
        PersonalTransaction,
        SavingsGoal,
    )

    return [
        ("personal.PersonalTransaction",
         lambda: PersonalTransaction.all_objects.filter(user=user)),
        ("personal.PersonalBudget",
         lambda: PersonalBudget.objects.filter(user=user)),
        ("personal.SavingsGoal",
         lambda: SavingsGoal.objects.filter(user=user)),
        ("personal.PersonalLoan",
         lambda: PersonalLoan.objects.filter(user=user)),
        ("personal.PersonalAccount",
         lambda: PersonalAccount.all_objects.filter(user=user)),
        ("personal.PersonalCategory",
         lambda: PersonalCategory.objects.filter(user=user)),
    ]


def _user_tasks(user):
    """Non-accounting rows of this user (assistant, sync, sessions, audit)."""
    from apps.assistant.models import AssistantQuery
    from apps.core.middleware import AuditLog
    from apps.ops.models import BackupRecord, SessionRecord, TOTPDevice, TrustedDevice
    from apps.sync.models import SyncCursor, SyncOp
    from apps.users.models import EmailOTP, LoginEvent, UserDeviceSession

    return [
        ("assistant.AssistantQuery",
         lambda: AssistantQuery.objects.filter(user=user)),
        ("sync.SyncOp", lambda: SyncOp.objects.filter(user=user)),
        ("sync.SyncCursor", lambda: SyncCursor.objects.filter(user=user)),
        ("ops.TrustedDevice", lambda: TrustedDevice.objects.filter(user=user)),
        ("ops.SessionRecord", lambda: SessionRecord.objects.filter(user=user)),
        ("ops.TOTPDevice", lambda: TOTPDevice.objects.filter(user=user)),
        ("ops.BackupRecord",
         lambda: BackupRecord.objects.filter(created_by=user)),
        ("users.UserDeviceSession",
         lambda: UserDeviceSession.objects.filter(user=user)),
        ("users.LoginEvent", lambda: LoginEvent.objects.filter(user=user)),
        ("users.EmailOTP", lambda: EmailOTP.objects.filter(user=user)),
        # business-scoped audit rows are already gone; this only clears the
        # user's personal-mode rows, so another business's trail stays intact
        ("core.AuditLog",
         lambda: AuditLog.objects.filter(business__isnull=True, actor=user)),
    ]


def _purge(qs):
    """Hard-delete every row in ``qs``; return (deleted, errors)."""
    model = qs.model
    soft = hasattr(model, "deleted_at")
    deleted, errors = 0, []
    for obj in list(qs.iterator()):
        try:
            if soft:
                obj.hard_delete()
            else:
                obj.delete()
            deleted += 1
        except Exception as exc:  # PROTECT — retried on the next pass
            errors.append(f"{model._meta.label}: {exc}")
    return deleted, errors


def _run_tasks(tasks, max_passes=25):
    """Delete until every task is empty, retrying blocked rows.

    PROTECT constraints mean an object can only go after the rows pointing at
    it; a pass deletes what it can, the next pass deletes what that freed.
    """
    deleted_total = 0
    last_leftovers = {}
    for _ in range(max_passes):
        leftovers = {}
        progressed = False
        for label, factory in tasks:
            try:
                qs = factory()
                if not qs.exists():
                    continue
                deleted, errors = _purge(qs)
                deleted_total += deleted
                if deleted:
                    progressed = True
                if factory().exists():
                    leftovers[label] = errors[0] if errors else "rows still present"
            except Exception as exc:
                leftovers[label] = str(exc)
        if not leftovers:
            return deleted_total
        last_leftovers = leftovers
        if not progressed:
            break
    raise ResetFailed(
        "Could not delete: " + "; ".join(
            f"{label} ({reason})" for label, reason in last_leftovers.items()))


def wipe_user_data(user):
    """Erase all of ``user``'s businesses and records, keeping the login.

    Runs in one transaction: on :class:`ResetFailed` nothing is committed.
    Returns a small summary dict for the UI.
    """
    from apps.organizations.models import Business

    with transaction.atomic():
        biz_ids = list(businesses_for(user).values_list("id", flat=True))

        rows = 0
        rows += _run_tasks(_business_tasks(biz_ids))
        rows += _run_tasks(_personal_tasks(user))
        rows += _run_tasks(_user_tasks(user))

        # The businesses themselves last — every PROTECTing row is gone now.
        business_failures = []
        for business in Business.all_objects.filter(pk__in=biz_ids):
            try:
                business.hard_delete()
                rows += 1
            except Exception as exc:
                business_failures.append(f"{business.name}: {exc}")
        if business_failures:
            raise ResetFailed("Could not delete businesses: " + "; ".join(business_failures))

        remaining = Business.all_objects.filter(pk__in=biz_ids).count()
        if remaining:
            raise ResetFailed(f"{remaining} business(es) could not be deleted")

        return {"businesses": len(biz_ids), "rows": rows}
