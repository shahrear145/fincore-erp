"""Cash book records — what is really in the drawer, and what the stock is.

Two small ledgers that the double-entry books cannot know by themselves:

* **CashCount** — the owner counts the money in the drawer and the system
  compares it with the book balance. The difference (গরমিল / ঘাটতি) is
  the only honest way to see a leak; the first count can also be used as
  the *opening* cash of the business (money that was already there before
  the app was used).
* **StockSnapshot** — the value of the goods still in the shop at a given
  date. Without it, every purchase looks like a loss: unsold stock is an
  asset, not a loss. With it, the real profit is
  ``sales − (goods purchased − stock increase) − running expenses``.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models

from apps.core.models import UUIDModel
from apps.organizations.models import Business


class CashCount(UUIDModel):
    """A physical count of the cash drawer, compared with the book."""

    class Kind(models.TextChoices):
        COUNT = "COUNT", "Count"
        OPENING = "OPENING", "Opening balance"

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="cash_counts"
    )
    counted_on = models.DateField(db_index=True)
    kind = models.CharField(
        max_length=10, choices=Kind.choices, default=Kind.COUNT, db_index=True,
    )
    counted_amount = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0.00"),
    )
    #: the book balance at `counted_on`, frozen when the count was saved
    book_amount = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0.00"),
    )
    #: counted − book (positive = গরমিল, negative = ঘাটতি)
    difference = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0.00"),
    )
    note = models.CharField(max_length=160, blank=True)
    counted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="cash_counts",
    )

    class Meta:
        ordering = ("-counted_on", "-created_at")
        indexes = [models.Index(fields=["business", "counted_on"])]

    def __str__(self):
        return f"{self.counted_on}: {self.counted_amount} (book {self.book_amount})"


class StockSnapshot(UUIDModel):
    """Value of the goods still in the shop at the end of a day."""

    class Kind(models.TextChoices):
        OPENING = "OPENING", "Opening stock"
        CLOSING = "CLOSING", "Closing stock"

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="stock_snapshots"
    )
    snapshot_on = models.DateField(db_index=True)
    kind = models.CharField(
        max_length=10, choices=Kind.choices, default=Kind.CLOSING,
        db_index=True,
    )
    closing_value = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0.00"),
        help_text="মজুদ পণ্যের মূল্য (যা এখনও বিক্রি হয়নি)",
    )
    note = models.CharField(max_length=160, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="stock_snapshots",
    )

    class Meta:
        ordering = ("-snapshot_on", "-created_at")
        indexes = [models.Index(fields=["business", "snapshot_on"])]

    def __str__(self):
        return f"{self.snapshot_on}: stock {self.closing_value}"
