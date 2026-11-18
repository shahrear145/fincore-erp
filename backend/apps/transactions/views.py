"""Transaction API views (Sections 6, 7 & 9).

Anything converting money is a POST to these endpoints goes through
apps.transactions.services — one validated writer, server-enforced rules.
"""
from decimal import Decimal

from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.middleware import record_audit
from apps.core.permissions import BusinessAccessRequired, BusinessRoleRequired
from apps.customers.models import CustomerAssignment, PaymentAllocation
from apps.organizations.models import BusinessMembership
from apps.transactions.models import (
    Transaction, TransactionDateMemory, TransactionStatus, TransactionType,
    CUSTOMER_REQUIRED_TYPES, PaymentMethod, RecurringTransaction,
)
from apps.transactions.serializers import (
    DateMemorySerializer, TransactionReadSerializer, TransactionWriteSerializer,
)
from apps.transactions.services import (
    TransactionServiceError, create_transaction, reverse_transaction,
)


def _service_error(exc: TransactionServiceError):
    payload = exc.errors if exc.errors else {"detail": str(exc)}
    return Response(payload, status=exc.status_code)


def _scoped_member(request):
    m = request.business_membership
    if m.role == BusinessMembership.Role.ADMIN:
        return None
    return m.business.partners.filter(linked_user=request.user).first()


class TransactionListCreateView(generics.ListCreateAPIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get_permissions(self):
        return [BusinessAccessRequired()]

    def get_serializer_class(self):
        return TransactionReadSerializer if self.request.method == "GET" \
            else TransactionWriteSerializer

    def get_queryset(self):
        m = self.request.business_membership
        own = _scoped_member(self.request)
        qs = Transaction.objects.filter(
            business=self.request.business)
        if own is not None:
            qs = qs.filter(partner=own)
        p = self.request.query_params
        if p.get("type"):
            qs = qs.filter(transaction_type=p["type"])
        if p.get("method"):
            qs = qs.filter(payment_method=p["method"])
        if p.get("partner"):
            qs = qs.filter(partner_id=p["partner"])
        if p.get("customer"):
            qs = qs.filter(customer_id=p["customer"])
        if p.get("from"):
            qs = qs.filter(transaction_date__gte=p["from"])
        if p.get("to"):
            qs = qs.filter(transaction_date__lte=p["to"])
        search = (p.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(reference_number__icontains=search)
                | Q(description__icontains=search)
                | Q(receipt_no__icontains=search)
                | Q(customer__name__icontains=search)
            )
        order = p.get("order", "-transaction_date")
        if order in ("transaction_date", "-transaction_date",
                     "amount", "-amount", "-created_at"):
            qs = qs.order_by(order, "-created_at")
        return qs

    def create(self, request, *args, **kwargs):
        try:
            txn, _created = create_transaction(
                request,
                business=request.business,
                amount=request.data.get("amount"),
                transaction_type=request.data.get("transaction_type")
                or TransactionType.PAYMENT,
                transaction_date=request.data.get("transaction_date"),
                partner_id=request.data.get("partner"),
                customer_id=request.data.get("customer"),
                payment_method=request.data.get("payment_method", "CASH"),
                to_payment_method=request.data.get("to_payment_method"),
                adjustment_direction=request.data.get("adjustment_direction"),
                transaction_time=request.data.get("transaction_time"),
                description=request.data.get("description", ""),
                notes=request.data.get("notes", ""),
                reference_number=request.data.get("reference_number", ""),
                client_reference=request.data.get("client_reference", ""),
                allow_beyond_limit=bool(
                    request.data.get("allow_beyond_limit", False)),
            )
        except TransactionServiceError as exc:
            return _service_error(exc)
        payload = TransactionReadSerializer(
            txn, context={"request": request}).data
        # Phase 7: advisory duplicate detection (never blocks the post)
        from apps.transactions.automation import find_potential_duplicates
        twins = find_potential_duplicates(txn)
        if twins:
            payload["duplicate_warning"] = {
                "message": "Similar committed transactions found within "
                           "±1 day — review the list if this was not a "
                           "repeat entry.",
                "transaction_ids": [str(t.pk) for t in twins],
            }
        return Response(
            payload, status=status.HTTP_201_CREATED)


class TransactionDetailView(generics.RetrieveAPIView):
    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = TransactionReadSerializer
    lookup_url_kwarg = "txn_id"

    def get_permissions(self):
        return [BusinessAccessRequired()]

    def get_queryset(self):
        own = _scoped_member(self.request)
        qs = Transaction.objects.filter(business=self.request.business)
        if own is not None:
            qs = qs.filter(partner=own)
        return qs


class TransactionReversalView(APIView):
    """ADMIN-only (Section 18): soft void + audit; never a silent delete."""

    def post(self, request, txn_id):
        BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, self)
        txn = get_object_or_404(
            Transaction, pk=txn_id, business=request.business)
        try:
            reversed_txn = reverse_transaction(
                request, txn, reason=request.data.get("reason", ""))
        except TransactionServiceError as exc:
            return _service_error(exc)
        return Response(TransactionReadSerializer(reversed_txn).data)


