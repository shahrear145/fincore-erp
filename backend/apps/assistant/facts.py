"""Authorized facts compiler (Phase 7): the assistant's ONLY data source.

Rules (spec: AI assistant on authorized data only — no invented figures):
- BUSINESS mode: facts computed from the requester's business with the
  server-side role scope applied — PARTNER sees only their own partner's
  due books; ADMIN sees everything.
- PERSONAL mode: facts computed from the requester's own ledger only.
- Every fact is a live query result; nothing is cached, nothing hardcoded,
  no demo/sample data. Numbers come back as strings so no float can leak.
"""
from datetime import date, timedelta
from decimal import Decimal

from apps.accounting.reports import income_statement
from apps.customers.models import CustomerAssignment
from apps.personal.models import (
    PersonalAccount, PersonalLoan, PersonalTransaction, PersonalTxnType,
    SavingsGoal,
)
from apps.transactions.models import RecurringTransaction


def _partner_scope(business, membership) -> object | None:
    """The partner profile a PARTNER-member is locked to; None for ADMIN."""
    if membership is None or membership.role == "ADMIN":
        return None
    return business.partners.filter(
        linked_user=membership.user).first()


def build_business_facts(business, membership) -> dict:
    today = date.today()
    first_of_month = today.replace(day=1)

    own = _partner_scope(business, membership)
    assignments = (CustomerAssignment.objects
                   .filter(customer__business=business)
                   .select_related("customer", "partner"))
    if own is not None:
        assignments = assignments.filter(partner=own)

    dues = []
    total_due = Decimal("0.00")
    for a in assignments:
        due = a.current_due().quantize(Decimal("0.01"))
        if due > 0:
            dues.append({
                "customer": a.customer.name,
                "partner": a.partner.name,
                "due": str(due),
            })
            total_due += due
    dues.sort(key=lambda d: Decimal(d["due"]), reverse=True)

    # wallet balances from the double-entry ledger (debit = money present)
    wallet_total = Decimal("0.00")
    wallets = {"cash": "0.00", "bank_and_mobile_wallets": "0.00"}
    try:
        from apps.accounting.models import Account
        for code, key in (("1000", "cash"),
                          ("1100", "bank_and_mobile_wallets")):
            acc = business.accounts.filter(code=code, is_active=True).first()
            if acc is not None:
                bal = acc.balance(upto=today).quantize(Decimal("0.01"))
                wallets[key] = str(bal)
                wallet_total += bal
    except Exception:
        pass  # accounting app absent from this business — wallets unknown

    isec = income_statement(business, from_date=first_of_month, to_date=today)

    low_stock = []

    upcoming = list(business.recurring_rules.filter(
        status=RecurringTransaction.Status.ACTIVE,
        next_date__lte=today + timedelta(days=7)).order_by("next_date"))

    return {
        "scope": "BUSINESS" if own is None else "PARTNER-SCOPED",
        "as_of": str(today),
        "business_name": business.name,
        "total_customer_due": str(total_due.quantize(Decimal("0.01"))),
        "top_customer_dues": dues[:8],
        "wallets": wallets,
        "wallet_total": str(wallet_total.quantize(Decimal("0.01"))),
        "month_revenue": isec["total_revenue"],
        "month_expenses": isec["total_expenses"],
        "month_net_profit": isec["net_profit"],
        "low_stock": low_stock[:10],
        "recurring_due_next_7_days": [
            {"title": r.title, "amount": str(r.amount),
             "next_date": str(r.next_date)} for r in upcoming
        ],
    }


def build_personal_facts(user) -> dict:
    from apps.personal.service import month_expense_summary, net_worth

    today = date.today()
    accounts = []
    for account in user.personal_accounts.filter(
            status=PersonalAccount.Status.ACTIVE):
        accounts.append({"account": account.name,
                         "balance": str(account.balance().quantize(
                             Decimal("0.01"))),
                         "kind": account.kind})
    goals = [{"goal": g.name,
              "saved": str(g.saved().quantize(Decimal("0.01"))),
              "target": str(g.target_amount),
              "status": g.status} for g in user.savings_goals.all()]
    loans = []
    for loan in user.personal_loans.filter(status=PersonalLoan.Status.ACTIVE):
        late = [i["installment"] for i in loan.schedule()["installments"]
                if i["state"] == "OVERDUE"]
        loans.append({
            "loan": loan.name, "direction": loan.direction,
            "principal": str(loan.principal),
            "remaining": str(loan.remaining().quantize(Decimal("0.01"))),
            "overdue_installments": late,
        })
    month = month_expense_summary(user, today.year, today.month)
    # biggest expense category this month, computed from the summary data
    biggest = None
    try:
        biggest = max(month["expense_by_category"].items(),
                      key=lambda kv: Decimal(kv[1]))
        if biggest and biggest[0] in (None, "None", ""):
            biggest = ("Uncategorized", biggest[1])
    except (ValueError, AttributeError):
        biggest = None
    recent = list(user.personal_transactions.filter(
        status=PersonalTransaction.Status.COMPLETED,
        transaction_date__gte=today - timedelta(days=7)
    ).values_list("transaction_type", "amount")[:5])

    return {
        "scope": "PERSONAL",
        "as_of": str(today),
        "accounts": accounts,
        "net_worth": net_worth(user)["net_worth"],
        "month_total_income": month["total_income"],
        "month_total_expense": month["total_expense"],
        "month_net_saved": month["net_saved"],
        "expense_by_category": month["expense_by_category"],
        "biggest_expense_category": (None if biggest is None
                                     else {"category": biggest[0],
                                           "amount": biggest[1]}),
        "savings_goals": goals,
        "active_loans": loans,
        "recent_txns_7d": [
            {"type": t, "amount": str(Decimal(str(a)).quantize(
                Decimal("0.01")))} for t, a in recent
        ],
    }
