"""Transaction model (Sections 6, 9 & part of 8).

Money rules
-----------
- `amount` is DecimalField(18,2). Floats are rejected at the API boundary.
- Two-date discipline: `transaction_date` (business date — drives all
  reports, Section 8) vs `created_at` (record timestamp, never edited).
- No hard deletes: `delete`/reversal path replaces the record with a
  reversal transaction and marks status=REVERSED (Section 18).

Idempotency
-----------
`client_reference` (UUID from client/sync engine) is unique per business —
re-sending a transaction during offline sync never duplicates money.
"""
from django.conf import settings
from django.core.validators import DecimalValidator
from decimal import Decimal
from django.db import models

from apps.core.models import SoftDeleteUUIDModel
from apps.organizations.models import Business


class TransactionType(models.TextChoices):
    CREDIT = "CREDIT", "Credit / Due"
    PAYMENT = "PAYMENT", "Payment / Collection"
    INCOME = "INCOME", "Income"
    EXPENSE = "EXPENSE", "Expense"
    ADVANCE = "ADVANCE", "Advance"
    REFUND = "REFUND", "Refund"
    ADJUSTMENT = "ADJUSTMENT", "Adjustment"
    TRANSFER = "TRANSFER", "Transfer"
    BORROW = "BORROW", "Loan taken (cash in)"
    REPAY = "REPAY", "Loan repaid (cash out)"
    INVEST = "INVEST", "Investment received (cash in)"
    WITHDRAW = "WITHDRAW", "Investment withdrawn (cash out)"


#: Types where money leaves the business (used by the cash-balance maths).
CASH_OUT_TYPES = {
    TransactionType.EXPENSE,
    TransactionType.REPAY,
    TransactionType.WITHDRAW,
}

#: Types where money enters the business.
CASH_IN_TYPES = {
    TransactionType.INCOME,
    TransactionType.BORROW,
    TransactionType.INVEST,
}


CUSTOMER_REQUIRED_TYPES = {
    TransactionType.CREDIT, TransactionType.PAYMENT, TransactionType.ADVANCE,
    TransactionType.REFUND, TransactionType.ADJUSTMENT,
}


class PaymentMethod(models.TextChoices):
    CASH = "CASH", "Cash"
    BANK = "BANK", "Bank"
    BKASH = "BKASH", "bKash"
    NAGAD = "NAGAD", "Nagad"
    ROCKET = "ROCKET", "Rocket"
    CARD = "CARD", "Card"
    OTHER = "OTHER", "Other"


class ExpenseKind(models.TextChoices):
    """What an EXPENSE really is — goods to sell, or a running cost.

    A meat shop buys stock (goods) and also pays rent, transport and
    electricity (running costs). Only the goods belong to the cost of
    what was sold; unsold stock is an asset, not a loss. The split is
    what makes the profit figure honest.

    Extra categories keep the expense book granular — salary, rent,
    transport and utilities each sit under their own heading, which is
    what the monthly budget + pie chart rely on.
    """

    GOODS = "GOODS", "কাঁচামাল / মজুদ কেনা"
    RAW_MATERIAL = "RAW_MATERIAL", "কাঁচামাল"
    SALARY = "SALARY", "বেতন / বোনাস"
    RENT = "RENT", "ভাড়া"
    TRANSPORT = "TRANSPORT", "পরিবহন / যাতায়াত"
    UTILITIES = "UTILITIES", "বিজলি / গ্যাস / ইন্টারনেট"
    OPERATING = "OPERATING", "দোকান খরচ (সফা/অন্যান্য)"
    OTHER = "OTHER", "অন্যান্য"


