"""Cashbook / position / super-calculator API (additive).

The web UI computes these figures with `apps.cashbook.services` and
`apps.web.supercalc`; this module exposes the very same functions over JSON
so the mobile app can show them.

It is purely ADDITIVE: no web view, template, model or data changes, and
the existing pages behave exactly as before.
"""
from datetime import date, datetime, timedelta

from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.cashbook.models import CashCount, StockSnapshot
from apps.cashbook.services import (
    business_position, cash_balance, drawer_cash, integrity_check,
    last_count, position_rows, profit_breakdown, real_cash_daily,
    stock_value, total_due, wallet_balance,
)
from apps.core.permissions import BusinessAccessRequired, BusinessRoleRequired
from apps.web.supercalc import (
    calc_day_rows, calc_month_rows, calc_totals, compare_calc,
)


def _money(value):
    return f"{value:.2f}"


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _preset_range(preset, start, end, today):
    """The same presets the web calculator offers."""
    if preset == "today":
        return today, today
    if preset == "week":
        return today - timedelta(days=today.weekday()), today
    if preset == "last_month":
        first_this = today.replace(day=1)
        last_end = first_this - timedelta(days=1)
        return last_end.replace(day=1), last_end
    if preset == "year":
        return today.replace(month=1, day=1), today.replace(month=12, day=31)
    return start, end


class CashbookOverviewView(APIView):
    """Everything the cashbook / calculator screen needs, in one call."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        if not BusinessAccessRequired().has_permission(request, self):
            return Response({"detail": "Business access required."},
                            status=403)

        business = request.business
        today = timezone.localdate()
        p = request.query_params
        start = _parse_date(p.get("from"))
        end = _parse_date(p.get("to"))
        preset = (p.get("preset") or "month").strip()
        start, end = _preset_range(preset, start, end, today)
        if not start or not end:
            start, end = today.replace(day=1), today
        if start > end:
            start, end = end, start

        totals = calc_totals(business, start, end)
        position = business_position(business, end)
        breakdown = profit_breakdown(business, start, end)
        drawer = drawer_cash(business, end)
        count = last_count(business)

        return Response({
            "preset": preset,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "totals": {
                "sell_cash": _money(totals["sell_cash"]),
                "sell_due": _money(totals["sell_due"]),
                "sell_total": _money(totals["sell_total"]),
                "khoroch": _money(totals["khoroch"]),
                "collection": _money(totals["collection"]),
                "profit": _money(totals["profit"]),
                "real_profit": _money(totals["real_profit"]),
                "goods": _money(totals["goods"]),
                "operating": _money(totals["operating"]),
                "stock_delta": _money(totals["stock_delta"]),
                "cogs": _money(totals["cogs"]),
            },
            "position": {k: (_money(v) if isinstance(v, (int, float, Decimal))
                              else v)
                         for k, v in position.items()
                         if not isinstance(v, dict)},
            "drawer_cash": {
                "amount": _money(drawer.get("amount", 0)),
                **{k: (_money(v) if isinstance(v, (int, float, Decimal)) else v)
                   for k, v in drawer.items()
                   if k != "amount" and not isinstance(v, dict)},
            },
            "cash_balance": _money(cash_balance(business, end)),
            "wallet_balance": _money(wallet_balance(business, end)),
            "stock_value": _money(stock_value(business, end)),
            "total_due": _money(total_due(business)),
            "profit_breakdown": {
                k: (_money(v) if isinstance(v, (int, float, Decimal)) else v)
                for k, v in breakdown.items() if not isinstance(v, (dict, list))
            },
            "last_count": ({
                "id": str(count.pk),
                "counted_on": count.counted_on.isoformat(),
                "kind": count.kind,
                "counted_amount": _money(count.counted_amount),
                "book_amount": _money(count.book_amount),
                "difference": _money(count.difference),
                "note": count.note,
            } if count else None),
            "integrity": {k: v for k, v in integrity_check(business).items()
                          if not isinstance(v, (dict, list))},
        })


class CashbookDayRowsView(APIView):
    """The day-by-day rows of the calculator."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        if not BusinessAccessRequired().has_permission(request, self):
            return Response({"detail": "Business access required."},
                            status=403)

        business = request.business
        today = timezone.localdate()
        p = request.query_parameters if hasattr(request, "query_parameters") \
            else request.query_params
        start = _parse_date(p.get("from"))
        end = _parse_date(p.get("to"))
        start, end = _preset_range((p.get("preset") or "month").strip(),
                                   start, end, today)
        if not start or not end:
            start, end = today.replace(day=1), today

        rows = calc_day_rows(business, start, end)
        return Response({
            "rows": [{
                "date": r.get("date").isoformat() if hasattr(r.get("date"), "isoformat")
                        else str(r.get("date")),
                **{k: (_money(v) if isinstance(v, (int, float, Decimal)) else v)
                   for k, v in r.items() if k != "date"},
            } for r in rows],
        })