class CollectionWorkflowView(APIView):
    """Fast due-collection endpoint (Section 9) → returns receipt-ready txn."""

    def post(self, request):
        BusinessAccessRequired().has_permission(request, self)
        if not request.data.get("partner"):
            raise serializers.ValidationError({"partner": "Required: choose the "
                                                      "partner whose due is being collected."})
        try:
            txn, _created = create_transaction(
                request,
                business=request.business,
                amount=request.data.get("amount"),
                transaction_type=TransactionType.PAYMENT,
                transaction_date=request.data.get("transaction_date"),
                partner_id=request.data.get("partner"),
                customer_id=request.data.get("customer"),
                payment_method=request.data.get("payment_method", "CASH"),
                transaction_time=request.data.get("transaction_time"),
                description=request.data.get("description",
                                             "Due collection"),
                client_reference=request.data.get("client_reference", ""),
            )
        except TransactionServiceError as exc:
            return _service_error(exc)
        receipt = ReceiptData(txn)
        return Response(
            {"transaction": TransactionReadSerializer(txn).data,
             "receipt": receipt},
            status=status.HTTP_201_CREATED)


def ReceiptData(txn):
    allocations = [
        {"credit_entry": str(a.credit_entry_id),
         "amount": a.amount,
         "credit_date": a.credit_entry.transaction_date,
         "credit_note": a.credit_entry.note}
        for a in PaymentAllocation.objects.filter(
            collection_transaction=txn)
    ]
    return {
        "receipt_no": txn.receipt_no,
        "issued_at": txn.created_at,
        "transaction_date": txn.transaction_date,
        "partner_id": str(txn.partner_id) if txn.partner_id else None,
        "partner_name": txn.partner.name if txn.partner_id else None,
        "customer_name": txn.customer.name if txn.customer_id else None,
        "amount": txn.amount,
        "payment_method": txn.payment_method,
        "description": txn.description,
        "reference_number": txn.reference_number,
        "allocations": allocations,
        "business": txn.business.name,
    }


class TransactionReceiptView(APIView):
    def get(self, request, txn_id):
        BusinessAccessRequired().has_permission(request, self)
        txn = get_object_or_404(
            Transaction, pk=txn_id, business=request.business,
            transaction_type=TransactionType.PAYMENT)
        return Response(ReceiptData(txn))


