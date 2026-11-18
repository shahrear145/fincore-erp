"""The cash book's single source of truth.

Everything the shop wants to know — "how much cash is in the drawer",
"what is the real profit", "does the ledger still add up" — is derived
here, from the double-entry ledger and the live transactions, so no two
pages can ever disagree.

Rules that matter for a small shop:

* **Cash ≠ wallet.** Money paid by bKash/Nagad/bank is never cash in the
  drawer; the ledger already knows the difference (account 1000 vs 1100).
* **A collection is cash.** When a customer pays their baki in cash the
  money is in the drawer, so it belongs to the cash position even though
  it is a due-book movement.
* **Unsold stock is not a loss.** Profit needs the stock that is still
  on the shelf: ``cogs = goods bought − stock increase``.
* **The book can only be trusted if it matches the transactions.** The
  integrity check compares every account with the live transactions.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Count, Sum

from apps.accounting.models import Account, JournalEntry, JournalLine
from apps.accounting.postings import CASH, WALLET
from apps.transactions.models import (
    ExpenseKind, Transaction, TransactionStatus, TransactionType,
)

from .models import CashCount, StockSnapshot

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def _q(value) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(CENT)
    except Exception:
        return ZERO


def _account(business, code) -> Account | None:
    return business.accounts.filter(code=code).first()


def ledger_balance(business, code: str, as_of: date | None = None) -> Decimal:
    """Balance of one account (asset sign: debit − credit) as of a date."""
    account = _account(business, code)
    if account is None:
        return ZERO
    lines = JournalLine.objects.filter(
        entry__business=business, account=account)
    if as_of is not None:
        lines = lines.filter(entry__transaction_date__lte=as_of)
    agg = lines.aggregate(d=Sum("debit"), c=Sum("credit"))
    return _q((agg["d"] or ZERO) - (agg["c"] or ZERO))


def ledger_deltas(business, start: date, end: date) -> dict:
    """{(day): {cash, wallet}} — the real movement of the money accounts.

    Read straight from the double-entry ledger, so a collection paid in
    cash is included (the money really is in the drawer) and bKash money
    stays in the wallet (it is not in the drawer).
    """
    accounts = {a.code: a for a in
                business.accounts.filter(code__in=[CASH, WALLET])}
    cash_acc = accounts.get(CASH)
    wallet_acc = accounts.get(WALLET)
    ids = [a.id for a in (cash_acc, wallet_acc) if a is not None]
    out: dict[date, dict] = defaultdict(lambda: {"cash": ZERO, "wallet": ZERO})
    if not ids:
        return out
    rows = (JournalLine.objects
            .filter(entry__business=business, account_id__in=ids,
                    entry__transaction_date__gte=start,
                    entry__transaction_date__lte=end)
            .values("entry__transaction_date", "account_id")
            .annotate(d=Sum("debit"), c=Sum("credit")))
    for row in rows:
        net = _q((row["d"] or ZERO) - (row["c"] or ZERO))
        key = "cash" if (cash_acc and row["account_id"] == cash_acc.id) \
            else "wallet"
        out[row["entry__transaction_date"]][key] += net
    return out


def opening_balances(business, start: date) -> tuple[Decimal, Decimal]:
    """Cash and wallet balances the day before the period starts."""
    prev = date.fromordinal(start.toordinal() - 1)
    return cash_balance(business, prev), wallet_balance(business, prev)


def position_rows(business, start: date, end: date) -> list[dict]:
    """One row per day with the real money position at the end of that day.

    ``closing_cash`` is what the drawer should hold (the ledger's own
    answer, including baki collected in cash); ``wallet_closing`` is the
    bKash/Nagad/bank money, which is never in the drawer.
    """
    deltas = ledger_deltas(business, start, end)
    cash, wallet = opening_balances(business, start)
    rows = []
    day = start
    while day <= end:
        values = deltas.get(day, {})
        cash = _q(cash + values.get("cash", ZERO))
        wallet = _q(wallet + values.get("wallet", ZERO))
        rows.append({"date": day, "closing_cash": cash,
                     "wallet_closing": wallet})
        day = date.fromordinal(day.toordinal() + 1)
    return rows


def cash_balance(business, as_of: date | None = None) -> Decimal:
    """Cash in the drawer (account 1000) — the ledger's own answer."""
    return ledger_balance(business, CASH, as_of)


