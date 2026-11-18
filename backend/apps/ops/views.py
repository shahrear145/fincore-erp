"""Operations, security & monitoring API (Phase 8).

- /api/health/               — unauthenticated liveness/readiness probe
- /api/ops/status/           — staff-only platform status
- /api/ops/backups/          — staff-only encrypted backup create/list/verify
- /api/security/devices/     — user's trusted devices (list/revoke)
- /api/security/sessions/    — user's session records (list/revoke)
- /api/security/totp/        — TOTP 2FA setup/confirm/disable/verify
"""
import json

from django.conf import settings
from django.db import connection
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ops.backup import create_backup, verify_backup
from apps.ops.models import (
    BackupRecord, SessionRecord, TOTPDevice, TrustedDevice,
)


class HealthView(APIView):
    """Liveness + readiness. No auth — safe for load balancers."""
    permission_classes = (permissions.AllowAny,)
    authentication_classes = ()
    throttle_classes = ()

    def get(self, request):
        checks = {"database": "ok"}
        ok = True
        try:
            with connection.cursor() as c:
                c.execute("SELECT 1")
                c.fetchone()
        except Exception as exc:
            checks["database"] = f"error: {exc}"
            ok = False
        return Response({
            "status": "ok" if ok else "degraded",
            "time": timezone.now().isoformat(),
            "checks": checks,
        }, status=200 if ok else 503)


class IsStaff(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated
                    and request.user.is_staff)


class OpsStatusView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsStaff)

    def get(self, request):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        db_ok = True
        try:
            with connection.cursor() as c:
                c.execute("SELECT 1")
        except Exception:
            db_ok = False
        redis_ok = None
        redis_url = getattr(settings, "CELERY_BROKER_URL", "") or ""
        if redis_url:
            try:
                import redis
                redis.from_url(redis_url).ping()
                redis_ok = True
            except Exception:
                redis_ok = False
        return Response({
            "database": db_ok,
            "redis_configured": bool(redis_url),
            "redis_reachable": redis_ok,
            "debug": settings.DEBUG,
            "users": User.objects.count(),
            "time": timezone.now().isoformat(),
        })


# --- Backups (staff) ---------------------------------------------------------

class BackupListCreateView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsStaff)

    def get(self, request):
        rows = [{
            "id": str(b.pk), "status": b.status, "storage": b.storage,
            "location": b.location, "size_bytes": b.size_bytes,
            "sha256": b.sha256, "encrypted": b.encrypted,
            "created_at": b.created_at, "error": b.error or None,
        } for b in BackupRecord.objects.all()[:50]]
        return Response({"count": len(rows), "backups": rows})

    def post(self, request):
        note = (request.data.get("note") or "").strip()
        record = create_backup(request.user, note=note)
        body = {"id": str(record.pk), "status": record.status,
                "storage": record.storage, "location": record.location,
                "size_bytes": record.size_bytes, "sha256": record.sha256,
                "encrypted": record.encrypted}
        if record.error:
            body["error"] = record.error
        return Response(body, status=201
                        if record.status == BackupRecord.Status.COMPLETED
                        else 500)


class BackupVerifyView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsStaff)

    def post(self, request, backup_id):
        record = BackupRecord.objects.filter(pk=backup_id).first()
        if record is None:
            return Response({"detail": "Not found."}, status=404)
        return Response(verify_backup(record))


# --- Devices & sessions (self-service) --------------------------------------

class DeviceListView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        rows = [{
            "id": str(d.pk), "device_id": d.device_id, "name": d.name,
            "platform": d.platform, "is_active": d.is_active,
            "last_ip": str(d.last_ip) if d.last_ip else None,
            "last_seen": d.last_seen,
        } for d in request.user.trusted_devices.all()]
        return Response({"devices": rows})

    def post(self, request):
        device_id = (request.data.get("device_id") or "").strip()
        if not device_id:
            return Response({"detail": "device_id required."}, status=400)
        ip = request.META.get("REMOTE_ADDR")
        dev, _ = TrustedDevice.objects.update_or_create(
            user=request.user, device_id=device_id,
            defaults={"name": request.data.get("name", ""),
                      "platform": request.data.get("platform", ""),
                      "last_ip": ip, "is_active": True})
        return Response({"id": str(dev.pk), "device_id": dev.device_id,
                         "is_active": dev.is_active}, status=201)


