"""Loan & Investment service — the validated writer for outside money.

Rules enforced here (all inside one atomic block):

- **Loan taken / capital invested** → the amount is *added* to the main
  (cash) account.
- **Loan repaid / investor withdrew** → the amount is *subtracted* from the
  main account.
- A repayment or withdrawal may never exceed what that party still has with
  the business (no negative outstanding).
- Every entry is written through
  ``apps.transactions.services.create_transaction`` so the cash account, the
  double-entry journal and this book can never drift apart.
- Nothing is hard-deleted: a correction reverses the entry and its
  transaction (ledger-safe), then posts a fresh one.
"""
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Sum

from apps.core.middleware import record_audit
from apps.loans.models import LoanEntry, LoanParty
from apps.transactions.models import PaymentMethod, TransactionType
from apps.transactions.services import (
    TransactionServiceError, create_transaction, reverse_transaction,
)

CENT = Decimal("0.01")

#: Which cash transaction type backs each (party kind, entry kind) pair.
ENTRY_TYPE = {
    (LoanParty.Kind.LOAN, LoanEntry.Kind.RECEIVED): TransactionType.BORROW,
    (LoanParty.Kind.LOAN, LoanEntry.Kind.REPAYMENT): TransactionType.REPAY,
    (LoanParty.Kind.LOAN, LoanEntry.Kind.WITHDRAWAL): TransactionType.REPAY,
    (LoanParty.Kind.INVESTMENT, LoanEntry.Kind.RECEIVED): TransactionType.INVEST,
    (LoanParty.Kind.INVESTMENT, LoanEntry.Kind.REPAYMENT):
        TransactionType.WITHDRAW,
    (LoanParty.Kind.INVESTMENT, LoanEntry.Kind.WITHDRAWAL):
        TransactionType.WITHDRAW,
}


def _money(value) -> Decimal:
    try:
        d = Decimal(str(value))
    except Exception:
        raise TransactionServiceError("amount must be a valid decimal number.")
    return d.quantize(CENT)


# ------------------------------------------------------------------ parties --

def create_party(request, *, business, name, kind=LoanParty.Kind.LOAN,
                 phone="", email="", notes=""):
    """Create a lender / investor."""
    name = (name or "").strip()
    if not name:
        raise TransactionServiceError(
            {"name": "The lender / investor name is required."})
    if kind not in LoanParty.Kind.values:
        raise TransactionServiceError(
            {"kind": "Choose either Loan or Investment."})
    with db_transaction.atomic():
        party = LoanParty.objects.create(
            business=business, name=name, kind=kind,
            phone=(phone or "").strip(), email=(email or "").strip(),
            notes=(notes or "").strip(), created_by=request.user)
        try:
            record_audit("create", party, business=business)
        except Exception:
            pass
    return party


# ------------------------------------------------------------------ entries --

def create_entry(request, *, business, party, kind, amount, transaction_date,
                 description="", notes="", contract_total=None):
    """Post one loan / investment movement and its cash-side transaction."""
    if kind not in LoanEntry.Kind.values:
        raise TransactionServiceError(
            {"kind": "Unknown entry type."})
    if party is None or party.business_id != business.id:
        raise TransactionServiceError(
            {"party": "Unknown lender / investor for this business."})
    amount = _money(amount)
    if amount <= 0:
        raise TransactionServiceError("amount must be greater than zero.")
    if not transaction_date:
        raise TransactionServiceError(
            {"transaction_date": "A date is required."})

    # Loan book only: the optional contracted total to repay (principal +
    # interest). It never moves the cash account — it only raises what the
    # party's card reports as "দিতে হবে / বাকি".
    ct_value = None
    if contract_total not in (None, ""):
        if kind != LoanEntry.Kind.RECEIVED:
            raise TransactionServiceError({
                "contract_total": (
                    "মোট ফেরত (চুক্তি) শুধু ঋণ নেওয়ার এন্ট্রিতে দেওয়া যায়। "
                    "(Total to repay only applies to a loan-taken entry.)")})
        ct_value = _money(contract_total)
        if ct_value < amount:
            raise TransactionServiceError({
                "contract_total": (
                    f"মোট ফেরত (সুদসহ) ঋণের টাকা ({amount:,.2f}) থেকে কম হতে "
                    f"পারে না। (The total to repay can't be less than the "
                    f"loan amount {amount:,.2f}.)")})

    if kind != LoanEntry.Kind.RECEIVED:
        outstanding = party.outstanding()
        if amount > outstanding:
            raise TransactionServiceError({
                "amount": (
                    f"{party.name} এর কাছে ব্যবসার সাথে এখনো "
                    f"{outstanding:,.2f} আছে — এর বেশি ফেরত দেওয়া যাবে না। "
                    f"(Only {outstanding:,.2f} is still with the business.)")})

    txn_type = ENTRY_TYPE[(party.kind, kind)]
    with db_transaction.atomic():
        txn, _created = create_transaction(
            request, business=business,
            amount=amount,
            transaction_type=txn_type,
            transaction_date=transaction_date,
            payment_method=PaymentMethod.CASH,
            description=(description or "").strip() or f"{party.name}",
            notes=(notes or "").strip(),
            client_reference="",
        )
        entry = LoanEntry.objects.create(
            business=business, party=party, kind=kind, amount=amount,
            contract_total=ct_value,
            transaction_date=transaction_date,
            description=(description or "").strip(), notes=(notes or "").strip(),
            status=LoanEntry.Status.POSTED, transaction=txn,
            created_by=request.user)
        try:
            record_audit("create", entry, business=business,
                         changes={"kind": kind, "amount": str(amount)})
        except Exception:
            pass
    return entry