def wallet_balance(business, as_of: date | None = None) -> Decimal:
    """bKash / Nagad / bank money (account 1100) — not in the drawer."""
    return ledger_balance(business, WALLET, as_of)


def real_cash_daily(business, start: date, end: date) -> list[dict]:
    """Day by day *real* movement of the drawer and the wallets.

    The general cash-flow report counts every journal line, so a reversed
    entry shows up as a huge bar in **both** directions (the original plus
    the reversal). That is correct for a statement but very confusing for
    a shop owner, so this helper only counts money that really moved:

    * entries of a transaction that is still ``COMPLETED`` are counted,
    * entries of a reversed transaction and the reversal entries themselves
      are skipped (they cancel each other out anyway).
    """
    accounts = list(business.accounts.filter(code__in=[CASH, WALLET]))
    if not accounts:
        return []
    account_ids = [a.id for a in accounts]

    entries = (JournalEntry.objects
               .filter(business=business,
                       transaction_date__gte=start,
                       transaction_date__lte=end,
                       lines__account_id__in=account_ids)
               .distinct())

    completed_ids = set(Transaction.objects
                         .filter(business=business,
                                 status=TransactionStatus.COMPLETED)
                         .values_list("id", flat=True))

    rows: dict[date, dict] = {}
    for entry in entries:
        kind = (entry.source_kind or "").upper()
        if kind == "TXN_REVERSAL":
            continue                      # the reversal of a reversed entry
        if kind == "TXN" and entry.source_id not in completed_ids:
            continue                      # the original of a reversed entry
        bucket = rows.setdefault(entry.transaction_date,
                                 {"date": entry.transaction_date,
                                  "inflow": ZERO, "outflow": ZERO})
        for line in entry.lines.filter(account_id__in=account_ids):
            bucket["inflow"] += _q(line.debit)
            bucket["outflow"] += _q(line.credit)

    out = []
    for day in sorted(rows):
        r = rows[day]
        r["inflow"] = _q(r["inflow"])
        r["outflow"] = _q(r["outflow"])
        r["net"] = _q(r["inflow"] - r["outflow"])
        out.append(r)
    return out


def total_due(business) -> Decimal:
    """What the customers owe, live (sum of every assignment's due)."""
    from apps.customers.models import CustomerAssignment
    total = ZERO
    assignments = (CustomerAssignment.objects
                   .filter(customer__business=business, is_active=True)
                   .select_related("customer"))
    for a in assignments:
        total += a.current_due()
    return _q(total)


def last_count(business) -> CashCount | None:
    return (CashCount.objects.filter(business=business)
            .order_by("-counted_on", "-created_at")
            .first())


def future_dated_cash(business, as_of: date | None = None) -> Decimal:
    """Cash movement booked on a date AFTER today.

    The app remembers the last used date, so an entry can end up dated in
    the future; that money is already counted in the drawer balance but
    not in "up to today". The page shows this so the numbers can be read
    correctly (and the date fixed).
    """
    today = as_of or date.today()
    account = _account(business, CASH)
    if account is None:
        return ZERO
    agg = JournalLine.objects.filter(
        entry__business=business, account=account,
        entry__transaction_date__gt=today
    ).aggregate(d=Sum("debit"), c=Sum("credit"))
    return _q((agg["d"] or ZERO) - (agg["c"] or ZERO))


