"""Auth views: register, login (records login events + device session),
refresh, logout (blacklists refresh token), current user, password/PIN
management, device sessions, and login history."""
from django.db.models import Q
from django.utils import timezone
from rest_framework import generics, permissions, status, serializers as drf_serializers
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.core.middleware import get_audit_context, record_audit
from apps.users.models import LoginEvent, User, UserDeviceSession
from apps.users.serializers import (
    ChangePasswordSerializer,
    DeviceSessionSerializer,
    LoginEventSerializer,
    PinSerializer,
    RegisterSerializer,
    UserSerializer,
)


class RegisterView(APIView):
    permission_classes = (permissions.AllowAny,)

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        record_audit("create", user, resource_type="User", resource_id=user.pk)
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class MeView(APIView):
    def get(self, request):
        return Response(UserSerializer(request.user).data)


class MeUpdateView(APIView):
    """Update only the whitelisted self-profile fields."""

    PATCHABLE = {
        "phone", "avatar", "biometric_enabled",
        "active_business_id", "session_auto_logout_minutes",
    }

    def patch(self, request):
        changed = {}
        for field, value in request.data.items():
            if field in self.PATCHABLE:
                setattr(request.user, field, value)
                changed[field] = value
        if not changed:
            raise drf_serializers.ValidationError(
                {"fields": f"No updatable fields. Allowed: {sorted(self.PATCHABLE)}"}
            )
        request.user.save()
        return Response(UserSerializer(request.user).data)


class LoginView(TokenObtainPairView):
    """JWT login with login-event logging and device session creation."""

    throttle_scope = "auth"

    def post(self, request, *args, **kwargs):
        identifier = (request.data.get("username") or "").strip()
        ip = get_audit_context().get("ip")
        ua = get_audit_context().get("user_agent", "")

        # Accept email as login identifier by mapping it to the username that
        # SimpleJWT's serializer knows about (USERNAME_FIELD).
        login_data = dict(request.data)
        if "$" not in str(identifier) and "@" in str(identifier):
            by_email = User.objects.filter(email__iexact=identifier).first()
            if by_email is None:
                LoginEvent.objects.create(
                    username_attempted=identifier,
                    result=LoginEvent.Result.FAILED, ip_address=ip, user_agent=ua,
                )
                return Response(
                    {"detail": "No active account found with the given credentials."},
                    status=status.HTTP_401_UNAUTHORIZED,
                )
            login_data["username"] = by_email.username
            identifier = by_email.username

        try:
            serializer = TokenObtainPairSerializer(data=login_data)
            serializer.is_valid(raise_exception=True)
        except Exception:
            attempted_user = User.objects.filter(
                Q(username__iexact=identifier) | Q(email__iexact=identifier)
            ).first() if identifier else None
            LoginEvent.objects.create(
                user=attempted_user,
                username_attempted=identifier,
                result=LoginEvent.Result.FAILED,
                ip_address=ip,
                user_agent=ua,
            )
            if attempted_user is not None:
                record_audit("login_failed", attempted_user,
                             resource_type="User", resource_id=attempted_user.pk)
            raise

        user = serializer.user
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])

        LoginEvent.objects.create(
            user=user, username_attempted=identifier,
            result=LoginEvent.Result.SUCCESS, ip_address=ip, user_agent=ua,
        )
        record_audit("login", user, resource_type="User", resource_id=user.pk)

        device_id = request.data.get("device_id", "")
        session, _ = UserDeviceSession.objects.get_or_create(
            user=user,
            device_id=device_id,
            defaults={
                "device_name": request.data.get("device_name", ""),
                "platform": request.data.get("platform", ""),
                "ip_address": ip,
                "is_active": True,
            },
        )
        data = dict(serializer.validated_data)
        data["session_id"] = str(session.id)
        data["user"] = UserSerializer(user).data
        return Response(data)


class LogoutView(APIView):
    """Blacklist the presented refresh token; deactivate matching session."""

    def post(self, request):
        refresh = request.data.get("refresh", "")
        if refresh:
            try:
                token = RefreshToken(refresh)
                jti = token.payload.get("jti", "")
                UserDeviceSession.objects.filter(
                    user=request.user, refresh_jti=jti
                ).update(is_active=False, revoked_at=timezone.now())
                token.blacklist()
                record_audit("logout", request.user,
                             resource_type="User", resource_id=request.user.pk)
            except TokenError:
                pass
        return Response({"detail": "Logged out."})


class ChangePasswordView(APIView):
    def post(self, request):
        serializer = ChangePasswordSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        request.user.set_password(serializer.validated_data["new_password"])
        request.user.save(update_fields=["password"])

        # Force re-login everywhere: blacklist all outstanding refresh tokens.
        from rest_framework_simplejwt.token_blacklist.models import (
            BlacklistedToken, OutstandingToken,
        )
        for token in OutstandingToken.objects.filter(user=request.user):
            BlacklistedToken.objects.get_or_create(token=token)
        request.user.device_sessions.filter(is_active=True).update(
            is_active=False, revoked_at=timezone.now()
        )
        record_audit(
            "update", request.user, resource_type="User", resource_id=request.user.pk,
            changes={"password": "***changed***"},
        )
        return Response({"detail": "Password changed. Please log in again."})


class PinManageView(APIView):
    """App-level PIN lock (prompted by clients after JWT login)."""

    def post(self, request):
        serializer = PinSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        request.user.set_pin(serializer.validated_data["pin"])
        request.user.save(update_fields=["pin_hash"])
        record_audit(
            "update", request.user, resource_type="User", resource_id=request.user.pk,
            changes={"pin": "***set***"},
        )
        return Response({"detail": "PIN updated."})


class DeviceSessionsAPIView(APIView):
    def get(self, request):
        sessions = request.user.device_sessions.all()[:100]
        return Response(DeviceSessionSerializer(sessions, many=True).data)

    def post(self, request):
        """Revoke a session: {"session_id": "<uuid>"}"""
        session = request.user.device_sessions.filter(
            id=request.data.get("session_id"), is_active=True
        ).first()
        if session is None:
            raise drf_serializers.ValidationError({"session_id": "Active session not found."})
        session.revoke()
        record_audit("logout", request.user, resource_type="UserDeviceSession",
                     resource_id=session.pk)
        return Response({"detail": "Session revoked."})


class LoginHistoryView(generics.ListAPIView):
    serializer_class = LoginEventSerializer

    def get_queryset(self):
        return LoginEvent.objects.filter(user=self.request.user)[:200]
