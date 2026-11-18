"""সুপার ক্যালকুলেটর — the shop's profit engine (meat-shop P&L).

    মোট সেল   = নগদে সেল (INCOME) + বাকিতে সেল (CREDIT)
    মোট খরচ  = খরচ / ক্রয় বই (EXPENSE)
    লাভ / লস  = মোট সেল − মোট খরচ

An old sale's collection (a PAYMENT in the বাকির বই) is never counted as
sell — it is money handed over for a *previous* sale, so exposing it only
as context keeps the toplines from ever double-counting.
Everything is computed on ``transaction_date`` with exact Decimals, and
reversed rows are excluded — the same ledger rules the books follow.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from apps.transactions.models import Transaction, TransactionStatus, TransactionType

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def _q(value) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(CENT)
    except Exception:
        return ZERO


def _days(start: date, end: date) -> list[date]:
    out: list[date] = []
    current = start
    while current <= end:
        out.append(current)
        current = date.fromordinal(current.toordinal() + 1)
    return out


def _buckets(business, start: date, end: date) -> dict[date, dict]:
    """{(day): {'sell_cash','sell_due','khoroch'}} from completed rows."""
    rows = (Transaction.objects
            .filter(business=business, status=TransactionStatus.COMPLETED,
                    transaction_date__gte=start, transaction_date__lte=end)
            .values("transaction_date", "transaction_type")
            .annotate(total=Sum("amount")))
    out: dict[date, dict] = defaultdict(
        lambda: {"sell_cash": ZERO, "sell_due": ZERO, "khoroch": ZERO})
    for row in rows:
        ttype = row["transaction_type"]
        if ttype == TransactionType.INCOME:
            key = "sell_cash"
        elif ttype == TransactionType.CREDIT:
            key = "sell_due"
        elif ttype == TransactionType.EXPENSE:
            key = "khoroch"
        else:
            continue
        out[row["transaction_date"]][key] += _q(row["total"])
    return out


def _collections_by_day(business, start: date, end: date) -> dict[date, Decimal]:
    """বাকি আদায় (PAYMENT) per day — context only, never part of sell."""
    rows = (Transaction.objects
            .filter(business=business, status=TransactionStatus.COMPLETED,
                    transaction_date__gte=start, transaction_date__lte=end,
                    transaction_type=TransactionType.PAYMENT)
            .values("transaction_date")
            .annotate(total=Sum("amount")))
    return {r["transaction_date"]: _q(r["total"]) for r in rows}


def calc_day_rows(business, start: date, end: date) -> list[dict]:
    """One row per day: সেল (নগদ+বাকি), খরচ, লাভ/লস and running profit."""
    buckets = _buckets(business, start, end)
    collections = _collections_by_day(business, start, end)

    rows: list[dict] = []
    running = ZERO
    for day in _days(start, end):
        b = buckets.get(day, {})
        sell_cash = b.get("sell_cash", ZERO)
        sell_due = b.get("sell_due", ZERO)
        khoroch = b.get("khoroch", ZERO)
        collection = collections.get(day, ZERO)
        sell_total = (sell_cash + sell_due).quantize(CENT)
        profit = (sell_total - khoroch).quantize(CENT)
        running = (running + profit).quantize(CENT)
        rows.append({
            "date": day,
            "sell_cash": sell_cash,
            "sell_due": sell_due,
            "sell_total": sell_total,
            "khoroch": khoroch,
            "profit": profit,
            "running_profit": running,
            "collection": collection,
            "has_data": bool(sell_cash or sell_due or khoroch or collection),
        })
    return rows


def calc_totals(business, start: date, end: date) -> dict:
    """Period totals: মোট সেল, মোট খরচ, লাভ/লস (+ the collection context).

    ``profit`` stays the cash-basis number the day rows show (sell −
    khoroch). ``real_profit`` is the honest one: unsold stock is not a
    loss, so the goods still on the shelf are taken out of the cost.
    """
    totals = {"sell_cash": ZERO, "sell_due": ZERO,
              "khoroch": ZERO, "collection": ZERO}
    for row in calc_day_rows(business, start, end):
        for key in ("sell_cash", "sell_due", "khoroch", "collection"):
            totals[key] = (totals[key] + row[key]).quantize(CENT)
    totals["sell_total"] = (totals["sell_cash"]
                            + totals["sell_due"]).quantize(CENT)
    totals["profit"] = (totals["sell_total"]
                        - totals["khoroch"]).quantize(CENT)
    # the stock-aware profit (same engine as the summary page)
    from apps.cashbook.services import profit_breakdown
    breakdown = profit_breakdown(business, start, end)
    totals["goods"] = breakdown["goods_bought"]
    totals["operating"] = breakdown["operating"]
    totals["stock_delta"] = breakdown["stock_delta"]
    totals["stock_complete"] = breakdown["stock_complete"]
    totals["cogs"] = breakdown["cogs"]
    totals["real_profit"] = breakdown["profit"]
    return totals


def calc_month_rows(business, year: int) -> list[dict]:
    """One row per month of `year`: sell, cost and the profit.

    The month totals are the same engine the day rows use, so the yearly
    table always adds up to the period totals on the page.
    """
    rows = calc_day_rows(business, date(year, 1, 1), date(year, 12, 31))
    months: list[dict] = []
    for month in range(1, 13):
        pick = [r for r in rows if r["date"].month == month]
        sell_cash = sum((r["sell_cash"] for r in pick), ZERO).quantize(CENT)
        sell_due = sum((r["sell_due"] for r in pick), ZERO).quantize(CENT)
        khoroch = sum((r["khoroch"] for r in pick), ZERO).quantize(CENT)
        collection = sum((r["collection"] for r in pick), ZERO).quantize(CENT)
        sell_total = (sell_cash + sell_due).quantize(CENT)
        profit = (sell_total - khoroch).quantize(CENT)
        if month == 12:
            last_day = date(year, 12, 31)
        else:
            last_day = date(year, month + 1, 1).fromordinal(
                date(year, month + 1, 1).toordinal() - 1)
        months.append({
            "month": month,
            "first_day": date(year, month, 1),
            "last_day": last_day,
            "sell_cash": sell_cash,
            "sell_due": sell_due,
            "sell_total": sell_total,
            "khoroch": khoroch,
            "collection": collection,
            "profit": profit,
            "has_data": bool(sell_cash or sell_due or khoroch or collection),
        })
    return months


def compare_calc(business, start: date, end: date) -> dict:
    """This period vs the previous period of equal length: deltas."""
    span = (end - start).days + 1
    prev_end = date.fromordinal(start.toordinal() - 1)
    prev_start = date.fromordinal(end.toordinal() - 2 * span + 1)
    current = calc_totals(business, start, end)
    previous = calc_totals(business, prev_start, prev_end)
    return {
        "current": current,
        "previous": previous,
        "prev_from": prev_start,
        "prev_to": prev_end,
        "delta": {key: (current.get(key, ZERO) - previous.get(key, ZERO))
                  .quantize(CENT)
                  for key in ("sell_total", "khoroch", "profit")},
    }
