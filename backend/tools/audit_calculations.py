"""পুরো হিসাবের যাচাই (full calculation audit).

এই স্ক্রিপ্ট প্রতিটি সংখ্যা **মূল রেকর্ড থেকে আবার হিসাব করে** (ট্রানজেকশন,
ডিউ এন্ট্রি, জার্নাল লাইন) এবং তারপর অ্যাপের নিজের সার্ভিস ও পেজের সাথে
মিলিয়ে দেখে। কোনো গরমিল থাকলে FAIL প্রিন্ট হয়।

ব্যবহার:
    python tools/audit_calculations.py                 # প্রথম ব্যবসাটি
    python tools/audit_calculations.py <business-uuid> # নির্দিষ্ট ব্যবসা
"""
from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from datetime import date  # noqa: E402
from decimal import Decimal  # noqa: E402

from django.db.models import Sum  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.accounting.models import JournalLine  # noqa: E402
from apps.accounting.reports import balance_sheet, income_statement, trial_balance  # noqa: E402
from apps.cashbook import services as cb  # noqa: E402
from apps.customers.models import CustomerAssignment  # noqa: E402
from apps.loans.models import LoanParty  # noqa: E402
from apps.loans.service import summarize  # noqa: E402
from apps.organizations.models import Business  # noqa: E402
from apps.transactions.models import Transaction, TransactionStatus, TransactionType  # noqa: E402
from apps.web import supercalc as sc  # noqa: E402

OK_COUNT = 0
FAILS: list[str] = []


def check(label, expected, actual, note=""):
    global OK_COUNT
    e = Decimal(str(expected)).quantize(Decimal("0.01"))
    a = Decimal(str(actual)).quantize(Decimal("0.01"))
    if e == a:
        OK_COUNT += 1
        print(f"  OK   {label}: {e:,} {note}")
    else:
        FAILS.append(label)
        print(f"  FAIL {label}: expected {e:,}  got {a:,} {note}")


def check_bool(label, cond, note=""):
    global OK_COUNT
    if cond:
        OK_COUNT += 1
        print(f"  OK   {label} {note}")
    else:
        FAILS.append(label)
        print(f"  FAIL {label} {note}")


def fmt(value):
    v = Decimal(str(value)).quantize(Decimal("0.01"))
    s = f"{abs(v):,.2f}"
    return ("-৳" + s) if v < 0 else ("৳" + s)


