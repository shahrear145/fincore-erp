from django.contrib import admin

from apps.personal.models import (
    PersonalAccount, PersonalBudget, PersonalCategory, PersonalLoan,
    PersonalTransaction, SavingsGoal,
)


@admin.register(PersonalAccount)
class PersonalAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "user", "initial_balance", "status")
    list_filter = ("kind", "status")


@admin.register(PersonalCategory)
class PersonalCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "user", "is_default")
    list_filter = ("kind", "is_default")


@admin.register(PersonalTransaction)
class PersonalTransactionAdmin(admin.ModelAdmin):
    list_display = ("transaction_type", "amount", "account",
                    "transaction_date", "status", "user")
    list_filter = ("transaction_type", "status")
    date_hierarchy = "transaction_date"


@admin.register(PersonalBudget)
class PersonalBudgetAdmin(admin.ModelAdmin):
    list_display = ("category", "year", "month", "budgeted_amount", "user")


@admin.register(SavingsGoal)
class SavingsGoalAdmin(admin.ModelAdmin):
    list_display = ("name", "target_amount", "status", "user")
    list_filter = ("status",)


@admin.register(PersonalLoan)
class PersonalLoanAdmin(admin.ModelAdmin):
    list_display = ("name", "counterparty_name", "direction", "principal",
                    "remaining_display", "status", "user")
    list_filter = ("direction", "status")

    @admin.display(description="Remaining")
    def remaining_display(self, obj):
        return obj.remaining()