class DeviceRevokeView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, device_id):
        dev = request.user.trusted_devices.filter(pk=device_id).first()
        if dev is None:
            return Response({"detail": "Not found."}, status=404)
        dev.is_active = False
        dev.save(update_fields=["is_active"])
        # revoke any active session records tied to this device
        now = timezone.now()
        dev.sessions.filter(revoked_at__isnull=True).update(
            revoked_at=now, revoke_reason="device revoked")
        return Response({"id": str(dev.pk), "is_active": False})


class SessionListView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        rows = [{
            "id": str(s.pk), "device_id": str(s.device_id)
            if s.device_id else None,
            "ip_address": str(s.ip_address) if s.ip_address else None,
            "user_agent": s.user_agent[:120],
            "created_at": s.created_at, "revoked_at": s.revoked_at,
            "is_active": s.is_active,
        } for s in request.user.session_records.all()[:50]]
        return Response({"sessions": rows})


class SessionRevokeView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, session_id):
        rec = request.user.session_records.filter(pk=session_id).first()
        if rec is None:
            return Response({"detail": "Not found."}, status=404)
        rec.revoked_at = timezone.now()
        rec.revoke_reason = "user revoked"
        rec.save(update_fields=["revoked_at", "revoke_reason"])
        # best-effort: blacklist the refresh token if JTI known
        if rec.refresh_jti:
            try:
                from rest_framework_simplejwt.token_blacklist.models import (
                    BlacklistedToken, OutstandingToken)
                tok = OutstandingToken.objects.filter(
                    jti=rec.refresh_jti).first()
                if tok:
                    BlacklistedToken.objects.get_or_create(token=tok)
            except Exception:
                pass
        return Response({"id": str(rec.pk), "revoked": True})


# --- TOTP 2FA (self-service, stdlib RFC6238) --------------------------------

class TOTPSetupView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        device, created = TOTPDevice.objects.get_or_create(
            user=request.user,
            defaults={"secret": TOTPDevice.generate_secret(),
                      "is_confirmed": False})
        if not created and device.is_confirmed:
            return Response({"detail": "TOTP already enabled. Disable first "
                                       "to re-enroll."}, status=400)
        if not created:
            device.secret = TOTPDevice.generate_secret()
            device.is_confirmed = False
            device.save(update_fields=["secret", "is_confirmed"])
        return Response({"secret": device.secret,
                         "provisioning_uri": device.provisioning_uri(),
                         "confirmed": False})


class TOTPConfirmView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        device = TOTPDevice.objects.filter(user=request.user).first()
        if device is None:
            return Response({"detail": "No pending TOTP setup."}, status=400)
        code = request.data.get("code")
        if not device.verify(code):
            return Response({"detail": "Invalid code."}, status=400)
        device.is_confirmed = True
        device.save(update_fields=["is_confirmed"])
        return Response({"confirmed": True})


class TOTPDisableView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        device = TOTPDevice.objects.filter(user=request.user).first()
        if device is None:
            return Response({"detail": "TOTP not enabled."}, status=400)
        code = request.data.get("code")
        if device.is_confirmed and not device.verify(code):
            return Response({"detail": "Invalid code."}, status=400)
        device.delete()
        return Response({"disabled": True})


class TOTPStatusView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        device = TOTPDevice.objects.filter(user=request.user).first()
        return Response({"enabled": bool(device and device.is_confirmed),
                         "pending": bool(device and not device.is_confirmed)})
