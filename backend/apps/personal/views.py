"""Personal finance API (Phase 6) — user-owned, business-free.

Every query below is scoped to `request.user`: two users can never read or
touch each other's personal ledgers (enforced server-side, not by the UI).
No `BusinessAccessRequired` here on purpose — personal data has no business
FK at all, so there is never a business context to resolve.
"""
from datetime import date, datetime

from decimal import Decimal

from rest_framework import permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.personal.models import (
    PersonalAccount, PersonalBudget, PersonalCategory, PersonalLoan,
    PersonalTransaction, PersonalTxnType, SavingsGoal,
)
from apps.personal.serializers import (
    PersonalAccountSerializer, PersonalBudgetSerializer,
    PersonalCategorySerializer, PersonalLoanSerializer,
    PersonalTransactionSerializer, SavingsGoalSerializer,
)
from apps.personal.service import (
    PersonalServiceError, create_personal_transaction, create_savings,
    ensure_default_accounts_and_categories, month_expense_summary,
    net_worth, pay_loan, reverse_personal_transaction,
)


def _service_error(exc: PersonalServiceError):
    payload = exc.errors if exc.errors else {"detail": str(exc)}
    return Response(payload, status=exc.status_code)


def _as_date(value):
    """Coerce raw JSON (string) into a date — never trust float/other."""
    if value in (None, "", "null"):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _as_decimal(value, field):
    from decimal import Decimal, InvalidOperation
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise serializers.ValidationError(
            {field: "A valid decimal amount is required."})


class BootstrapView(APIView):
    """First visit: lazy-seed default categories + a Cash account."""
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        ensure_default_accounts_and_categories(request.user)
        accounts = PersonalAccountSerializer(
            request.user.personal_accounts.all(), many=True).data
        categories = PersonalCategorySerializer(
            request.user.personal_categories.all(), many=True).data
        return Response({"accounts": accounts, "categories": categories})


class PersonalAccountListCreateView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        qs = request.user.personal_accounts.all()
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        ser = PersonalAccountSerializer(qs, many=True)
        return Response({"count": len(ser.data), "accounts": ser.data})

    def post(self, request):
        name = (request.data.get("name") or "").strip()
        if not name:
            raise serializers.ValidationError({"name": "Required."})
        if request.user.personal_accounts.filter(name=name).exists():
            raise serializers.ValidationError(
                {"name": "You already have an account with this name."})
        account = PersonalAccount.objects.create(
            user=request.user, name=name,
            kind=request.data.get("kind") or "CASH",
            initial_balance=_as_decimal(
                request.data.get("initial_balance", "0") or "0",
                "initial_balance"),
            status=PersonalAccount.Status.ACTIVE,
        )
        return Response(PersonalAccountSerializer(account).data, status=201)


class PersonalAccountDetailView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def _own(self, request, account_id):
        return PersonalAccount.objects.filter(
            user=request.user, pk=account_id).first()

    def get(self, request, account_id):
        account = self._own(request, account_id)
        if account is None:
            return Response({"detail": "Not found."}, status=404)
        return Response(PersonalAccountSerializer(account).data)

    def patch(self, request, account_id):
        account = self._own(request, account_id)
        if account is None:
            return Response({"detail": "Not found."}, status=404)
        if "name" in request.data:
            name = (request.data["name"] or "").strip()
            dup = request.user.personal_accounts.filter(name=name) \
                .exclude(pk=account.pk).exists()
            if not name or dup:
                raise serializers.ValidationError(
                    {"name": "Name required and must be unique."})
            account.name = name
        if "kind" in request.data:
            account.kind = request.data["kind"]
        if "status" in request.data:
            wanted = request.data["status"]
            if wanted not in PersonalAccount.Status.values:
                raise serializers.ValidationError(
                    {"status": "ACTIVE or INACTIVE only."})
            account.status = wanted
        account.save()
        return Response(PersonalAccountSerializer(account).data)

    def delete(self, request, account_id):
        account = self._own(request, account_id)
        if account is None:
            return Response({"detail": "Not found."}, status=404)
        from django.db.models import Q
        used = PersonalTransaction.objects.filter(user=request.user).filter(
            Q(account=account) | Q(to_account=account)).exists()
        if used:
            return Response(
                {"detail": "Account has transactions — deactivate instead "
                           "of deleting."},
                status=400)
        account.delete()  # soft delete (SoftDeleteUUIDModel) + audit trail
        return Response(status=204)