def reverse_entry(request, entry, reason=""):
    """Reverse an entry (and its cash transaction). Never a hard delete."""
    if entry.status == LoanEntry.Status.REVERSED:
        raise TransactionServiceError("This entry is already reversed.")
    if entry.transaction_id:
        # A repayment/withdrawal that was already returned must not be
        # reversed into a negative outstanding balance.
        if entry.kind != LoanEntry.Kind.RECEIVED:
            capital = Decimal("0")
            for contract, amount in LoanEntry.objects.filter(
                party=entry.party,
                kind=LoanEntry.Kind.RECEIVED,
                status=LoanEntry.Status.POSTED,
            ).exclude(pk=entry.pk).values_list("contract_total", "amount"):
                capital += Decimal(str(contract if contract is not None
                                       else amount))
            returned = LoanEntry.objects.filter(
                party=entry.party,
                kind__in=[LoanEntry.Kind.REPAYMENT, LoanEntry.Kind.WITHDRAWAL],
                status=LoanEntry.Status.POSTED,
            ).exclude(pk=entry.pk).aggregate(t=Sum("amount"))["t"]
            outstanding = (capital
                           - Decimal(str(returned or 0))).quantize(CENT)
            if outstanding < 0:
                raise TransactionServiceError(
                    "Reversing this entry would make the outstanding balance "
                    "negative.")

    with db_transaction.atomic():
        if entry.transaction_id:
            txn = entry.transaction
            txn.refresh_from_db()
            reverse_transaction(request, txn, reason=reason or
                                "Reversed loan entry")
        entry.status = LoanEntry.Status.REVERSED
        entry.save(update_fields=["status", "updated_at"])
        try:
            record_audit("reversal", entry, business=entry.business,
                         reason=reason, changes={"status": "REVERSED"})
        except Exception:
            pass
    return entry


def hard_delete_entry(request, entry, reason=""):
    """Remove an entry completely — row, transaction and its postings.

    A reversal keeps the row (marked REVERSED); this one erases it for
    good, which is what the shop asked for when clearing a wrong
    investment entry. The double-entry postings go with it, so the ledger
    can never keep money that belongs to a deleted row — the cash
    balance returns to exactly what it was before the entry was posted.
    """
    business = entry.business
    party = entry.party
    txn = entry.transaction if entry.transaction_id else None
    details = {
        "party": party.name, "kind": entry.kind,
        "amount": str(entry.amount), "date": str(entry.transaction_date),
    }
    with db_transaction.atomic():
        # the entry row first — LoanEntry.transaction is PROTECT, so the
        # transaction may only go after its entry is gone
        from django.db.models.query import QuerySet
        QuerySet.delete(LoanEntry.all_objects.filter(pk=entry.pk))
        if txn is not None:
            _purge_transaction(txn)
        # the party goes too when nothing is left of it
        if not LoanEntry.objects.filter(party=party).exists():
            QuerySet.delete(LoanParty.objects.filter(pk=party.pk))
        try:
            record_audit("hard_delete", entry, business=business,
                         reason=reason or "Deleted from history",
                         changes=details)
        except Exception:
            pass
    return entry


