"""Personal finance service â€” the validated writer for all money movement.

Rules enforced server-side (never by the UI):
- Every query/writer is scoped to request.user ONLY â€” personal ledgers of
  two users can never touch each other (isolation test in the suite).
- Overdrawing a source account (EXPENSE / TRANSFER / contribution / loan
  payment) is rejected with the available balance named.
- TRANSFER requires two distinct active accounts of the same owner.
- Amounts are exact Decimals (quantized to paisa) â€” no float anywhere.
- client_reference idempotency for offline sync; reversal sets status
  REVERSED (mirror-free: balances simply stop counting the row) + audit.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction as db_transaction
from django.db.models import Sum

from apps.core.middleware import record_audit
from apps.personal.models import (
    PersonalAccount, PersonalCategory, PersonalTransaction,
    PersonalTxnType, SavingsGoal, PersonalLoan,
)

CENT = Decimal("0.01")


class PersonalServiceError(Exception):
    def __init__(self, message, status_code=400, errors=None):
        if isinstance(message, dict):
            # dict-as-message means structured field errors (API shape)
            errors = errors or message
            message = ""
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or {}


def _money(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except Exception:
        raise PersonalServiceError("amount must be a valid decimal number.")


def _own_account(user, account_id):
    account = user.personal_accounts.filter(pk=account_id).first()
    if account is None:
        # never leaks existence of another user's account (isolation)
        raise PersonalServiceError(
            {"account": "Account not found in your personal ledger."},
            status_code=404)
    if account.status != PersonalAccount.Status.ACTIVE:
        raise PersonalServiceError({"account": "Account is deactivated."})
    return account


def ensure_default_accounts_and_categories(user):
    """Lazy bootstrap: personal Cash + default categories on first visit."""
    PersonalCategory.seed_defaults(user)
    PersonalAccount.objects.get_or_create(
        user=user, name="Cash",
        defaults={"kind": "CASH", "initial_balance": Decimal("0.00")})


def _assert_no_overdraft(account, amount, ledger_context: str):
    available = account.balance()
    if amount > available:
        raise PersonalServiceError(
            {"account": f"{ledger_context} of à§³{amount} exceeds the "
                        f"available à§³{available} in {account.name}. "
                        "Add income first or lower the amount."})


def validate_no_mismatch(user, txn_type, account, to_account, category,
                         savings_goal, loan):
    if txn_type == PersonalTxnType.TRANSFER:
        if to_account is None:
            raise PersonalServiceError(
                {"to_account": "TRANSFER requires a destination account."})
        if to_account.user_id != user.id or account.user_id != user.id:
            raise PersonalServiceError("You do not own this record.", status_code=403)
        if to_account.id == account.id:
            raise PersonalServiceError(
                {"to_account": "Destination must differ from source."})
        if to_account.status != PersonalAccount.Status.ACTIVE:
            raise PersonalServiceError(
                {"to_account": "Destination account is inactive."})
    else:
        if to_account is not None:
            raise PersonalServiceError(
                {"to_account": "Only TRANSFER uses a destination account."})
    if category is not None:
        if category.user_id != user.id:
            raise PersonalServiceError("You do not own this record.", status_code=403)
        kind = (PersonalCategory.Kind.EXPENSE
                if txn_type in (PersonalTxnType.EXPENSE,
                                PersonalTxnType.TRANSFER)
                else PersonalCategory.Kind.INCOME)
        if category.kind != kind:
            raise PersonalServiceError(
                {"category": f"This is an {kind} category, but the "
                             "transaction type differs."})
    if savings_goal is not None:
        if savings_goal.user_id != user.id:
            raise PersonalServiceError("You do not own this record.", status_code=403)
        if txn_type != PersonalTxnType.EXPENSE:
            raise PersonalServiceError(
                {"savings_goal": "Savings contributions are EXPENSE "
                                 "transactions from an account."})
    if loan is not None and loan.user_id != user.id:
        raise PersonalServiceError("You do not own this record.", status_code=403)


def create_personal_transaction(user, *, transaction_type, amount,
                                transaction_date, account_id,
                                to_account_id=None, category_id=None,
                                savings_goal_id=None, loan_id=None,
                                transaction_time=None, description="",
                                notes="", client_reference=""):
    txn_type = str(transaction_type).upper()
    if txn_type not in PersonalTxnType.values:
        raise PersonalServiceError({"transaction_type": "Invalid type."})
    amount = _money(amount)
    if amount <= 0:
        raise PersonalServiceError("amount must be greater than zero.")
    if not transaction_date:
        raise PersonalServiceError(
            {"transaction_date": "A transaction date is required."})

    if client_reference:
        existing = user.personal_transactions.filter(
            client_reference=client_reference).first()
        if existing is not None:
            return existing, False

    account = _own_account(user, account_id) if account_id else None
    if account is None:
        raise PersonalServiceError({"account": "Required."})
    to_account = (_own_account(user, to_account_id)
                  if to_account_id else None)
    category = user.personal_categories.filter(
        pk=category_id).first() if category_id else None
    savings_goal = user.savings_goals.filter(
        pk=savings_goal_id).first() if savings_goal_id else None
    loan = user.personal_loans.filter(pk=loan_id).first() if loan_id \
        else None

    validate_no_mismatch(user, txn_type, account, to_account, category,
                         savings_goal, loan)

    # Overdraft guard for outgoing effects.
    if txn_type in (PersonalTxnType.EXPENSE, PersonalTxnType.TRANSFER):
        _assert_no_overdraft(account, amount, f"{txn_type.title()}")

    with db_transaction.atomic():
        txn = PersonalTransaction.objects.create(
            user=user, transaction_type=txn_type, amount=amount,
            account=account, to_account=to_account, category=category,
            savings_goal=savings_goal, loan=loan,
            transaction_date=transaction_date,
            transaction_time=transaction_time,
            description=description or "", notes=notes or "",
            client_reference=client_reference or "", created_by=user,
        )
        record_audit("create", txn, resource_type="PersonalTransaction",
                     resource_id=None)
    return txn, True


def reverse_personal_transaction(user, txn):
    if txn.user_id != user.id:
        raise PersonalServiceError("You do not own this record.", status_code=403)
    if txn.status == PersonalTransaction.Status.REVERSED:
        raise PersonalServiceError("Already reversed.")
    if txn.reversals.exists():
        raise PersonalServiceError("Already reversed (mirror exists).")
    txn.status = PersonalTransaction.Status.REVERSED
    txn.save(update_fields=["status"])
    record_audit("reversal", txn, resource_type="PersonalTransaction",
                 changes={"status": "REVERSED"})
    return txn


def create_savings(user, *, goal, amount, transaction_date, account_id,
                   description="", notes="", client_reference=""):
    """Contribute to a savings goal: an EXPENSE transaction out of the
    source account, linked to the goal â€” the overdraft guard applies."""
    if goal.status != SavingsGoal.Status.ACTIVE:
        raise PersonalServiceError(
            {"goal": "This goal is not active."})
    savings_category = user.personal_categories.filter(
        kind=PersonalCategory.Kind.EXPENSE, name="Savings").first()
    txn, _created = create_personal_transaction(
        user, transaction_type=PersonalTxnType.EXPENSE, amount=amount,
        transaction_date=transaction_date, account_id=account_id,
        category_id=str(savings_category.id) if savings_category else None,
        savings_goal_id=str(goal.id), description=description or
        f"Savings â†’ {goal.name}", notes=notes,
        client_reference=client_reference,
    )
    if goal.saved() >= goal.target_amount and \
            goal.status == SavingsGoal.Status.ACTIVE:
        goal.status = SavingsGoal.Status.ACHIEVED
        goal.save(update_fields=["status"])
    return txn


def pay_loan(user, loan, *, amount, transaction_date, account_id,
             description="", notes="", client_reference=""):
    """Record a repayment (TAKEN: we pay; GIVEN: we receive).
    TAKEN loans pay FROM an account (overdraft guarded).
    GIVEN loans land money INTO an account (no overdraft)."""
    if loan.status != PersonalLoan.Status.ACTIVE:
        raise PersonalServiceError({"loan": "This loan is closed."})
    amount = _money(amount)
    if amount <= 0:
        raise PersonalServiceError("amount must be greater than zero.")
    if amount > loan.remaining():
        raise PersonalServiceError(
            {"amount": f"Payment à§³{amount} exceeds the remaining "
                       f"à§³{loan.remaining()} on {loan.name}."})

    if loan.direction == PersonalLoan.Direction.TAKEN:
        txn_type = PersonalTxnType.EXPENSE
        category_name = "Loan EMI"
        description = description or f"EMI â†’ {loan.name}"
    else:
        txn_type = PersonalTxnType.INCOME
        category_name = "Other Income"
        description = description or f"Loan received back: {loan.name}"
    category = user.personal_categories.filter(
        kind=PersonalCategory.Kind.EXPENSE if txn_type
        == PersonalTxnType.EXPENSE else PersonalCategory.Kind.INCOME,
        name=category_name).first()

    txn, _created = create_personal_transaction(
        user, transaction_type=txn_type, amount=amount,
        transaction_date=transaction_date, account_id=account_id,
        category_id=str(category.id) if category else None,
        loan_id=str(loan.id), description=description, notes=notes,
        client_reference=client_reference,
    )
    if loan.remaining() == 0:
        loan.status = PersonalLoan.Status.CLOSED
        loan.save(update_fields=["status"])
    return txn


def net_worth(user) -> dict:
    """Assets âˆ’ liabilities for the personal environment."""
    account_rows = []
    assets = Decimal("0.00")
    for account in user.personal_accounts.filter(
            status=PersonalAccount.Status.ACTIVE):
        bal = account.balance()
        account_rows.append({"account": account.name,
                             "kind": account.kind,
                             "balance": str(bal.quantize(CENT))})
        assets += bal
    receivables = Decimal("0.00")
    payables = Decimal("0.00")
    loan_rows = []
    for loan in user.personal_loans.filter(
            status=PersonalLoan.Status.ACTIVE):
        remaining = loan.remaining().quantize(CENT)
        if remaining == 0:
            continue
        if loan.direction == PersonalLoan.Direction.GIVEN:
            receivables += remaining
            role = "receivable"
        else:
            payables += remaining
            role = "payable"
        loan_rows.append({"loan": loan.name,
                          "direction": loan.direction,
                          "remaining": str(remaining), "role": role})
    assets += receivables
    net = assets - payables
    return {
        "accounts": account_rows,
        "loans": loan_rows,
        "total_assets": str(assets.quantize(CENT)),
        "receivables": str(receivables.quantize(CENT)),
        "payables": str(payables.quantize(CENT)),
        "net_worth": str(net.quantize(CENT)),
    }


def expense_summary_range(user, dfrom, dto):
    """Same totals as month_expense_summary, for any date range.

    Used by the dashboard period filter. Budget rows are only meaningful
    for a single month, so they are left to month_expense_summary.
    """
    profile = user.personal_transactions.filter(
        status=PersonalTransaction.Status.COMPLETED,
        transaction_date__gte=dfrom,
        transaction_date__lte=dto,
    )
    income = sum((t.amount for t in profile.filter(
        transaction_type=PersonalTxnType.INCOME)), Decimal("0.00"))
    expense = sum((t.amount for t in profile.filter(
        transaction_type=PersonalTxnType.EXPENSE)), Decimal("0.00"))
    by_category = {row["category__name"]: row["t"] for row in
                   profile.filter(transaction_type=PersonalTxnType.EXPENSE)
                   .values("category__name")
                   .annotate(t=Sum("amount"))}
    return {
        "total_income": str(income.quantize(CENT)),
        "total_expense": str(expense.quantize(CENT)),
        "net_saved": str((income - expense).quantize(CENT)),
        "expense_by_category": {k: str(v.quantize(CENT))
                                for k, v in by_category.items()},
    }


def month_expense_summary(user, year, month):
    """Budget overview + month totals from committed transactions."""
    budget_rows = []
    income = Decimal("0.00")
    expense = Decimal("0.00")
    for b in user.personal_budgets.filter(year=year, month=month):
        spent = b.spent()
        budget_rows.append({
            "category": b.category.name,
            "budgeted": str(b.budgeted_amount),
            "spent": str(spent.quantize(CENT)),
            "remaining": str((b.budgeted_amount - spent).quantize(CENT)),
            "over_budget": spent > b.budgeted_amount,
        })
    profile = user.personal_transactions.filter(
        status=PersonalTransaction.Status.COMPLETED,
        transaction_date__year=year,
        transaction_date__month=month,
    )
    income = sum((t.amount for t in profile.filter(
        transaction_type=PersonalTxnType.INCOME)), Decimal("0.00"))
    expense = sum((t.amount for t in profile.filter(
        transaction_type=PersonalTxnType.EXPENSE)), Decimal("0.00"))
    by_category = {row["category__name"]: row["t"] for row in
                   profile.filter(transaction_type=PersonalTxnType.EXPENSE)
                   .values("category__name")
                   .annotate(t=Sum("amount"))}
    return {
        "year": year, "month": month,
        "total_income": str(income.quantize(CENT)),
        "total_expense": str(expense.quantize(CENT)),
        "net_saved": str((income - expense).quantize(CENT)),
        "budgets": budget_rows,
        "expense_by_category": {k: str(v.quantize(CENT))
                                for k, v in by_category.items()},
    }