class CashbookMonthsView(APIView):
    """One row per month of a year: sell, cost, profit."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        if not BusinessAccessRequired().has_permission(request, self):
            return Response({"detail": "Business access required."},
                            status=403)

        business = request.business
        today = timezone.localdate()
        year = int(request.query_params.get("year") or today.year)
        rows = calc_month_rows(business, year)
        return Response({
            "year": year,
            "rows": [{k: (_money(v) if isinstance(v, (int, float, Decimal)) else v)
                      for k, v in r.items()} for r in rows],
        })


class CashbookCompareView(APIView):
    """This period vs the previous one."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        if not BusinessAccessRequired().has_permission(request, self):
            return Response({"detail": "Business access required."},
                            status=403)

        business = request.business
        today = timezone.localdate()
        p = request.query_params
        start = _parse_date(p.get("from"))
        end = _parse_date(p.get("to"))
        start, end = _preset_range((p.get("preset") or "month").strip(),
                                   start, end, today)
        if not start or not end:
            start, end = today.replace(day=1), today
        data = compare_calc(business, start, end)
        return Response({k: (_money(v) if isinstance(v, (int, float, Decimal)) else v)
                         for k, v in data.items() if not isinstance(v, (dict, list))})


class CashbookPositionRowsView(APIView):
    """Partner-wise position rows."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        if not BusinessAccessRequired().has_permission(request, self):
            return Response({"detail": "Business access required."},
                            status=403)

        business = request.business
        today = timezone.localdate()
        p = request.query_params
        start = _parse_date(p.get("from")) or today.replace(day=1)
        end = _parse_date(p.get("to")) or today
        rows = position_rows(business, start, end)
        return Response({"rows": rows})


class CashCountListCreateView(APIView):
    """Cash counts (গোনা হিসাব)."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        if not BusinessAccessRequired().has_permission(request, self):
            return Response({"detail": "Business access required."},
                            status=403)
        rows = CashCount.objects.filter(business=request.business).order_by(
            "-counted_on")[:100]
        return Response({"counts": [{
            "id": str(r.pk),
            "counted_on": r.counted_on.isoformat(),
            "kind": r.kind,
            "counted_amount": _money(r.counted_amount),
            "book_amount": _money(r.book_amount),
            "difference": _money(r.difference),
            "note": r.note,
        } for r in rows]})

    def post(self, request):
        if not BusinessAccessRequired().has_permission(request, self):
            return Response({"detail": "Business access required."},
                            status=403)
        if not BusinessRoleRequired(roles=("ADMIN",)).has_permission(
                request, self):
            return Response({"detail": "Only ADMIN can record a count."},
                            status=403)

        business = request.business
        counted_on = _parse_date(request.data.get("counted_on")) \
            or timezone.localdate()
        kind = (request.data.get("kind") or CashCount.Kind.COUNT).upper()
        try:
            counted_amount = request.data.get("counted_amount")
            book_amount = request.data.get("book_amount")
            counted = _money_amount(counted_amount)
            book = _money_amount(book_amount)
        except (TypeError, ValueError):
            return Response({"detail": "Invalid amount."}, status=400)

        difference = counted - book
        row = CashCount.objects.create(
            business=business, counted_on=counted_on, kind=kind,
            counted_amount=counted, book_amount=book,
            difference=difference,
            note=(request.data.get("note") or "")[:160],
            counted_by=request.user,
        )
        return Response({
            "id": str(row.pk),
            "counted_on": row.counted_on.isoformat(),
            "kind": row.kind,
            "counted_amount": _money(row.counted_amount),
            "book_amount": _money(row.book_amount),
            "difference": _money(row.difference),
        }, status=201)


class StockSnapshotListCreateView(APIView):
    """Stock snapshots (স্টকের হিসাব)."""

    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        if not BusinessAccessRequired().has_permission(request, self):
            return Response({"detail": "Business access required."},
                            status=403)
        rows = StockSnapshot.objects.filter(
            business=request.business).order_by("-snapshot_on")[:100]
        return Response({"snapshots": [{
            "id": str(r.pk),
            "snapshot_on": r.snapshot_on.isoformat(),
            "kind": r.kind,
            "closing_value": _money(r.closing_value),
            "note": r.note,
        } for r in rows]})

    def post(self, request):
        if not BusinessAccessRequired().has_permission(request, self):
            return Response({"detail": "Business access required."},
                            status=403)
        if not BusinessRoleRequired(roles=("ADMIN",)).has_permission(
                request, self):
            return Response({"detail": "Only ADMIN can record a snapshot."},
                            status=403)

        business = request.business
        snapshot_on = _parse_date(request.data.get("snapshot_on")) \
            or timezone.localdate()
        kind = (request.data.get("kind")
                or StockSnapshot.Kind.CLOSING).upper()
        try:
            value = _money_amount(request.data.get("closing_value"))
        except (TypeError, ValueError):
            return Response({"detail": "Invalid amount."}, status=400)

        row = StockSnapshot.objects.create(
            business=business, snapshot_on=snapshot_on, kind=kind,
            closing_value=value,
            note=(request.data.get("note") or "")[:160],
            created_by=request.user,
        )
        return Response({
            "id": str(row.pk),
            "snapshot_on": row.snapshot_on.isoformat(),
            "kind": row.kind,
            "closing_value": _money(row.closing_value),
        }, status=201)


def _money_amount(value):
    from decimal import Decimal, InvalidOperation
    try:
        return Decimal(str(value if value is not None else "0")).quantize(
            Decimal("0.01"))
    except (InvalidOperation, ArithmeticError):
        raise ValueError("bad amount")


from decimal import Decimal  # noqa: E402  (used by the isinstance checks)