class DateMemoryView(APIView):
    """Last-used transaction date (Section 7): partner-specific dates with a
    business-wide default fallback; persisted server-side."""

    def get(self, request):
        BusinessAccessRequired().has_permission(request, self)
        partner_id = request.query_params.get("partner") or None
        row = TransactionDateMemory.resolve(request.business, partner=partner_id)
        if row is None:
            return Response({"partner": partner_id, "last_date": None,
                             "default": True})
        return Response(DateMemorySerializer(row).data)

    def post(self, request):
        BusinessAccessRequired().has_permission(request, self)
        date = request.data.get("date")
        if not date:
            raise serializers.ValidationError({"date": "Required (YYYY-MM-DD)."})
        partner = None
        partner_id = request.data.get("partner") or None
        if partner_id:
            from apps.partners.models import Partner
            partner = request.business.partners.filter(pk=partner_id).first()
            if partner is None:
                raise serializers.ValidationError({"partner": "Unknown partner."})
        row = TransactionDateMemory.touch(request.business, date, partner=partner)
        record_audit("update", row, business=request.business,
                     resource_type="TransactionDateMemory")
        return Response(DateMemorySerializer(row).data)

    def delete(self, request):
        """Reset a memory to default (Section 7 rule)."""
        BusinessAccessRequired().has_permission(request, self)
        partner_id = request.query_params.get("partner") or None
        row = TransactionDateMemory.get(request.business, partner=partner_id)
        if row is not None:
            row.delete()
        return Response({"detail": "Reset to default."})


class DashboardSummaryView(APIView):
    """Phase 3 dashboard feed: per-type totals, method split, daily series,
    dues snapshot — all date/partner filterable."""

    def get(self, request):
        BusinessAccessRequired().has_permission(request, self)
        p = request.query_params
        qs = Transaction.objects.filter(
            business=request.business,
            status=TransactionStatus.COMPLETED)
        if p.get("from"):
            qs = qs.filter(transaction_date__gte=p["from"])
        if p.get("to"):
            qs = qs.filter(transaction_date__lte=p["to"])
        if p.get("partner"):
            qs = qs.filter(partner_id=p["partner"])

        by_type = {row["transaction_type"]: row["total"] for row in
                   qs.values("transaction_type").annotate(total=Sum("amount"))}
        by_method = {row["payment_method"]: row["total"] for row in
                     qs.exclude(transaction_type=TransactionType.TRANSFER)
                     .values("payment_method").annotate(total=Sum("amount"))}
        daily = [
            {"date": row["transaction_date"], "amount": row["total"]}
            for row in qs.values("transaction_date").annotate(total=Sum("amount"))
            .order_by("transaction_date")
        ]

        own = _scoped_member(request)
        assignments = (
            CustomerAssignment.objects.filter(
                customer__business=request.business, is_active=True)
            .select_related("customer", "partner")
        )
        if own is not None:
            assignments = assignments.filter(partner=own)
        elif p.get("partner"):
            assignments = assignments.filter(partner_id=p["partner"])
        receivable = sum(
            (a.current_due() for a in assignments), Decimal("0.00"))

        recent = qs.order_by("-transaction_date", "-created_at")[:10]

        return Response({
            "date_range": {"from": p.get("from"), "to": p.get("to")},
            "totals_by_type": by_type,
            "totals_by_method": by_method,
            "daily_series": daily,
            "total_receivables": receivable,
            "recent_transactions": TransactionReadSerializer(recent, many=True).data,
        })


# --------------------------------------------------------------------------
# Phase 7: recurring rules, duplicate review, business alerts
# --------------------------------------------------------------------------

from datetime import date as _date

from apps.transactions.automation import (
    AutomationError, run_due_recurring, run_recurring, scan_duplicates,
)
from apps.transactions.models import RecurringTransaction


