"""Customer management (Section 5) and partner-wise due separation (Section 4/9).

Core structural invariant:
  A Customer belongs to one Business.
  A CustomerAssignment links (customer, partner) and carries that partner's
  OWN due book for that customer. Collections always allocate against one
  assignment only — collecting Partner 1's due can never reduce Partner 2's.

Partner rules (server-enforced, Section 25 rule 7-8):
  ADMIN  — sees/manages every customer of the business.
  PARTNER — sees only customers assigned to them; may add clients (auto
  assignment to self) if their membership grants can_manage_customers.
"""
from decimal import Decimal

from django.conf import settings
from django.core.validators import DecimalValidator
from django.db import models

from apps.core.middleware import record_audit
from apps.core.models import SoftDeleteUUIDModel, UUIDModel
from apps.organizations.models import Business


class CustomerCategory(models.TextChoices):
    RETAIL = "RETAIL", "Retail"
    WHOLESALE = "WHOLESALE", "Wholesale"
    REGULAR = "REGULAR", "Regular"
    VIP = "VIP", "VIP"
    OTHER = "OTHER", "Other"


class Customer(SoftDeleteUUIDModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="customers"
    )
    serial_no = models.PositiveIntegerField(
        null=True, blank=True, db_index=True,
        help_text="Sequential customer serial number within the business.",
    )
    name = models.CharField(max_length=160)
    phone = models.CharField(max_length=32, blank=True, db_index=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    profile_photo = models.ImageField(upload_to="customers/", null=True, blank=True)
    category = models.CharField(
        max_length=12, choices=CustomerCategory.choices,
        default=CustomerCategory.REGULAR, db_index=True,
    )
    # Opening balance > 0 means customer owed money at creation; < 0 advance.
    opening_balance = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0.00"),
        validators=[DecimalValidator(18, 2)],
    )
    advance_payment = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0.00"),
        validators=[DecimalValidator(18, 2)],
    )
    due_limit = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        validators=[DecimalValidator(18, 2)],
        help_text="Optional: block new credit beyond this total due (Section 5).",
    )
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.ACTIVE, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="customers_created",
    )

    class Meta:
        ordering = ("business", "name")
        indexes = [models.Index(fields=["business", "status"])]

    def __str__(self):
        return f"{self.name} [{self.business_id}:{self.status}]"

    # --- Due aggregation (Section 5: partner-wise AND total) ---------------

    def dues_by_partner(self) -> list[dict]:
        """[{partner_id, partner_name, due}] for every linked partner."""
        rows = []
        for assignment in self.assignments.select_related("partner"):
            rows.append({
                "partner_id": str(assignment.partner_id),
                "partner_name": assignment.partner.name,
                "due": assignment.current_due(),
            })
        return rows

    def total_due(self) -> Decimal:
        """Sum of partner-wise dues — never 'netted' against advances here;
        advances live on this row and are reported separately."""
        return sum(
            (a.current_due() for a in self.assignments.all()), Decimal("0.00")
        )


class CustomerAssignment(UUIDModel):
    """One partner's independent due book for one customer."""

    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="assignments"
    )
    partner = models.ForeignKey(
        "partners.Partner", on_delete=models.CASCADE,
        related_name="customer_assignments",
    )
    opening_due = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0.00"),
        validators=[DecimalValidator(18, 2)],
    )
    advance = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0.00"),
        validators=[DecimalValidator(18, 2)],
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["customer", "partner"], name="uniq_customer_partner_assignment"
            ),
        ]
        indexes = [models.Index(fields=["customer", "partner"])]

    def clean(self):
        if self.customer and self.partner \
                and self.customer.business_id != self.partner.business_id:
            raise ValueError("Partner and customer must belong to the same business.")

    def current_due(self) -> Decimal:
        """Opening due − advance + signed due-entry movements."""
        due = Decimal(str(self.opening_due)) - Decimal(str(self.advance))
        movements = sum(
            (e.signed_amount() for e in self.due_entries.all()),
            Decimal("0.00"),
        )
        return due + movements

    def __str__(self):
        return f"{self.customer} × {self.partner}"


class DueReminder(UUIDModel):
    """A payment-reminder attempt for a customer with an outstanding due.

    A reminder is a *hand-off*, never an accounting event: it does not
    change a due, it only tells the customer what is owed. Every attempt
    is logged so a shop can see when it last chased a customer (and can
    avoid chasing the same person every single day).
    """

    class Channel(models.TextChoices):
        WHATSAPP = "WHATSAPP", "WhatsApp"
        EMAIL = "EMAIL", "Email"

    class Status(models.TextChoices):
        SENT = "SENT", "Sent"        # email handed to the mail server
        FAILED = "FAILED", "Failed"  # the mail server refused it

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="due_reminders"
    )
    partner = models.ForeignKey(
        "partners.Partner", on_delete=models.CASCADE, related_name="due_reminders"
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="due_reminders"
    )
    assignment = models.ForeignKey(
        CustomerAssignment, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="due_reminders",
    )
    channel = models.CharField(max_length=10, choices=Channel.choices)
    recipient = models.CharField(max_length=160, blank=True)
    amount = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0.00"),
        validators=[DecimalValidator(18, 2)],
    )
    message = models.TextField(blank=True)
    language = models.CharField(max_length=2, default="bn")
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.SENT, db_index=True,
    )
    error = models.TextField(blank=True)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="due_reminders",
    )

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["business", "customer", "-created_at"])]

    def __str__(self):
        return f"{self.customer} → {self.get_channel_display()} ({self.amount})"


class CustomerDocument(UUIDModel):
    """Supporting files for a customer (id card, trade license, etc.)."""

    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="documents"
    )
    file = models.FileField(upload_to="customer_docs/%Y/%m/")
    title = models.CharField(max_length=160, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )


