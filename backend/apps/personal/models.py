"""Personal finance (Phase 6) — a SEPARATE environment from business books.

Schema-level isolation guarantees (Section 2 of the architecture):
- Every model here is owned by `User` only and carries **no business FK**.
- Personal rows can never join into business queries and vice versa.
- The user is the tenant: endpoints scope every query to request.user.

Model notes:
- Personal balances are ledger-derived: account balance =
  initial_balance + Σ signed committed transactions (REVERSED excluded) —
  the same "no mutable counter" rule as business due books/stock.
- Money actions that would overdraw a source account are rejected with the
  available balance named.
- Savings and loans are expressed AS transactions so account balances stay
  one source of truth; goal progress and loan payables are derived sums.
"""
from decimal import Decimal

from django.conf import settings
from django.core.validators import DecimalValidator
from django.db import models

from apps.core.models import SoftDeleteUUIDModel, UUIDModel


class PersonalTxnType(models.TextChoices):
    INCOME = "INCOME", "Income"
    EXPENSE = "EXPENSE", "Expense"
    TRANSFER = "TRANSFER", "Transfer between own accounts"


class PersonalAccount(SoftDeleteUUIDModel):
    """A personal vault: cash, bank, mobile wallet, etc."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="personal_accounts")
    name = models.CharField(max_length=120)
    kind = models.CharField(
        max_length=12,
        choices=[("CASH", "Cash"), ("BANK", "Bank"), ("MOBILE", "Mobile "
                                                             "wallet"),
                 ("OTHER", "Other")], default="CASH")
    initial_balance = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0.00"),
        validators=[DecimalValidator(18, 2)])
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.ACTIVE)

    class Meta:
        ordering = ("user", "name")
        constraints = [models.UniqueConstraint(
            fields=["user", "name"], name="uniq_personal_account_name")]

    def __str__(self):
        return f"{self.name} [{self.user_id}]"

    def balance(self) -> Decimal:
        bal = Decimal(str(self.initial_balance))
        for t in self.personal_source_transactions.exclude(
                status=PersonalTransaction.Status.REVERSED):
            if t.transaction_type == PersonalTxnType.INCOME:
                bal += Decimal(str(t.amount))
            elif t.transaction_type == PersonalTxnType.TRANSFER:
                bal -= Decimal(str(t.amount))
            else:  # EXPENSE (incl. savings contribution, loan payment)
                bal -= Decimal(str(t.amount))
        for t in self.personal_destination_transactions.exclude(
                status=PersonalTransaction.Status.REVERSED).filter(
                    transaction_type=PersonalTxnType.TRANSFER):
            bal += Decimal(str(t.amount))
        return bal


class PersonalCategory(UUIDModel):
    class Kind(models.TextChoices):
        INCOME = "INCOME", "Income category"
        EXPENSE = "EXPENSE", "Expense category"

    DEFAULTS = {
        "INCOME": ["Salary", "Freelance", "Rent Income", "Gifts",
                   "Other Income"],
        "EXPENSE": ["Food", "Rent", "Transport", "Utilities", "Health",
                    "Education", "Shopping", "Savings", "Loan EMI", "Other"],
    }

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="personal_categories")
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ("user", "kind", "name")
        constraints = [models.UniqueConstraint(
            fields=["user", "kind", "name"],
            name="uniq_personal_category_user_kind_name")]

    def __str__(self):
        return f"{self.name} ({self.kind})"

    @classmethod
    def seed_defaults(cls, user):
        for kind, names in cls.DEFAULTS.items():
            for name in names:
                cls.objects.get_or_create(
                    user=user, kind=kind, name=name,
                    defaults={"is_default": True})


class PersonalTransaction(SoftDeleteUUIDModel):
    """Personal money movement — the ONE writer for account balances.

    Money effects (committed only):
      INCOME   +amount → account (the account where money lands)
      EXPENSE  −amount ← account (incl. savings & loan-EMI payments)
      TRANSFER −amount ← account, +amount → to_account
    """

    class Status(models.TextChoices):
        COMPLETED = "COMPLETED", "Completed"
        REVERSED = "REVERSED", "Reversed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="personal_transactions")
    transaction_type = models.CharField(max_length=10, choices=PersonalTxnType
                                        .choices)
    amount = models.DecimalField(
        max_digits=18, decimal_places=2, validators=[DecimalValidator(18, 2)])
    account = models.ForeignKey(
        PersonalAccount, on_delete=models.PROTECT,
        related_name="personal_source_transactions")
    to_account = models.ForeignKey(
        PersonalAccount, null=True, blank=True, on_delete=models.PROTECT,
        related_name="personal_destination_transactions",
        help_text="TRANSFER only: the receiving account.")
    category = models.ForeignKey(
        PersonalCategory, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="transactions")
    savings_goal = models.ForeignKey(
        "personal.SavingsGoal", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="transactions")
    loan = models.ForeignKey(
        "personal.PersonalLoan", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="transactions")
    transaction_date = models.DateField(db_index=True)
    transaction_time = models.TimeField(null=True, blank=True)
    description = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.COMPLETED, db_index=True)
    reversal_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT,
        related_name="reversals")
    client_reference = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text="Offline-sync idempotency key.")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="personal_txns_created")

    class Meta:
        ordering = ("-transaction_date", "-created_at")
        indexes = [
            models.Index(fields=["user", "transaction_date"]),
            models.Index(fields=["user", "transaction_type"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(amount__gt=0),
                name="personal_transaction_amount_positive"),
        ]

    def __str__(self):
        who = "personal"
        return f"{self.transaction_type} {self.amount} [{who} @ " \
               f"{self.transaction_date}]"


class PersonalBudget(UUIDModel):
    """Planned spend limit per user, per category, per calendar month.

    `spent` is always derived from committed EXPENSE transactions in the
    same month/category — never a stored counter.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="personal_budgets")
    category = models.ForeignKey(
        PersonalCategory, on_delete=models.CASCADE,
        related_name="budgets")
    year = models.PositiveIntegerField()
    month = models.PositiveIntegerField()  # 1..12
    budgeted_amount = models.DecimalField(
        max_digits=18, decimal_places=2,
        validators=[DecimalValidator(18, 2)])

    class Meta:
        ordering = ("user", "year", "month")
        constraints = [
            models.UniqueConstraint(fields=["user", "category", "year",
                                            "month"],
                                    name="uniq_budget_user_cat_month"),
            models.CheckConstraint(check=models.Q(month__gte=1)
                                   & models.Q(month__lte=12),
                                   name="budget_month_1_to_12"),
        ]

    def __str__(self):
        return f"{self.category} budget {self.year}-{self.month:02d}"

    def spent(self) -> Decimal:
        if self.category.kind != self.category.Kind.EXPENSE:
            return Decimal("0.00")
        agg = PersonalTransaction.objects.filter(
            user=self.user, transaction_type=PersonalTxnType.EXPENSE,
            category=self.category,
            status=PersonalTransaction.Status.COMPLETED,
            transaction_date__year=self.year,
            transaction_date__month=self.month,
        ).aggregate(total=models.Sum("amount"))
        return Decimal(str(agg["total"] or "0"))


