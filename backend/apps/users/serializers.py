from rest_framework import serializers

from apps.users.models import LoginEvent, User, UserDeviceSession


class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)
    businesses_count = serializers.SerializerMethodField()

    class Meta:
        model = User
        read_only_fields = (
            "email", "username", "date_joined", "last_login",
            "two_factor_enabled", "two_factor_secret", "pin_hash",
        )
        fields = (
            "id", "username", "email", "full_name", "phone", "avatar",
            "two_factor_enabled", "biometric_enabled", "pin_hash",
            "active_business_id", "session_auto_logout_minutes",
            "businesses_count", "date_joined", "last_login",
        )

    def get_businesses_count(self, obj):
        return obj.memberships.filter(is_active=True).count()


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)
    password2 = serializers.CharField(write_only=True, trim_whitespace=False)

    class Meta:
        model = User
        fields = ("username", "email", "password", "password2", "phone")

    def validate_username(self, value):
        slug_ok = value.replace("_", "").replace(".", "").replace("-", "").isalnum() \
            and ("_" in value or "." in value or "-" in value or value.isalnum())
        if not slug_ok:
            raise serializers.ValidationError(
                "Username may only contain letters, digits, '.', '-' and '_'."
            )
        return value

    def validate(self, attrs):
        if attrs.get("password") != attrs.pop("password2", None):
            raise serializers.ValidationError({"password2": "Passwords do not match."})
        from django.contrib.auth.password_validation import validate_password
        from django.contrib.auth.models import User as _DUser  # noqa
        from apps.users.models import User

        candidate = User(username=attrs.get("username", ""), email=attrs.get("email", ""))
        validate_password(attrs["password"], user=candidate)
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(trim_whitespace=False)
    new_password = serializers.CharField(trim_whitespace=False, min_length=8)

    def validate_current_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value):
        from django.contrib.auth.password_validation import validate_password
        validate_password(value, self.context["request"].user)
        return value


class PinSerializer(serializers.Serializer):
    pin = serializers.RegexField(r"^\d{4,6}$", trim_whitespace=True)
    old_pin = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        user = self.context["request"].user
        if user.pin_hash and attrs.get("old_pin") and not user.check_pin(attrs["old_pin"]):
            raise serializers.ValidationError({"old_pin": "Current PIN is incorrect."})
        return attrs


class DeviceSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserDeviceSession
        fields = (
            "id", "device_name", "device_id", "platform",
            "ip_address", "last_seen_at", "is_active",
        )


class LoginEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoginEvent
        fields = ("id", "username_attempted", "result", "ip_address", "created_at")
