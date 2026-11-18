"""The baki (due) book's purge service.

Erases one wrong baki entry for good — row, due movement, allocations and
its double-entry postings — so the ledger can never keep money that belongs
to a deleted row.

A plain reversal (the ledger-safe default) keeps the row behind, marked
REVERSED. That is the right thing for an audit trail. But when the shop says
"এই ভুল এন্ট্রিটা মুছে দাও" and the entry is *not* linked to any other
money, it must really go — and the taka must still be exactly right
afterwards.

Rules
-----
* A **collection (PAYMENT)** that still has active allocations against
  credit lines cannot be purged — the money is linked. It is *reversed*
  instead.
* A **baki (CREDIT)** that has already been collected against cannot be
  purged — the collections took money from it. It is *reversed* instead.
* Anything else: row, due movement, allocations and journal postings are all
  erased, and the cash/drawer balance returns to exactly what it was before
  the entry was posted.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction as db_transaction

from django.db.models.query import QuerySet

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def _q(value) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(CENT)
    except Exception:
        return ZERO


def can_purge(txn) -> bool:
    """Is this entry free to erase completely?

    Returns False when the entry's money is linked to another entry:
      * a payment that still has live allocations to credit lines, or
      * a baki entry that has already been collected against.

    In both cases the caller should *reverse* instead of purge, so no taka
    is ever lost or doubled.
    """
    if txn is None:
        return False
    from apps.customers.models import PaymentAllocation, DueEntry

    # this payment still holds money from credit lines
    if txn.allocations.filter(freed_at__isnull=True).exists():
        return False
    # this baki entry has been paid against — collections hold its money
    if txn.due_entries.filter(allocations__freed_at__isnull=True).exists():
        return False
    return True


def purge_due_transaction(txn, user=None):
    """Erase one baki entry for good — no ghost money left behind.

    Steps (all inside one atomic transaction):
      1. Give back to each credit line whatever this payment allocated to it
         (restores `remaining` on those DueEntry rows).
      2. Delete all allocations belonging to this transaction.
      3. Delete all due movements this transaction made.
      4. Delete both sides of its double-entry journal postings.
      5. Hard-delete the transaction row itself.
    """
    from apps.accounting.models import JournalEntry, JournalLine
    from apps.customers.models import DueEntry, PaymentAllocation
    from apps.transactions.models import Transaction

    details = {
        "customer": str(txn.customer_id or ""),
        "type": txn.transaction_type,
        "amount": str(txn.amount),
        "date": str(txn.transaction_date),
    }

    with db_transaction.atomic():
        # 1) restore `remaining` on credit lines this payment consumed
        for alloc in PaymentAllocation.objects.filter(
                collection_transaction=txn, freed_at__isnull=True):
            entry = alloc.credit_entry
            left = entry.remaining if entry.remaining is not None else entry.amount
            entry.remaining = _q(left) + _q(alloc.amount)
            entry.save(update_fields=["remaining"])

        # 2) drop every allocation tied to this transaction
        PaymentAllocation.objects.filter(collection_transaction=txn).delete()

        # 3) erase the due movements this entry created
        DueEntry.objects.filter(transaction=txn).delete()

        # 4) erase both sides of the double-entry postings
        entries = JournalEntry.objects.filter(
            business=txn.business,
            source_kind__in=("TXN", "TXN_REVERSAL"),
            source_id=txn.id)
        JournalLine.objects.filter(entry__in=entries).delete()
        entries.delete()

        # 5) hard-delete the transaction row itself (bypass soft-delete)
        QuerySet.delete(Transaction.all_objects.filter(pk=txn.pk))

    try:
        from apps.core.middleware import record_audit
        record_audit("hard_delete", txn, business=txn.business,
                     reason="Deleted from baki history", changes=details)
    except Exception:
        pass
    return txn