class PersonalCategoryListCreateView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        qs = request.user.personal_categories.all()
        if request.query_params.get("kind"):
            qs = qs.filter(kind=request.query_params["kind"])
        ser = PersonalCategorySerializer(qs, many=True)
        return Response({"count": len(ser.data), "categories": ser.data})

    def post(self, request):
        name = (request.data.get("name") or "").strip()
        kind = (request.data.get("kind") or "").strip()
        if not name:
            raise serializers.ValidationError({"name": "Required."})
        if kind not in PersonalCategory.Kind.values:
            raise serializers.ValidationError(
                {"kind": "INCOME or EXPENSE required."})
        if request.user.personal_categories.filter(
                kind=kind, name=name).exists():
            raise serializers.ValidationError(
                {"name": "You already have this category."})
        category = PersonalCategory.objects.create(
            user=request.user, name=name, kind=kind, is_default=False)
        return Response(PersonalCategorySerializer(category).data, status=201)


class PersonalCategoryDetailView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def delete(self, request, category_id):
        category = request.user.personal_categories.filter(
            pk=category_id).first()
        if category is None:
            return Response({"detail": "Not found."}, status=404)
        if PersonalTransaction.objects.filter(user=request.user,
                                              category=category).exists():
            return Response(
                {"detail": "Category has transactions and cannot be "
                           "deleted."}, status=400)
        category.delete()
        return Response(status=204)


class PersonalTransactionListCreateView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        qs = request.user.personal_transactions.all()
        p = request.query_params
        if p.get("type"):
            qs = qs.filter(transaction_type=p["type"])
        if p.get("account"):
            qs = qs.filter(account_id=p["account"])
        if p.get("category"):
            qs = qs.filter(category_id=p["category"])
        if p.get("from"):
            qs = qs.filter(transaction_date__gte=p["from"])
        if p.get("to"):
            qs = qs.filter(transaction_date__lte=p["to"])
        if p.get("goal"):
            qs = qs.filter(savings_goal_id=p["goal"])
        if p.get("loan"):
            qs = qs.filter(loan_id=p["loan"])
        ser = PersonalTransactionSerializer(
            qs[:int(p.get("limit", 100))], many=True)
        return Response({"count": len(ser.data),
                         "transactions": ser.data})

    def post(self, request):
        try:
            txn, _created = create_personal_transaction(
                request.user,
                transaction_type=request.data.get("transaction_type"),
                amount=request.data.get("amount"),
                transaction_date=_as_date(
                    request.data.get("transaction_date")
                    or date.today().isoformat()),
                account_id=request.data.get("account"),
                to_account_id=request.data.get("to_account"),
                category_id=request.data.get("category"),
                savings_goal_id=request.data.get("savings_goal"),
                loan_id=request.data.get("loan"),
                transaction_time=request.data.get("transaction_time"),
                description=request.data.get("description", ""),
                notes=request.data.get("notes", ""),
                client_reference=request.data.get("client_reference", ""),
            )
        except PersonalServiceError as exc:
            return _service_error(exc)
        return Response(
            PersonalTransactionSerializer(txn).data,
            status=(201 if _created else 200))


class PersonalTransactionDetailView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, txn_id):
        txn = request.user.personal_transactions.filter(
            pk=txn_id).first()
        if txn is None:
            return Response({"detail": "Not found."}, status=404)
        return Response(PersonalTransactionSerializer(txn).data)


class PersonalTransactionReversalView(APIView):
    def post(self, request, txn_id):
        txn = request.user.personal_transactions.filter(pk=txn_id).first()
        if txn is None:
            return Response({"detail": "Not found."}, status=404)
        try:
            reversed_txn = reverse_personal_transaction(
                request.user, txn)
        except PersonalServiceError as exc:
            return _service_error(exc)
        return Response(PersonalTransactionSerializer(reversed_txn).data)


class PersonalBudgetListCreateView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        qs = request.user.personal_budgets.all()
        p = request.query_params
        if p.get("year"):
            qs = qs.filter(year=p["year"])
        if p.get("month"):
            qs = qs.filter(month=p["month"])
        ser = PersonalBudgetSerializer(qs, many=True)
        return Response({"count": len(ser.data), "budgets": ser.data})

    def post(self, request):
        category = request.user.personal_categories.filter(
            pk=request.data.get("category")).first()
        if category is None:
            raise serializers.ValidationError(
                {"category": "One of your categories is required."})
        if category.kind != PersonalCategory.Kind.EXPENSE:
            raise serializers.ValidationError(
                {"category": "Budgets apply to EXPENSE categories."})
        year = request.data.get("year")
        month = request.data.get("month")
        try:
            year = int(year)
            month = int(month)
            if not 1 <= month <= 12:
                raise ValueError
        except (TypeError, ValueError):
            raise serializers.ValidationError(
                {"year": "A valid year and month 1..12 are required."})
        budgeted = _as_decimal(
            request.data.get("budgeted_amount"), "budgeted_amount")
        if budgeted < 0:
            raise serializers.ValidationError(
                {"budgeted_amount": "Cannot be negative."})
        budget, created = PersonalBudget.objects.get_or_create(
            user=request.user, category=category, year=year, month=month,
            defaults={"budgeted_amount": budgeted})
        if not created:
            budget.budgeted_amount = budgeted
            budget.save(update_fields=["budgeted_amount"])
        return Response(PersonalBudgetSerializer(budget).data,
                        status=(201 if created else 200))


