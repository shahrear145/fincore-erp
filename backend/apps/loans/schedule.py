"""কিস্তি প্ল্যান — installment schedules for the loan book.

Admin builds one schedule per lender (first due date + count). Rows are
generated evenly; the last row absorbs the rounding remainder so the
installments always sum back to the contracted total.

Checking a row off posts the matching REPAYMENT entry through the one
validated writer (``apps.loans.service.create_entry``) — cash transaction,
journal and this book move together. Unchecking reverses that entry (row +
transaction + postings), so a single source of truth stays intact.
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction as db_transaction
from django.utils import timezone

from apps.loans.models import LoanEntry, LoanInstallment, LoanParty
from apps.loans.service import create_entry, reverse_entry

CENT = Decimal("0.01")


class ScheduleError(Exception):
    """User-facing problem with a schedule action."""


def _due_date(start: date, seq: int) -> date:
    """The seq-th monthly due date, anchored to ``start``'s day of month.

    Jan 31 → Feb 28 (clamped) → **Mar 31** (back to the anchor day) — so a
    plan anchored on the 31st keeps hitting month ends.
    """
    month_index = start.month - 1 + (seq - 1)
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def create_schedule(request, *, business, party, total, count,
                    first_due, notes: str = "") -> list[LoanInstallment]:
    """Generate ``count`` rows summing exactly to ``total``."""
    try:
        total = Decimal(str(total)).quantize(CENT, rounding=ROUND_HALF_UP)
        count = int(count)
    except Exception:
        raise ScheduleError("Enter a valid total and number of installments.")

    if party is None or party.business_id != business.id:
        raise ScheduleError("Unknown lender for this business.")
    if party.kind != LoanParty.Kind.LOAN:
        raise ScheduleError("Installment schedules only make sense for loans.")
    if total <= 0:
        raise ScheduleError("Total to repay must be greater than zero.")
    if not (1 <= count <= 360):
        raise ScheduleError("Number of installments must be between 1 and 360.")
    if party.installments.exists():
        raise ScheduleError("This lender already has a schedule — delete it first.")

    base = (total / Decimal(count)).quantize(CENT, rounding=ROUND_HALF_UP)
    rows: list[LoanInstallment] = []
    remaining = total
    for seq in range(1, count + 1):
        amount = remaining if seq == count else base
        rows.append(LoanInstallment(
            business=business, party=party, sequence=seq,
            due_date=_due_date(first_due, seq),
            amount=amount, notes=(notes or "").strip(),
            created_by=getattr(request, "user", None)))
        remaining -= amount

    with db_transaction.atomic():
        LoanInstallment.objects.bulk_create(rows)
    return list(party.installments.order_by("due_date", "sequence"))


def schedule_groups(business, today: date | None = None) -> list[dict]:
    """One summary dict per lender that has a schedule."""
    today = today or timezone.localdate()
    groups: list[dict] = []
    parties = (LoanParty.objects
               .filter(business=business, kind=LoanParty.Kind.LOAN,
                       installments__isnull=False)
               .prefetch_related("installments")
               .distinct().order_by("name"))
    for party in parties:
        installs = list(party.installments.order_by("due_date", "sequence"))
        paid = [i for i in installs if i.is_paid]
        unpaid = [i for i in installs if not i.is_paid]
        total = sum((i.amount for i in installs), Decimal("0.00"))
        paid_total = sum((i.amount for i in paid), Decimal("0.00"))
        next_due = min((i.due_date for i in unpaid), default=None)
        groups.append({
            "party": party,
            "rows": installs,
            "installment_count": len(installs),
            "paid_count": len(paid),
            "unpaid_count": len(unpaid),
            "overdue_count": len([i for i in unpaid if i.due_date < today]),
            "total": total,
            "paid_total": paid_total,
            "remaining": total - paid_total,
            "next_due": next_due,
            "is_fully_paid": not unpaid,
            "all_paid_sum_check": paid_total + (total - paid_total) == total,
        })
    return groups


def pay_installment(request, *, business, installment_id,
                    paid_date: date | None = None) -> LoanInstallment:
    """Check a row off: post the REPAYMENT entry + link it to the row."""
    installment = (LoanInstallment.objects
                   .filter(business=business, pk=installment_id)
                   .select_related("party")
                   .first())
    if installment is None:
        raise ScheduleError("Installment not found.")
    if installment.is_paid:
        raise ScheduleError("This installment is already checked off.")

    paid_date = paid_date or timezone.localdate()
    label = f"কিস্তি {installment.sequence} · {installment.party.name}"
    with db_transaction.atomic():
        entry = create_entry(
            request, business=business, party=installment.party,
            kind=LoanEntry.Kind.REPAYMENT, amount=installment.amount,
            transaction_date=paid_date, description=label, notes="")
        installment.is_paid = True
        installment.paid_date = paid_date
        installment.paid_entry = entry
        installment.save(update_fields=["is_paid", "paid_date", "paid_entry",
                                        "updated_at"])
    return installment


def unpay_installment(request, *, business, installment_id) -> LoanInstallment:
    """Uncheck a row: reverse the linked REPAYMENT entry."""
    installment = (LoanInstallment.objects
                   .filter(business=business, pk=installment_id)
                   .select_related("party", "paid_entry")
                   .first())
    if installment is None:
        raise ScheduleError("Installment not found.")
    if not installment.is_paid:
        raise ScheduleError("This installment is not checked off yet.")

    with db_transaction.atomic():
        if installment.paid_entry_id:
            entry = installment.paid_entry
            reverse_entry(request, entry,
                          reason=f"Unchecked installment {installment.sequence}")
        installment.is_paid = False
        installment.paid_date = None
        installment.paid_entry = None
        installment.save(update_fields=["is_paid", "paid_date", "paid_entry",
                                        "updated_at"])
    return installment


def delete_schedule(request, *, business, party_id) -> int:
    """Delete all pending rows of one schedule. Paid rows must be undone first."""
    party = (LoanParty.objects.filter(business=business, pk=party_id)
             .first())
    if party is None:
        raise ScheduleError("Unknown lender.")
    if party.installments.filter(is_paid=True).exists():
        raise ScheduleError(
            "Some installments are already checked off — uncheck them first.")

    with db_transaction.atomic():
        return party.installments.all().delete()
