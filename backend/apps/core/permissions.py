"""Server-side permission enforcement (Sections 4 & 25, rule 7-8).

Permission model
----------------
Two roles inside a business:

ADMIN   - full access to everything in that business.
PARTNER - access limited to records assigned to their platform account
          (their own PartnerProfile). Backend-enforced everywhere; the UI is
          never trusted.

How a request resolves:
1. JWT identifies the user.
2. `BusinessAccessRequired` resolves the active business (URL kwarg or
   `?business=<id>`), checks a BusinessMembership exists and is active, and
   attaches `request.business` + `request.business_membership`.
3. `BusinessRoleRequired` restricts to given roles (e.g. ADMIN-only actions).
"""
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from apps.core.middleware import record_audit
from apps.organizations.models import BusinessMembership
from apps.organizations.views_utils import resolve_business_id


class BusinessAccessRequired(BasePermission):
    """Verify the requester is an active member of the target business.

    Sets on success:
      request.business              -> Business
      request.business_membership   -> BusinessMembership (role, permissions)
    """

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False

        business_id = resolve_business_id(view, request)
        if not business_id:
            raise PermissionDenied(
                "A business must be selected (active_business header, ?business=, or URL)."
            )
        try:
            membership = (
                BusinessMembership.objects.select_related("business", "user")
                .filter(
                    business_id=business_id,
                    user=user,
                    is_active=True,
                    business__is_active=True,
                )
                .first()
            )
        except Exception:
            membership = None
        if membership is None:
            raise PermissionDenied(
                "You are not an active member of this business, or it does not exist."
            )

        request.business = membership.business
        request.business_membership = membership
        return True


class BusinessRoleRequired(BasePermission):
    """Require one of the given business roles, e.g. ADMIN-only actions."""

    def __init__(self, roles=("ADMIN",)):
        super().__init__()
        self.roles = roles

    def has_permission(self, request, view):
        BusinessAccessRequired().has_permission(request, view)
        membership = getattr(request, "business_membership", None)
        if membership is None or membership.role not in self.roles:
            raise PermissionDenied(
                f"This action requires one of the roles: {', '.join(self.roles)}."
            )
        return True


def is_admin(request) -> bool:
    m = getattr(request, "business_membership", None)
    return bool(m and m.role == BusinessMembership.Role.ADMIN)


def partner_scope_partnersqs(request):
    """Partners the requesting user may see.

    ADMIN -> every active partner in the business.
    PARTNER -> only their own partner profile.
    """
    m = getattr(request, "business_membership", None)
    if m is None:
        return None
    if m.role == BusinessMembership.Role.ADMIN:
        return m.business.partners.all()
    return m.business.partners.filter(linked_user=request.user)


def assert_can_manage_partners(request):
    """Only ADMINs may create/edit/deactivate partners (Section 4)."""
    if not is_admin(request):
        record_audit(
            "permission_change", None,
            resource_type="PartnerManagement", resource_id="attempt_denied",
        )
        raise PermissionDenied("Only business ADMINs can manage partners.")
