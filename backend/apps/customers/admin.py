from django.contrib import admin

from apps.customers.models import (
    Customer, CustomerAssignment, CustomerDocument, DueEntry,
)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "phone", "category", "status",
                    "opening_balance")
    list_filter = ("status", "category", "business")
    search_fields = ("name", "phone", "email")


@admin.register(CustomerAssignment)
class CustomerAssignmentAdmin(admin.ModelAdmin):
    list_display = ("customer", "partner", "opening_due", "advance", "is_active")
    list_filter = ("is_active", "partner__business")


@admin.register(DueEntry)
class DueEntryAdmin(admin.ModelAdmin):
    list_display = ("assignment", "kind", "amount", "transaction_date")
    list_filter = ("kind",)


@admin.register(CustomerDocument)
class CustomerDocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "customer", "created_at")
