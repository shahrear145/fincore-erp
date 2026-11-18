"""Auto-posting engine (Phase 5): domain events → balanced journal entries.

Mapping (system control accounts, seeded lazily per business):
  Txn CREDIT (customer due)     Dr 1200 AR           Cr 4000 Sales Revenue
  Txn PAYMENT (collection)      Dr wallet(1000/1100) Cr 1200 AR
  Txn ADVANCE                   Dr wallet            Cr 2100 Customer Advances
  Txn REFUND                    Dr 2100 advances     Cr wallet
  Txn INCOME                    Dr wallet            Cr 4100 Other Income
  Txn EXPENSE                   Dr 5100 Expenses     Cr wallet
  Txn BORROW (loan taken)        Dr wallet            Cr 2400 Loans Payable
  Txn REPAY (loan repaid)        Dr 2400 Loans Payable Cr wallet
  Txn INVEST (capital received)  Dr wallet            Cr 3200 Investor Capital
  Txn WITHDRAW (capital back)    Dr 3200 Investor Cap Cr wallet
  Txn TRANSFER                  Dr destination wallet Cr source wallet
  Txn ADJUSTMENT UP             Dr 1200 AR           Cr 3000 Owner's Equity
  Txn ADJUSTMENT DOWN           Dr 3000 equity       Cr 1200 AR
Every reversal mirrors the original lines (reversal_of link).
"""
from decimal import Decimal

from apps.accounting.models import Account, JournalEntry
from apps.accounting.service import create_journal_entry

SYSTEM_ACCOUNTS = [
    ("1000", "Cash on Hand", "ASSET"),
    ("1100", "Bank & Mobile Wallets", "ASSET"),
    ("1200", "Accounts Receivable", "ASSET"),
    ("2100", "Customer Advances", "LIABILITY"),
    ("2400", "Loans Payable", "LIABILITY"),
    ("3000", "Owner's Equity", "EQUITY"),
    ("3200", "Investor Capital", "EQUITY"),
    ("4000", "Sales Revenue", "INCOME"),
    ("4100", "Other Income", "INCOME"),
    ("5100", "Business Expenses", "EXPENSE"),
]

CASH = "1000"; WALLET = "1100"; AR = "1200"
ADVANCES = "2100"; LOANS_PAYABLE = "2400"; EQUITY = "3000"
INVESTOR_CAPITAL = "3200"
SALES_REV = "4000"; OTHER_INCOME = "4100"; EXPENSES = "5100"


def ensure_system_accounts(business, user=None):
    have = set(business.accounts.filter(is_system=True).values_list(
        "code", flat=True))
    for code, name, ttype in SYSTEM_ACCOUNTS:
        if code not in have:
            Account.objects.get_or_create(
                business=business, code=code,
                defaults={"name": name, "type": ttype, "is_system": True,
                          "created_by": user},
            )
    return business


def acc(business, code) -> Account:
    account = business.accounts.filter(code=code).first()
    if account is None:
        ensure_system_accounts(business)
        account = business.accounts.get(code=code)
    return account


def wallet_code(payment_method) -> str:
    return CASH if payment_method == "CASH" else WALLET


def post_lines(business, *, transaction_date, lines, memo="",
               source_kind, source_id=None, user=None):
    return create_journal_entry(
        business, transaction_date=transaction_date, lines=lines,
        memo=memo, source_kind=source_kind, source_id=source_id,
        user=user, check_locked=False, record=True,
    )


def mirror_entry(business, entry, *, memo, user=None, source_kind=None):
    """Append a mirrored (reversal) entry of `entry`."""
    if entry.reversals.exists():
        raise RuntimeError(f"Journal {entry.journal_no} already reversed.")
    return post_lines(
        business, transaction_date=entry.transaction_date,
        lines=[{"account": l.account_id, "debit": l.credit,
                "credit": l.debit, "note": l.note}
               for l in entry.lines.order_by("line_no")],
        memo=memo, source_kind=source_kind or f"{entry.source_kind}_REVERSAL"
        if entry.source_kind not in ("", "MANUAL") else "MANUAL_REVERSAL",
        source_id=entry.source_id, user=user,
    )


