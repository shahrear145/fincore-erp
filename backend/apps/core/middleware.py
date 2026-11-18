"""Immutable audit trail (Section 18).

Every create/edit/delete of an important record is logged with:
previous value, new value, actor, timestamp, optional reason.

The AuditContextMiddleware stores the acting user + request metadata in a
thread-local so service-layer code can log audits without threading a
`request` argument through every call.
"""
import contextvars
import traceback

from django.db import models

from apps.core.models import UUIDModel

_current = contextvars.ContextVar("audit_context", default=None)


def set_audit_context(user, ip=None, user_agent=""):
    _current.set({"user": user, "ip": ip, "user_agent": user_agent})


def get_audit_context():
    return _current.get() or {"user": None, "ip": None, "user_agent": ""}


class AuditContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        set_audit_context(
            user if (user is not None and user.is_authenticated) else None,
            ip=_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:512],
        )
        return self.get_response(request)


def _client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class AuditLog(UUIDModel):
    class Action(models.TextChoices):
        CREATE = "create"
        UPDATE = "update"
        DELETE = "delete"          # soft delete
        REVERSAL = "reversal"
        LOGIN = "login"
        LOGOUT = "logout"
        LOGIN_FAILED = "login_failed"
        PERMISSION_CHANGE = "permission_change"
        LOCK_PERIOD = "lock_period"
        UNLOCK_REQUEST = "unlock_request"

    actor = models.ForeignKey(
        "users.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    action = models.CharField(max_length=32, choices=Action.choices, db_index=True)
    resource_type = models.CharField(max_length=64, db_index=True)
    resource_id = models.CharField(max_length=64, blank=True, db_index=True)
    business = models.ForeignKey(
        "organizations.Business", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="audit_logs",
    )
    previous = models.JSONField(null=True, blank=True)
    changes = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["resource_type", "resource_id"]),
            models.Index(fields=["actor", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.action} {self.resource_type}#{self.resource_id} by {self.actor_id}"


def record_audit(action, resource, *, business=None, previous=None, changes=None,
                 reason="", resource_type="", resource_id=""):
    """Log an audit entry using the ambient request context when available."""
    ctx = get_audit_context()
    return AuditLog.objects.create(
        actor=ctx.get("user"),
        action=action,
        resource_type=resource_type or getattr(resource, "_meta", None)
        and resource._meta.label or resource_type,
        resource_id=str(resource_id or (
            resource.pk if resource is not None else resource_id or ""
        )) or "",
        business=business or getattr(resource, "business", None),
        previous=previous,
        changes=changes,
        reason=reason,
        ip_address=ctx.get("ip"),
        user_agent=ctx.get("user_agent", ""),
    )


def model_diff(instance, updated_data: dict) -> dict:
    """Return {field: (old_value, new_value)} for changed scalar fields."""
    diff = {}
    for field, new_value in updated_data.items():
        old_value = getattr(instance, field, None)
        if old_value != new_value:
            diff[field] = [str(old_value), str(new_value)]
    return diff


def debug_traceback() -> str:
    return "".join(traceback.format_stack(limit=6))
