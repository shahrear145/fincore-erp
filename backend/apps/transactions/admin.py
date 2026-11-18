from django.contrib import admin

from apps.transactions.models import Transaction, TransactionDateMemory


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("transaction_type", "amount", "customer", "partner",
                    "transaction_date", "payment_method", "status", "receipt_no")
    list_filter = ("status", "transaction_type", "payment_method", "business")
    search_fields = ("reference_number", "receipt_no", "description",
                     "customer__name", "partner__name")
    date_hierarchy = "transaction_date"


@admin.register(TransactionDateMemory)
class TransactionDateMemoryAdmin(admin.ModelAdmin):
    list_display = ("business", "partner", "last_date", "updated_at")
    list_filter = ("business",)
