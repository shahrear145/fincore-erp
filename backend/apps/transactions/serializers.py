from rest_framework import serializers

from apps.transactions.models import (
    Transaction, TransactionDateMemory,
)


class TransactionReadSerializer(serializers.ModelSerializer):
    partner_name = serializers.CharField(source="partner.name", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    type = serializers.CharField(source="transaction_type", read_only=True)

    class Meta:
        model = Transaction
        fields = (
            "id", "transaction_type", "type", "amount", "partner", "partner_name",
            "customer", "customer_name", "transaction_date", "transaction_time",
            "payment_method", "to_payment_method", "adjustment_direction",
            "description", "notes", "reference_number", "receipt_no",
            "expense_kind",
            "status", "reversal_of", "client_reference",
            "created_at", "updated_at", "created_by",
        )


class TransactionWriteSerializer(serializers.ModelSerializer):
    """Transport for POST /api/transactions/ — business rules live in
    apps.transactions.services (the only writer)."""

    class Meta:
        model = Transaction
        fields = (
            "id", "transaction_type", "amount", "partner", "customer",
            "transaction_date", "transaction_time", "payment_method",
            "to_payment_method", "adjustment_direction", "description",
            "notes", "reference_number", "client_reference", "expense_kind",
        )

    def to_internal_value(self, data):
        return data


class DateMemorySerializer(serializers.ModelSerializer):
    partner_name = serializers.CharField(source="partner.name", read_only=True)

    class Meta:
        model = TransactionDateMemory
        fields = ("partner", "partner_name", "last_date", "updated_at")
