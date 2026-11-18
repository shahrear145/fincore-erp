"""হিসাব সারাংশ (Summary) — day-wise and month-wise totals across all books.

Every figure is derived from the same ledger rows the individual book pages
show, so the summary can never disagree with them:

* **নগদের হিসাব (cash book)** — only what really moves the cash account:
  INCOME (received), EXPENSE (spent), BORROW / REPAY (loan in / out),
  INVEST / WITHDRAW (capital in / out).
* **বাকির হিসাব (due book)** — the receivable movement: due added vs due
  collected, taken from the DueEntry ledger (``signed_amount``).

The two books stay apart, exactly like on their own pages: a due collection
never appears as cash, and cash never appears as a due.

**On top of that** every row also carries the *real* money position, taken
from the double-entry ledger (account 1000 = cash in the drawer, 1100 =
bKash/Nagad/bank):

* ``closing_cash`` — what the drawer holds at the end of that day
  (a collection in cash is included, because the money really is there);
* ``wallet_closing`` — the mobile/bank balance, which is NOT in the drawer.

Everything is computed on ``transaction_date`` (never ``created_at``), with
exact Decimals and reversed rows excluded.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from apps.accounting.models import JournalLine
from apps.accounting.postings import CASH, WALLET
from apps.customers.models import DueEntry
from apps.transactions.models import Transaction, TransactionStatus, TransactionType

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

#: Cash books — money that physically enters or leaves the cash account.
CASH_IN_TYPES = {
    TransactionType.INCOME: "cash_in",
    TransactionType.BORROW: "loan_in",
    TransactionType.INVEST: "invest_in",
}
CASH_OUT_TYPES = {
    TransactionType.EXPENSE: "cash_out",
    TransactionType.REPAY: "loan_out",
    TransactionType.WITHDRAW: "invest_out",
}
#: Due-book transaction types (they never move the cash account).
DUE_UP_TYPES = {TransactionType.CREDIT}
DUE_DOWN_TYPES = {TransactionType.PAYMENT}


def _q(value) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(CENT)
    except Exception:
        return ZERO


def _dec_days(start: date, end: date) -> list[date]:
    out = []
    current = start
    while current <= end:
        out.append(current)
        current = date.fromordinal(current.toordinal() + 1)
    return out


# ------------------------------------------------------------------ helpers --

def _cash_by_day(business, start: date, end: date) -> dict[date, dict]:
    """{(day): {bucket: amount}} for the cash books."""
    rows = (Transaction.objects
            .filter(business=business, status=TransactionStatus.COMPLETED,
                    transaction_date__gte=start, transaction_date__lte=end)
            .exclude(transaction_type__in=list(DUE_UP_TYPES | DUE_DOWN_TYPES))
            .values("transaction_date", "transaction_type")
            .annotate(total=Sum("amount")))
    out: dict[date, dict] = defaultdict(lambda: defaultdict(Decimal))
    for row in rows:
        ttype = row["transaction_type"]
        bucket = CASH_IN_TYPES.get(ttype) or CASH_OUT_TYPES.get(ttype)
        if not bucket:
            continue
        out[row["transaction_date"]][bucket] += _q(row["total"])
    return out


def _due_by_day(business, start: date, end: date) -> dict[date, dict]:
    """{(day): {due_up, due_down, net}} from the due-entry ledger."""
    rows = (DueEntry.objects
            .filter(assignment__customer__business=business,
                    transaction_date__gte=start, transaction_date__lte=end)
            .values("transaction_date", "kind", "direction")
            .annotate(total=Sum("amount")))
    out: dict[date, dict] = defaultdict(
        lambda: {"due_up": ZERO, "due_down": ZERO, "net": ZERO})
    for row in rows:
        day = out[row["transaction_date"]]
        amount = _q(row["total"])
        kind = row["kind"]
        if kind == DueEntry.Kind.CREDIT:
            day["due_up"] += amount
            day["net"] += amount
        elif kind in (DueEntry.Kind.COLLECTION, DueEntry.Kind.ADVANCE,
                      DueEntry.Kind.REFUND, DueEntry.Kind.RETURN):
            day["due_down"] += amount
            day["net"] -= amount
        elif kind == DueEntry.Kind.ADJUSTMENT:
            if row["direction"] == DueEntry.Direction.UP:
                day["due_up"] += amount
                day["net"] += amount
            else:
                day["due_down"] += amount
                day["net"] -= amount
    return out


def _due_added_by_day(business, start: date, end: date) -> dict[date, Decimal]:
    """Due added per day straight from the CREDIT transactions."""
    rows = (Transaction.objects
            .filter(business=business, status=TransactionStatus.COMPLETED,
                    transaction_date__gte=start, transaction_date__lte=end,
                    transaction_type=TransactionType.CREDIT)
            .values("transaction_date")
            .annotate(total=Sum("amount")))
    return {row["transaction_date"]: _q(row["total"]) for row in rows}


def _due_collected_by_day(business, start: date, end: date) -> dict[date, Decimal]:
    rows = (Transaction.objects
            .filter(business=business, status=TransactionStatus.COMPLETED,
                    transaction_date__gte=start, transaction_date__lte=end,
                    transaction_type=TransactionType.PAYMENT)
            .values("transaction_date")
            .annotate(total=Sum("amount")))
    return {row["transaction_date"]: _q(row["total"]) for row in rows}


def _ledger_deltas(business, start: date, end: date) -> dict[date, dict]:
    """{(day): {cash, wallet}} — the real movement of the money accounts.

    Delegates to the cash book service so there is exactly one truth.
    """
    from apps.cashbook.services import ledger_deltas
    return ledger_deltas(business, start, end)


def _opening_balances(business, start: date) -> tuple[Decimal, Decimal]:
    """Cash and wallet balances the day before the period starts."""
    from apps.cashbook.services import opening_balances
    return opening_balances(business, start)


# ------------------------------------------------------------------- totals --

def period_totals(business, start: date, end: date) -> dict:
    """Headline totals for one period, split into the cash book and the due book."""
    cash = _cash_by_day(business, start, end)
    due = _due_by_day(business, start, end)
    due_added = _due_added_by_day(business, start, end)
    due_collected = _due_collected_by_day(business, start, end)

    totals = {
        "cash_in": ZERO, "cash_out": ZERO,
        "loan_in": ZERO, "loan_out": ZERO,
        "invest_in": ZERO, "invest_out": ZERO,
        "due_added": ZERO, "due_collected": ZERO, "due_net": ZERO,
    }
    for buckets in cash.values():
        for key in ("cash_in", "cash_out", "loan_in", "loan_out",
                    "invest_in", "invest_out"):
            totals[key] += buckets.get(key, ZERO)
    for day in due_added:
        totals["due_added"] += due_added[day]
    for day in due_collected:
        totals["due_collected"] += due_collected[day]
    for buckets in due.values():
        totals["due_net"] += buckets["net"]

    totals["cash_money_in"] = (totals["cash_in"] + totals["loan_in"]
                               + totals["invest_in"]).quantize(CENT)
    totals["cash_money_out"] = (totals["cash_out"] + totals["loan_out"]
                                + totals["invest_out"]).quantize(CENT)
    totals["net_cash"] = (totals["cash_money_in"]
                           - totals["cash_money_out"]).quantize(CENT)
    totals["due_net"] = (totals["due_added"]
                         - totals["due_collected"]).quantize(CENT)
    # What physically came into the drawer in this period: the cash book
    # plus the baki that was collected in cash.
    totals["cash_in_physical"] = (totals["cash_money_in"]
                                  + totals["due_collected"]).quantize(CENT)
    return totals


# --------------------------------------------------------------- day rows ---

def day_rows(business, start: date, end: date, *,
             include_empty: bool = True) -> list[dict]:
    """One row per day: what came in, what went out, what the due book did.

    Besides the book movement every row carries the *real* money position
    at the end of that day (``closing_cash`` — the drawer, ``wallet_closing``
    — bKash/Nagad/bank).
    """
    cash = _cash_by_day(business, start, end)
    due = _due_by_day(business, start, end)
    due_added = _due_added_by_day(business, start, end)
    due_collected = _due_collected_by_day(business, start, end)
    deltas = _ledger_deltas(business, start, end)
    cash_open, wallet_open = _opening_balances(business, start)

    days = _dec_days(start, end) if include_empty else sorted(
        set(cash) | set(due) | set(due_added) | set(due_collected))

    rows = []
    running = ZERO
    running_cash = cash_open
    running_wallet = wallet_open
    for day in days:
        buckets = cash.get(day, {})
        due_b = due.get(day, {})
        money_in = (buckets.get("cash_in", ZERO)
                    + buckets.get("loan_in", ZERO)
                    + buckets.get("invest_in", ZERO)).quantize(CENT)
        money_out = (buckets.get("cash_out", ZERO)
                     + buckets.get("loan_out", ZERO)
                     + buckets.get("invest_out", ZERO)).quantize(CENT)
        net_cash = (money_in - money_out).quantize(CENT)
        running = (running + net_cash).quantize(CENT)
        delta = deltas.get(day, {})
        running_cash = (running_cash + delta.get("cash", ZERO)).quantize(CENT)
        running_wallet = (running_wallet + delta.get("wallet", ZERO)).quantize(CENT)
        collected = due_collected.get(day, ZERO)
        rows.append({
            "date": day,
            "cash_in": buckets.get("cash_in", ZERO),
            "cash_out": buckets.get("cash_out", ZERO),
            "loan_in": buckets.get("loan_in", ZERO),
            "loan_out": buckets.get("loan_out", ZERO),
            "invest_in": buckets.get("invest_in", ZERO),
            "invest_out": buckets.get("invest_out", ZERO),
            "money_in": money_in,
            "money_out": money_out,
            "net_cash": net_cash,
            "due_added": due_added.get(day, ZERO),
            "due_collected": collected,
            "due_net": due_b.get("net", ZERO),
            "running_cash": running,
            # the truth: what the drawer really holds at the end of the day
            "closing_cash": running_cash,
            "wallet_closing": running_wallet,
            "cash_in_physical": (money_in + collected).quantize(CENT),
            "has_data": bool(money_in or money_out or due_b.get("net")
                             or due_added.get(day) or due_collected.get(day)),
        })
    return rows


# -------------------------------------------------------------- month rows --

def month_rows(business, year: int) -> list[dict]:
    """One row per month of `year` (same shape as :func:`day_rows`)."""
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    cash = _cash_by_day(business, start, end)
    due = _due_by_day(business, start, end)
    due_added = _due_added_by_day(business, start, end)
    due_collected = _due_collected_by_day(business, start, end)
    deltas = _ledger_deltas(business, start, end)

    buckets: dict[int, dict] = defaultdict(
        lambda: {"cash_in": ZERO, "cash_out": ZERO, "loan_in": ZERO,
                 "loan_out": ZERO, "invest_in": ZERO, "invest_out": ZERO,
                 "due_added": ZERO, "due_collected": ZERO, "due_net": ZERO})
    for day, values in cash.items():
        target = buckets[day.month]
        for key, value in values.items():
            target[key] += value
    for day, value in due_added.items():
        buckets[day.month]["due_added"] += value
    for day, value in due_collected.items():
        buckets[day.month]["due_collected"] += value
    for day, values in due.items():
        buckets[day.month]["due_net"] += values["net"]

    month_deltas: dict[int, dict] = defaultdict(
        lambda: {"cash": ZERO, "wallet": ZERO})
    for day, values in deltas.items():
        month_deltas[day.month]["cash"] += values.get("cash", ZERO)
        month_deltas[day.month]["wallet"] += values.get("wallet", ZERO)

    rows = []
    running = ZERO
    running_cash, running_wallet = _opening_balances(business, start)
    for month in range(1, 13):
        b = buckets[month]
        money_in = (b["cash_in"] + b["loan_in"] + b["invest_in"]).quantize(CENT)
        money_out = (b["cash_out"] + b["loan_out"] + b["invest_out"]).quantize(CENT)
        net_cash = (money_in - money_out).quantize(CENT)
        running = (running + net_cash).quantize(CENT)
        running_cash = (running_cash + month_deltas[month]["cash"]).quantize(CENT)
        running_wallet = (running_wallet
                          + month_deltas[month]["wallet"]).quantize(CENT)
        if month == 12:
            last_day = date(year, 12, 31)
        else:
            last_day = date(year, month + 1, 1).fromordinal(
                date(year, month + 1, 1).toordinal() - 1)
        rows.append({
            "month": month,
            "first_day": date(year, month, 1),
            "last_day": last_day,
            "cash_in": b["cash_in"], "cash_out": b["cash_out"],
            "loan_in": b["loan_in"], "loan_out": b["loan_out"],
            "invest_in": b["invest_in"], "invest_out": b["invest_out"],
            "money_in": money_in, "money_out": money_out,
            "net_cash": net_cash,
            "due_added": b["due_added"],
            "due_collected": b["due_collected"],
            "due_net": (b["due_added"] - b["due_collected"]).quantize(CENT),
            "running_cash": running,
            "closing_cash": running_cash,
            "wallet_closing": running_wallet,
            "cash_in_physical": (money_in + b["due_collected"]).quantize(CENT),
            "has_data": bool(money_in or money_out or b["due_added"]
                             or b["due_collected"]),
        })
    return rows


# -------------------------------------------------------------- comparison --

def compare_periods(business, start: date, end: date) -> dict:
    """This period vs the previous period of the same length."""
    span = (end - start).days + 1
    prev_end = start.fromordinal(start.toordinal() - 1)
    prev_start = prev_end.fromordinal(prev_end.toordinal() - span + 1)
    current = period_totals(business, start, end)
    previous = period_totals(business, prev_start, prev_end)
    return {
        "current": current,
        "previous": previous,
        "prev_from": prev_start,
        "prev_to": prev_end,
        "delta": {key: (current.get(key, ZERO) - previous.get(key, ZERO))
                  .quantize(CENT)
                  for key in current},
    }
