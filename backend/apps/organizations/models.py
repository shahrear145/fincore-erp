"""Organizations: businesses, memberships, financial periods.

Economy rules fed by the spec:
- A user may belong to many businesses; every business-scoped row carries a
  FK to Business so Personal and Business accounting stay physically separate.
- Personal finance is modelled later (Phase 6) as a reserved personal ledger
  owned by the user with no business FK — never mixed with business data.
- Financial periods support locking: edits inside a locked period require
  ADMIN approval and produce audit logs (Section 8).
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.core.middleware import record_audit
from apps.core.models import SoftDeleteUUIDModel

CURRENCY_CHOICES = [("BDT", "Bangladeshi Taka ৳")]


class Business(SoftDeleteUUIDModel):
    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=180, unique=True)
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="BDT")
    timezone = models.CharField(max_length=64, default=settings.PRIMARY_TZ)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_businesses"
    )
    contact_phone = models.CharField(max_length=32, blank=True)
    contact_email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    logo = models.ImageField(upload_to="business_logos/", null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return f"{self.name} ({self.slug})"


class BusinessMembership(models.Model):
    """Links a user to a business with a role and granular permissions."""

    class Role(models.TextChoices):
        ADMIN = "ADMIN", "Owner"            # full access
        ACCOUNTANT = "ACCOUNTANT", "Accountant"  # entries + view, no deletes
        VIEWER = "VIEWER", "Viewer"          # read-only
        PARTNER = "PARTNER", "Partner"       # own book only

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=10, choices=Role.choices)
    is_active = models.BooleanField(default=True)
    # Granular capabilities for PARTNER role. ADMIN implicitly has all.
    partner_permissions = models.JSONField(default=dict, blank=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "business"],
                                    name="uniq_user_business_membership")
        ]

    DEFAULT_PARTNER_PERMISSIONS = {
        # Backend-enforced; UI only mirrors these (Section 4).
        "can_create_transactions": True,
        "can_edit_own_records": True,
        "can_edit_other_partners_dues": False,   # always needs ADMIN approval
        "can_view_all_reports": False,
        "can_manage_customers": True,            # their assigned customers only
        "can_view_partners": False,              # see own profile only
    }

    def save(self, *args, **kwargs):
        is_create = self.pk is None
        if self.role == self.Role.PARTNER and not self.partner_permissions:
            self.partner_permissions = dict(self.DEFAULT_PARTNER_PERMISSIONS)
        super().save(*args, **kwargs)
        if is_create:
            record_audit(
                "create", None, business=self.business,
                resource_type="BusinessMembership", resource_id=self.pk,
                changes={"user": str(self.user), "role": self.role},
            )


class FinancialPeriod(models.Model):
    """Admin-lockable accounting periods (Section 8: period locking)."""

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="financial_periods"
    )
    name = models.CharField(max_length=64)          # e.g. "FY2026" or "2026-01"
    start_date = models.DateField()
    end_date = models.DateField()
    is_locked = models.BooleanField(default=False)
    locked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="locked_periods",
    )
    locked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("business", "start_date")
        constraints = [
            models.UniqueConstraint(fields=["business", "name"],
                                    name="uniq_period_name_per_business"),
            models.CheckConstraint(
                check=models.Q(end_date__gt=models.F("start_date")),
                name="period_end_after_start",
            ),
        ]

    def clean(self):
        if self.end_date <= self.start_date:
            raise ValidationError("Period end date must be after start date.")
        if self.business_id and FinancialPeriod.objects.filter(
            business=self.business, start_date__lt=self.end_date,
            end_date__gt=self.start_date, is_locked=True,
        ).exclude(pk=self.pk).exists():
            raise ValidationError("Overlapping locked financial period exists.")

    @classmethod
    def period_for_date(cls, business, date) -> "FinancialPeriod | None":
        return cls.objects.filter(
            business=business, start_date__lte=date, end_date__gte=date
        ).first()

    @classmethod
    def assert_date_not_locked(cls, business, date):
        period = cls.period_for_date(business, date)
        if period and period.is_locked:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied(
                f"Financial period {period.name} is locked. "
                "Backdated changes need ADMIN approval."
            )
        return period

    def __str__(self):
        return f"{self.business_id}:{self.name} [{self.start_date}..{self.end_date}]"