class PersonalBudgetDetailView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def delete(self, request, budget_id):
        budget = request.user.personal_budgets.filter(pk=budget_id).first()
        if budget is None:
            return Response({"detail": "Not found."}, status=404)
        budget.delete()
        return Response(status=204)


class SavingsGoalListCreateView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        qs = request.user.savings_goals.all()
        ser = SavingsGoalSerializer(qs, many=True)
        return Response({"count": len(ser.data), "goals": ser.data})

    def post(self, request):
        name = (request.data.get("name") or "").strip()
        if not name:
            raise serializers.ValidationError({"name": "Required."})
        goal = SavingsGoal.objects.create(
            user=request.user, name=name,
            target_amount=_as_decimal(request.data.get("target_amount"),
                                      "target_amount"),
            target_date=_as_date(request.data.get("target_date")),
            status=SavingsGoal.Status.ACTIVE,
        )
        return Response(SavingsGoalSerializer(goal).data, status=201)


class SavingsGoalDetailView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, goal_id):
        goal = request.user.savings_goals.filter(pk=goal_id).first()
        if goal is None:
            return Response({"detail": "Not found."}, status=404)
        return Response(SavingsGoalSerializer(goal).data)

    def patch(self, request, goal_id):
        goal = request.user.savings_goals.filter(pk=goal_id).first()
        if goal is None:
            return Response({"detail": "Not found."}, status=404)
        wanted = request.data.get("status")
        if wanted not in SavingsGoal.Status.values:
            raise serializers.ValidationError(
                {"status": "ACTIVE/ACHIEVED/CLOSED."})
        goal.status = wanted
        goal.save(update_fields=["status"])
        return Response(SavingsGoalSerializer(goal).data)


class SavingsGoalContributeView(APIView):
    def post(self, request, goal_id):
        goal = request.user.savings_goals.filter(pk=goal_id).first()
        if goal is None:
            return Response({"detail": "Not found."}, status=404)
        try:
            txn = create_savings(
                request.user, goal=goal,
                amount=request.data.get("amount"),
                transaction_date=_as_date(
                    request.data.get("transaction_date")
                    or date.today().isoformat()),
                account_id=request.data.get("account"),
                description=request.data.get("description", ""),
                notes=request.data.get("notes", ""),
                client_reference=request.data.get("client_reference", ""),
            )
        except PersonalServiceError as exc:
            return _service_error(exc)
        return Response(SavingsGoalSerializer(goal).data, status=201)


class PersonalLoanListCreateView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        qs = request.user.personal_loans.all()
        ser = PersonalLoanSerializer(qs, many=True)
        return Response({"count": len(ser.data), "loans": ser.data})

    def post(self, request):
        direction = (request.data.get("direction") or "").strip()
        if direction not in PersonalLoan.Direction.values:
            raise serializers.ValidationError(
                {"direction": "TAKEN or GIVEN required."})
        principal = _as_decimal(request.data.get("principal"), "principal")
        if principal <= 0:
            raise serializers.ValidationError(
                {"principal": "Must be greater than zero."})
        loan = PersonalLoan.objects.create(
            user=request.user,
            name=(request.data.get("name") or "").strip() or "Loan",
            counterparty_name=(request.data.get("counterparty_name")
                               or "").strip(),
            direction=direction,
            principal=principal,
            emi_amount=_as_decimal(
                request.data.get("emi_amount", "0") or "0", "emi_amount"),
            installments=int(request.data.get("installments", 0) or 0),
            start_date=_as_date(request.data.get("start_date")
                                or date.today().isoformat()),
            notes=request.data.get("notes", ""),
            client_reference=request.data.get("client_reference", ""),
        )
        return Response(PersonalLoanSerializer(loan).data, status=201)


