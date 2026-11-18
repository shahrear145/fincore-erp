from decimal import Decimal

from rest_framework import serializers

from apps.personal.models import (
    PersonalAccount, PersonalBudget, PersonalCategory, PersonalLoan,
    PersonalTransaction, SavingsGoal,
)

CENT = Decimal("0.01")


class PersonalAccountSerializer(serializers.ModelSerializer):
    balance = serializers.SerializerMethodField()

    class Meta:
        model = PersonalAccount
        fields = ("id", "name", "kind", "initial_balance", "status",
                  "balance", "created_at")

    def get_balance(self, obj):
        return str(obj.balance().quantize(CENT))


class PersonalCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = PersonalCategory
        fields = ("id", "name", "kind", "is_default")


class PersonalTransactionSerializer(serializers.ModelSerializer):
    account_name = serializers.CharField(source="account.name",
                                         read_only=True)
    to_account_name = serializers.CharField(source="to_account.name",
                                            read_only=True)
    category_name = serializers.CharField(source="category.name",
                                          read_only=True)

    class Meta:
        model = PersonalTransaction
        fields = ("id", "transaction_type", "amount", "account",
                  "account_name", "to_account", "to_account_name",
                  "category", "category_name", "savings_goal", "loan",
                  "transaction_date", "transaction_time", "description",
                  "notes", "status", "reversal_of", "client_reference",
                  "created_at")


class PersonalBudgetSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name",
                                          read_only=True)
    spent = serializers.SerializerMethodField()
    remaining = serializers.SerializerMethodField()
    over_budget = serializers.SerializerMethodField()

    class Meta:
        model = PersonalBudget
        fields = ("id", "category", "category_name", "year", "month",
                  "budgeted_amount", "spent", "remaining", "over_budget")

    def get_spent(self, obj):
        return str(obj.spent().quantize(CENT))

    def get_remaining(self, obj):
        val = (obj.budgeted_amount - obj.spent()).quantize(CENT)
        return str(val)

    def get_over_budget(self, obj):
        return obj.spent() > obj.budgeted_amount


class SavingsGoalSerializer(serializers.ModelSerializer):
    saved = serializers.SerializerMethodField()
    progress_percent = serializers.SerializerMethodField()

    class Meta:
        model = SavingsGoal
        fields = ("id", "name", "target_amount", "target_date", "status",
                  "saved", "progress_percent", "created_at")

    def get_saved(self, obj):
        return str(obj.saved().quantize(CENT))

    def get_progress_percent(self, obj):
        return str(obj.progress_percent())


class PersonalLoanSerializer(serializers.ModelSerializer):
    paid = serializers.SerializerMethodField()
    remaining = serializers.SerializerMethodField()

    class Meta:
        model = PersonalLoan
        fields = ("id", "name", "counterparty_name", "direction",
                  "principal", "emi_amount", "installments", "start_date",
                  "status", "notes", "paid", "remaining", "client_reference",
                  "created_at")

    def get_paid(self, obj):
        return str(obj.paid().quantize(CENT))

    def get_remaining(self, obj):
        return str(obj.remaining().quantize(CENT))
