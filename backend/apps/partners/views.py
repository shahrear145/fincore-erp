"""Partner views with server-side scoping (Sections 4 & 25 rule 8).

GET  /api/partners/?business=<id>          list (ADMIN: all; PARTNER: own only)
POST /api/partners/?business=<id>          create (ADMIN only) — unlimited
GET/PUT/PATCH /api/partners/<id>/          detail/update (ADMIN; PARTNER: own)
POST /api/partners/<id>/status/            toggle ACTIVE/INACTIVE, audited
"""
from rest_framework import generics, permissions
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.middleware import record_audit
from apps.core.permissions import BusinessAccessRequired, BusinessRoleRequired
from apps.organizations.models import BusinessMembership
from apps.partners.models import Partner
from apps.partners.serializers import PartnerCreateSerializer, PartnerSerializer


def _scoped_partners(membership):
    """The queryset of partners this membership may see. Server-side, always."""
    if membership.role == BusinessMembership.Role.ADMIN:
        return Partner.objects.filter(business=membership.business)
    return Partner.objects.filter(business=membership.business, linked_user=membership.user)


class PartnerListCreateView(generics.ListCreateAPIView):
    permission_classes = (permissions.IsAuthenticated,)
    filterset_fields = ("status",)

    def get_permissions(self):
        if self.request.method == "POST":
            return [BusinessRoleRequired(roles=("ADMIN",))]
        return [BusinessAccessRequired()]

    def get_serializer_class(self):
        return PartnerCreateSerializer if self.request.method == "POST" else PartnerSerializer

    def get_queryset(self):
        return _scoped_partners(self.request.business_membership)

    def perform_create(self, serializer):
        serializer.save()


class PartnerDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = PartnerSerializer
    lookup_url_kwarg = "partner_id"

    def get_permissions(self):
        if self.request.method in ("PUT", "PATCH"):
            return [BusinessRoleRequired(roles=("ADMIN",))]
        return [BusinessAccessRequired()]

    def get_queryset(self):
        return _scoped_partners(self.request.business_membership)

    def perform_update(self, serializer):
        changes = {
            f: self.request.data[f]
            for f in self.request.data
            if f in PartnerSerializer.Meta.fields
            and f not in ("perf_summary", "linked_user")
        }
        previous = {}
        instance = self.get_object()
        for field, new_value in changes.items():
            old_value = getattr(instance, field, None)
            if old_value != new_value:
                previous[field] = str(old_value)
        instance = serializer.save()
        record_audit(
            "update", instance, business=instance.business,
            previous=previous or None,
        )


class PartnerStatusView(APIView):
    """ADMIN-only: toggle ACTIVE/INACTIVE. Partner records are never erased."""
    def post(self, request, partner_id):
        BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, self)
        partner = Partner.objects.get(pk=partner_id, business=request.business)
        new_status = request.data.get("status")
        if new_status not in Partner.Status.values:
            raise PermissionDenied("status must be one of Active/Inactive/Suspended/Exited.")
        previous = {"status": partner.status}
        partner.status = new_status
        partner.save(update_fields=["status"])
        record_audit(
            "update", partner,
            business=partner.business,
            previous=previous, changes={"status": new_status},
            reason=request.data.get("reason", ""),
        )
        return Response(PartnerSerializer(partner, context={"request": request}).data)


class PartnerDashboardView(APIView):
    """Partner profile landing data — informational contract for Phase 3."""

    def get(self, request, partner_id):
        BusinessAccessRequired().has_permission(request, self)
        m = request.business_membership
        if m.role != BusinessMembership.Role.ADMIN:
            partner = Partner.objects.get(
                pk=partner_id, business=request.business, linked_user=request.user
            )
        else:
            partner = Partner.objects.get(pk=partner_id, business=request.business)
        data = PartnerSerializer(partner, context={"request": request}).data
        perf = partner.perf_summary()
        data["dashboard"] = {
            "partner_id": str(partner.id),
            "assigned_customers": perf["assigned_customers"],
            "note": "Financial figures activate with Phase 3 (transactions).",
        }
        return Response(data)