class PersonalLoanDetailView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, loan_id):
        loan = request.user.personal_loans.filter(pk=loan_id).first()
        if loan is None:
            return Response({"detail": "Not found."}, status=404)
        data = PersonalLoanSerializer(loan).data
        data["schedule"] = loan.schedule()
        return Response(data)

    def patch(self, request, loan_id):
        loan = request.user.personal_loans.filter(pk=loan_id).first()
        if loan is None:
            return Response({"detail": "Not found."}, status=404)
        wanted = request.data.get("status")
        if wanted not in PersonalLoan.Status.values:
            raise serializers.ValidationError(
                {"status": "ACTIVE or CLOSED."})
        loan.status = wanted
        loan.save(update_fields=["status"])
        return Response(PersonalLoanSerializer(loan).data)


class PersonalLoanPaymentView(APIView):
    def post(self, request, loan_id):
        loan = request.user.personal_loans.filter(pk=loan_id).first()
        if loan is None:
            return Response({"detail": "Not found."}, status=404)
        try:
            pay_loan(
                request.user, loan,
                amount=request.data.get("amount"),
                transaction_date=_as_date(
                    request.data.get("transaction_date")
                    or date.today().isoformat()),
                account_id=request.data.get("account"),
                description=request.data.get("description", ""),
                notes=request.data.get("notes", ""),
                client_reference=request.data.get("client_reference", ""),
            )
        except PersonalServiceError as exc:
            return _service_error(exc)
        loan.refresh_from_db()
        data = PersonalLoanSerializer(loan).data
        data["schedule"] = loan.schedule()
        return Response(data, status=201)


class PersonalDashboardView(APIView):
    """Net worth + this-month income/expense/budget overview."""
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        today = date.today()
        try:
            year = int(request.query_params.get("year", today.year))
            month = int(request.query_params.get("month", today.month))
        except ValueError:
            return Response(
                {"detail": "year/month must be integers."}, status=400)
        if not 1 <= month <= 12:
            return Response({"detail": "month must be 1..12."}, status=400)
        return Response({
            "net_worth": net_worth(request.user),
            "month": month_expense_summary(request.user, year, month),
        })


class PersonalAlertsView(APIView):
    """Phase 7 feed: budget warnings (≥80% consumed / over budget),
    overdue loan installments, stalled goals past their target date,
    overdraft-risk accounts (≤0 balance). All figures read live from the
    user's own ledger rows — nothing invented."""

    def get(self, request):
        today = date.today()
        threshold = 0.80
        alerts = []

        for budget in request.user.personal_budgets.filter(
                year=today.year, month=today.month).select_related("category"):
            budgeted = Decimal(str(budget.budgeted_amount))
            if budgeted <= 0:
                continue
            spent = Decimal(str(budget.spent()))
            consumed = float((spent / budgeted).quantize(Decimal("0.01")))
            if spent > budgeted:
                alerts.append({
                    "kind": "BUDGET_OVER",
                    "category": budget.category.name,
                    "budgeted": budgeted, "spent": spent,
                    "consumed_percent": consumed,
                })
            elif consumed >= threshold:
                alerts.append({
                    "kind": "BUDGET_WARNING",
                    "category": budget.category.name,
                    "budgeted": budgeted, "spent": spent,
                    "consumed_percent": consumed,
                })

        for loan in request.user.personal_loans.filter(
                status=PersonalLoan.Status.ACTIVE):
            schedule = loan.schedule()
            for inst in schedule["installments"]:
                if inst["state"] == "OVERDUE":
                    alerts.append({
                        "kind": "LOAN_OVERDUE",
                        "loan": loan.name,
                        "installment": inst["installment"],
                        "due_date": inst["due_date"],
                        "amount": inst["amount"],
                        "remaining": loan.remaining(),
                    })

        for goal in request.user.savings_goals.all():
            if (goal.target_date is not None
                    and goal.target_date < today
                    and goal.status == SavingsGoal.Status.ACTIVE
                    and goal.saved() < goal.target_amount):
                alerts.append({
                    "kind": "GOAL_PAST_TARGET",
                    "goal": goal.name,
                    "target_amount": goal.target_amount,
                    "target_date": goal.target_date,
                    "saved": goal.saved(),
                })

        for account in request.user.personal_accounts.filter(
                status=PersonalAccount.Status.ACTIVE):
            balance = account.balance()
            if balance <= Decimal("0.00"):
                alerts.append({
                    "kind": "ACCOUNT_EMPTY",
                    "account": account.name,
                    "balance": balance,
                })
        return Response({
            "date": str(today),
            "count": len(alerts),
            "alerts": [self._stringify(a) for a in alerts],
        })

    @staticmethod
    def _stringify(alert: dict) -> dict:
        for key, value in list(alert.items()):
            if isinstance(value, Decimal):
                alert[key] = str(value.quantize(Decimal("0.01")))
        if "consumed_percent" in alert:
            alert["consumed_percent"] = round(alert["consumed_percent"] * 100)
        return alert
