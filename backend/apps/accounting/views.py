"""Accounting API (Phase 5).

Role policy (server-enforced): the double-entry books are the business
owner's view — chart-of-accounts writes, manual journals, reversals and all
reports are ADMIN-only. Account *listing* is readable by every member.
"""
from django.shortcuts import get_object_or_404
from rest_framework import permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounting.models import Account, AccountType
from apps.accounting.postings import ensure_system_accounts
from apps.accounting.reports import (
    account_ledger, balance_sheet, cash_flow, income_statement,
    trial_balance,
)
from apps.accounting.serializers import (
    AccountSerializer, JournalEntrySerializer,
)
from apps.accounting.service import (
    AccountingError, create_journal_entry, reverse_journal_entry,
)
from apps.core.permissions import (
    BusinessAccessRequired, BusinessRoleRequired,
)


def _role_guard(request, admin_only):
    if admin_only:
        BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, None)


def _service_error(exc: AccountingError):
    payload = exc.errors if exc.errors else {"detail": str(exc)}
    return Response(payload, status=exc.status_code)


def _as_date(value):
    from datetime import date, datetime
    if not value:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


class AccountListCreateView(APIView):
    def get_permissions(self):
        return [permissions.IsAuthenticated()]

    def get(self, request):
        BusinessAccessRequired().has_permission(request, self)
        ensure_system_accounts(request.business, user=request.user)
        qs = request.business.accounts.all().order_by("code")
        if request.query_params.get("type"):
            qs = qs.filter(type=request.query_params["type"])
        if request.query_params.get("active") == "1":
            qs = qs.filter(is_active=True)
        ser = AccountSerializer(qs, many=True)
        return Response({"count": len(ser.data), "accounts": ser.data})

    def post(self, request):
        has = BusinessAccessRequired().has_permission(request, self)
        if has:
            BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, self)
        code = (request.data.get("code") or "").strip()
        name = (request.data.get("name") or "").strip()
        atype = (request.data.get("type") or "").strip()
        if not name:
            raise serializers.ValidationError({"name": "Required."})
        if atype not in AccountType.values:
            raise serializers.ValidationError(
                {"type": "One of ASSET/LIABILITY/EQUITY/INCOME/EXPENSE."})
        if code and request.business.accounts.filter(code=code).exists():
            raise serializers.ValidationError(
                {"code": "This code is already used."})
        parent = None
        if request.data.get("parent"):
            parent = request.business.accounts.filter(
                pk=request.data["parent"]).first()
            if parent is None:
                raise serializers.ValidationError(
                    {"parent": "Unknown account."})
        account = Account.objects.create(
            business=request.business,
            code=code or "", name=name, type=atype, parent=parent,
            is_system=False, is_active=True, created_by=request.user,
        )
        return Response(AccountSerializer(account).data, status=201)


class AccountLedgerView(APIView):
    def get(self, request, account_id):
        BusinessAccessRequired().has_permission(request, self)
        account = get_object_or_404(request.business.accounts,
                                    pk=account_id)
        try:
            report = account_ledger(
                request.business, account,
                from_date=_as_date(request.query_params.get("from")),
                to_date=_as_date(request.query_params.get("to")),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(report)


class JournalEntryListCreateView(APIView):
    def get_permissions(self):
        return [permissions.IsAuthenticated()]

    def get(self, request):
        BusinessAccessRequired().has_permission(request, self)
        qs = request.business.journal_entries.all().select_related("created_by")
        p = request.query_params
        if p.get("from"):
            qs = qs.filter(transaction_date__gte=p["from"])
        if p.get("to"):
            qs = qs.filter(transaction_date__lte=p["to"])
        if p.get("source_kind"):
            qs = qs.filter(source_kind=p["source_kind"])
        ser = JournalEntrySerializer(
            qs.order_by("-transaction_date", "-created_at")[:100], many=True)
        return Response({"count": len(ser.data), "entries": ser.data})

    def post(self, request):
        BusinessAccessRequired().has_permission(request, self)
        BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, self)
        lines = request.data.get("lines") or []
        try:
            entry = create_journal_entry(
                request.business,
                transaction_date=_as_date(request.data.get("transaction_date")),
                lines=lines, memo=request.data.get("memo", ""),
                source_kind="MANUAL",
                client_reference=request.data.get("client_reference", ""),
                user=request.user,
            )
        except AccountingError as exc:
            return _service_error(exc)
        return Response(JournalEntrySerializer(entry).data, status=201)


class JournalEntryDetailView(APIView):
    def get_permissions(self):
        return [permissions.IsAuthenticated()]

    def get(self, request, entry_id):
        BusinessAccessRequired().has_permission(request, self)
        entry = get_object_or_404(request.business.journal_entries,
                                  pk=entry_id)
        return Response(JournalEntrySerializer(entry).data)


class JournalEntryReversalView(APIView):
    def post(self, request, entry_id):
        BusinessAccessRequired().has_permission(request, self)
        BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, self)
        entry = get_object_or_404(request.business.journal_entries,
                                  pk=entry_id)
        try:
            mirror = reverse_journal_entry(
                request.business, entry,
                reason=request.data.get("reason", ""), user=request.user,
            )
        except AccountingError as exc:
            return _service_error(exc)
        return Response(JournalEntrySerializer(mirror).data)


class TrialBalanceView(APIView):
    def get(self, request):
        BusinessAccessRequired().has_permission(request, self)
        BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, self)
        return Response(trial_balance(
            request.business,
            from_date=_as_date(request.query_params.get("from")),
            to_date=_as_date(request.query_params.get("to")),
        ))


class IncomeStatementView(APIView):
    def get(self, request):
        BusinessAccessRequired().has_permission(request, self)
        BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, self)
        return Response(income_statement(
            request.business,
            from_date=_as_date(request.query_params.get("from")),
            to_date=_as_date(request.query_params.get("to")),
        ))


class BalanceSheetView(APIView):
    def get(self, request):
        BusinessAccessRequired().has_permission(request, self)
        BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, self)
        return Response(balance_sheet(
            request.business,
            as_of=_as_date(request.query_params.get("as_of")),
        ))


class CashFlowView(APIView):
    def get(self, request):
        BusinessAccessRequired().has_permission(request, self)
        BusinessRoleRequired(roles=("ADMIN",)).has_permission(request, self)
        return Response(cash_flow(
            request.business,
            from_date=_as_date(request.query_params.get("from")),
            to_date=_as_date(request.query_params.get("to")),
        ))
