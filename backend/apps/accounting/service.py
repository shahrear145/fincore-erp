"""Journal posting service (Phase 5) — the single validated writer.

Every entry (manual or auto) passes through `create_journal_entry`:
- Σdebit must equal Σcredit and be > 0.
- Accounts must belong to the business and be active.
- Locked financial periods reject backdated entries.
- Reversals append a mirrored entry linked via reversal_of — the original
  is never edited or deleted, and the trial balance stays balanced.
"""
from decimal import Decimal

from django.db import transaction as db_transaction

from apps.accounting.models import Account, JournalEntry, JournalLine
from apps.core.middleware import record_audit


class AccountingError(Exception):
    def __init__(self, message, status_code=400, errors=None):
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or {}


def _money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _make_journal_no(business, created_at) -> str:
    day = created_at.strftime("%Y%m%d")
    count = business.journal_entries.filter(
        journal_no__startswith=f"JE-{day}-").count() + 1
    return f"JE-{day}-{count:04d}"


def create_journal_entry(business, *, transaction_date, lines, memo="",
                         source_kind="MANUAL", source_id=None, user=None,
                         client_reference="", check_locked=True,
                         record=True):
    """lines: [{account: id-or-Account, debit, credit, note}].

    Internal writer used by both the manual API and the auto-posting engine.
    """
    if transaction_date is None:
        raise AccountingError({"transaction_date": "A date is required."})

    if client_reference:
        existing = JournalEntry.objects.filter(
            business=business, client_reference=client_reference).first()
        if existing is not None:
            return existing

    prepared = []
    total_debit = Decimal("0.00")
    total_credit = Decimal("0.00")
    for idx, raw in enumerate(lines):
        account = raw.get("account")
        if account is None:
            raise AccountingError(
                {"lines": f"Row {idx + 1}: account is required."})
        if not isinstance(account, Account):
            account = business.accounts.filter(pk=account).first()
            if account is None:
                raise AccountingError(
                    {"lines": f"Row {idx + 1}: account not found in this "
                              "business."})
        if not account.is_active:
            raise AccountingError(
                {"lines": f"Row {idx + 1}: account {account.name} is "
                          "inactive."})
        debit = _money(raw.get("debit", 0) or 0)
        credit = _money(raw.get("credit", 0) or 0)
        if debit < 0 or credit < 0:
            raise AccountingError(
                {"lines": f"Row {idx + 1}: debit/credit cannot be negative."})
        prepared.append((account, debit, credit, raw.get("note", "")))
        total_debit += debit
        total_credit += credit

    total_debit = total_debit.quantize(Decimal("0.01"))
    total_credit = total_credit.quantize(Decimal("0.01"))
    if total_debit != total_credit or total_debit == 0:
        raise AccountingError(
            {"lines": f"Entry is not balanced: debits ৳{total_debit} ≠ "
                      f"credits ৳{total_credit}. Double-entry requires "
                      "equal totals that are greater than zero."})
    if len(prepared) < 2:
        raise AccountingError({"lines": "At least two lines are required."})

    if check_locked:
        from apps.organizations.models import FinancialPeriod
        FinancialPeriod.assert_date_not_locked(business, transaction_date)

    with db_transaction.atomic():
        entry = JournalEntry.objects.create(
            business=business, journal_no="", transaction_date=transaction_date,
            memo=memo or "", source_kind=source_kind or "MANUAL",
            source_id=source_id, client_reference=client_reference or "",
            created_by=user,
        )
        entry.journal_no = _make_journal_no(business, entry.created_at)
        entry.save(update_fields=["journal_no"])
        for i, (account, debit, credit, note) in enumerate(prepared, start=1):
            JournalLine.objects.create(
                entry=entry, account=account, debit=debit, credit=credit,
                line_no=i, note=note or "",
            )
        if record:
            record_audit("create", entry, business=business,
                         resource_type="JournalEntry")
    return entry


def reverse_journal_entry(business, entry, *, reason="", user=None):
    """ADMIN-only mirror reversal: original untouched, links recorded."""
    from apps.core.middleware import record_audit as _audit

    if entry.business_id != business.id:
        raise AccountingError("Not this business's journal entry.")
    if entry.reversals.exists():
        raise AccountingError("This journal entry is already reversed.")

    mirror = create_journal_entry(
        business,
        transaction_date=entry.transaction_date,
        lines=[{"account": l.account_id, "debit": l.credit,
                "credit": l.debit, "note": l.note}
               for l in entry.lines.order_by("line_no")],
        memo=(f"Reversal of {entry.journal_no}"
              + (f": {reason}" if reason else "")),
        source_kind=entry.source_kind,
        source_id=entry.source_id,
        user=user, check_locked=False, record=False,
    )
    # Link both directions through the reversal_of FK on the mirror.
    JournalEntry.objects.filter(pk=mirror.pk).update(reversal_of=entry)
    mirror.refresh_from_db()
    _audit("reversal", entry, business=business, resource_type="JournalEntry",
           reason=reason, changes={"reversed_by": str(mirror.journal_no)})
    return mirror