class MonthlyBudget(models.Model):
    """Monthly spending cap per expense category, per business.

    One row = (business, category, first-of-month). When expenses in a
    month cross this cap, the expenses page shows a warning badge.
    """

    business = models.ForeignKey(
        "organizations.Business", on_delete=models.CASCADE)
    category = models.CharField(
        max_length=20, choices=ExpenseKind.choices,
        default=ExpenseKind.OTHER)
    month = models.DateField(help_text="First day of the budget month")
    limit_amount = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text="Monthly spending cap for this category (৳)")

    class Meta:
        unique_together = ("business", "category", "month")
        ordering = ["-month", "category"]
        indexes = [
            models.Index(fields=["business", "month"]),
        ]

    def __str__(self):
        return f"{self.business} · {self.get_category_display()} · {self.month:%B %Y} · ৳{self.limit_amount}"

    @property
    def spent(self):
        from django.db.models import Sum
        from django.db.models import Q as _Q
        from apps.transactions.models import Transaction
        return (Transaction.objects
                .filter(business=self.business,
                        transaction_type=TransactionType.EXPENSE,
                        payment_method=PaymentMethod.CASH,
                        expense_kind=self.category,
                        transaction_date__year=self.month.year,
                        transaction_date__month=self.month.month)
                .exclude(status=TransactionStatus.REVERSED)
                .aggregate(s=Sum("amount"))["s"] or Decimal("0.00"))

    @property
    def remaining(self):
        return self.limit_amount - self.spent

    @property
    def is_over(self):
        return self.spent > self.limit_amount


class TransactionStatus(models.TextChoices):
    COMPLETED = "COMPLETED", "Completed"
    REVERSED = "REVERSED", "Reversed (voided)"


class RecurringTransaction(SoftDeleteUUIDModel):
    """Auto-draft rule (Phase 7): posts a real transaction on a schedule.

    Runs ALWAYS go through create_transaction (the single validated
    writer) with a deterministic client_reference `RR:{id}:{date}` —
    re-running a due date can never double-post. next_date is advanced
    in the same atomic block as each posting; times_run/max_runs bound
    the schedule.
    """

    class Frequency(models.TextChoices):
        DAILY = "DAILY", "Every N days"
        WEEKLY = "WEEKLY", "Every N weeks"
        MONTHLY = "MONTHLY", "Every N months"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        PAUSED = "PAUSED", "Paused"

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="recurring_rules")
    title = models.CharField(max_length=160)
    transaction_type = models.CharField(
        max_length=12, choices=TransactionType.choices)
    amount = models.DecimalField(
        max_digits=18, decimal_places=2, validators=[DecimalValidator(18, 2)])
    payment_method = models.CharField(
        max_length=8, choices=PaymentMethod.choices, default=PaymentMethod.CASH)
    to_payment_method = models.CharField(
        max_length=8, choices=PaymentMethod.choices, null=True, blank=True)
    adjustment_direction = models.CharField(
        max_length=4, choices=[("UP", "Up"), ("DOWN", "Down")],
        null=True, blank=True)
    partner = models.ForeignKey(
        "partners.Partner", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="recurring_rules")
    customer = models.ForeignKey(
        "customers.Customer", null=True, blank=True,
        on_delete=models.CASCADE, related_name="recurring_rules")
    description = models.CharField(max_length=200, blank=True)
    frequency = models.CharField(max_length=8, choices=Frequency.choices)
    interval = models.PositiveIntegerField(default=1)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    max_runs = models.PositiveIntegerField(
        null=True, blank=True, help_text="Stop after N postings; empty = no end.")
    client_reference_prefix = models.CharField(max_length=40, blank=True)
    next_date = models.DateField(db_index=True)
    times_run = models.PositiveIntegerField(default=0)
    last_run_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=8, choices=Status.choices, default=Status.ACTIVE,
        db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="recurring_rules_created")

    class Meta:
        ordering = ("business", "next_date")
        indexes = [models.Index(fields=["business", "status", "next_date"])]

    def __str__(self):
        return f"{self.title} {self.frequency} ৳{self.amount} ({self.status})"

    def client_ref_for(self, due_date) -> str:
        prefix = self.client_reference_prefix or "RR"
        return f"{prefix}:{self.pk}:{due_date:%Y%m%d}"


