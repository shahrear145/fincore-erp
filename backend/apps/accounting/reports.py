"""Accounting reports (Phase 5) — all run on `transaction_date` (never
created_at) and only on posted journal lines.

Every report is computed from inclusive balance-difference arithmetic:
  balance_in_range = Σ(upto `to`) − Σ(before `from`)
so opening balances and period movements are always exact Decimals.

Accounting equation check (balance sheet):
  Assets == Liabilities + Equity + Retained Net Income
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum

from apps.accounting.models import (
    DEBIT_NORMAL_TYPES, Account, AccountType, JournalLine,
)
from apps.accounting.postings import CASH, WALLET

CENT = Decimal("0.01")


def _balances(business, upto=None, before=None, from_date=None, to_date=None):
    """{(account_id): (Σdebit, Σcredit)} within optional filters."""
    qs = JournalLine.objects.filter(entry__business=business)
    if upto is not None:
        qs = qs.filter(entry__transaction_date__lte=upto)
    if before is not None:
        qs = qs.filter(entry__transaction_date__lt=before)
    if from_date is not None:
        qs = qs.filter(entry__transaction_date__gte=from_date)
    if to_date is not None:
        qs = qs.filter(entry__transaction_date__lte=to_date)
    agg = qs.values("account").annotate(
        debit=Sum("debit"), credit=Sum("credit"))
    return {row["account"]: (
        Decimal(str(row["debit"] or "0")),
        Decimal(str(row["credit"] or "0"))) for row in agg}


def _natural(account, debit: Decimal, credit: Decimal) -> Decimal:
    if account.type in DEBIT_NORMAL_TYPES:
        return debit - credit
    return credit - debit


def _fmt(value: Decimal) -> str:
    return str(value.quantize(CENT))


# ---------------------------------------------------------------- reports ---

def trial_balance(business, from_date=None, to_date=None):
    """Range movement = cumulative-up-to(`to`) − cumulative-before(`from`)."""
    inner = _balances(business, upto=to_date)
    outer = (_balances(business, before=from_date)
             if from_date else {})

    rows = []
    total_dr = Decimal("0.00")
    total_cr = Decimal("0.00")
    for account in business.accounts.order_by("code"):
        d2, c2 = inner.get(account.id, (Decimal("0"), Decimal("0")))
        d1, c1 = outer.get(account.id, (Decimal("0"), Decimal("0")))
        d = d2 - d1
        c = c2 - c1
        if d == 0 and c == 0:
            continue
        net = (d - c).quantize(CENT)  # positive ⇒ debit column
        rows.append({
            "code": account.code, "name": account.name, "type": account.type,
            "debit_balance": _fmt(net) if net > 0 else "0.00",
            "credit_balance": _fmt(-net) if net < 0 else "0.00",
        })
        total_dr += net if net > 0 else Decimal("0")
        total_cr += -net if net < 0 else Decimal("0")
    total_dr = total_dr.quantize(CENT)
    total_cr = total_cr.quantize(CENT)
    return {
        "from": from_date, "to": to_date,
        "rows": rows,
        "total_debits": _fmt(total_dr),
        "total_credits": _fmt(total_cr),
        "balanced": total_dr == total_cr,
    }


def income_statement(business, from_date=None, to_date=None):
    # Range movement = cumulative-up-to(`to`) - cumulative-before(`from`),
    # exactly like trial_balance. Using `from_date` in the inner query too
    # would subtract the opening balance twice and turn revenue negative
    # whenever the business has postings before the chosen range.
    inner = _balances(business, upto=to_date)
    outer = (_balances(business, before=from_date) if from_date else {})

    revenues, expenses = [], []
    total_rev = Decimal("0.00")
    total_exp = Decimal("0.00")
    for account in business.accounts.order_by("code"):
        if account.type not in (AccountType.INCOME, AccountType.EXPENSE):
            continue
        d2, c2 = inner.get(account.id, (Decimal("0"), Decimal("0")))
        d1, c1 = outer.get(account.id, (Decimal("0"), Decimal("0")))
        d = d2 - d1
        c = c2 - c1
        if account.type == AccountType.INCOME:
            amt = (c - d).quantize(CENT)
            if amt or d or c:
                revenues.append({"code": account.code,
                                 "name": account.name,
                                 "amount": _fmt(amt)})
                total_rev += amt
        else:
            amt = (d - c).quantize(CENT)
            if amt or d or c:
                expenses.append({"code": account.code,
                                 "name": account.name,
                                 "amount": _fmt(amt)})
                total_exp += amt
    total_rev = total_rev.quantize(CENT)
    total_exp = total_exp.quantize(CENT)
    return {
        "from": from_date, "to": to_date,
        "revenues": revenues,
        "expenses": expenses,
        "total_revenue": _fmt(total_rev),
        "total_expenses": _fmt(total_exp),
        "net_profit": _fmt(total_rev - total_exp),
    }


def balance_sheet(business, as_of=None):
    upto_bal = _balances(business, upto=as_of)

    assets, liabilities, equity = [], [], []
    total_a = Decimal("0.00")
    total_l = Decimal("0.00")
    total_e = Decimal("0.00")
    for account in business.accounts.order_by("code"):
        d, c = upto_bal.get(account.id, (Decimal("0"), Decimal("0")))
        if d == 0 and c == 0:
            continue
        val = _natural(account, d, c).quantize(CENT)
        if account.type == AccountType.ASSET:
            assets.append({"code": account.code, "name": account.name,
                           "amount": _fmt(val)})
            total_a += val
        elif account.type == AccountType.LIABILITY:
            liabilities.append({"code": account.code, "name": account.name,
                                "amount": _fmt(val)})
            total_l += val
        elif account.type == AccountType.EQUITY:
            equity.append({"code": account.code, "name": account.name,
                           "amount": _fmt(val)})
            total_e += val

    isec = income_statement(business, from_date=None, to_date=as_of)
    net_income = Decimal(isec["net_profit"])

    total_a = total_a.quantize(CENT)
    total_l = total_l.quantize(CENT)
    total_e = total_e.quantize(CENT)
    right_side = (total_l + total_e + net_income).quantize(CENT)
    return {
        "as_of": as_of,
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "net_income_retained": _fmt(net_income),
        "total_assets": _fmt(total_a),
        "total_liabilities": _fmt(total_l),
        "total_equity": _fmt(total_e),
        "liabilities_plus_equity": _fmt(right_side),
        "balanced": total_a == right_side,
    }


def cash_flow(business, from_date=None, to_date=None):
    """Money in/out of cash & bank/wallet accounts over the range."""
    wallets = list(business.accounts.filter(
        code__in=[CASH, WALLET]).order_by("code"))

    opening_total = Decimal("0.00")
    total_in = Decimal("0.00")
    total_out = Decimal("0.00")
    per_wallet = []
    opening_balances = _balances(business, before=from_date) if from_date \
        else {}
    period_balances = _balances(business, from_date=from_date,
                                to_date=to_date)
    for account in wallets:
        d0, c0 = opening_balances.get(account.id,
                                      (Decimal("0"), Decimal("0")))
        opening = (d0 - c0).quantize(CENT)  # asset: debit-positive
        d, c = period_balances.get(account.id, (Decimal("0"), Decimal("0")))
        inflow = d.quantize(CENT)
        outflow = c.quantize(CENT)
        closing = (opening + inflow - outflow).quantize(CENT)
        per_wallet.append({
            "code": account.code, "name": account.name,
            "opening": _fmt(opening), "inflow": _fmt(inflow),
            "outflow": _fmt(outflow), "closing": _fmt(closing),
        })
        opening_total += opening
        total_in += inflow
        total_out += outflow

    # Daily net-on-wallet series (money movement per business date).
    daily_rows = []
    if wallets:
        wallet_ids = [a.id for a in wallets]
        qs = JournalLine.objects.filter(
            entry__business=business, account_id__in=wallet_ids)
        if from_date:
            qs = qs.filter(entry__transaction_date__gte=from_date)
        if to_date:
            qs = qs.filter(entry__transaction_date__lte=to_date)
        rows = list(qs.values("entry__transaction_date")
                    .annotate(d=Sum("debit"), c=Sum("credit"))
                    .order_by("entry__transaction_date"))
        running = opening_total
        for row in rows:
            d = Decimal(str(row["d"] or "0"))
            c = Decimal(str(row["c"] or "0"))
            net = (d - c).quantize(CENT)
            running = (running + net).quantize(CENT)
            daily_rows.append({
                "date": row["entry__transaction_date"],
                "inflow": _fmt(d), "outflow": _fmt(c),
                "net": _fmt(net), "closing_wallet_balance": _fmt(running),
            })

    net = (total_in - total_out).quantize(CENT)
    return {
        "from": from_date, "to": to_date,
        "wallets": per_wallet,
        "total_money_in": _fmt(total_in),
        "total_money_out": _fmt(total_out),
        "net_change": _fmt(net),
        "daily": daily_rows,
        "balanced": (opening_total + net) == sum(
            Decimal(w["closing"]) for w in per_wallet),
    }


def account_ledger(business, account, from_date=None, to_date=None):
    """One account's statement (general ledger) with a running balance in the
    account's natural sign."""
    if account.business_id != business.id:
        raise ValueError("Account not in this business.")
    opening_bal = _balances(business, before=from_date) if from_date else {}
    d0, c0 = opening_bal.get(account.id, (Decimal("0"), Decimal("0")))
    running = _natural(account, d0, c0).quantize(CENT)

    qs = JournalLine.objects.filter(
        entry__business=business, account=account)
    if from_date:
        qs = qs.filter(entry__transaction_date__gte=from_date)
    if to_date:
        qs = qs.filter(entry__transaction_date__lte=to_date)
    qs = qs.select_related("entry").order_by(
        "entry__transaction_date", "entry__created_at", "line_no")

    rows = []
    credit_normal = account.type not in DEBIT_NORMAL_TYPES
    for line in qs:
        entry = line.entry
        effect = ((line.debit - line.credit) if not credit_normal
                  else (line.credit - line.debit))
        running = (running + effect).quantize(CENT)
        rows.append({
            "date": entry.transaction_date,
            "journal_no": entry.journal_no,
            "memo": entry.memo,
            "source_kind": entry.source_kind,
            "debit": _fmt(line.debit), "credit": _fmt(line.credit),
            "note": line.note,
        })
    return {
        "account": {"code": account.code, "name": account.name,
                    "type": account.type},
        "from": from_date, "to": to_date,
        "opening_balance": _fmt(_natural(account, d0, c0).quantize(CENT))
        if from_date else "0.00",
        "rows": rows,
        "closing_balance": _fmt(running),
    }