class DueEntry(UUIDModel):
    """Ledger line against one specific CustomerAssignment.

    kinds:
      CREDIT        due increases (sale / charged amount)   [+]
      COLLECTION    payment against the due                 [-]
      ADVANCE       advance payment (reduces future dues)   [-]
      ADJUSTMENT    admin correction — direction UP (+) or DOWN (-)
      REFUND        refund to customer                      [-]
      REVERSAL      auto entry voiding a reversed transaction [opposite]

    `remaining` is the not-yet-collected portion of a credit-like entry;
    collections consume it oldest-first via PaymentAllocation.
    """

    class Kind(models.TextChoices):
        CREDIT = "CREDIT", "Credit (due increase)"
        COLLECTION = "COLLECTION", "Collection (due decrease)"
        ADVANCE = "ADVANCE", "Advance payment"
        ADJUSTMENT = "ADJUSTMENT", "Adjustment"
        REFUND = "REFUND", "Refund"
        RETURN = "RETURN", "Sales return (due decrease)"

    class Direction(models.TextChoices):
        UP = "UP", "Due up"
        DOWN = "DOWN", "Due down"


    assignment = models.ForeignKey(
        CustomerAssignment, on_delete=models.CASCADE, related_name="due_entries"
    )
    kind = models.CharField(max_length=12, choices=Kind.choices)
    direction = models.CharField(max_length=4, choices=Direction.choices,
                                 default=Direction.UP)
    amount = models.DecimalField(
        max_digits=18, decimal_places=2, validators=[DecimalValidator(18, 2)],
        help_text="Always positive; kind determines sign applied to the due.",
    )
    remaining = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text="Credit-like entries only: uncollected balance of this line.",
    )
    transaction = models.ForeignKey(
        "transactions.Transaction", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="due_entries",
    )
    note = models.TextField(blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    transaction_date = models.DateField()

    class Meta:
        ordering = ("transaction_date", "created_at")

    def signed_amount(self) -> Decimal:
        """Effect of this entry on the assignment due (+ adds, − reduces)."""
        a = self.amount
        if self.kind in (self.Kind.COLLECTION, self.Kind.ADVANCE,
                         self.Kind.REFUND, self.Kind.RETURN):
            return -a
        if self.kind == self.Kind.ADJUSTMENT:
            return a if self.direction == self.Direction.UP else -a
        return a            # CREDIT

    @property
    def is_credit_like(self) -> bool:
        """Entries that track `remaining` for oldest-first allocation."""
        return self.kind == self.Kind.CREDIT or (
            self.kind == self.Kind.ADJUSTMENT and self.direction == self.Direction.UP
        )


class PaymentAllocation(UUIDModel):
    """A collection's consumption of a credit-like due entry (oldest first).

    Kept as rows so receipts and payment history show exactly *which* charged
    amount a payment settled (Section 9). A reversal of the collection frees
    its allocations (restores `remaining`) — the audit trail stays intact.
    """

    collection_transaction = models.ForeignKey(
        "transactions.Transaction", on_delete=models.PROTECT,
        related_name="allocations",
    )
    credit_entry = models.ForeignKey(
        DueEntry, on_delete=models.PROTECT, related_name="allocations"
    )
    amount = models.DecimalField(max_digits=18, decimal_places=2,
                                 validators=[DecimalValidator(18, 2)])
    freed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return (f"{self.amount} → {self.credit_entry_id} "
                f"by {self.collection_transaction_id}")


def allocate_collection(assignment, transaction) -> Decimal:
    """Apply a collection transaction against open credit lines, oldest first.

    Returns the unapplied remainder (should be 0 — overpayment is rejected
    upstream; leftover only appears in race edge cases and is surfaced).
    """
    from decimal import Decimal

    remaining = Decimal(str(transaction.amount))
    open_credits = assignment.due_entries.filter(
        kind=DueEntry.Kind.CREDIT).exclude(remaining__lte=0)
    open_credits = [e for e in open_credits if (e.remaining or e.amount) > 0]
    for entry in open_credits:
        if remaining <= 0:
            break
        uncollected = entry.remaining if entry.remaining is not None else entry.amount
        take = min(Decimal(str(uncollected)), remaining)
        PaymentAllocation.objects.create(
            collection_transaction=transaction, credit_entry=entry, amount=take,
        )
        entry.remaining = Decimal(str(uncollected)) - take
        entry.save(update_fields=["remaining"])
        remaining -= take
    return remaining


def free_allocations(transaction) -> Decimal:
    """Undo a collection's allocations when the collection is reversed."""
    from django.utils import timezone
    from decimal import Decimal

    total = Decimal("0.00")
    for alloc in transaction.allocations.select_for_update():
        entry = alloc.credit_entry
        entry.remaining = (Decimal(str(entry.remaining if entry.remaining is not None
                                                       else entry.amount))
                           + Decimal(str(alloc.amount)))
        entry.save(update_fields=["remaining"])
        alloc.freed_at = timezone.now()
        alloc.save(update_fields=["freed_at"])
        total += Decimal(str(alloc.amount))
    return total


def record_due_entry(assignment, kind, amount, transaction_date, note="", user=None,
                     direction=None, transaction=None, remaining=None):
    """Single writer for due movements so the assignment book stays consistent."""
    from decimal import Decimal

    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError("Due amount must be positive.")
    entry = DueEntry.objects.create(
        assignment=assignment, kind=kind, amount=amount,
        direction=direction or DueEntry.Direction.UP,
        transaction_date=transaction_date, note=note, recorded_by=user,
        transaction=transaction, remaining=remaining,
    )
    record_audit("create", entry, business=assignment.customer.business)
    return entry