class RecurringListCreateView(APIView):
    """ADMIN-only auto-draft rules (Section 7). POST goes through the
    standard 403 path for partners — server-enforced again."""

    def get(self, request):
        BusinessAccessRequired().has_permission(request, self)
        BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, self)
        qs = request.business.recurring_rules.filter()
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        rows = [{
            "id": str(r.pk), "title": r.title,
            "transaction_type": r.transaction_type,
            "amount": str(r.amount), "payment_method": r.payment_method,
            "frequency": r.frequency, "interval": r.interval,
            "start_date": r.start_date, "end_date": r.end_date,
            "next_date": r.next_date, "times_run": r.times_run,
            "max_runs": r.max_runs, "status": r.status,
            "partner_id": str(r.partner_id) if r.partner_id else None,
            "customer_id": str(r.customer_id) if r.customer_id else None,
        } for r in qs]
        return Response({"count": len(rows), "rules": rows})

    def post(self, request):
        BusinessAccessRequired().has_permission(request, self)
        BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, self)
        d = request.data
        ttype = d.get("transaction_type")
        if ttype not in TransactionType.values:
            raise serializers.ValidationError(
                {"transaction_type": "A valid transaction type is required."})
        amount = d.get("amount")
        try:
            amount = Decimal(str(amount)).quantize(Decimal("0.01"))
        except (TypeError, ValueError):
            raise serializers.ValidationError(
                {"amount": "A valid decimal amount is required."})
        if amount <= 0:
            raise serializers.ValidationError(
                {"amount": "Must be greater than zero."})
        frequency = d.get("frequency")
        if frequency not in RecurringTransaction.Frequency.values:
            raise serializers.ValidationError(
                {"frequency": "DAILY/WEEKLY/MONTHLY required."})
        try:
            interval = max(1, int(d.get("interval", 1) or 1))
        except (TypeError, ValueError):
            raise serializers.ValidationError(
                {"interval": "Must be an integer >= 1."})
        try:
            start = _date.fromisoformat(str(d.get("start_date")))
        except (TypeError, ValueError):
            raise serializers.ValidationError(
                {"start_date": "A valid ISO date is required."})
        end = None
        if d.get("end_date"):
            try:
                end = _date.fromisoformat(str(d["end_date"]))
            except (TypeError, ValueError):
                raise serializers.ValidationError(
                    {"end_date": "A valid ISO date is required."})
            if end < start:
                raise serializers.ValidationError(
                    {"end_date": "Cannot be before start_date."})
        max_runs = d.get("max_runs")
        if max_runs is not None and max_runs != "":
            try:
                max_runs = max(0, int(max_runs))
            except (TypeError, ValueError):
                raise serializers.ValidationError(
                    {"max_runs": "Must be an integer."})

        partner = customer = None
        if d.get("partner"):
            partner = request.business.partners.filter(
                pk=d["partner"]).first()
            if partner is None:
                raise serializers.ValidationError({"partner": "Unknown."})
        if d.get("customer"):
            customer = request.business.customers.filter(
                pk=d["customer"]).first()
            if customer is None:
                raise serializers.ValidationError({"customer": "Unknown."})
        if ttype in CUSTOMER_REQUIRED_TYPES and customer is None:
            raise serializers.ValidationError(
                {"customer": f"Type {ttype} requires a customer."})
        if ttype == TransactionType.TRANSFER and not d.get("to_payment_method"):
            raise serializers.ValidationError(
                {"to_payment_method": "Transfers need a destination method."})

        rule = RecurringTransaction.objects.create(
            business=request.business,
            title=(d.get("title") or "").strip() or f"Recurring {ttype}",
            transaction_type=ttype, amount=amount,
            payment_method=d.get("payment_method") or PaymentMethod.CASH,
            to_payment_method=d.get("to_payment_method"),
            adjustment_direction=d.get("adjustment_direction"),
            partner=partner, customer=customer,
            description=d.get("description", ""),
            frequency=frequency, interval=interval,
            start_date=start, end_date=end, max_runs=max_runs,
            client_reference_prefix=(d.get("client_reference_prefix")
                                     or "").strip(),
            next_date=start, status=RecurringTransaction.Status.ACTIVE,
            created_by=request.user,
        )
        record_audit("create", rule, business=request.business,
                     resource_type="RecurringTransaction")
        return Response({"id": str(rule.pk), "title": rule.title,
                         "next_date": str(rule.next_date)}, status=201)


