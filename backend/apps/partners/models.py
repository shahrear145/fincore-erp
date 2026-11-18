"""Partner management (Section 4).

Rules implemented here:
- Unlimited partner creation — no application-level cap (validated in tests).
- Partner dues are BUSINESS receivables with partner *attribution*; a partner
  profile never "owns" customer money (Section 14).
- Only ADMIN can create/edit/deactivate partners.
- A PARTNER sees only their own partner profile.
- Every edit stores previous value + new value in the audit log.
"""
from decimal import Decimal

from django.conf import settings
from django.core.validators import DecimalValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.middleware import record_audit
from apps.core.models import SoftDeleteUUIDModel
from apps.organizations.models import Business


class Partner(SoftDeleteUUIDModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"
        SUSPENDED = "SUSPENDED", "Suspended"
        EXITED = "EXITED", "Exited"

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="partners"
    )
    name = models.CharField(max_length=160)
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    profile_image = models.ImageField(upload_to="partners/", null=True, blank=True)
    joining_date = models.DateField(null=True, blank=True)
    initial_capital = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0.00"),
        validators=[DecimalValidator(18, 2)],
        help_text=_("Paid-in capital contributed by this partner (BDT)."),
    )
    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.ACTIVE, db_index=True)
    notes = models.TextField(blank=True)
    permissions = models.JSONField(default=dict, blank=True)
    linked_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="partner_profiles",
        help_text=_("Platform account used by this partner when logging in."),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="partners_created",
    )

    class Meta:
        ordering = ("business", "name")
        indexes = [
            models.Index(fields=["business", "status"]),
            models.Index(fields=["linked_user"]),
        ]

    def __str__(self):
        return f"{self.name} [{self.business_id}:{self.status}]"

    # --- Attribution ≠ ownership (Section 14) ------------------------------
    # These totals are computed by the accounting/report engine in Phase 5;
    # Phase 1 exposes partner CRUD plus this structural dashboard contract.

    def capital_summary(self) -> dict:
        """Capital contributions, withdrawals and net capital position.
        Implemented by the accounting engine (Phase 5)."""
        raise NotImplementedError

    def perf_summary(self) -> dict:
        """Assigned customers, sales, collections, dues.
        Filled progressively: Phase 2 assigns customers, Phase 3 transactions.
        Never returns fake financial figures."""
        summary = {
            "total_sales": None,
            "total_collections": None,
            "total_outstanding_due": None,
            "total_expenses": None,
        }
        if hasattr(self, "customer_assignments"):
            summary["assigned_customers"] = (
                self.customer_assignments.filter(is_active=True).count()
            )
            summary["total_outstanding_due"] = sum(
                (
                    a.current_due()
                    for a in self.customer_assignments.filter(is_active=True)
                ),
                Decimal("0.00"),
            )
        else:
            summary["assigned_customers"] = 0
        return summary
