from rest_framework import serializers

from apps.partners.models import Partner


class PartnerSerializer(serializers.ModelSerializer):
    perf_summary = serializers.SerializerMethodField()

    class Meta:
        model = Partner
        read_only_fields = ("created_by", "created_at", "updated_at")
        fields = (
            "id", "name", "phone", "email", "address", "profile_image",
            "joining_date", "initial_capital", "status", "notes",
            "permissions", "linked_user", "perf_summary",
            "created_at", "updated_at",
        )

    def get_perf_summary(self, obj):
        return obj.perf_summary()

    def validate_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Partner name is required.")
        return value.strip()

    def validate_initial_capital(self, value):
        if value < 0:
            raise serializers.ValidationError("Initial capital cannot be negative.")
        return value


class PartnerCreateSerializer(PartnerSerializer):
    """ADMIN creates a partner; may simultaneously link a platform user and
    auto-create their PARTNER membership (Section 4 permissions)."""

    def create(self, validated_data):
        request = self.context["request"]
        business = request.business
        partner = Partner.objects.create(
            business=business,
            created_by=request.user,
            **validated_data,
        )
        linked_user = validated_data.get("linked_user")
        if linked_user is not None:
            from apps.organizations.models import BusinessMembership

            BusinessMembership.objects.get_or_create(
                user=linked_user,
                business=business,
                defaults={"role": BusinessMembership.Role.PARTNER},
            )
        return partner
