"""Loan & Investment (বিনিয়োগ ও ঋণ).

Two kinds of outside money that lands in the business and later leaves it:

* **Loan** — money borrowed and owed back. Taking it *adds* to the main
  (cash) account; repaying it *subtracts* from the main account.
* **Investment** — capital contributed by an investor. Receiving it *adds* to
  the main account; the investor withdrawing their capital *subtracts* from
  the main account.

Every entry is posted through the one validated writer
(``apps.transactions.services.create_transaction``) as a cash transaction, so
the main account, the double-entry journal and this book can never drift
apart. Nothing is ever hard-deleted: a correction reverses the entry (and its
transaction) and posts a fresh one.
"""
from decimal import Decimal

from django.conf import settings
from django.core.validators import DecimalValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import SoftDeleteUUIDModel
from apps.organizations.models import Business


class LoanParty(SoftDeleteUUIDModel):
    """A lender (loan) or an investor (investment)."""

    class Kind(models.TextChoices):
        LOAN = "LOAN", "Loan — borrowed money"
        INVESTMENT = "INVESTMENT", "Investment — investor capital"

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="loan_parties")
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=12, choices=Kind.choices,
                             default=Kind.LOAN, db_index=True)
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="loan_parties_created")

    class Meta:
        ordering = ("kind", "name")
        indexes = [
            models.Index(fields=["business", "kind"]),
            models.Index(fields=["business", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} [{self.kind}]"

    # -- balances ---------------------------------------------------------
    def received_total(self) -> Decimal:
        total = self.entries.filter(
            kind=LoanEntry.Kind.RECEIVED, status=LoanEntry.Status.POSTED
        ).aggregate(t=models.Sum("amount"))["t"]
        return Decimal(str(total or 0)).quantize(Decimal("0.01"))

    def returned_total(self) -> Decimal:
        total = self.entries.filter(
            kind__in=[LoanEntry.Kind.REPAYMENT, LoanEntry.Kind.WITHDRAWAL],
            status=LoanEntry.Status.POSTED,
        ).aggregate(t=models.Sum("amount"))["t"]
        return Decimal(str(total or 0)).quantize(Decimal("0.01"))

    def payable_total(self) -> Decimal:
        """The contracted total this party must be paid back / paid.

        Loan entries may carry a `contract_total` (principal + interest);
        where it is absent — plain entries and the whole investment book —
        the payable is simply the amount received.
        """
        total = Decimal("0")
        for contract, amount in self.entries.filter(
            kind=LoanEntry.Kind.RECEIVED, status=LoanEntry.Status.POSTED,
        ).values_list("contract_total", "amount"):
            total += Decimal(str(contract if contract is not None else amount))
        return total.quantize(Decimal("0.01"))

    def interest_total(self) -> Decimal:
        """How much of the payable is interest (loan book; 0 otherwise)."""
        return (self.payable_total() - self.received_total()).quantize(
            Decimal("0.01"))

    def outstanding(self) -> Decimal:
        """What this party still has with the business.

        Payable-based: the loan book measures against the *contracted*
        total (so the real ২,১০,০০০ to repay shows, interest included),
        while the cash account only ever moves by actual money.
        """
        return (self.payable_total() - self.returned_total()).quantize(
            Decimal("0.01"))


class LoanEntry(SoftDeleteUUIDModel):
    """One movement of loan / investment money."""

    class Kind(models.TextChoices):
        RECEIVED = "RECEIVED", "Received — loan taken / capital invested"
        REPAYMENT = "REPAYMENT", "Repayment — loan paid back"
        WITHDRAWAL = "WITHDRAWAL", "Withdrawal — investor took capital back"

    class Status(models.TextChoices):
        POSTED = "POSTED", "Posted"
        REVERSED = "REVERSED", "Reversed"

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="loan_entries")
    party = models.ForeignKey(
        LoanParty, on_delete=models.PROTECT, related_name="entries")
    kind = models.CharField(max_length=12, choices=Kind.choices,
                             db_index=True)
    amount = models.DecimalField(
        max_digits=18, decimal_places=2, validators=[DecimalValidator(18, 2)])
    transaction_date = models.DateField(db_index=True)
    description = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices,
                               default=Status.POSTED, db_index=True)
    # Loan book only: the contracted total to repay (principal + interest).
    # The cash side always moves by `amount`; this field only raises the
    # party's payable, so "বাকি" can show the real ২,১০,০০০ while the cash
    # never saw more than the ১,৯০,০০০ actually received.
    contract_total = models.DecimalField(
        max_digits=18, decimal_places=2,
        validators=[DecimalValidator(18, 2)], null=True, blank=True,
        help_text="Loan book: agreed total to repay (principal + interest).")
    # The cash-side mirror written through the validated transaction writer.
    transaction = models.OneToOneField(
        "transactions.Transaction", null=True, blank=True,
        on_delete=models.PROTECT, related_name="loan_entry")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="loan_entries_created")

    class Meta:
        ordering = ("-transaction_date", "-created_at")
        indexes = [
            models.Index(fields=["business", "transaction_date"]),
            models.Index(fields=["party", "kind"]),
        ]

    def __str__(self) -> str:
        return (f"{self.kind} {self.amount} — {self.party.name} "
                f"@ {self.transaction_date}")

    @property
    def is_inflow(self) -> bool:
        """RECEIVED adds to the main account; repayment/withdrawal removes."""
        return self.kind == LoanEntry.Kind.RECEIVED

    @property
    def signed_amount(self) -> Decimal:
        amount = Decimal(str(self.amount))
        return amount if self.is_inflow else -amount


class LoanInstallment(SoftDeleteUUIDModel):
    """এক ঋণের কিস্তি প্ল্যান — "কত তারিখে কত".

    One row per planned installment of a loan repayment plan. Admin sets up
    a plan (first due date + number of installments); every row can be
    checked off once the money has been returned. Checking a row creates the
    matching REPAYMENT LoanEntry (and its cash transaction + journal), so the
    বাকি book, the cash drawer and the ledger all stay consistent.
    """

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="loan_installments")
    party = models.ForeignKey(
        LoanParty, on_delete=models.CASCADE, related_name="installments")
    sequence = models.PositiveIntegerField()
    due_date = models.DateField(db_index=True)
    amount = models.DecimalField(
        max_digits=18, decimal_places=2,
        validators=[DecimalValidator(18, 2)])
    is_paid = models.BooleanField(default=False, db_index=True)
    paid_date = models.DateField(null=True, blank=True)
    # The REPAYMENT entry created when the row was checked off. Unchecking
    # reverses that entry (row, cash transaction and journal together).
    paid_entry = models.ForeignKey(
        LoanEntry, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="installments_paid")
    notes = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="loan_installments_created")

    class Meta:
        ordering = ("due_date", "sequence")
        indexes = [
            models.Index(fields=["business", "due_date"]),
            models.Index(fields=["party", "is_paid"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["party", "sequence", "due_date"],
                condition=models.Q(deleted_at__isnull=True),
                name="uniq_active_installment",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.party.name} · কিস্তি {self.sequence} · {self.due_date}"

    @property
    def is_overdue(self) -> bool:
        from django.utils import timezone
        return not self.is_paid and self.due_date < timezone.localdate()
