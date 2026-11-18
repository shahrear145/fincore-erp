from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import generics, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.middleware import record_audit
from apps.core.permissions import BusinessAccessRequired, BusinessRoleRequired
from apps.organizations.models import Business, BusinessMembership, FinancialPeriod
from apps.organizations.serializers import (
    BusinessMembershipSerializer,
    BusinessSerializer,
    FinancialPeriodSerializer,
)
from apps.users.models import User


class MyBusinessesView(APIView):
    """All businesses the current user actively belongs to."""

    def get(self, request):
        memberships = (
            BusinessMembership.objects.filter(user=request.user, is_active=True)
            .select_related("business")
        )
        return Response([
            {
                "business": BusinessSerializer(
                    m.business, context={"request": request}
                ).data,
                "membership": BusinessMembershipSerializer(m).data,
            }
            for m in memberships
        ])


class BusinessListCreateView(generics.ListCreateAPIView):
    """Businesses the user belongs to; creating one makes the creator ADMIN owner."""
    serializer_class = BusinessSerializer

    def get_queryset(self):
        return Business.objects.filter(
            memberships__user=self.request.user,
            memberships__is_active=True,
        ).distinct()

    def perform_create(self, serializer):
        user = self.request.user
        data = self.request.data
        business = Business.objects.create(
            owner=user,
            name=data.get("name", "Untitled business"),
            contact_phone=data.get("contact_phone", ""),
            contact_email=data.get("contact_email", ""),
            address=data.get("address", ""),
            currency=data.get("currency", "BDT"),
            timezone=data.get("timezone", "Asia/Dhaka"),
            slug=self._unique_slug(data.get("name", "business")),
        )
        BusinessMembership.objects.create(
            user=user,
            business=business,
            role=BusinessMembership.Role.ADMIN,
            partner_permissions={
                "can_create_transactions": True,
                "can_edit_own_records": True,
                "can_edit_other_partners_dues": True,
                "can_view_all_reports": True,
                "can_manage_customers": True,
                "can_view_partners": True,
            },
        )
        record_audit(
            "create", None, business=business,
            resource_type="Business", resource_id=business.pk,
        )
        serializer.instance = business

    @staticmethod
    def _unique_slug(name: str) -> str:
        from django.utils.text import slugify

        base = slugify(name)[:150] or "business"
        slug, i = base, 1
        while Business.objects.filter(slug=slug).exists():
            i += 1
            slug = f"{base}-{i}"
        return slug


class BusinessMembersListAddView(generics.ListCreateAPIView):
    """GET: business members (any active member may list).
    POST (ADMIN-only): grant membership by email/username, role in {ADMIN, PARTNER}."""
    serializer_class = BusinessMembershipSerializer

    def get_permissions(self):
        if self.request.method == "POST":
            return [BusinessRoleRequired(roles=("ADMIN",))]
        return [BusinessAccessRequired()]

    def get_queryset(self):
        return BusinessMembership.objects.filter(
            business=self.request.business
        ).select_related("user")

    def perform_create(self, serializer):
        business = self.request.business
        identifier = (
            self.request.data.get("email")
            or self.request.data.get("username")
        ).strip()
        role = self.request.data.get("role", BusinessMembership.Role.PARTNER)
        if role not in BusinessMembership.Role.values:
            raise serializers.ValidationError({"role": "Invalid role."})
        target = User.objects.filter(
            Q(email__iexact=identifier) | Q(username__iexact=identifier)
        ).first()
        if target is None:
            raise serializers.ValidationError({
                "email": "No registered user with that email/username. "
                         "The person must create an account first.",
            })

        obj, created = BusinessMembership.objects.get_or_create(
            user=target, business=business, defaults={"role": role}
        )
        if not created:
            previous = {"role": obj.role, "is_active": obj.is_active}
            obj.is_active = True
            obj.role = role
            obj.save()
            record_audit("update", obj, business=business,
                         previous=previous, changes={"role": role, "is_active": True})
        else:
            record_audit("create", obj, business=business,
                         changes={"user": str(target), "role": role})
        serializer.instance = obj


class BusinessMemberActivateDeactivateView(APIView):
    """ADMIN-only: activate/deactivate a membership. Memberships are never
    deleted — deactivation preserves history (Section 18)."""

    def post(self, request, pk, member_pk):
        BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, self)
        member = get_object_or_404(
            BusinessMembership, pk=member_pk, business=request.business
        )
        if not request.data.get("is_active", True) and member.user_id == request.user.pk:
            raise serializers.ValidationError(
                {"is_active": "You cannot deactivate your own membership."}
            )
        previous = {"is_active": member.is_active}
        member.is_active = bool(request.data.get("is_active", member.is_active))
        member.save(update_fields=["is_active"])
        record_audit(
            "update" if member.is_active else "delete", member,
            business=request.business, previous=previous,
            changes={"is_active": member.is_active},
            reason=request.data.get("reason", ""),
        )
        return Response(BusinessMembershipSerializer(member).data)


class FinancialPeriodListCreateView(generics.ListCreateAPIView):
    """ADMIN-only: manage lockable financial periods for this business."""
    serializer_class = FinancialPeriodSerializer

    def get_permissions(self):
        return [BusinessRoleRequired(roles=("ADMIN",))]

    def get_queryset(self):
        return FinancialPeriod.objects.filter(business=self.request.business)

    def perform_create(self, serializer):
        serializer.save(business=self.request.business)


class FinancialPeriodLockView(APIView):
    """ADMIN-only: lock / unlock a period. Both actions are audited."""

    def post(self, request, pk, period_pk):
        BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, self)
        period = get_object_or_404(
            FinancialPeriod, pk=period_pk, business=request.business
        )
        lock = bool(request.data.get("is_locked", True))
        previous = {"is_locked": period.is_locked}
        period.is_locked = lock
        period.locked_by = request.user if lock else None
        period.save(update_fields=["is_locked", "locked_by"])
        record_audit(
            "lock_period" if lock else "unlock_request", period,
            business=period.business, previous=previous,
            changes={"is_locked": lock, "locked_by": str(request.user)},
            reason=request.data.get("reason", ""),
        )
        return Response(FinancialPeriodSerializer(period).data)
