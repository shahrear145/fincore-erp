from decimal import Decimal

from rest_framework import serializers

from apps.customers.models import (
    Customer, CustomerAssignment, CustomerDocument, DueEntry,
)
from apps.organizations.models import BusinessMembership


class CustomerWriteSerializer(serializers.ModelSerializer):
    """Create/update. Partner auto-gets an assignment on their new clients."""

    class Meta:
        model = Customer
        fields = (
            "id", "name", "phone", "email", "address", "profile_photo",
            "category", "opening_balance", "advance_payment", "due_limit",
            "notes", "status", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Customer name is required.")
        return value.strip()

    def validate(self, attrs):
        if attrs.get("opening_balance") is not None and attrs.get(
                "advance_payment") and attrs["opening_balance"] > 0 \
                and attrs["advance_payment"] > 0:
            raise serializers.ValidationError(
                "Opening balance and advance cannot both be positive."
            )
        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        m = request.business_membership
        from apps.partners.models import Partner

        partner = None
        if m.role == BusinessMembership.Role.ADMIN:
            payload_partner_id = request.data.get("partner_id") or request.data.get(
                "assign_to_partner")
            if payload_partner_id:
                partner = Partner.objects.filter(
                    pk=payload_partner_id, business=request.business
                ).first()
                if partner is None:
                    raise serializers.ValidationError({"partner_id": "Unknown partner."})
        else:
            # A partner creating a client assigns it to themselves (Section 4).
            partner = Partner.objects.filter(
                business=request.business, linked_user=request.user
            ).first()
            if partner is None:
                raise serializers.ValidationError(
                    "No partner profile linked to this account."

                )

        customer = Customer.objects.create(
            business=request.business, created_by=request.user, **validated_data
        )
        opening = validated_data.get("opening_balance", Decimal("0.00"))
        advance = validated_data.get("advance_payment", Decimal("0.00"))
        if partner is not None:
            CustomerAssignment.objects.create(
                customer=customer, partner=partner,
                opening_due=max(opening, Decimal("0.00")),
                advance=max(advance, Decimal("0.00")),
            )
        return customer


class CustomerListSerializer(serializers.ModelSerializer):
    """List payload: fast, with aggregates but no nested dues table."""

    total_due = serializers.SerializerMethodField()
    partners_count = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = (
            "id", "name", "phone", "category", "status", "profile_photo",
            "opening_balance", "advance_payment", "total_due", "partners_count",
            "created_at",
        )

    def get_total_due(self, obj):
        return obj.total_due()

    def get_partners_count(self, obj):
        return obj.assignments.filter(is_active=True).count()


class CustomerDetailSerializer(serializers.ModelSerializer):
    total_due = serializers.SerializerMethodField()
    partner_wise_dues = serializers.SerializerMethodField()
    assigned_partners = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = (
            "id", "name", "phone", "email", "address", "profile_photo",
            "category", "status", "opening_balance", "advance_payment",
            "due_limit", "notes", "total_due", "partner_wise_dues",
            "assigned_partners", "created_at", "updated_at",
        )

    def get_total_due(self, obj):
        return obj.total_due()

    def get_partner_wise_dues(self, obj):
        return obj.dues_by_partner()

    def get_assigned_partners(self, obj):
        return [
            {"partner_id": str(a.partner_id), "partner_name": a.partner.name,
             "is_active": a.is_active}
            for a in obj.assignments.select_related("partner")
        ]


class PartnerMiniSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()


class CustomerAssignmentScalarSerializer(serializers.ModelSerializer):
    partner_name = serializers.CharField(source="partner.name", read_only=True)
    current_due = serializers.SerializerMethodField()

    class Meta:
        model = CustomerAssignment
        fields = ("id", "partner", "partner_name", "opening_due", "advance",
                  "current_due", "is_active")

    def get_current_due(self, obj):
        return obj.current_due()


class DueEntryWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = DueEntry
        fields = ("id", "kind", "amount", "transaction_date", "note", "created_at")
        read_only_fields = ("id", "created_at")

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Amount must be positive.")
        return value


class DueEntryReadSerializer(serializers.ModelSerializer):
    partner_id = serializers.UUIDField(source="assignment.partner_id", read_only=True)
    partner_name = serializers.CharField(source="assignment.partner.name",
                                         read_only=True)

    class Meta:
        model = DueEntry
        fields = ("id", "kind", "amount", "transaction_date", "note",
                  "partner_id", "partner_name", "created_at")


class CustomerDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerDocument
        fields = ("id", "file", "title", "uploaded_by", "created_at")
        read_only_fields = ("id", "uploaded_by", "created_at")