def _purge_transaction(txn):
    """Delete a transaction with its journal postings (no ghost money)."""
    from django.db.models.query import QuerySet

    from apps.accounting.models import JournalEntry, JournalLine
    from apps.customers.models import DueEntry

    entries = JournalEntry.objects.filter(
        business=txn.business, source_kind__in=("TXN", "TXN_REVERSAL"),
        source_id=txn.id)
    JournalLine.objects.filter(entry__in=entries).delete()
    entries.delete()
    # a customer-book link (should not exist for investments, but never
    # leave a dangling reference behind)
    DueEntry.objects.filter(transaction=txn).update(transaction=None)
    from apps.transactions.models import Transaction
    QuerySet.delete(Transaction.all_objects.filter(pk=txn.pk))


# --------------------------------------------------------------- summaries --

def _sum(qs) -> Decimal:
    total = qs.aggregate(t=Sum("amount"))["t"]
    return Decimal(str(total or 0)).quantize(CENT)


def summarize(business, *, kind=None, from_date=None, to_date=None) -> dict:
    """High-level figures for ONE book (loan or investment).

    Every number is derived from posted entries only, so the totals, the
    per-party balances and the main-account impact can never disagree.

    ``kind`` narrows the book:
      * ``LoanParty.Kind.LOAN``       → the loan book (lenders)
      * ``LoanParty.Kind.INVESTMENT`` → the investment book (investors)
      * ``None``                      → both books combined
    """
    entries = LoanEntry.objects.filter(
        business=business, status=LoanEntry.Status.POSTED)
    if kind:
        entries = entries.filter(party__kind=kind)
    if from_date:
        entries = entries.filter(transaction_date__gte=from_date)
    if to_date:
        entries = entries.filter(transaction_date__lte=to_date)

    received = _sum(entries.filter(kind=LoanEntry.Kind.RECEIVED))
    returned = _sum(entries.filter(
        kind__in=[LoanEntry.Kind.REPAYMENT, LoanEntry.Kind.WITHDRAWAL]))

    # The contracted total to repay (loan book entries may carry a
    # contract_total — principal + interest). The *cash* side keeps moving
    # by actual money only; this just raises the liability the book shows.
    payable = Decimal("0")
    for contract, amount in entries.filter(
        kind=LoanEntry.Kind.RECEIVED,
    ).values_list("contract_total", "amount"):
        payable += Decimal(str(contract if contract is not None else amount))
    payable = payable.quantize(CENT)

    parties = LoanParty.objects.filter(business=business, is_active=True)
    if kind:
        parties = parties.filter(kind=kind)

    return {
        "kind": kind,
        "received": received,
        "returned": returned,
        "payable": payable,
        "interest_total": (payable - received).quantize(CENT),
        "outstanding": (payable - returned).quantize(CENT),
        "total_in": received,
        "total_out": returned,
        "net_main_account": (received - returned).quantize(CENT),
        "total_outstanding": (payable - returned).quantize(CENT),
        "entry_count": entries.count(),
        "party_count": parties.count(),
    }


def summarize_both(business, *, from_date=None, to_date=None) -> dict:
    """Both books side by side (used by the dashboard)."""
    loan = summarize(business, kind=LoanParty.Kind.LOAN,
                     from_date=from_date, to_date=to_date)
    invest = summarize(business, kind=LoanParty.Kind.INVESTMENT,
                       from_date=from_date, to_date=to_date)
    return {
        "loan": loan,
        "invest": invest,
        "total_in": (loan["total_in"] + invest["total_in"]).quantize(CENT),
        "total_out": (loan["total_out"] + invest["total_out"]).quantize(CENT),
        "net_main_account": (loan["net_main_account"]
                              + invest["net_main_account"]).quantize(CENT),
        "total_outstanding": (loan["outstanding"]
                               + invest["outstanding"]).quantize(CENT),
    }


def party_rows(business, *, kind=None, from_date=None, to_date=None):
    """Per-party received / returned / outstanding rows of ONE book."""
    rows = []
    parties = LoanParty.objects.filter(business=business)
    if kind:
        parties = parties.filter(kind=kind)
    parties = parties.order_by("name")
    for party in parties:
        entries = party.entries.filter(status=LoanEntry.Status.POSTED)
        if from_date:
            entries = entries.filter(transaction_date__gte=from_date)
        if to_date:
            entries = entries.filter(transaction_date__lte=to_date)
        received = _sum(entries.filter(kind=LoanEntry.Kind.RECEIVED))
        returned = _sum(entries.filter(
            kind__in=[LoanEntry.Kind.REPAYMENT, LoanEntry.Kind.WITHDRAWAL]))
        pay_total = party.payable_total()
        rows.append({
            "party": party,
            "received": received,
            "returned": returned,
            "pay_total": pay_total,
            "interest": (pay_total - received).quantize(CENT),
            "outstanding": (pay_total - returned).quantize(CENT),
            "entry_count": entries.count(),
        })
    return rows
