from rest_framework import serializers

from apps.accounting.models import Account, JournalEntry, JournalLine


class AccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = Account
        fields = ("id", "code", "name", "type", "parent", "is_system",
                  "is_active", "created_at")


class JournalLineSerializer(serializers.ModelSerializer):
    account_code = serializers.CharField(source="account.code",
                                         read_only=True)
    account_name = serializers.CharField(source="account.name",
                                         read_only=True)

    class Meta:
        model = JournalLine
        fields = ("account", "account_code", "account_name", "debit",
                  "credit", "line_no", "note")


class JournalEntrySerializer(serializers.ModelSerializer):
    lines = serializers.SerializerMethodField()

    class Meta:
        model = JournalEntry
        fields = ("id", "journal_no", "transaction_date", "memo",
                  "reversal_of", "source_kind", "source_id", "lines",
                  "created_at")

    def get_lines(self, obj):
        return JournalLineSerializer(obj.lines.order_by("line_no"),
                                     many=True).data
