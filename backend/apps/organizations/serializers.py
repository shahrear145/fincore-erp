from rest_framework import serializers

from apps.organizations.models import Business, BusinessMembership, FinancialPeriod


class BusinessMembershipSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source="user.get_full_name", read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = BusinessMembership
        read_only_fields = ("user", "business", "joined_at")
        fields = (
            "id", "user", "user_name", "user_email", "business",
            "role", "is_active", "partner_permissions", "joined_at",
        )


class BusinessSerializer(serializers.ModelSerializer):
    my_role = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = Business
        read_only_fields = ("owner", "slug")
        fields = (
            "id", "name", "slug", "currency", "timezone", "owner",
            "contact_phone", "contact_email", "address", "logo",
            "is_active", "my_role", "member_count", "created_at", "updated_at",
        )

    def get_my_role(self, obj):
        request = self.context.get("request")
        membership = obj.memberships.filter(user=request.user, is_active=True).first()
        return membership.role if membership else None

    def get_member_count(self, obj):
        return obj.memberships.filter(is_active=True).count()


class FinancialPeriodSerializer(serializers.ModelSerializer):
    class Meta:
        model = FinancialPeriod
        fields = (
            "id", "name", "start_date", "end_date",
            "is_locked", "locked_by", "locked_at",
        )
        read_only_fields = ("locked_by", "locked_at")

    def validate(self, attrs):
        if attrs.get("end_date") and attrs.get("start_date"):
            if attrs["end_date"] <= attrs["start_date"]:
                raise serializers.ValidationError(
                    "Period end date must be after start date."
                )
        return attrs