def run(biz_id: str | None = None) -> int:
    if biz_id:
        b = Business.objects.get(pk=biz_id)
    else:
        b = Business.objects.order_by("created_at").first()
    if b is None:
        print("No business found.")
        return 1
    print("=" * 74)
    print(f"BUSINESS AUDIT — {b.name} ({b.id})")
    print("=" * 74)

    today = timezone.localdate()
    first = today.replace(day=1)
    done = list(Transaction.objects.filter(
        business=b, status=TransactionStatus.COMPLETED))

    def ledger_bal(code):
        acc = b.accounts.filter(code=code).first()
        if not acc:
            return Decimal("0.00")
        agg = JournalLine.objects.filter(
            entry__business=b, account=acc).aggregate(d=Sum("debit"), c=Sum("credit"))
        return (agg["d"] or Decimal(0)) - (agg["c"] or Decimal(0))

    # ---------------------------------------------------------- 1) ledger
    print("\n1) LEDGER INTEGRITY")
    tb = trial_balance(b, from_date="1900-01-01", to_date="2099-12-31")
    dr = sum((Decimal(str(r["debit_balance"])) for r in tb["rows"]
               if Decimal(str(r["debit_balance"])) > 0), Decimal("0"))
    cr = sum((Decimal(str(r["credit_balance"])) for r in tb["rows"]
               if Decimal(str(r["credit_balance"])) > 0), Decimal("0"))
    check("trial balance debits == credits", dr, cr)
    ic = cb.integrity_check(b)
    check_bool("every account matches the live transactions",
               ic.get("clean") is True, f"(broken: {ic.get('broken')})")

    bs = balance_sheet(b, as_of=today)
    assets = sum((Decimal(str(x["amount"])) for x in bs["assets"]), Decimal("0"))
    liab = sum((Decimal(str(x["amount"])) for x in bs["liabilities"]), Decimal("0"))
    eq = sum((Decimal(str(x["amount"])) for x in bs["equity"]), Decimal("0"))
    retained = Decimal(str(bs.get("net_income_retained") or 0))
    check("balance sheet: assets == liabilities + equity + retained income",
          assets, liab + eq + retained)

    # ---------------------------------------------------------- 2) cash
    print("\n2) CASH / WALLET")
    cash_in = {TransactionType.INCOME, TransactionType.PAYMENT,
               TransactionType.INVEST, TransactionType.BORROW}
    cash_out = {TransactionType.EXPENSE, TransactionType.REPAY,
                TransactionType.WITHDRAW}
    raw_cash = Decimal("0.00")
    raw_wallet = Decimal("0.00")
    for t in done:
        sign = Decimal("0")
        if t.transaction_type in cash_in:
            sign = t.amount
        elif t.transaction_type in cash_out:
            sign = -t.amount
        if not sign:
            continue
        if t.payment_method == "CASH":
            raw_cash += sign
        else:
            raw_wallet += sign
    check("drawer cash: ledger == raw cash entries", raw_cash, ledger_bal("1000"))
    check("drawer cash: ledger == cashbook service", ledger_bal("1000"),
          cb.cash_balance(b))
    check("wallet: ledger == raw wallet entries", raw_wallet, ledger_bal("1100"))
    pos = cb.business_position(b)
    check("position.have == cash + wallet + due", pos["have"],
          pos["cash"] + pos["wallet"] + pos["due"])
    check("position.owe == loan + advance + payable", pos["owe"],
          pos["loan_outstanding"] + pos["advances"] + pos["payable"])
    check("position.net == have - owe", pos["net"], pos["have"] - pos["owe"])
    check("cash_in_hand == cash + wallet", pos["cash_in_hand"],
          pos["cash"] + pos["wallet"])

    # ---------------------------------------------------------- 3) dues
    print("\n3) DUES (বাকি)")
    raw_due = Decimal("0.00")
    mismatch = 0
    for a in CustomerAssignment.objects.filter(customer__business=b, is_active=True):
        calc = (Decimal(str(a.opening_due)) - Decimal(str(a.advance))
                + sum((e.signed_amount() for e in a.due_entries.all()), Decimal("0")))
        if calc != a.current_due():
            mismatch += 1
        if a.current_due() > 0:
            raw_due += a.current_due()
    check_bool("every assignment due recomputes exactly", mismatch == 0,
               f"({mismatch} mismatches)")
    check("total due: assignments == cashbook service", raw_due, cb.total_due(b))
    check("total due: assignments == position.due", raw_due, pos["due"])

    # ---------------------------------------------------------- 4) profit
    print("\n4) PROFIT (super calculator + dashboard)")
    def total_of(ttype, start=first, end=today):
        return sum((t.amount for t in done if t.transaction_type == ttype
                    and start <= t.transaction_date <= end), Decimal("0"))

    sell_cash = total_of(TransactionType.INCOME)
    sell_due = total_of(TransactionType.CREDIT)
    khoroch = total_of(TransactionType.EXPENSE)
    collect = total_of(TransactionType.PAYMENT)
    tot = sc.calc_totals(b, first, today)
    check("supercalc: sell_cash", sell_cash, tot["sell_cash"])
    check("supercalc: sell_due", sell_due, tot["sell_due"])
    check("supercalc: sell_total", sell_cash + sell_due, tot["sell_total"])
    check("supercalc: khoroch", khoroch, tot["khoroch"])
    check("supercalc: collection", collect, tot["collection"])
    check("supercalc: profit == sell - khoroch", sell_cash + sell_due - khoroch,
          tot["profit"])
    rows = sc.calc_day_rows(b, first, today)
    check("supercalc: day rows sum to the period totals", tot["sell_total"],
          sum((r["sell_total"] for r in rows), Decimal("0")))
    run_sum = Decimal("0")
    run_bad = 0
    for r in rows:
        run_sum += r["profit"]
        if run_sum != r["running_profit"]:
            run_bad += 1
    check_bool("supercalc: running profit is a correct cumulative sum", run_bad == 0)
    isec = income_statement(b, from_date=first, to_date=today)
    check("dashboard revenue == supercalc sell_total",
          Decimal(str(isec["total_revenue"])), tot["sell_total"])
    check("dashboard expenses == supercalc khoroch",
          Decimal(str(isec["total_expenses"])), tot["khoroch"])
    check("dashboard net profit == revenue - expenses",
          Decimal(str(isec["net_profit"])),
          Decimal(str(isec["total_revenue"])) - Decimal(str(isec["total_expenses"])))
    pb = cb.profit_breakdown(b, first, today)
    check("summary profit == supercalc profit", tot["profit"], pb["profit"])

    # ---------------------------------------------------------- 5) books
    print("\n5) LOAN / INVESTMENT BOOKS")
    loan = summarize(b, kind=LoanParty.Kind.LOAN)
    inv = summarize(b, kind=LoanParty.Kind.INVESTMENT)
    check("loan outstanding == ledger balance", loan["outstanding"],
          -ledger_bal("2400"))
    check("investor capital == ledger balance", inv["outstanding"],
          -ledger_bal("3200"))
    check("loan book: received - returned == outstanding",
          loan["received"] - loan["returned"], loan["outstanding"])
    check("invest book: received - returned == outstanding",
          inv["received"] - inv["returned"], inv["outstanding"])

    # ---------------------------------------------------------- 6) pages
    print("\n6) THE PAGES REALLY SHOW THESE NUMBERS")
    from django.test import Client
    from apps.users.models import User

    user = User.objects.filter(username="shagor").first() or User.objects.first()
    c = Client()
    c.force_login(user)
    s = c.session
    s["active_business_id"] = str(b.id)
    s["ui_lang"] = "bn"
    s.save()

    def page(url):
        r = c.get(url, HTTP_HOST="127.0.0.1")
        assert r.status_code == 200, (url, r.status_code)
        return r.content.decode("utf-8")

    summary_html = page(f"/summary/?from={first}&to={today}")
    dash_html = page("/")
    sc_html = page(f"/supercalculator/?preset=custom&from={first}&to={today}")
    check_bool("summary page shows the net position", fmt(pos["net"]) in summary_html)
    check_bool("summary page shows the cash in hand",
               fmt(pos["cash_in_hand"]) in summary_html)
    check_bool("summary page shows the due", fmt(pos["due"]) in summary_html)
    check_bool("dashboard shows the cash in hand",
               fmt(pos["cash_in_hand"]) in dash_html)
    check_bool("dashboard shows the total due", fmt(pos["due"]) in dash_html)
    check_bool("dashboard shows the month revenue", fmt(tot["sell_total"]) in dash_html)
    check_bool("dashboard shows the month expenses", fmt(tot["khoroch"]) in dash_html)
    check_bool("super calculator shows the total sell",
               fmt(tot["sell_total"]) in sc_html)
    check_bool("super calculator shows the profit", fmt(tot["profit"]) in sc_html)
    check_bool("super calculator shows the collection",
               fmt(tot["collection"]) in sc_html)

    print("\n" + "=" * 74)
    print(f"RESULT: {OK_COUNT} checks OK, {len(FAILS)} FAILED")
    if FAILS:
        print("FAILED:", ", ".join(FAILS))
    print("=" * 74)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else None))
