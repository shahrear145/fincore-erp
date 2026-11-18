from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from apps.organizations.models import Business, BusinessMembership, FinancialPeriod


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "currency", "owner", "is_active")
    search_fields = ("name", "slug", "owner__username", "owner__email")
    list_filter = ("is_active", "currency")

    def save_model(self, request, obj, form, change):
        """Keep the owner able to reach their own business.

        The web UI finds the active business through BusinessMembership, so a
        business created here without one would be invisible to its owner.
        """
        super().save_model(request, obj, form, change)
        BusinessMembership.objects.get_or_create(
            user=obj.owner,
            business=obj,
            defaults={"role": BusinessMembership.Role.ADMIN},
        )


@admin.register(BusinessMembership)
class BusinessMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "business", "role", "is_active", "joined_at")
    list_filter = ("role", "is_active")
    search_fields = ("user__username", "user__email", "business__name")


@admin.register(FinancialPeriod)
class FinancialPeriodAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "start_date", "end_date", "is_locked", "locked_by")
    list_filter = ("is_locked", "business")
    search_fields = ("name", "business__name")