class SavingsGoal(UUIDModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        ACHIEVED = "ACHIEVED", "Achieved"
        CLOSED = "CLOSED", "Closed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="savings_goals")
    name = models.CharField(max_length=160)
    target_amount = models.DecimalField(
        max_digits=18, decimal_places=2,
        validators=[DecimalValidator(18, 2)])
    target_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.ACTIVE, db_index=True)

    class Meta:
        ordering = ("-status", "-created_at")

    def __str__(self):
        return f"{self.name} → ৳{self.target_amount}"

    def saved(self) -> Decimal:
        """Σ committed savings transactions linked to this goal."""
        agg = self.transactions.filter(
            transaction_type=PersonalTxnType.EXPENSE,
            status=PersonalTransaction.Status.COMPLETED,
        ).aggregate(total=models.Sum("amount"))
        return Decimal(str(agg["total"] or "0"))

    def progress_percent(self) -> Decimal:
        if self.target_amount <= 0:
            return Decimal("0.00")
        pct = self.saved() / self.target_amount * Decimal("100")
        return min(pct, Decimal("100.00")).quantize(Decimal("0.01"))


class PersonalLoan(UUIDModel):
    """A loan TAKEN (we owe) or GIVEN (they owe us) + EMI schedule math.

    `remaining` = principal − Σ committed loan payments (derived).
    Schedule: `installments` monthly EMIs of `emi_amount` from
    `start_date`; a payment stamps committed amounts against that plan
    (any remaining balance shows as a late/open installment).
    """

    class Direction(models.TextChoices):
        TAKEN = "TAKEN", "We received (we owe)"
        GIVEN = "GIVEN", "We lent (they owe us)"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        CLOSED = "CLOSED", "Closed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="personal_loans")
    name = models.CharField(max_length=160)
    counterparty_name = models.CharField(max_length=160)
    direction = models.CharField(max_length=5, choices=Direction.choices)
    principal = models.DecimalField(
        max_digits=18, decimal_places=2,
        validators=[DecimalValidator(18, 2)])
    emi_amount = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0.00"))
    installments = models.PositiveIntegerField(default=0,
                                               help_text="0 = no EMI plan "
                                                         "(pay anytime).")
    start_date = models.DateField()
    status = models.CharField(max_length=8, choices=Status.choices,
                              default=Status.ACTIVE, db_index=True)
    notes = models.TextField(blank=True)
    client_reference = models.CharField(max_length=64, blank=True,
                                        db_index=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.direction} loan {self.name} ৳{self.principal}"

    def paid(self) -> Decimal:
        agg = self.transactions.filter(
            status=PersonalTransaction.Status.COMPLETED,
        ).aggregate(total=models.Sum("amount"))
        return Decimal(str(agg["total"] or "0"))

    def remaining(self) -> Decimal:
        return max(Decimal(str(self.principal)) - self.paid(),
                   Decimal("0.00"))

    def schedule(self):
        """Monthly EMI plan with paid-through tracking, exact Decimal."""
        from datetime import date

        # start_date may still be an ISO string on unsaved/created instances
        start_date = (self.start_date if isinstance(self.start_date, date)
                      else date.fromisoformat(str(self.start_date)))

        def add_months(d, months):
            y = d.year + (d.month - 1 + months) // 12
            m = (d.month - 1 + months) % 12 + 1
            day = min(d.day, 31 if m in (1, 3, 5, 7, 8, 10, 12)
                      else 30 if m in (4, 6, 9, 11) else 28)
            return date(y, m, day)

        cumulative = Decimal("0.00")
        principal = Decimal(str(self.principal))
        emi = Decimal(str(self.emi_amount))
        installed = self.installments
        paid_total = self.paid()
        rows = []
        for k in range(1, installed + 1):
            due_date = add_months(start_date, k)
            amount = emi
            if k == installed:
                # last EMI absorbs the exact remainder (avoids rounding drift)
                amount = principal - (emi * (installed - 1))
            paid_before = cumulative
            cumulative += amount
            if cumulative <= paid_total:
                state = "PAID"
            elif paid_total > paid_before:
                state = "PARTIAL"
            elif due_date <= date.today():
                state = "OVERDUE"
            else:
                state = "UPCOMING"
            rows.append({
                "installment": k, "due_date": due_date,
                "amount": amount,
                "cumulative": cumulative,
                "state": state,
                "notional_paid_through": min(paid_total, cumulative),
            })
        # Payments beyond scheduled total remain in `remaining` math, not
        # schedule flagging — surface them as an extra cash-overpay line.
        over = paid_total - cumulative
        return {
            "loan_id": str(self.id),
            "installments": rows,
            "extra_paid": over.quantize(Decimal("0.01"))
            if installed else paid_total,
        }
