"""Loan / Investment book API (additive).

The web UI (`web:loans_view`) has always been the only way to reach this
book. This module exposes the very same service functions over JSON so the
mobile app can use them.

It is purely ADDITIVE: no web view, template, model or data changes, and
the existing pages behave exactly as before. The same role rules apply
(ADMIN may add parties/entries, members may read).
"""
from datetime import date
from types import SimpleNamespace

from django.shortcuts import get_object_or_404
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import BusinessAccessRequired, BusinessRoleRequired
from apps.loans.models import LoanEntry, LoanParty
from apps.loans.service import (
    create_entry, create_party, party_rows, reverse_entry, summarize,
)
from apps.transactions.services import TransactionServiceError


def _actor(request):
    """The service layer wants an actor with user/business/membership."""
    return SimpleNamespace(user=request.user, business=request.business,
                           business_membership=request.business_membership)


def _money(value):
    return f"{value:.2f}"


def _date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


class LoanBookView(APIView):
    """One book (LOAN or INVESTMENT): summary, parties and entries."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        if not BusinessAccessRequired().has_permission(request, self):
            return Response({"detail": "Business access required."},
                            status=403)

        kind = (request.query_params.get("kind")
                or LoanParty.Kind.LOAN).upper()
        if kind not in (LoanParty.Kind.LOAN, LoanParty.Kind.INVESTMENT):
            return Response({"detail": "kind must be LOAN or INVESTMENT."},
                            status=400)

        from_date = request.query_params.get("from") or None
        to_date = request.query_params.get("to") or None

        summary = summarize(request.business, kind=kind,
                             from_date=from_date, to_date=to_date)
        rows = party_rows(request.business, kind=kind,
                          from_date=from_date, to_date=to_date)

        entries_qs = LoanEntry.objects.filter(
            business=request.business, party__kind=kind)
        if from_date:
            entries_qs = entries_qs.filter(transaction_date__gte=from_date)
        if to_date:
            entries_qs = entries_qs.filter(transaction_date__lte=to_date)
        entries = entries_qs.order_by(
            "-transaction_date", "-created_at")[:300]

        return Response({
            "kind": kind,
            "summary": {
                "total_in": _money(summary["total_in"]),
                "total_out": _money(summary["total_out"]),
                "outstanding": _money(summary["outstanding"]),
                "net_main_account": _money(summary["net_main_account"]),
                "entry_count": summary.get("entry_count", 0),
            },
            "parties": [{
                "id": str(row["party"].pk),
                "name": row["party"].name,
                "phone": row["party"].phone,
                "kind": row["party"].kind,
                "received": _money(row["received"]),
                "returned": _money(row["returned"]),
                "outstanding": _money(row["outstanding"]),
                "entry_count": row["entry_count"],
            } for row in rows],
            "entries": [{
                "id": str(entry.pk),
                "party": str(entry.party_id),
                "party_name": entry.party.name,
                "kind": entry.kind,
                "amount": _money(entry.amount),
                "transaction_date": _date(entry.transaction_date),
                "description": entry.description,
                "notes": entry.notes,
                "status": entry.status,
            } for entry in entries],
        })


class LoanPartyCreateView(APIView):
    """Add a lender / investor."""

    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        if not BusinessAccessRequired().has_permission(request, self):
            return Response({"detail": "Business access required."},
                            status=403)
        if not BusinessRoleRequired(roles=("ADMIN",)).has_permission(
                request, self):
            return Response({"detail": "Only ADMIN can add a party."},
                            status=403)

        kind = (request.data.get("kind")
                or LoanParty.Kind.LOAN).upper()
        if kind not in (LoanParty.Kind.LOAN, LoanParty.Kind.INVESTMENT):
            return Response({"detail": "kind must be LOAN or INVESTMENT."},
                            status=400)

        try:
            party = create_party(
                _actor(request), business=request.business,
                name=request.data.get("name"),
                kind=kind,
                phone=request.data.get("phone") or "",
                email=request.data.get("email") or "",
                notes=request.data.get("notes") or "",
            )
        except TransactionServiceError as exc:
            return Response(exc.errors or {"detail": str(exc)}, status=400)

        return Response({
            "id": str(party.pk),
            "name": party.name,
            "phone": party.phone,
            "kind": party.kind,
        }, status=201)


class LoanEntryCreateView(APIView):
    """Post one loan / investment movement."""

    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        if not BusinessAccessRequired().has_permission(request, self):
            return Response({"detail": "Business access required."},
                            status=403)
        if not BusinessRoleRequired(roles=("ADMIN",)).has_permission(
                request, self):
            return Response({"detail": "Only ADMIN can add an entry."},
                            status=403)

        party = get_object_or_404(
            LoanParty, pk=request.data.get("party"),
            business=request.business)
        try:
            entry = create_entry(
                _actor(request), business=request.business, party=party,
                kind=request.data.get("kind") or LoanEntry.Kind.RECEIVED,
                amount=request.data.get("amount"),
                transaction_date=request.data.get("transaction_date"),
                description=request.data.get("description") or "",
                notes=request.data.get("notes") or "",
            )
        except TransactionServiceError as exc:
            return Response(exc.errors or {"detail": str(exc)}, status=400)

        return Response({
            "id": str(entry.pk),
            "party": str(entry.party_id),
            "kind": entry.kind,
            "amount": _money(entry.amount),
            "transaction_date": _date(entry.transaction_date),
            "status": entry.status,
        }, status=201)


class LoanEntryReverseView(APIView):
    """Reverse (void) one posted entry."""

    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, entry_id):
        if not BusinessAccessRequired().has_permission(request, self):
            return Response({"detail": "Business access required."},
                            status=403)
        if not BusinessRoleRequired(roles=("ADMIN",)).has_permission(
                request, self):
            return Response({"detail": "Only ADMIN can reverse an entry."},
                            status=403)

        entry = get_object_or_404(LoanEntry, pk=entry_id,
                                  business=request.business)
        try:
            reverse_entry(_actor(request), entry,
                          reason=request.data.get("reason") or "")
        except TransactionServiceError as exc:
            return Response(exc.errors or {"detail": str(exc)}, status=400)
        return Response({"status": "REVERSED", "id": str(entry.pk)})