class Transaction(SoftDeleteUUIDModel):
    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="transactions"
    )
    transaction_type = models.CharField(
        max_length=12, choices=TransactionType.choices, db_index=True
    )
    amount = models.DecimalField(
        max_digits=18, decimal_places=2, validators=[DecimalValidator(18, 2)]
    )
    partner = models.ForeignKey(
        "partners.Partner", null=True, blank=True, on_delete=models.PROTECT,
        related_name="transactions",
        help_text="Partner attribution — never ownership of the money.",
    )
    customer = models.ForeignKey(
        "customers.Customer", null=True, blank=True, on_delete=models.PROTECT,
        related_name="transactions",
    )
    transaction_date = models.DateField(db_index=True)   # business date
    transaction_time = models.TimeField(null=True, blank=True)
    payment_method = models.CharField(
        max_length=8, choices=PaymentMethod.choices, default=PaymentMethod.CASH
    )
    to_payment_method = models.CharField(
        max_length=8, choices=PaymentMethod.choices, null=True, blank=True,
        help_text="TRANSFER only: destination account (cash→bank etc.)."
    )
    adjustment_direction = models.CharField(
        max_length=4, null=True, blank=True,
        help_text="ADJUSTMENT only: UP (due increases) or DOWN (due decreases)."
    )
    description = models.CharField(max_length=200, blank=True)
    expense_kind = models.CharField(
        max_length=25, choices=ExpenseKind.choices, default=ExpenseKind.GOODS,
        help_text="EXPENSE only: category of the cost (goods, salary, rent, transport, utilities…).",
    )
    notes = models.TextField(blank=True)
    attachment = models.FileField(upload_to="txn_attachments/%Y/%m/",
                                  null=True, blank=True)
    reference_number = models.CharField(max_length=64, blank=True)
    receipt_no = models.CharField(max_length=64, blank=True, db_index=True,
                                  help_text="COLLECTION only.")
    status = models.CharField(max_length=10, choices=TransactionStatus.choices,
                              default=TransactionStatus.COMPLETED, db_index=True)
    reversal_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT,
        related_name="reversals",
    )
    client_reference = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text="Client-generated UUID: offline-sync idempotency key.",
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                   on_delete=models.SET_NULL,
                                   related_name="transactions_created")

    class Meta:
        ordering = ("-transaction_date", "-created_at")
        indexes = [
            models.Index(fields=["business", "transaction_date"]),
            models.Index(fields=["business", "partner", "transaction_date"]),
            models.Index(fields=["business", "customer", "transaction_date"]),
            models.Index(fields=["business", "transaction_type"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(amount__gt=0), name="transaction_amount_positive"
            ),
        ]

    def __str__(self):
        who = self.customer.name if self.customer_id else "-"
        return (f"{self.transaction_type} {self.amount} "
                f"[{who} @ {self.transaction_date}]")


class TransactionDateMemory(models.Model):
    """Persistent last-used transaction date (Section 7).

    One row per (business, partner); partner NULL = business-wide default.
    The Flutter client reads this to prefill the date field of every new
    transaction card, remembering it across restarts — and per partner:
    switching partners restores *their* date, never overwrites it.
    """

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="date_memories"
    )
    partner = models.ForeignKey(
        "partners.Partner", null=True, blank=True,
        on_delete=models.CASCADE, related_name="date_memory",
    )
    last_date = models.DateField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "partner"],
                                    name="uniq_date_memory_business_partner"),
        ]

    @classmethod
    def get(cls, business, partner=None):
        row = cls.objects.filter(business=business, partner=partner).first()
        return row

    @classmethod
    def touch(cls, business, date, partner=None):
        if date is None:
            return None
        cls.objects.update_or_create(
            business=business, partner=partner, defaults={"last_date": date}
        )
        return cls.get(business, partner)

    @classmethod
    def resolve(cls, business, partner=None):
        """Default rule: partner-specific → business default → today."""
        row = cls.get(business, partner) or cls.get(business, None)
        return row
