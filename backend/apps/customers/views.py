"""Customer views — server-side scoping for every role (Sections 4-6).

Routes (all require an active business context, X-Active-Business or ?business=):
  GET/POST   /api/customers/
  GET/PATCH  /api/customers/<id>/
  GET        /api/customers/<id>/dues/          partner-wise due breakdown
  POST       /api/customers/<id>/assignments/   (ADMIN) link partner + opening due
  GET        /api/customers/<id>/statement/     due ledger (partner/date filters)
  GET/POST   /api/customers/<id>/documents/     supporting files
"""
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.middleware import record_audit
from apps.core.permissions import BusinessAccessRequired, BusinessRoleRequired
from apps.customers.models import Customer, CustomerAssignment, CustomerDocument, DueEntry
from apps.customers.serializers import (
    CustomerDetailSerializer,
    CustomerDocumentSerializer,
    CustomerListSerializer,
    CustomerWriteSerializer,
)
from apps.organizations.models import BusinessMembership


def _scope_customers(membership, user):
    """ADMIN: all business customers. PARTNER: only customers assigned to
    their partner profile. Enforced here, always."""
    if membership.role == BusinessMembership.Role.ADMIN:
        return Customer.objects.filter(business=membership.business)
    return Customer.objects.filter(
        business=membership.business,
        assignments__partner__linked_user=user,
        assignments__is_active=True,
    ).distinct()


class CustomerListCreateView(generics.ListCreateAPIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get_permissions(self):
        return [BusinessAccessRequired()]

    def get_serializer_class(self):
        return CustomerListSerializer if self.request.method == "GET" \
            else CustomerWriteSerializer

    def get_queryset(self):
        qs = _scope_customers(self.request.business_membership, self.request.user)
        params = self.request.query_params

        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(phone__icontains=search)
                | Q(email__icontains=search)
            )
        category = params.get("category")
        if category:
            qs = qs.filter(category=category)
        stat = params.get("status")
        if stat:
            qs = qs.filter(status=stat)
        partner_id = params.get("partner")
        if partner_id:
            # Partner-centric client list (Section 4: inside a partner profile).
            qs = qs.filter(assignments__partner_id=partner_id,
                           assignments__is_active=True)
        ordering = params.get("order", "name")
        allowed = {"name", "-name", "-created_at", "created_at"}
        if ordering in allowed:
            qs = qs.order_by(ordering)
        return qs

    def perform_create(self, serializer):
        instance = serializer.save()
        record_audit("create", instance, business=instance.business)


class CustomerDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = (permissions.IsAuthenticated,)
    lookup_url_kwarg = "customer_id"

    def get_permissions(self):
        return [BusinessAccessRequired()]

    def get_queryset(self):
        return _scope_customers(self.request.business_membership, self.request.user)

    def get_serializer_class(self):
        return CustomerDetailSerializer if self.request.method == "GET" \
            else CustomerWriteSerializer

    def perform_update(self, serializer):
        previous = {}
        instance = self.get_object()
        for f in self.request.data:
            if f in CustomerWriteSerializer.Meta.fields and f not in (
                    "id", "created_at", "updated_at"):
                old = getattr(instance, f, None)
                if str(old) != str(self.request.data[f]):
                    previous[f] = str(old)
        updated = serializer.save()
        record_audit(
            "update", updated, business=updated.business,
            previous=previous or None,
        )


class CustomerDuesView(APIView):
    """GET /dues/ → partner-wise breakdown + total (Section 5 example:
    Partner 1 due ৳5,000, Partner 2 due ৳3,000, total ৳8,000)."""

    def get(self, request, customer_id):
        BusinessAccessRequired().has_permission(request, self)
        customer = get_object_or_404(
            _scope_customers(self.request.business_membership, self.request.user),
            pk=customer_id,
        )
        return Response({
            "customer": customer.name,
            "partner_wise": customer.dues_by_partner(),
            "total_due": customer.total_due(),
        })


class CustomerAssignPartnerView(APIView):
    """ADMIN-only: link a partner to a customer with opening due.
    Shared customers keep SEPARATE partner-wise books automatically."""

    def post(self, request, customer_id):
        BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, self)
        customer = get_object_or_404(Customer, pk=customer_id,
                                     business=request.business)
        partner_id = request.data.get("partner_id")
        if not partner_id:
            raise serializers.ValidationError(
                {"partner_id": "partner_id is required."}
            )
        from apps.partners.models import Partner
        partner = Partner.objects.filter(pk=partner_id,
                                         business=request.business).first()
        if partner is None:
            raise serializers.ValidationError({"partner_id": "Unknown partner."})

        from decimal import Decimal as D
        raw = request.data.get("opening_due",
                               request.data.get("opening_balance", 0)) or 0
        opening_due = D(str(raw))
        assignment, created = CustomerAssignment.objects.get_or_create(
            customer=customer, partner=partner,
            defaults={"opening_due": opening_due},
        )
        record_audit("create" if created else "update", assignment,
                     business=customer.business)
        return Response(
            {
                "detail": "Partner assigned." if created
                else "Assignment already exists — reactivated.",
                "assignment_id": str(assignment.id),
                "current_due": assignment.current_due(),
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class CustomerStatementView(APIView):
    """Customer statement: due ledger lines across all partner books with a
    running balance, filterable by partner and date range."""

    def get(self, request, customer_id):
        BusinessAccessRequired().has_permission(request, self)
        customer = get_object_or_404(
            _scope_customers(self.request.business_membership, self.request.user),
            pk=customer_id,
        )
        qs = DueEntry.objects.filter(
            assignment__customer=customer
        ).select_related("assignment__partner").order_by(
            "transaction_date", "created_at"
        )
        partner = request.query_params.get("partner")
        if partner:
            qs = qs.filter(assignment__partner_id=partner)
        from_date = request.query_params.get("from")
        to_date = request.query_params.get("to")
        if from_date:
            qs = qs.filter(transaction_date__gte=from_date)
        if to_date:
            qs = qs.filter(transaction_date__lte=to_date)

        lines, balance, opening_due_sum = [], 0, 0
        for assignment in customer.assignments.select_related("partner"):
            opening_due_sum += assignment.opening_due
        balance = opening_due_sum
        for e in qs:
            signed = e.signed_amount()
            balance = balance + signed
            lines.append({
                "id": str(e.id),
                "kind": e.kind,
                "amount": e.amount,
                "signed": signed,
                "transaction_date": e.transaction_date,
                "partner_id": str(e.assignment.partner_id),
                "partner_name": e.assignment.partner.name,
                "note": e.note,
                "balance_after": balance,
            })
        return Response({
            "customer": customer.name,
            "opening_due_total": opening_due_sum,
            "lines": lines,
            "closing_due": customer.total_due(),
        })


class CustomerDocumentsView(APIView):
    def get(self, request, customer_id):
        BusinessAccessRequired().has_permission(request, self)
        customer = get_object_or_404(
            _scope_customers(self.request.business_membership, self.request.user),
            pk=customer_id,
        )
        docs = CustomerDocument.objects.filter(customer=customer)
        return Response(CustomerDocumentSerializer(docs, many=True).data)

    def post(self, request, customer_id):
        BusinessAccessRequired().has_permission(request, self)
        customer = get_object_or_404(
            _scope_customers(self.request.business_membership, self.request.user),
            pk=customer_id,
        )
        serializer = CustomerDocumentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        doc = serializer.save(customer=customer, uploaded_by=request.user)
        record_audit("create", doc, business=customer.business)
        return Response(CustomerDocumentSerializer(doc).data,
                        status=status.HTTP_201_CREATED)
