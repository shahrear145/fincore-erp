from django.contrib import admin

from apps.loans.models import LoanEntry, LoanParty


@admin.register(LoanParty)
class LoanPartyAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "kind", "is_active", "created_by")
    list_filter = ("kind", "is_active", "business")
    search_fields = ("name", "phone", "email", "business__name")


@admin.register(LoanEntry)
class LoanEntryAdmin(admin.ModelAdmin):
    list_display = ("transaction_date", "party", "kind", "amount", "status")
    list_filter = ("kind", "status", "business")
    search_fields = ("party__name", "description")