# --- Transaction events ----------------------------------------------------

def post_auto_journal(txn, user=None):
    """Journal for one customer/business transaction (called inside the
    transaction writer's atomic block)."""
    business = txn.business
    t = txn.transaction_type
    date = txn.transaction_date
    memo = txn.description or f"{t} transaction"

    if t == "CREDIT":
        lines = [
            {"account": acc(business, AR), "debit": txn.amount},
            {"account": acc(business, SALES_REV), "credit": txn.amount},
        ]
    elif t == "PAYMENT":
        lines = [
            {"account": acc(business, wallet_code(txn.payment_method)),
             "debit": txn.amount},
            {"account": acc(business, AR), "credit": txn.amount},
        ]
    elif t == "ADVANCE":
        lines = [
            {"account": acc(business, wallet_code(txn.payment_method)),
             "debit": txn.amount},
            {"account": acc(business, ADVANCES), "credit": txn.amount},
        ]
    elif t == "REFUND":
        lines = [
            {"account": acc(business, ADVANCES), "debit": txn.amount},
            {"account": acc(business, wallet_code(txn.payment_method)),
             "credit": txn.amount},
        ]
    elif t == "INCOME":
        lines = [
            {"account": acc(business, wallet_code(txn.payment_method)),
             "debit": txn.amount},
            {"account": acc(business, OTHER_INCOME), "credit": txn.amount},
        ]
    elif t == "EXPENSE":
        lines = [{"account": acc(business, EXPENSES), "debit": txn.amount},
                 {"account": acc(business, wallet_code(txn.payment_method)),
                  "credit": txn.amount}]
    elif t == "BORROW":
        # Loan taken: cash in, liability up.
        lines = [{"account": acc(business, wallet_code(txn.payment_method)),
                  "debit": txn.amount},
                 {"account": acc(business, LOANS_PAYABLE), "credit": txn.amount}]
    elif t == "REPAY":
        # Loan repaid: liability down, cash out.
        lines = [{"account": acc(business, LOANS_PAYABLE), "debit": txn.amount},
                 {"account": acc(business, wallet_code(txn.payment_method)),
                  "credit": txn.amount}]
    elif t == "INVEST":
        # Investor capital received: cash in, equity up.
        lines = [{"account": acc(business, wallet_code(txn.payment_method)),
                  "debit": txn.amount},
                 {"account": acc(business, INVESTOR_CAPITAL),
                  "credit": txn.amount}]
    elif t == "WITHDRAW":
        # Investor took capital back: equity down, cash out.
        lines = [{"account": acc(business, INVESTOR_CAPITAL),
                  "debit": txn.amount},
                 {"account": acc(business, wallet_code(txn.payment_method)),
                  "credit": txn.amount}]
    elif t == "TRANSFER":
        src = acc(business, wallet_code(txn.payment_method))
        dst = acc(business, wallet_code(txn.to_payment_method))
        lines = [{"account": dst, "debit": txn.amount},
                 {"account": src, "credit": txn.amount}]
    elif t == "ADJUSTMENT":
        if txn.adjustment_direction == "UP":
            lines = [{"account": acc(business, AR), "debit": txn.amount},
                     {"account": acc(business, EQUITY), "credit": txn.amount}]
        else:
            lines = [{"account": acc(business, EQUITY), "debit": txn.amount},
                     {"account": acc(business, AR), "credit": txn.amount}]
    else:
        return None

    return post_lines(business, transaction_date=date, lines=lines,
                      memo=memo, source_kind="TXN", source_id=txn.id,
                      user=user)


def post_reversal_for_transaction(txn, user=None):
    """Mirror the TXN journal when a transaction is reversed."""
    business = txn.business
    entry = JournalEntry.objects.filter(
        business=business, source_kind="TXN", source_id=txn.id,
        reversal_of__isnull=True).first()
    if entry is None:
        return None
    return mirror_entry(business, entry,
                        memo=f"Reversal of transaction "
                             f"{str(txn.id)[:8]} — {txn.description}",
                        user=user)


# --- Invoice events ---------------------------------------------------------





