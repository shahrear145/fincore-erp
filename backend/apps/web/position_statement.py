"""হিসাব কমান্ড সেন্টার — the self-auditing position statement.

One read-only page that explains *every taka* behind the net position:

* composition (cash, wallet, customer due, loans, advances, bills to pay);
* a ledger drill-down with a running balance for every money account;
* a deterministic audit engine (ledger integrity, future dates, duplicates,
  negative cash days, drawer differences, loan-ledger mismatch …);
* a 90-day trend and a two-date comparison;
* a transparent health score (no magic numbers);
* a what-if simulator that never posts anything.

Nothing here writes to the books.  The page is safe for every role that can
already open the summary.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Min, Q, Sum
from django.utils import timezone

from apps.accounting.models import JournalEntry, JournalLine
from apps.accounting.postings import ADVANCES, CASH, LOANS_PAYABLE, WALLET
from apps.cashbook import services as cashbook
from apps.customers.models import Customer, CustomerAssignment, DueEntry
from apps.loans.models import LoanEntry, LoanInstallment, LoanParty
from apps.transactions.models import Transaction, TransactionStatus, TransactionType

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

DUE_KIND_BN = {
    "CREDIT": "বাকি যোগ",
    "COLLECTION": "আদায়",
    "ADVANCE": "অগ্রিম",
    "ADJUSTMENT": "সমন্বয়",
    "REFUND": "ফেরত",
    "RETURN": "সেল ফেরত",
    "REVERSAL": "বাতিল",
}
LOAN_KIND_BN = {
    "RECEIVED": "নেওয়া / বিনিয়োগ",
    "REPAYMENT": "পরিশোধ",
    "WITHDRAWAL": "মূল ফেরত",
}


# ------------------------------------------------------------------ utils --

def _q(value) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(CENT)
    except Exception:
        return ZERO


def money(value) -> str:
    """-৳500.00 / ৳600.00 — the minus sits before the taka sign."""
    amount = _q(value)
    if amount < 0:
        return f"-৳{-amount:,.2f}"
    return f"৳{amount:,.2f}"


def _account(business, code):
    return business.accounts.filter(code=code).first()


def _balance(business, code, as_of=None, credit_positive=False) -> Decimal:
    account = _account(business, code)
    if account is None:
        return ZERO
    lines = JournalLine.objects.filter(
        entry__business=business, account=account)
    if as_of is not None:
        lines = lines.filter(entry__transaction_date__lte=as_of)
    agg = lines.aggregate(d=Sum("debit"), c=Sum("credit"))
    debit = _q(agg["d"])
    credit = _q(agg["c"])
    return _q(credit - debit) if credit_positive else _q(debit - credit)


def _source_label(kind: str) -> str:
    kind = (kind or "").upper()
    if kind.startswith("TXN"):
        return "লেনদেন"
    if kind in ("SALE", "PURCHASE", "SALE_RETURN", "PURCHASE_RETURN"):
        return "বিক্রয় / ক্রয়"
    if kind.startswith("MANUAL"):
        return "ম্যানুয়াল"
    return kind.title() if kind else "—"


def _source_url(kind: str, source_id, day) -> str:
    kind = (kind or "").upper()
    if kind.startswith("TXN") or kind in (
            "SALE", "PURCHASE", "SALE_RETURN", "PURCHASE_RETURN"):
        return "/transactions/"
    if kind.startswith("MANUAL"):
        return ""
    return ""


# ------------------------------------------------------------- ledger view --

def _ledger_section(business, code, as_of=None, credit_positive=False,
                    limit=400) -> dict:
    """Every journal line of one account, with a running balance."""
    account = _account(business, code)
    if account is None:
        return {"rows": [], "total": ZERO, "count": 0, "truncated": 0,
                "opening": ZERO, "closing": ZERO}

    lines = (JournalLine.objects
             .filter(entry__business=business, account=account)
             .select_related("entry")
             .order_by("entry__transaction_date", "entry__created_at",
                       "line_no"))
    if as_of is not None:
        lines = lines.filter(entry__transaction_date__lte=as_of)

    total_count = lines.count()
    running = ZERO
    rows: list[dict] = []
    for line in lines.iterator(chunk_size=800):
        delta = (_q(line.credit - line.debit) if credit_positive
                 else _q(line.debit - line.credit))
        running = _q(running + delta)
        rows.append({
            "date": line.entry.transaction_date,
            "memo": (line.entry.memo or line.note or "—")[:120],
            "journal_no": line.entry.journal_no,
            "debit": _q(line.debit),
            "credit": _q(line.credit),
            "delta": delta,
            "running": running,
            "source_kind": line.entry.source_kind,
            "source_label": _source_label(line.entry.source_kind),
            "source_url": _source_url(line.entry.source_kind,
                                      line.entry.source_id,
                                      line.entry.transaction_date),
        })
    truncated = max(0, len(rows) - limit)
    if truncated:
        rows = rows[-limit:]
    return {
        "rows": rows,
        "total": running,
        "count": total_count,
        "truncated": truncated,
        "opening": ZERO,
        "closing": running,
    }


# -------------------------------------------------------------------- dues --

def _assignment_due(assignment, as_of=None) -> Decimal:
    due = _q(Decimal(str(assignment.opening_due))
             - Decimal(str(assignment.advance)))
    entries = assignment.due_entries.all()
    if as_of is not None:
        entries = [e for e in entries if e.transaction_date <= as_of]
    else:
        entries = list(entries)
    for entry in entries:
        due = _q(due + entry.signed_amount())
    return due


def _due_total(business, as_of=None) -> Decimal:
    assignments = (CustomerAssignment.objects
                   .filter(customer__business=business, is_active=True)
                   .prefetch_related("due_entries"))
    total = ZERO
    for assignment in assignments:
        total = _q(total + _assignment_due(assignment, as_of))
    return total


def _due_section(business, as_of=None) -> dict:
    assignments = (CustomerAssignment.objects
                   .filter(customer__business=business, is_active=True)
                   .select_related("customer", "partner")
                   .prefetch_related("due_entries")
                   .order_by("partner__name", "customer__name"))
    groups: dict[str, dict] = {}
    # Same rule as the বাকি খাতা: first credit stays on top; no amount sort.
    first_credit = {
        str(r["customer_id"]): r["first"]
        for r in (Transaction.objects
                  .filter(business=business,
                          transaction_type=TransactionType.CREDIT)
                  .values("customer_id")
                  .annotate(first=Min("created_at")))
    }
    total = ZERO
    customer_count = 0
    for assignment in assignments:
        due = _assignment_due(assignment, as_of)
        entries = list(assignment.due_entries.all())
        if as_of is not None:
            entries = [e for e in entries if e.transaction_date <= as_of]
        entries.sort(key=lambda e: (e.transaction_date, e.created_at))
        raw_entries = []
        for entry in entries:
            raw_entries.append({
                "date": entry.transaction_date,
                "kind": entry.kind,
                "kind_label": DUE_KIND_BN.get(entry.kind, entry.kind),
                "amount": _q(entry.amount),
                "signed": _q(entry.signed_amount()),
                "note": (entry.note or "")[:100],
            })
        truncated = max(0, len(raw_entries) - 60)
        if truncated:
            raw_entries = raw_entries[-60:]
        partner = assignment.partner
        group = groups.setdefault(str(partner.id), {
            "partner_id": str(partner.id),
            "partner_name": partner.name,
            "customers": [],
            "total": ZERO,
        })
        group["customers"].append({
            "customer_id": str(assignment.customer_id),
            "name": assignment.customer.name,
            "phone": assignment.customer.phone,
            "opening": _q(assignment.opening_due),
            "advance": _q(assignment.advance),
            "due": due,
            "entries": raw_entries,
            "truncated": truncated,
            "url": (f"/customers/?partner={partner.id}"
                    f"&history={assignment.customer_id}"),
            "_first_at": first_credit.get(str(assignment.customer_id),
                                          assignment.created_at),
        })
        group["total"] = _q(group["total"] + due)
        total = _q(total + due)
        customer_count += 1

    ordered = list(groups.values())
    for group in ordered:
        # As in the বাকি খাতা: first added stays on top, never sorted by due.
        group["customers"].sort(
            key=lambda c: (c.pop("_first_at"), (c["name"] or "").lower()))
    return {
        "groups": ordered,
        "total": total,
        "partner_count": len(ordered),
        "customer_count": customer_count,
    }


# ------------------------------------------------------------------- loans --

def _loan_totals(business, as_of=None, kind=LoanParty.Kind.LOAN) -> dict:
    entries = LoanEntry.objects.filter(
        business=business, party__kind=kind,
        status=LoanEntry.Status.POSTED)
    if as_of is not None:
        entries = entries.filter(transaction_date__lte=as_of)
    received = ZERO
    payable = ZERO
    returned = ZERO
    for entry in entries.values("kind", "amount", "contract_total"):
        amount = _q(entry["amount"])
        if entry["kind"] == LoanEntry.Kind.RECEIVED:
            received = _q(received + amount)
            contract = entry["contract_total"]
            payable = _q(payable + _q(contract if contract is not None
                                      else amount))
        else:
            returned = _q(returned + amount)
    return {
        "received": received,
        "returned": returned,
        "payable": payable,
        "outstanding": _q(payable - returned),
        "net_main_account": _q(received - returned),
    }


def _loans_section(business, as_of=None) -> dict:
    parties = (LoanParty.objects
               .filter(business=business)
               .filter(Q(is_active=True)
                       | Q(entries__status=LoanEntry.Status.POSTED))
               .distinct()
               .order_by("kind", "name"))
    entries = LoanEntry.objects.filter(
        business=business, status=LoanEntry.Status.POSTED)
    if as_of is not None:
        entries = entries.filter(transaction_date__lte=as_of)
    by_party: dict[str, list] = defaultdict(list)
    for entry in entries.select_related("party").order_by(
            "-transaction_date", "-created_at"):
        by_party[str(entry.party_id)].append(entry)

    loan_rows = []
    invest_rows = []
    for party in parties:
        raw = by_party.get(str(party.id), [])
        received = ZERO
        payable = ZERO
        returned = ZERO
        lines = []
        for entry in raw:
            amount = _q(entry.amount)
            if entry.kind == LoanEntry.Kind.RECEIVED:
                received = _q(received + amount)
                contract = entry.contract_total
                payable = _q(payable + _q(contract if contract is not None
                                          else amount))
            else:
                returned = _q(returned + amount)
            lines.append({
                "date": entry.transaction_date,
                "kind": entry.kind,
                "kind_label": LOAN_KIND_BN.get(entry.kind, entry.kind),
                "amount": amount,
                "description": (entry.description or "")[:100],
            })
        outstanding = _q(payable - returned)
        row = {
            "party_id": str(party.id),
            "name": party.name,
            "phone": party.phone,
            "kind": party.kind,
            "received": received,
            "returned": returned,
            "outstanding": outstanding,
            "interest": _q(payable - received),
            "lines": lines[:60],
            "truncated": max(0, len(lines) - 60),
        }
        (loan_rows if party.kind == LoanParty.Kind.LOAN
         else invest_rows).append(row)

    loan_rows.sort(key=lambda r: (-r["outstanding"], r["name"]))
    invest_rows.sort(key=lambda r: (-r["outstanding"], r["name"]))
    loan_total = _q(sum((r["outstanding"] for r in loan_rows), ZERO))
    invest_total = _q(sum((r["outstanding"] for r in invest_rows), ZERO))
    return {
        "loans": loan_rows,
        "investments": invest_rows,
        "loan_total": loan_total,
        "invest_total": invest_total,
    }


def _next_installments(business, as_of=None, days=30) -> dict:
    today = as_of or timezone.localdate()
    rows = (LoanInstallment.objects
            .filter(business=business, is_paid=False,
                    party__kind=LoanParty.Kind.LOAN,
                    due_date__gte=today,
                    due_date__lte=today + timedelta(days=days))
            .select_related("party")
            .order_by("due_date"))
    total = _q(sum((_q(r.amount) for r in rows), ZERO))
    items = [{
        "party": r.party.name,
        "due_date": r.due_date,
        "amount": _q(r.amount),
        "sequence": r.sequence,
    } for r in rows[:12]]
    return {"total": total, "items": items, "count": len(rows)}


# ----------------------------------------------------------------- position --

def _position_at(business, as_of=None) -> dict:
    """The same shape as ``business_position``, but honouring an as-of date."""
    cash = _balance(business, CASH, as_of)
    wallet = _balance(business, WALLET, as_of)
    due = _due_total(business, as_of)
    loans = _loan_totals(business, as_of, LoanParty.Kind.LOAN)
    invest = _loan_totals(business, as_of, LoanParty.Kind.INVESTMENT)
    advances = _balance(business, ADVANCES, as_of, credit_positive=True)
    payable = _balance(business, "2000", as_of, credit_positive=True)

    cash_in_hand = _q(cash + wallet)
    have = _q(cash + wallet + due)
    owe = _q(loans["outstanding"] + advances + payable)
    return {
        "cash": cash,
        "wallet": wallet,
        "cash_in_hand": cash_in_hand,
        "due": due,
        "invest_outstanding": invest["outstanding"],
        "invest_received": invest["received"],
        "invest_returned": invest["returned"],
        "loan_outstanding": loans["outstanding"],
        "loan_received": loans["received"],
        "loan_returned": loans["returned"],
        "advances": advances,
        "payable": payable,
        "have": have,
        "owe": owe,
        "net": _q(have - owe),
    }


def _composition(position: dict) -> list[dict]:
    """The waterfall, in the order a reader expects it."""
    return [
        {"key": "cash", "icon": "fa-money-bill-wave", "tone": "blue",
         "amount": position["cash"], "kind": "have",
         "anchor": "sec-cash"},
        {"key": "wallet", "icon": "fa-mobile-screen-button", "tone": "violet",
         "amount": position["wallet"], "kind": "have",
         "anchor": "sec-wallet"},
        {"key": "due", "icon": "fa-address-book", "tone": "amber",
         "amount": position["due"], "kind": "have",
         "anchor": "sec-due"},
        {"key": "loans", "icon": "fa-hand-holding-dollar", "tone": "rose",
         "amount": position["loan_outstanding"], "kind": "owe",
         "anchor": "sec-loans"},
        {"key": "advances", "icon": "fa-arrow-down-up-across-line",
         "tone": "rose", "amount": position["advances"], "kind": "owe",
         "anchor": "sec-advances"},
        {"key": "payable", "icon": "fa-file-invoice-dollar", "tone": "rose",
         "amount": position["payable"], "kind": "owe",
         "anchor": "sec-payable"},
    ]


def _waterfall(composition: list[dict], net: Decimal) -> dict:
    """Percent widths for the visual waterfall (server-side, no JS needed)."""
    peak = max((abs(c["amount"]) for c in composition), default=ZERO)
    peak = max(peak, abs(net), Decimal("1.00"))
    for item in composition:
        item["pct"] = float((abs(item["amount"]) / peak) * 100)
    return {"peak": peak, "net_pct": float((abs(net) / peak) * 100)}


# -------------------------------------------------------------------- trend --

def _daily_series(business, start: date, end: date) -> list[dict]:
    """Day-by-day net position, built from aggregate queries (not N+1)."""
    if end < start:
        start, end = end, start
    days = [start + timedelta(days=i)
            for i in range((end - start).days + 1)]

    accounts = {a.code: a for a in
                business.accounts.filter(code__in=[CASH, WALLET])}
    cash_start = ZERO
    wallet_start = ZERO
    cash_delta: dict[date, Decimal] = defaultdict(lambda: ZERO)
    wallet_delta: dict[date, Decimal] = defaultdict(lambda: ZERO)
    if accounts:
        before = (JournalLine.objects
                  .filter(entry__business=business,
                          account__in=list(accounts.values()),
                          entry__transaction_date__lt=start)
                  .values("account__code")
                  .annotate(d=Sum("debit"), c=Sum("credit")))
        for row in before:
            amount = _q(_q(row["d"]) - _q(row["c"]))
            if row["account__code"] == CASH:
                cash_start = _q(cash_start + amount)
            else:
                wallet_start = _q(wallet_start + amount)
        within = (JournalLine.objects
                  .filter(entry__business=business,
                          account__in=list(accounts.values()),
                          entry__transaction_date__gte=start,
                          entry__transaction_date__lte=end)
                  .values("entry__transaction_date", "account__code")
                  .annotate(d=Sum("debit"), c=Sum("credit")))
        for row in within:
            amount = _q(_q(row["d"]) - _q(row["c"]))
            if row["account__code"] == CASH:
                cash_delta[row["entry__transaction_date"]] = _q(
                    cash_delta[row["entry__transaction_date"]] + amount)
            else:
                wallet_delta[row["entry__transaction_date"]] = _q(
                    wallet_delta[row["entry__transaction_date"]] + amount)

    # due: opening balances + every signed due entry
    assignments = (CustomerAssignment.objects
                   .filter(customer__business=business, is_active=True)
                   .prefetch_related("due_entries"))
    due_start = ZERO
    due_delta: dict[date, Decimal] = defaultdict(lambda: ZERO)
    for assignment in assignments:
        due_start = _q(due_start + _q(
            Decimal(str(assignment.opening_due))
            - Decimal(str(assignment.advance))))
        for entry in assignment.due_entries.all():
            amount = _q(entry.signed_amount())
            if entry.transaction_date < start:
                due_start = _q(due_start + amount)
            elif entry.transaction_date <= end:
                due_delta[entry.transaction_date] = _q(
                    due_delta[entry.transaction_date] + amount)

    # loans: contracted payable on the way in, actual money on the way out
    loan_start = ZERO
    loan_delta: dict[date, Decimal] = defaultdict(lambda: ZERO)
    loan_entries = (LoanEntry.objects
                    .filter(business=business,
                            party__kind=LoanParty.Kind.LOAN,
                            status=LoanEntry.Status.POSTED)
                    .values("kind", "amount", "contract_total",
                            "transaction_date"))
    for entry in loan_entries:
        amount = _q(entry["amount"])
        if entry["kind"] == LoanEntry.Kind.RECEIVED:
            contract = entry["contract_total"]
            delta = _q(contract if contract is not None else amount)
        else:
            delta = -amount
        if entry["transaction_date"] < start:
            loan_start = _q(loan_start + delta)
        elif entry["transaction_date"] <= end:
            loan_delta[entry["transaction_date"]] = _q(
                loan_delta[entry["transaction_date"]] + delta)

    # advances / bills: liability accounts, credit-positive
    def _liability_start(code):
        account = _account(business, code)
        if account is None:
            return ZERO, defaultdict(lambda: ZERO)
        before = (JournalLine.objects
                  .filter(entry__business=business, account=account,
                          entry__transaction_date__lt=start)
                  .aggregate(d=Sum("debit"), c=Sum("credit")))
        opening = _q(_q(before["c"]) - _q(before["d"]))
        rows = (JournalLine.objects
                .filter(entry__business=business, account=account,
                        entry__transaction_date__gte=start,
                        entry__transaction_date__lte=end)
                .values("entry__transaction_date")
                .annotate(d=Sum("debit"), c=Sum("credit")))
        delta = defaultdict(lambda: ZERO)
        for row in rows:
            delta[row["entry__transaction_date"]] = _q(
                delta[row["entry__transaction_date"]]
                + _q(row["c"]) - _q(row["d"]))
        return opening, delta

    adv_start, adv_delta = _liability_start(ADVANCES)
    pay_start, pay_delta = _liability_start("2000")

    cash, wallet = cash_start, wallet_start
    due, loans = due_start, loan_start
    advances, payable = adv_start, pay_start
    series = []
    for day in days:
        cash = _q(cash + cash_delta[day])
        wallet = _q(wallet + wallet_delta[day])
        due = _q(due + due_delta[day])
        loans = _q(loans + loan_delta[day])
        advances = _q(advances + adv_delta[day])
        payable = _q(payable + pay_delta[day])
        owe = _q(loans + advances + payable)
        series.append({
            "date": day,
            "cash": cash,
            "wallet": wallet,
            "due": due,
            "loans": loans,
            "advances": advances,
            "payable": payable,
            "owe": owe,
            "net": _q(cash + wallet + due - owe),
        })
    return series


def _trend_payload(series: list[dict]) -> dict:
    """Turn the daily series into inline-SVG coordinates."""
    width, height = 760, 230
    pad_top, pad_bottom, pad_left, pad_right = 26, 28, 88, 18
    values = [p["net"] for p in series]
    if not values:
        return {"points": [], "path": "", "area": "", "grid": [],
                "min": ZERO, "max": ZERO, "zero_y": None,
                "start_net": ZERO, "end_net": ZERO, "change": ZERO,
                "width": width, "height": height,
                "pad_top": pad_top, "pad_bottom": pad_bottom,
                "pad_left": pad_left, "pad_right": pad_right,
                "plot_left": pad_left, "plot_right": width - pad_right}
    lo, hi = min(values), max(values)
    if hi == lo:
        hi, lo = _q(hi + Decimal("1")), _q(lo - Decimal("1"))
    span = hi - lo
    count = len(values)

    def x_at(index: int) -> float:
        return (pad_left + index * (width - pad_left - pad_right)
                / max(1, count - 1))

    def y_at(value: Decimal) -> float:
        return (height - pad_bottom
                - float((value - lo) / span)
                * (height - pad_top - pad_bottom))

    points = []
    for index, row in enumerate(series):
        points.append({
            "x": round(x_at(index), 1),
            "y": round(y_at(row["net"]), 1),
            "date": row["date"],
            "net": row["net"],
        })

    path = "M " + " L ".join(f"{p['x']},{p['y']}" for p in points)
    area = (path + f" L {points[-1]['x']},{height - pad_bottom}"
            f" L {points[0]['x']},{height - pad_bottom} Z")

    # the three biggest day-over-day moves get a marker
    moves = []
    for index in range(1, count):
        delta = _q(values[index] - values[index - 1])
        if delta:
            moves.append((abs(delta), index, delta))
    moves.sort(reverse=True)
    marked = {index: delta for _abs, index, delta in moves[:3]}
    for index, point in enumerate(points):
        if index in marked:
            point["marker"] = True
            point["delta"] = marked[index]

    grid = []
    for step in range(4):
        value = _q(lo + span * Decimal(step) / Decimal(3))
        grid.append({"y": round(y_at(value), 1),
                     "label": money(value)})

    zero_y = round(y_at(ZERO), 1) if lo < 0 < hi else None
    first, last = series[0], series[-1]
    return {
        "points": points,
        "path": path,
        "area": area,
        "grid": grid,
        "min": lo,
        "max": hi,
        "zero_y": zero_y,
        "width": width,
        "height": height,
        "pad_top": pad_top,
        "pad_bottom": pad_bottom,
        "pad_left": pad_left,
        "pad_right": pad_right,
        "plot_left": pad_left,
        "plot_right": width - pad_right,
        "start_date": first["date"],
        "end_date": last["date"],
        "start_net": first["net"],
        "end_net": last["net"],
        "change": _q(last["net"] - first["net"]),
    }


# -------------------------------------------------------------------- audit --

def _finding(key, severity, title_bn, title_en, detail_bn, detail_en,
             count=0, amount=ZERO, items=None, fix_url="",
             fix_bn="", fix_en=""):
    return {
        "key": key,
        "severity": severity,
        "title_bn": title_bn,
        "title_en": title_en,
        "detail_bn": detail_bn,
        "detail_en": detail_en,
        "count": count,
        "amount": _q(amount),
        "items": items or [],
        "fix_url": fix_url,
        "fix_bn": fix_bn,
        "fix_en": fix_en,
    }


def _audit(business, series: list[dict], today: date) -> dict:
    findings: list[dict] = []
    integrity = cashbook.integrity_check(business)

    if integrity["broken"]:
        items = [{
            "label": (f"{row['code']} {row['name']} — "
                      f"{money(row['difference'])}"),
            "url": "/summary/",
        } for row in integrity["broken"][:6]]
        findings.append(_finding(
            "ledger", "high",
            "লেজার মিলছে না", "The ledger does not match",
            (f"{len(integrity['broken'])}টি অ্যাকাউন্টের ব্যালেন্স লাইভ "
             "লেনদেনের সাথে মিলছে না — কোনো পোস্টিং হারিয়েছে বা বাড়তি আছে।"),
            (f"{len(integrity['broken'])} account(s) disagree with the live "
             "transactions — a posting is missing or duplicated."),
            count=len(integrity["broken"]), items=items,
            fix_url="/summary/", fix_bn="হিসাব যাচাই খুলুন",
            fix_en="Open the ledger check"))

    if integrity["orphan_entries"]:
        findings.append(_finding(
            "orphan", "high",
            "অনাথ জার্নাল এন্ট্রি", "Orphan journal entries",
            (f"{integrity['orphan_entries']}টি জার্নাল এন্ট্রি এমন এক "
             "লেনদেনের সাথে জুড়ে আছে যা আর নেই।"),
            (f"{integrity['orphan_entries']} journal entr(ies) point at a "
             "transaction that no longer exists."),
            count=integrity["orphan_entries"],
            fix_url="/summary/", fix_bn="হিসাব যাচাই খুলুন",
            fix_en="Open the ledger check"))

    unposted = cashbook.unposted_transactions(business)
    if unposted:
        items = [{
            "label": (f"{row[3]} · {row[1]} · {money(row[2])}"),
            "url": "/transactions/",
        } for row in unposted[:6]]
        findings.append(_finding(
            "unposted", "high",
            "জার্নাল ছাড়া লেনদেন", "Transactions without a journal",
            (f"{len(unposted)}টি লেনদেনের ডাবল-এন্ট্রি জার্নাল নেই — "
             "হাতে নগদ ও লাভের হিসাব কম/বেশি দেখাতে পারে।"),
            (f"{len(unposted)} transaction(s) never got their double-entry "
             "journal — cash and profit can be off."),
            count=len(unposted), items=items, fix_url="/summary/",
            fix_bn="লেজার রিবিল্ড করুন", fix_en="Rebuild the ledger"))

    # unbalanced entries (defensive: the writer should prevent this)
    unbalanced = []
    for entry in (JournalEntry.objects
                  .filter(business=business)
                  .annotate(d=Sum("lines__debit"), c=Sum("lines__credit"))
                  .filter(d__isnull=False)):
        if abs(_q(entry.d) - _q(entry.c)) > Decimal("0.005"):
            unbalanced.append(entry)
    if unbalanced:
        items = [{
            "label": (f"{e.transaction_date} · {e.journal_no or 'JE'} · "
                      f"{money(_q(e.d) - _q(e.c))}"),
            "url": "/summary/",
        } for e in unbalanced[:6]]
        findings.append(_finding(
            "unbalanced", "high",
            "অসামঞ্জস্যপূর্ণ জার্নাল", "Unbalanced journal entries",
            (f"{len(unbalanced)}টি জার্নালে ডেবিট ও ক্রেডিট সমান নয়।"),
            (f"{len(unbalanced)} journal entr(ies) have unequal debits and "
             "credits."),
            count=len(unbalanced), items=items, fix_url="/summary/",
            fix_bn="হিসাব যাচাই খুলুন", fix_en="Open the ledger check"))

    future = (Transaction.objects
              .filter(business=business, status=TransactionStatus.COMPLETED,
                      transaction_date__gt=today)
              .order_by("transaction_date"))
    if future.exists():
        amount = _q(future.aggregate(t=Sum("amount"))["t"])
        items = [{
            "label": (f"{t.transaction_date} · {t.transaction_type} · "
                      f"{money(t.amount)}"),
            "url": "/transactions/",
        } for t in future[:6]]
        findings.append(_finding(
            "future", "medium",
            "ভবিষ্যতের তারিখের এন্ট্রি", "Future-dated entries",
            (f"{future.count()}টি এন্ট্রি আজকের পরে ডেটেড "
             f"({money(amount)}) — “এখনকার” হিসাব ভুল দেখাতে পারে।"),
            (f"{future.count()} entr(ies) are dated after today "
             f"({money(amount)}) — they distort the current position."),
            count=future.count(), amount=amount, items=items,
            fix_url="/transactions/", fix_bn="তারিখ ঠিক করুন",
            fix_en="Fix the dates"))

    # negative cash / wallet days in the audit window
    for key, label_bn, label_en in (
            ("cash", "ড্রয়ারের নগদ ঋণাত্মক", "Negative drawer cash"),
            ("wallet", "ওয়ালেট ঋণাত্মক", "Negative wallet")):
        bad = [p for p in series if p[key] < 0]
        if bad:
            items = [{
                "label": f"{p['date']} · {money(p[key])}",
                "url": "/transactions/",
            } for p in bad[:6]]
            findings.append(_finding(
                f"negative_{key}", "high",
                label_bn, label_en,
                (f"{len(bad)} দিন {label_bn.lower()} দেখাচ্ছে — সাধারণত "
                 "একটি ভুল পোস্টিং বা তারিখের লক্ষণ।"),
                (f"{len(bad)} day(s) show {label_en.lower()} — usually a "
                 "posting error or a wrong date."),
                count=len(bad), items=items, fix_url="/transactions/",
                fix_bn="লেনদেন দেখুন", fix_en="Review transactions"))

    # duplicate-looking transactions (last 90 days)
    since = today - timedelta(days=90)
    duplicate_groups = list(
        Transaction.objects
        .filter(business=business, status=TransactionStatus.COMPLETED,
                transaction_date__gte=since)
        .values("amount", "transaction_date", "transaction_type")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .order_by("-transaction_date")[:5])
    if duplicate_groups:
        items = []
        for group in duplicate_groups:
            twins = (Transaction.objects
                     .filter(business=business,
                             amount=group["amount"],
                             transaction_date=group["transaction_date"],
                             transaction_type=group["transaction_type"],
                             status=TransactionStatus.COMPLETED)
                     .order_by("created_at")[:4])
            names = " / ".join(
                (t.description or t.customer.name if t.customer_id
                 else t.description or "—") for t in twins)
            items.append({
                "label": (f"{group['transaction_date']} · "
                          f"{money(group['amount'])} × {group['n']} · {names}"),
                "url": "/transactions/",
            })
        findings.append(_finding(
            "duplicates", "medium",
            "ডুপ্লিকেট সন্দেহ", "Possible duplicates",
            (f"{len(duplicate_groups)}টি গ্রুপে একই তারিখে একই টাকার "
             "একাধিক এন্ট্রি আছে — আসল কিনা যাচাই করুন।"),
            (f"{len(duplicate_groups)} group(s) share the same date and "
             "amount — check whether they are genuine."),
            count=len(duplicate_groups), items=items,
            fix_url="/transactions/", fix_bn="যাচাই করুন",
            fix_en="Check them"))

    # drawer count difference + same-amount candidate finder
    count = cashbook.last_count(business)
    if count is not None and abs(_q(count.difference)) > Decimal("0.005"):
        gap = abs(_q(count.difference))
        candidates = (Transaction.objects
                      .filter(business=business, amount=gap,
                              transaction_date__gte=(
                                  count.counted_on - timedelta(days=7)),
                              transaction_date__lte=(
                                  count.counted_on + timedelta(days=7)))
                      .exclude(status=TransactionStatus.REVERSED)
                      .order_by("transaction_date")[:6])
        items = [{
            "label": (f"{t.transaction_date} · {t.transaction_type} · "
                      f"{money(t.amount)} · {t.description or '—'}"),
            "url": "/transactions/",
        } for t in candidates]
        detail_bn = (f"শেষ গোনায় {money(gap)} "
                     f"{'গরমিল' if count.difference >= 0 else 'ঘাটতি'}।")
        if candidates:
            detail_bn += (f" ওই টাকার {len(candidates)}টি কাছাকাছি এন্ট্রি "
                          "পাওয়া গেছে — হয়তো একটি বাদ পড়েছে।")
        findings.append(_finding(
            "drawer", "medium",
            "ড্রয়ার গণনার গরমিল", "Drawer count difference",
            detail_bn,
            (f"The last count differs by {money(gap)}."
             + (f" {len(candidates)} nearby entr(ies) have exactly that "
                "amount — one may be missing." if candidates else "")),
            count=1, amount=gap, items=items, fix_url="/summary/",
            fix_bn="গোনা দেখুন", fix_en="Open the count"))

    # loan book vs the 2400 ledger account
    loan_book = _loan_totals(business, None, LoanParty.Kind.LOAN)
    loan_ledger = _balance(business, LOANS_PAYABLE, None,
                           credit_positive=True)
    if abs(_q(loan_book["net_main_account"]) - loan_ledger) > Decimal("0.005"):
        findings.append(_finding(
            "loan_ledger", "medium",
            "ঋণ খাতা বনাম লেজার", "Loan book vs ledger",
            (f"ঋণ খাতায় মূল টাকা {money(loan_book['net_main_account'])}, "
             f"কিন্তু ২৪০০ লেজারে {money(loan_ledger)} — একটি এন্ট্রি "
             "জার্নাল ছাড়া হতে পারে।"),
            (f"The loan book says {money(loan_book['net_main_account'])}, "
             f"the 2400 ledger says {money(loan_ledger)} — one entry may "
             "lack its journal."),
            count=1, amount=_q(loan_book["net_main_account"] - loan_ledger),
            items=[{"label": f"লেজার ২৪০০ · {money(loan_ledger)}",
                    "url": "/loans/"},
                   {"label": f"ঋণ খাতা · {money(loan_book['net_main_account'])}",
                    "url": "/loans/"}],
            fix_url="/loans/", fix_bn="ঋণ খাতা খুলুন",
            fix_en="Open the loan book"))

    future_due = (DueEntry.objects
                  .filter(assignment__customer__business=business,
                          transaction_date__gt=today)
                  .select_related("assignment__customer"))
    if future_due.exists():
        amount = _q(future_due.aggregate(t=Sum("amount"))["t"])
        items = [{
            "label": (f"{e.transaction_date} · "
                      f"{e.assignment.customer.name} · {money(e.amount)}"),
            "url": "/customers/",
        } for e in future_due[:6]]
        findings.append(_finding(
            "future_due", "medium",
            "ভবিষ্যতের বাকি এন্ট্রি", "Future-dated due entries",
            (f"{future_due.count()}টি বাকি এন্ট্রি আজকের পরে ডেটেড "
             f"({money(amount)})।"),
            (f"{future_due.count()} due entr(ies) are dated after today "
             f"({money(amount)})."),
            count=future_due.count(), amount=amount, items=items,
            fix_url="/customers/", fix_bn="বাকি খাতা খুলুন",
            fix_en="Open the due book"))

    no_partner = (Customer.objects
                  .filter(business=business, status="ACTIVE")
                  .exclude(assignments__is_active=True)
                  .order_by("name"))
    if no_partner.exists():
        items = [{"label": c.name, "url": "/customers/"}
                 for c in no_partner[:8]]
        findings.append(_finding(
            "no_partner", "low",
            "পার্টনার ছাড়া গ্রাহক", "Customers without a partner",
            (f"{no_partner.count()} জন গ্রাহকের কোনো পার্টনার নেই — তাদের "
             "বাকি কোনো পার্টনার খাতায় দেখা যাবে না।"),
            (f"{no_partner.count()} customer(s) have no partner — their due "
             "cannot appear in any partner book."),
            count=no_partner.count(), items=items, fix_url="/customers/",
            fix_bn="গ্রাহক ঠিক করুন", fix_en="Fix the customers"))

    counts = {"high": 0, "medium": 0, "low": 0}
    for finding in findings:
        counts[finding["severity"]] += 1
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["key"]))
    return {
        "findings": findings,
        "counts": counts,
        "clean": not findings,
        "total": len(findings),
        "checks_run": 11,
    }


# ------------------------------------------------------------------- health --

def _health(business, position: dict, audit: dict, as_of=None) -> dict:
    today = as_of or timezone.localdate()
    installments = _next_installments(business, today, 30)
    near_term = _q(position["payable"] + installments["total"])
    liquid = position["cash_in_hand"]
    if near_term <= 0:
        liquidity_score = 30.0
        liquidity_note = "৩০ দিনে পরিশোধযোগ্য বিল/কিস্তি নেই"
    else:
        ratio = float(liquid / near_term)
        liquidity_score = round(min(30.0, ratio * 30.0), 1)
        liquidity_note = (f"নগদ {money(liquid)} ÷ দিতে হবে "
                          f"{money(near_term)}")

    since = today - timedelta(days=30)
    collected = _q((DueEntry.objects
                    .filter(assignment__customer__business=business,
                            kind=DueEntry.Kind.COLLECTION,
                            transaction_date__gte=since,
                            transaction_date__lte=today)
                    .aggregate(t=Sum("amount"))["t"]))
    due_then = _due_total(business, since - timedelta(days=1))
    if due_then <= 0:
        collection_score = 25.0
        collection_note = "৩০ দিন আগে আদায়যোগ্য বাকি ছিল না"
    else:
        ratio = float(collected / due_then)
        collection_score = round(min(25.0, ratio * 25.0), 1)
        collection_note = (f"৩০ দিনে আদায় {money(collected)} ÷ "
                           f"তখনকার বাকি {money(due_then)}")

    have = position["have"]
    owe = position["owe"]
    if have <= 0:
        debt_score = 25.0 if owe <= 0 else 0.0
        debt_note = f"দিতে হবে {money(owe)}"
    else:
        ratio = float(owe / have)
        debt_score = round(max(0.0, min(25.0, 25.0 * (1.0 - ratio))), 1)
        debt_note = f"দেনা {money(owe)} ÷ সম্পদ {money(have)}"

    penalty = (audit["counts"]["high"] * 5
               + audit["counts"]["medium"] * 2
               + audit["counts"]["low"] * 1)
    quality_score = float(max(0, 20 - penalty))
    if audit["clean"]:
        quality_note = "১১টি অডিট চেক — সব পরিষ্কার"
    else:
        quality_note = (f"{audit['counts']['high']} উচ্চ · "
                        f"{audit['counts']['medium']} মাঝারি · "
                        f"{audit['counts']['low']} নিম্ন")

    total = round(liquidity_score + collection_score + debt_score
                  + quality_score)
    total = max(0, min(100, total))
    if total >= 80:
        grade, tone = "চমৎকার", "ok"
    elif total >= 60:
        grade, tone = "ভালো", "info"
    elif total >= 40:
        grade, tone = "সতর্ক", "warn"
    else:
        grade, tone = "ঝুঁকি", "danger"

    return {
        "score": total,
        "grade": grade,
        "tone": tone,
        "parts": [
            {"key": "liquidity", "label": "তারল্য", "score": liquidity_score,
             "max": 30, "note": liquidity_note},
            {"key": "collection", "label": "আদায়", "score": collection_score,
             "max": 25, "note": collection_note},
            {"key": "debt", "label": "ঋণের চাপ", "score": debt_score,
             "max": 25, "note": debt_note},
            {"key": "quality", "label": "ডেটা গুণমান", "score": quality_score,
             "max": 20, "note": quality_note},
        ],
        "next_installments": installments,
    }


# ------------------------------------------------------------------ compare --

def _compare(business, as_of: date, compare: date) -> dict:
    start = _position_at(business, compare)
    end = _position_at(business, as_of)
    keys = [
        ("cash", "ড্রয়ারের নগদ", "Drawer cash", "have"),
        ("wallet", "বিকাশ / নগদ / ব্যাংক", "Wallet", "have"),
        ("due", "কাস্টমারের বাকি", "Customer due", "have"),
        ("loan_outstanding", "ঋণ", "Loans", "owe"),
        ("advances", "কাস্টমারের অগ্রিম", "Advances", "owe"),
        ("payable", "খরচ বাকি", "Bills to pay", "owe"),
    ]
    rows = []
    for key, bn, en, side in keys:
        before = _q(start[key])
        after = _q(end[key])
        delta = _q(after - before)
        rows.append({
            "key": key, "label_bn": bn, "label_en": en, "side": side,
            "before": before, "after": after, "delta": delta,
        })
    peak = max((abs(r["delta"]) for r in rows), default=ZERO)
    peak = max(peak, Decimal("1.00"))
    for row in rows:
        row["pct"] = float((abs(row["delta"]) / peak) * 100)
    return {
        "compare_date": compare,
        "before": start,
        "after": end,
        "rows": rows,
        "net_before": start["net"],
        "net_after": end["net"],
        "net_delta": _q(end["net"] - start["net"]),
        "peak": peak,
    }


# ---------------------------------------------------------------- statement --

def build_statement(business, as_of=None, compare=None,
                    lang: str = "bn") -> dict:
    """Everything the page (and the exports) need, in one read-only pass."""
    today = timezone.localdate()
    as_of = as_of or today
    if as_of > today:
        as_of = today

    if as_of == today:
        position = cashbook.business_position(business)
    else:
        position = _position_at(business, as_of)

    proof_parts = _q(position["cash"] + position["wallet"] + position["due"]
                     - position["loan_outstanding"] - position["advances"]
                     - position["payable"])
    proof = {
        "parts_sum": proof_parts,
        "net": position["net"],
        "difference": _q(proof_parts - position["net"]),
        "ok": abs(proof_parts - position["net"]) <= Decimal("0.005"),
    }

    composition = _composition(position)
    waterfall = _waterfall(composition, position["net"])

    cash_section = _ledger_section(business, CASH, as_of, False)
    wallet_section = _ledger_section(business, WALLET, as_of, False)
    advances_section = _ledger_section(business, ADVANCES, as_of, True)
    payable_section = _ledger_section(business, "2000", as_of, True)
    due_section = _due_section(business, as_of)
    loans_section = _loans_section(business, as_of)
    next_installments = _next_installments(business, as_of, 30)

    # the audit is always about today's data, even when the page is time-travelling
    audit_series = _daily_series(business, today - timedelta(days=89), today)
    audit = _audit(business, audit_series, today)

    trend_series = (audit_series if as_of == today else
                    _daily_series(business, as_of - timedelta(days=89), as_of))
    trend = _trend_payload(trend_series)

    health = _health(business, position, audit, as_of)

    comparison = None
    if compare is not None:
        if compare > as_of:
            compare = as_of
        comparison = _compare(business, as_of, compare)

    whatif = {
        "cash": str(position["cash_in_hand"]),
        "due": str(position["due"]),
        "loans": str(position["loan_outstanding"]),
        "advances": str(position["advances"]),
        "payable": str(position["payable"]),
        "net": str(position["net"]),
        "customers": due_section["customer_count"],
        "next_installments": str(next_installments["total"]),
    }

    return {
        "as_of": as_of,
        "today": today,
        "position": position,
        "composition": composition,
        "waterfall": waterfall,
        "proof": proof,
        "cash": cash_section,
        "wallet": wallet_section,
        "due": due_section,
        "loans": loans_section,
        "advances": advances_section,
        "payable": payable_section,
        "next_installments": next_installments,
        "audit": audit,
        "trend": trend,
        "health": health,
        "comparison": comparison,
        "whatif": whatif,
    }