def business_position(business, as_of: date | None = None) -> dict:
    """The shop's real position: what it HAS vs what it OWES.

    * **আমার টাকা** — cash in the drawer, mobile/bank money, what the
      customers owe, and the investors' capital still inside the business.
    * **আমার দেনা** — what has to be given back: the lenders' money, the
      customers' advances and the bills still to pay.
    * **নিট অবস্থান** — the first block minus the second one.

    Every figure comes from the double-entry ledger or from the live
    books, never from a guess.
    """
    from apps.accounting.postings import ADVANCES
    from apps.loans.models import LoanParty
    from apps.loans.service import summarize

    cash = cash_balance(business, as_of)
    wallet = wallet_balance(business, as_of)
    due = total_due(business)

    loans = summarize(business, kind=LoanParty.Kind.LOAN)
    invest = summarize(business, kind=LoanParty.Kind.INVESTMENT)

    advances = _credit_balance(business, ADVANCES)
    payable = _credit_balance(business, "2000")

    # The investors' money is the owner's own money in this business, so it
    # is never a debt and never added again (the cash it became is already
    # counted). It is reported as information only.
    cash_in_hand = _q(cash + wallet)
    have = _q(cash + wallet + due)
    owe = _q(loans["outstanding"] + advances + payable)
    return {
        "cash": cash,
        "wallet": wallet,
        # the money you can touch right now
        "cash_in_hand": cash_in_hand,
        # the money the customers still owe — not in the drawer yet
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


def _credit_balance(business, code: str) -> Decimal:
    """Balance of a liability account (credit-positive)."""
    account = _account(business, code)
    if account is None:
        return ZERO
    agg = JournalLine.objects.filter(
        entry__business=business, account=account).aggregate(
            d=Sum("debit"), c=Sum("credit"))
    return _q((agg["c"] or ZERO) - (agg["d"] or ZERO))


def drawer_cash(business, as_of: date | None = None) -> dict:
    """What the drawer should hold right now, and how it compares with
    the last physical count.

    * ``book_cash`` — the ledger's cash balance (the honest book value).
    * ``projected`` — the last counted amount plus every cash movement
      since that count: what the drawer *should* hold now.
    * ``difference`` — গরমিল/ঘাটতি found at the last count.
    """
    book = cash_balance(business, as_of)
    count = last_count(business)
    out = {
        "book_cash": book,
        "count": count,
        "counted_amount": count.counted_amount if count else None,
        "counted_on": count.counted_on if count else None,
        "difference": count.difference if count else None,
        "note": count.note if count else "",
        "projected": book,
        "has_count": count is not None,
    }
    if count is not None:
        # the book has moved since the count; add that movement to it
        out["projected"] = _q(count.counted_amount + (book - count.book_amount))
    return out


def stock_value(business, on_date: date | None = None) -> Decimal:
    """Value of the goods still in the shop (last snapshot on/before date)."""
    snap = last_stock_snapshot(business, on_date)
    return snap.closing_value if snap else ZERO


def last_stock_snapshot(business, on_date: date | None = None) -> StockSnapshot | None:
    qs = StockSnapshot.objects.filter(business=business)
    if on_date is not None:
        qs = qs.filter(snapshot_on__lte=on_date)
    return qs.order_by("-snapshot_on", "-created_at").first()


def stock_bounds(business, start: date, end: date) -> tuple:
    """Opening and closing stock of a period.

    The opening stock is the last snapshot BEFORE the period (the value
    carried into it) and the closing stock the last one inside the
    period. When either is missing the stock movement is left out — a
    half-known stock must never invent a profit.
    """
    before = (StockSnapshot.objects
              .filter(business=business, snapshot_on__lt=start)
              .order_by("-snapshot_on", "-created_at"))
    inside = (StockSnapshot.objects
              .filter(business=business, snapshot_on__gte=start,
                      snapshot_on__lte=end)
              .order_by("snapshot_on", "created_at"))
    opening = before.first()
    closing = inside.last() or before.first()
    return opening, closing


def profit_breakdown(business, start: date, end: date) -> dict:
    """The real profit of a period.

    ``profit = sales − (goods bought − stock increase) − running costs``

    When the stock is not recorded on both ends of the period the stock
    movement is left out (and flagged) — a half-known stock must never
    invent a profit.
    """
    rows = (Transaction.objects
            .filter(business=business, status=TransactionStatus.COMPLETED,
                    transaction_date__gte=start, transaction_date__lte=end)
            .values("transaction_type", "expense_kind")
            .annotate(total=Sum("amount")))

    sales_cash = sales_due = collections = ZERO
    goods = operating = ZERO
    for r in rows:
        ttype = r["transaction_type"]
        amount = _q(r["total"])
        if ttype == TransactionType.INCOME:
            sales_cash += amount
        elif ttype == TransactionType.CREDIT:
            sales_due += amount
        elif ttype == TransactionType.PAYMENT:
            collections += amount
        elif ttype == TransactionType.EXPENSE:
            if r.get("expense_kind") == ExpenseKind.OPERATING:
                operating += amount
            else:
                goods += amount

    start_stock, end_stock = stock_bounds(business, start, end)
    stock_complete = bool(start_stock and end_stock)
    if stock_complete:
        stock_delta = _q(end_stock.closing_value - start_stock.closing_value)
    else:
        stock_delta = ZERO

    cogs = _q(goods - stock_delta)
    sales = _q(sales_cash + sales_due)
    profit = _q(sales - cogs - operating)
    return {
        "sales_cash": _q(sales_cash),
        "sales_due": _q(sales_due),
        "sales": sales,
        "collections": _q(collections),
        "goods_bought": _q(goods),
        "operating": _q(operating),
        "stock_start": start_stock.closing_value if start_stock else None,
        "stock_end": end_stock.closing_value if end_stock else None,
        "stock_delta": stock_delta,
        "stock_complete": stock_complete,
        "cogs": cogs,
        "profit": profit,
        "margin": (_q(profit / sales * 100) if sales else None),
    }


def integrity_check(business) -> dict:
    """Compare every account with the effect of the live transactions.

    A healthy ledger has, for each account:
    ``ledger balance == effect of live transactions + manual entries``.
    Anything left over is an orphan posting (money that belongs to a
    deleted or unknown record) and is reported so it can be fixed.
    """
    live_ids = list(Transaction.objects.filter(business=business)
                    .values_list("id", flat=True))
    entries = JournalEntry.objects.filter(business=business)
    txn_entries = entries.filter(source_kind__in=("TXN", "TXN_REVERSAL"),
                                 source_id__in=live_ids)
    manual_entries = entries.filter(source_kind__in=("MANUAL",
                                                      "MANUAL_REVERSAL"))

    def effect(qs):
        """Raw debit − credit per account (sign fixed per account below)."""
        out: dict[str, Decimal] = {}
        for row in (JournalLine.objects.filter(entry__in=qs)
                    .values("account_id")
                    .annotate(d=Sum("debit"), c=Sum("credit"))):
            out[row["account_id"]] = _q((row["d"] or ZERO) - (row["c"] or ZERO))
        return out

    txn_effect = effect(txn_entries)
    manual_effect = effect(manual_entries)

    rows = []
    for account in business.accounts.order_by("code"):
        agg = JournalLine.objects.filter(
            entry__business=business, account=account
        ).aggregate(d=Sum("debit"), c=Sum("credit"))
        debit = _q(agg["d"] or ZERO)
        credit = _q(agg["c"] or ZERO)
        if account.type in ("ASSET", "EXPENSE"):
            ledger = _q(debit - credit)
        else:
            ledger = _q(credit - debit)
        raw = _q(txn_effect.get(account.id, ZERO)
                 + manual_effect.get(account.id, ZERO))
        expected = raw if account.type in ("ASSET", "EXPENSE") else _q(-raw)
        difference = _q(ledger - expected)
        rows.append({
            "code": account.code, "name": account.name, "type": account.type,
            "ledger": ledger, "expected": expected, "difference": difference,
        })
    broken = [r for r in rows if abs(r["difference"]) > Decimal("0.005")]
    orphan_entries = (entries
                       .filter(source_kind__in=("TXN", "TXN_REVERSAL"))
                       .exclude(source_id__in=live_ids)
                       .count())
    return {
        "rows": rows,
        "broken": broken,
        "clean": not broken,
        "orphan_entries": orphan_entries,
    }


def unposted_transactions(business) -> list:
    """Live transactions that never got their double-entry journal.

    Two shapes of "missing" count as unposted:

    * no journal entry at all (older records, or a path that skipped
      posting), and
    * a journal entry with **zero lines** — which is what a backup from
      before the fix restores, because the backup never carried the lines.

    (The integrity check compares ledger vs journal lines, so it cannot
    see either of these — this helper can.)
    """
    posted = set(JournalEntry.objects.filter(
        business=business, source_kind="TXN", source_id__isnull=False,
        lines__isnull=False).values_list("source_id", flat=True).distinct())
    return list((Transaction.objects
                 .filter(business=business)
                 .exclude(status=TransactionStatus.REVERSED)
                 .exclude(pk__in=posted)
                 .order_by("transaction_date")
                 .values_list("id", "transaction_type", "amount",
                              "transaction_date")))


def rebuild_ledger_from_transactions(business, user=None) -> int:
    """Post the missing journal for every live transaction.

    Some older records have no journal lines at all — the transaction and
    the books (loan book, dues) show the money, but the double-entry ledger
    never saw it, so "হাতে নগদ" (which is ledger-derived) reads 0 while the
    books disagree. This posts the auto journal for each transaction that
    has none.

    * a transaction that already has its TXN journal is never touched, so
      running this any number of times is safe;
    * reversed transactions are skipped (their net effect is zero);
    * nothing is deleted — this only adds what is missing.
    """
    from apps.accounting.postings import post_auto_journal, ensure_system_accounts

    # System accounts (cash 1000, wallet 1100, AR 1200, etc.) must exist
    # before we can post journals — otherwise post_auto_journal silently
    # does nothing because acc() returns None.
    ensure_system_accounts(business, user=user)

    # Drop restored journal entries that carry **zero lines** (a backup from
    # before the fix restored the entries but never had the lines). They
    # hold no financial effect, so deleting them is safe; the normal posting
    # path then re-creates complete entries.
    empty_ids = [e.id for e in JournalEntry.objects.filter(
        business=business, source_kind="TXN", source_id__isnull=False)
        .annotate(n=Count("lines")).filter(n=0)]
    if empty_ids:
        JournalLine.objects.filter(entry_id__in=empty_ids).delete()
        JournalEntry.objects.filter(id__in=empty_ids).delete()

    fixed = 0
    for txn_id, _ttype, _amount, _tdate in unposted_transactions(business):
        txn = Transaction.objects.filter(pk=txn_id).first()
        if txn is None:
            continue
        try:
            with db_transaction.atomic():
                result = post_auto_journal(txn, user=user)
        except Exception:
            continue   # leave the row alone; the next run can retry it
        if result:
            fixed += 1
    return fixed


def repair_ledger(business, user=None) -> int:
    """Mirror every orphan posting so the ledger matches the transactions.

    Nothing is deleted: a mirrored entry (the same pattern the app uses
    for reversals) is appended, so the audit trail keeps both sides. Each
    orphan is mirrored exactly once.
    """
    from apps.accounting.postings import post_lines

    live_ids = set(Transaction.objects.filter(business=business)
                   .values_list("id", flat=True))
    orphans = (JournalEntry.objects
               .filter(business=business, source_kind="TXN",
                       source_id__isnull=False)
               .exclude(source_id__in=live_ids))
    fixed = 0
    for entry in orphans:
        if JournalEntry.objects.filter(
                business=business, source_kind="TXN_REVERSAL",
                source_id=entry.id).exists():
            continue
        post_lines(
            business, transaction_date=entry.transaction_date,
            lines=[{"account": l.account_id, "debit": l.credit,
                    "credit": l.debit, "note": l.note}
                   for l in entry.lines.order_by("line_no")],
            memo=(f"Orphan posting balanced — no matching transaction "
                  f"({entry.journal_no})"),
            source_kind="TXN_REVERSAL", source_id=entry.id, user=user,
        )
        fixed += 1
    return fixed