class RecurringDetailView(APIView):
    def patch(self, request, rule_id):
        BusinessAccessRequired().has_permission(request, self)
        BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, self)
        rule = get_object_or_404(request.business.recurring_rules,
                                 pk=rule_id)
        changed = []
        d = request.data
        if "status" in d:
            if d["status"] not in RecurringTransaction.Status.values:
                raise serializers.ValidationError(
                    {"status": "ACTIVE or PAUSED."})
            rule.status = d["status"]
            changed.append("status")
        if "amount" in d:
            try:
                rule.amount = Decimal(str(d["amount"])).quantize(
                    Decimal("0.01"))
                changed.append("amount")
            except (TypeError, ValueError):
                raise serializers.ValidationError(
                    {"amount": "A valid decimal amount is required."})
        if "next_date" in d and d["next_date"]:
            try:
                rule.next_date = _date.fromisoformat(str(d["next_date"]))
                changed.append("next_date")
            except (TypeError, ValueError):
                raise serializers.ValidationError(
                    {"next_date": "A valid ISO date is required."})
        if "frequency" in d:
            if d["frequency"] not in RecurringTransaction.Frequency.values:
                raise serializers.ValidationError(
                    {"frequency": "DAILY/WEEKLY/MONTHLY required."})
            rule.frequency = d["frequency"]
            changed.append("frequency")
        rule.save(update_fields=changed)
        record_audit("edit", rule, business=request.business,
                     changes={c: str(getattr(rule, c)) for c in changed},
                     resource_type="RecurringTransaction")
        return Response({"id": str(rule.pk), "status": rule.status,
                         "next_date": str(rule.next_date),
                         "amount": str(rule.amount)})

    def delete(self, request, rule_id):
        """Soft delete a rule (uses SoftDeleteUUIDModel; history kept)."""
        BusinessAccessRequired().has_permission(request, self)
        BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, self)
        rule = get_object_or_404(request.business.recurring_rules,
                                 pk=rule_id)
        rule.delete()
        record_audit("delete", rule, business=request.business,
                     resource_type="RecurringTransaction",
                     reason="Rule removed")
        return Response({"detail": "Rule removed (history retained)."})


class RecurringRunView(APIView):
    """POST: run one rule now (ADMIN). Everything posts via the single
    writer with per-due-date idempotency — running twice is harmless."""

    def post(self, request, rule_id):
        BusinessAccessRequired().has_permission(request, self)
        BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, self)
        rule = get_object_or_404(request.business.recurring_rules,
                                 pk=rule_id)
        upto = request.data.get("upto")
        try:
            upto_d = _date.fromisoformat(str(upto)) if upto else None
        except (TypeError, ValueError):
            raise serializers.ValidationError({"upto": "Invalid ISO date."})
        try:
            if upto_d is not None:
                outcome = run_recurring(request, rule, upto=upto_d)
            else:
                outcome = run_recurring(request, rule)
        except AutomationError as exc:
            return Response(
                exc.errors or {"detail": str(exc)}, status=exc.status_code)
        return Response(outcome)


class RecurringRunDueView(APIView):
    """POST: process every due rule of the business (ADMIN / scheduler)."""

    def post(self, request):
        BusinessAccessRequired().has_permission(request, self)
        BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, self)
        return Response(run_due_recurring(request))


class DuplicatesReviewView(APIView):
    def get(self, request):
        BusinessAccessRequired().has_permission(request, self)
        return Response({"groups": scan_duplicates(request.business)})


class BusinessAlertsView(APIView):
    """Recurring due today/overdue — one feed for the UI."""

    def get(self, request):
        BusinessAccessRequired().has_permission(request, self)
        today = timezone.localdate()
        due_rules = request.business.recurring_rules.filter(
            status=RecurringTransaction.Status.ACTIVE,
            next_date__lte=today)
        recurring_alerts = [{
            "kind": "RECURRING_DUE",
            "rule_id": str(r.pk), "title": r.title,
            "amount": str(r.amount), "next_date": r.next_date,
            "overdue": r.next_date < today,
        } for r in due_rules]
        return Response({
            "date": str(today),
            "recurring_due": recurring_alerts,
            "low_stock": [],
        })
