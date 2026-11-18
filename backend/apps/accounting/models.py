"""Double-entry accounting models (Phase 5).

Design rules:
- Chart of accounts per business; system accounts are created lazily the
  first time a posting needs them (backfills for existing businesses).
- JournalEntry + JournalLine: Σdebit == Σcredit enforced at the single
  writer (apps.accounting.service). A DB constraint additionally guarantees
  each line moves exactly one side.
- Reversals append a mirrored entry (reversal_of) — no edits, no deletes;
  trial balance stays balanced because both entries count.
- Two-date discipline: `transaction_date` drives every report.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models

from apps.core.models import UUIDModel
from apps.organizations.models import Business


class AccountType(models.TextChoices):
    ASSET = "ASSET", "Asset"
    LIABILITY = "LIABILITY", "Liability"
    EQUITY = "EQUITY", "Equity"
    INCOME = "INCOME", "Income"
    EXPENSE = "EXPENSE", "Expense"


DEBIT_NORMAL_TYPES = (AccountType.ASSET, AccountType.EXPENSE)


class Account(UUIDModel):
    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="accounts"
    )
    code = models.CharField(max_length=10, db_index=True)
    name = models.CharField(max_length=160)
    type = models.CharField(max_length=10, choices=AccountType.choices)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="children",
    )
    is_system = models.BooleanField(
        default=False, help_text="Auto-created control accounts.")
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ("business", "code")
        constraints = [
            models.UniqueConstraint(fields=["business", "code"],
                                    name="uniq_account_code_business"),
            models.UniqueConstraint(fields=["business", "name"],
                                    name="uniq_account_name_business"),
        ]

    def __str__(self):
        return f"{self.code} {self.name}"

    # --- Natural-sign balance from the line aggregate ---------------------

    def balance(self, upto=None, from_date=None, to_date=None,
                cache=None) -> Decimal:
        """Natural balance: debit-positive for ASSET/EXPENSE, credit-positive
        for LIABILITY/EQUITY/INCOME."""
        if cache is not None and self.id in cache:
            return cache[self.id]
        agg = JournalLine.objects.filter(
            entry__business=self.business, account=self,
            entry__transaction_date__lte=upto
        ).aggregate(d=models.Sum("debit"), c=models.Sum("credit"))
        d = Decimal(str(agg["d"] or "0"))
        c = Decimal(str(agg["c"] or "0"))
        if self.type in DEBIT_NORMAL_TYPES:
            val = d - c
        else:
            val = c - d
        if cache is not None:
            cache[self.id] = val
        return val


class JournalEntry(UUIDModel):
    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="journal_entries"
    )
    journal_no = models.CharField(max_length=40, blank=True, db_index=True)
    transaction_date = models.DateField(db_index=True)
    memo = models.CharField(max_length=200, blank=True)
    reversal_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT,
        related_name="reversals",
        help_text="Mirrored reversal entries link back — never edits.")
    source_kind = models.CharField(
        max_length=20, blank=True, db_index=True,
        help_text="TXN | SALE | PURCHASE | SALE_RETURN | PURCHASE_RETURN | MANUAL")
    source_id = models.UUIDField(null=True, blank=True)
    client_reference = models.CharField(max_length=64, blank=True,
                                        db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="journal_entries_created")

    class Meta:
        ordering = ("-transaction_date", "-created_at")
        indexes = [
            models.Index(fields=["business", "transaction_date"]),
            models.Index(fields=["business", "source_kind", "source_id"]),
        ]

    def __str__(self):
        return f"JE {self.journal_no} ৳{self.total_debit()}"

    def total_debit(self) -> Decimal:
        return sum((l.debit for l in self.lines.all()), Decimal("0.00"))

    def total_credit(self) -> Decimal:
        return sum((l.credit for l in self.lines.all()), Decimal("0.00"))


class JournalLine(UUIDModel):
    entry = models.ForeignKey(
        JournalEntry, on_delete=models.CASCADE, related_name="lines"
    )
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="journal_lines"
    )
    debit = models.DecimalField(max_digits=18, decimal_places=2,
                                default=Decimal("0.00"))
    credit = models.DecimalField(max_digits=18, decimal_places=2,
                                 default=Decimal("0.00"))
    line_no = models.PositiveIntegerField(default=1)
    note = models.CharField(max_length=160, blank=True)

    class Meta:
        ordering = ("entry", "line_no", "created_at")
        constraints = [
            # exactly one side > 0, never both, never negative
            models.CheckConstraint(
                check=(models.Q(debit__gte=0) & models.Q(credit__gte=0) &
                       ((models.Q(debit__gt=0) & models.Q(credit=0)) |
                        (models.Q(credit__gt=0) & models.Q(debit=0)))),
                name="journalline_one_side_only",
            ),
        ]

    def __str__(self):
        side = f"Dr {self.debit}" if self.debit else f"Cr {self.credit}"
        return f"{self.account} {side}"
