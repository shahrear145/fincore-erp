"""AI assistant models (Phase 7): nothing invented, everything logged.

Every query/answer pair is persisted (AssistantQuery) with the exact
fact references used, so an answer can always be traced back to the
ledger rows it was computed from (Section 18 audit spirit).
"""
from django.conf import settings
from django.db import models

from apps.core.models import UUIDModel
from apps.organizations.models import Business


class AssistantAction(UUIDModel):
    """A proposed action from natural language, to confirm with one click.

    Nothing is posted until the user explicitly confirms the proposal —
    every row here is thumb-driven, not auto-applied. The audit trail is
    `AssistantAction`, the ledger rows underneath are the normal
    transactions, so reversing/auditing stays the same as everywhere else.
    """

    class Kind(models.TextChoices):
        ADD_BAKI = "ADD_BAKI", "বাকি যোগ"
        ADD_PAYMENT = "ADD_PAYMENT", "পরিশোধ যোগ"
        ADD_EXPENSE = "ADD_EXPENSE", "খরচ যোগ"

    class Status(models.TextChoices):
        PROPOSED = "PROPOSED", "Awaiting confirmation"
        DONE = "DONE", "Done"
        DISCARDED = "DISCARDED", "Discarded"
        FAILED = "FAILED", "Failed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="assistant_actions")
    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="assistant_actions")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    partner = models.ForeignKey(
        "partners.Partner", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="assistant_actions")
    customer = models.ForeignKey(
        "customers.Customer", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="assistant_actions")
    description = models.TextField(blank=True)
    expense_kind = models.CharField(
        max_length=25, blank=True,
        help_text="ADD_EXPENSE only: ExpenseKind code.")
    status = models.CharField(max_length=16, choices=Status.choices,
                              default=Status.PROPOSED, db_index=True)
    raw_question = models.TextField()
    transaction = models.ForeignKey(
        "transactions.Transaction", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="assistant_actions",
        help_text="The row that was created when the user confirmed.")
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.user.username}: {self.get_kind_display()} {self.amount}"


class AssistantQuery(UUIDModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="assistant_queries")
    business = models.ForeignKey(
        Business, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="assistant_queries",
        help_text="NULL = personal-finance mode.")
    question = models.TextField()
    answer = models.TextField()
    intent = models.CharField(max_length=40, blank=True, db_index=True)
    facts_used = models.JSONField(default=list)
    scope = models.CharField(max_length=10, blank=True)  # PERSONAL | BUSINESS
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"Q by {self.user_id}: {self.question[:60]}"
