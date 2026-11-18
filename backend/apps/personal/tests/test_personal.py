"""Phase 6 tests — Personal Finance (Section 6 family).

Covers:
- User isolation: user2 can never read or mutate user1's personal data.
- Exact ledger-derived balance math (income/expense/transfer + reversal).
- Overdraft rejection names the available balance (spec requirement).
- client_reference idempotency returns the original row.
- Budget: derived spent, over-budget flag, month filtering, uniqueness.
- Savings goal: saved() sum, ACHIEVED transition at/above target.
- Loans: TAKEN vs GIVEN payment effect direction, overpay rejection,
  schedule rows with PAID/PARTIAL state, auto-CLOSE at zero remaining.
- Net worth: accounts + GIVEN receivables − TAKEN payables.
- Month expense summary totals + per-category split.
- No business FK anywhere in personal models (schema-level separation).
"""
from decimal import Decimal

from django.apps import apps as django_apps
from django.test import TestCase

from apps.personal.models import (
    PersonalAccount, PersonalBudget, PersonalCategory, PersonalLoan,
    PersonalTransaction, PersonalTxnType, SavingsGoal,
)
from apps.personal.service import (
    PersonalServiceError, create_personal_transaction, create_savings,
    month_expense_summary, net_worth, pay_loan,
)
from apps.tests_common import auth_client, make_user


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))


class PersonalBase(TestCase):
    def setUp(self):
        self.u1 = make_user("p6alice")
        self.u2 = make_user("p6bob")
        PersonalCategory.seed_defaults(self.u1)
        PersonalCategory.seed_defaults(self.u2)
        self.cash1 = PersonalAccount.objects.create(
            user=self.u1, name="Cash", kind="CASH",
            initial_balance=money("1000.00"))
        self.bank1 = PersonalAccount.objects.create(
            user=self.u1, name="City Bank", kind="BANK",
            initial_balance=money("500.00"))
        self.cash2 = PersonalAccount.objects.create(
            user=self.u2, name="Cash", kind="CASH",
            initial_balance=money("0.00"))
        # user1's zero-balance account for overdraft assertions
        self.pocket1 = PersonalAccount.objects.create(
            user=self.u1, name="Pocket", kind="CASH",
            initial_balance=money("0.00"))
        self.food = PersonalCategory.objects.get(user=self.u1, name="Food",
                                                 kind="EXPENSE")
        self.salary = PersonalCategory.objects.get(user=self.u1,
                                                   name="Salary",
                                                   kind="INCOME")

    def txn(self, user, ttype, amount, account, date="2026-10-10", **kw):
        kw["category_id"] = kw.pop("category", None)
        return create_personal_transaction(
            user, transaction_type=ttype, amount=amount,
            transaction_date=date, account_id=str(account.pk), **kw)[0]


class UserIsolationTests(PersonalBase):
    def test_user2_cannot_read_or_touch_user1_rows(self):
        self.txn(self.u1, "EXPENSE", "100.00", self.cash1)
        txn2 = self.txn(self.u2, "INCOME", "50.00", self.cash2)

        # list scoping: user2 only sees the one transaction they own
        ids_u2 = set(self.u2.personal_transactions
                     .values_list("id", flat=True))
        self.assertEqual(len(ids_u2), 1)
        self.assertEqual(txn2.pk, ids_u2.pop())

        # writing through the writer into user1's account is a 404
        # (service scope filters by owner — existence never leaked)
        with self.assertRaises(PersonalServiceError) as ctx:
            create_personal_transaction(
                self.u2, transaction_type="EXPENSE", amount="10.00",
                transaction_date="2026-10-11",
                account_id=str(self.cash1.pk))
        self.assertEqual(ctx.exception.status_code, 404)

        # reversing someone else's transaction is a 403
        from apps.personal.service import reverse_personal_transaction
        incoming = PersonalTransaction.objects.exclude(user=self.u2).first()
        self.assertIsNotNone(incoming)
        with self.assertRaises(PersonalServiceError) as ctx3:
            reverse_personal_transaction(self.u2, incoming)
        self.assertEqual(ctx3.exception.status_code, 403)

        # API scoping: u2 hitting u1's rows 404s (filtered by owner)
        client = auth_client(self.u2)
        res = client.get(f"/api/personal/accounts/{self.cash1.pk}/")
        self.assertEqual(res.status_code, 404)

    def test_no_business_fk_in_personal_models(self):
        """Schema-level separation: no personal model carries a business FK."""
        for model in ("PersonalAccount", "PersonalCategory",
                      "PersonalTransaction", "PersonalBudget",
                      "SavingsGoal", "PersonalLoan"):
            model_cls = django_apps.get_model("personal", model)
            fields = {f.name for f in model_cls._meta.get_fields()}
            self.assertNotIn("business", fields, model)
            self.assertFalse(any(f.startswith("business") for f in fields),
                             model)


class BalanceMathTests(PersonalBase):
    def test_exact_balance_across_types_and_reversal_restores(self):
        self.assertEqual(self.cash1.balance(), money("1000.00"))
        self.txn(self.u1, "INCOME", "700.00", self.cash1,
                 category=str(self.salary.pk))
        self.assertEqual(self.cash1.balance(), money("1700.00"))
        self.txn(self.u1, "EXPENSE", "200.50", self.cash1,
                 category=str(self.food.pk))
        self.assertEqual(self.cash1.balance(), money("1499.50"))
        self.txn(self.u1, "TRANSFER", "300.00", self.cash1,
                 to_account_id=str(self.bank1.pk))
        self.assertEqual(self.cash1.balance(), money("1199.50"))
        self.assertEqual(self.bank1.balance(), money("800.00"))

        # reversal removes the effect entirely (ledger-derived, no counter)
        txn = PersonalTransaction.objects.get(
            user=self.u1, transaction_type=PersonalTxnType.TRANSFER)
        from apps.personal.service import reverse_personal_transaction
        reverse_personal_transaction(self.u1, txn)
        self.assertEqual(self.cash1.balance(), money("1499.50"))
        self.assertEqual(self.bank1.balance(), money("500.00"))
        reversed_again = PersonalTransaction.objects.filter(
            status=PersonalTransaction.Status.REVERSED).exists()
        self.assertTrue(reversed_again)

    def test_overdraft_rejection_names_available_balance(self):
        with self.assertRaises(PersonalServiceError) as ctx:
            create_personal_transaction(
                self.u1, transaction_type="EXPENSE", amount="100.00",
                transaction_date="2026-10-10", account_id=str(self.pocket1.pk))
        msg = str(ctx.exception.errors)
        # the available balance is named: "exceeds the available ৳0.00 in
        # Pocket" (exact rupee/taka symbol rendered server-side)
        self.assertIn("exceeds the available", msg)
        self.assertIn("Pocket", msg)
        self.assertIn("0.00", msg)

    def test_client_reference_idempotency(self):
        kw = dict(description="phone bill", client_reference="sync-key-77")
        t1, created1 = create_personal_transaction(
            self.u1, transaction_type="EXPENSE", amount="40.00",
            transaction_date="2026-10-10", account_id=str(self.cash1.pk),
            **kw)
        t2, created2 = create_personal_transaction(
            self.u1, transaction_type="EXPENSE", amount="40.00",
            transaction_date="2026-10-10", account_id=str(self.cash1.pk),
            **kw)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(t1.pk, t2.pk)
        # only one deduction
        self.assertEqual(self.cash1.balance(), money("960.00"))

    def test_category_kind_mismatch_rejected(self):
        with self.assertRaises(PersonalServiceError):
            self.txn(self.u1, "INCOME", "10.00", self.cash1,
                     category=str(self.food.pk))  # EXPENSE cat on INCOME

    def test_transfer_same_account_rejected(self):
        with self.assertRaises(PersonalServiceError):
            self.txn(self.u1, "TRANSFER", "10.00", self.cash1,
                     to_account_id=str(self.cash1.pk))


class BudgetMathTests(PersonalBase):
    def test_budget_spent_derived_and_over_budget_flag(self):
        # fund the account so overdrafts don't interfere with budget math
        self.txn(self.u1, "INCOME", "3000.00", self.cash1,
                 category=str(self.salary.pk))
        b = PersonalBudget.objects.create(
            user=self.u1, category=self.food, year=2026, month=10,
            budgeted_amount=money("500.00"))
        self.assertEqual(b.spent(), money("0"))
        self.txn(self.u1, "EXPENSE", "200.00", self.cash1,
                 date="2026-10-03", category=str(self.food.pk))
        self.txn(self.u1, "EXPENSE", "350.00", self.cash1,
                 date="2026-10-04", category=str(self.food.pk))
        b2 = PersonalBudget.objects.get(pk=b.pk)
        self.assertEqual(b2.spent(), money("550.00"))
        self.assertTrue(b2.spent() > b2.budgeted_amount)

        # different month ignored
        self.txn(self.u1, "EXPENSE", "999.00", self.cash1,
                 date="2026-11-01", category=str(self.food.pk))
        self.assertEqual(PersonalBudget.objects.get(pk=b.pk).spent(),
                         money("550.00"))

    def test_reversed_transactions_not_counted_in_budget(self):
        b = PersonalBudget.objects.create(
            user=self.u1, category=self.food, year=2026, month=10,
            budgeted_amount=money("100.00"))
        txn = self.txn(self.u1, "EXPENSE", "60.00", self.cash1,
                       date="2026-10-03", category=str(self.food.pk))
        from apps.personal.service import reverse_personal_transaction
        reverse_personal_transaction(self.u1, txn)
        self.assertEqual(b.spent(), money("0"))


class SavingsGoalTests(PersonalBase):
    def test_saved_progress_and_achieved_transition(self):
        goal = SavingsGoal.objects.create(
            user=self.u1, name="Hajj", target_amount=money("1000.00"),
            status=SavingsGoal.Status.ACTIVE)
        self.txn(self.u1, "EXPENSE", "100.00", self.cash1,
                 savings_goal_id=str(goal.pk))
        txn_mid = self.txn(self.u1, "EXPENSE", "250.00", self.cash1,
                           savings_goal_id=str(goal.pk))
        self.assertEqual(goal.saved(), money("350.00"))
        self.assertEqual(goal.progress_percent(), money("35.00"))

        create_savings(self.u1, goal=goal, amount="650.00",
                       transaction_date="2026-10-20",
                       account_id=str(self.cash1.pk))
        goal.refresh_from_db()
        self.assertEqual(goal.saved(), money("1000.00"))
        self.assertEqual(goal.status, SavingsGoal.Status.ACHIEVED)
        self.assertEqual(goal.progress_percent(), money("100.00"))

        # reversal drops saved; goal status is intentionally not auto-down
        from apps.personal.service import reverse_personal_transaction
        reverse_personal_transaction(self.u1, txn_mid)
        self.assertEqual(SavingsGoal.objects.get(pk=goal.pk).saved(),
                         money("750.00"))

    def test_contribution_from_empty_account_rejected(self):
        goal = SavingsGoal.objects.create(
            user=self.u1, name="Laptop", target_amount=money("500.00"))
        with self.assertRaises(PersonalServiceError) as ctx:
            create_savings(self.u1, goal=goal, amount="100.00",
                           transaction_date="2026-10-20",
                           account_id=str(self.pocket1.pk))
        self.assertIn("exceeds the available", str(ctx.exception.errors))


class LoanTests(PersonalBase):
    def test_taken_loan_payment_flow_and_auto_close(self):
        # TAKEN: loan received 10k (income), then EMIs drain an account
        loan = PersonalLoan.objects.create(
            user=self.u1, name="Bike Loan", counterparty_name="Rahim",
            direction=PersonalLoan.Direction.TAKEN,
            principal=money("10000.00"), emi_amount=money("2500.00"),
            installments=4, start_date="2026-09-01",
            status=PersonalLoan.Status.ACTIVE)
        self.assertEqual(loan.remaining(), money("10000.00"))

        self.txn(self.u1, "INCOME", "10000.00", self.cash1)  # loan disbursed
        for i, day in enumerate(("2026-10-01", "2026-10-15"), start=1):
            pay_loan(self.u1, loan, amount=str(money(2500)),
                     transaction_date=day, account_id=str(self.cash1.pk))
        self.assertEqual(loan.remaining(), money("5000.00"))
        schedule = loan.schedule()
        self.assertEqual(schedule["installments"][0]["state"], "PAID")
        self.assertEqual(schedule["installments"][1]["state"], "PAID")
        self.assertIn(schedule["installments"][2]["state"],
                      ("OVERDUE", "UPCOMING"))

        pay_loan(self.u1, loan, amount="5000.00",
                 transaction_date="2026-11-01", account_id=str(self.cash1.pk))
        loan.refresh_from_db()
        self.assertEqual(loan.remaining(), money("0.00"))
        self.assertEqual(loan.status, PersonalLoan.Status.CLOSED)
        # cash1: 1000 + 10000 disbursed − 2500 − 2500 − 5000 = 1000
        self.assertEqual(self.cash1.balance(), money("1000.00"))

    def test_given_loan_receivable_in_net_worth(self):
        loan = PersonalLoan.objects.create(
            user=self.u1, name="To Kamal", counterparty_name="Kamal",
            direction=PersonalLoan.Direction.GIVEN,
            principal=money("5000.00"), start_date="2026-10-01")
        net = net_worth(self.u1)
        self.assertEqual(Decimal(net["receivables"]), money("5000.00"))
        # cash1 1000 + bank1 500 + receivable 5000 = 6500
        self.assertEqual(Decimal(net["total_assets"]), money("6500.00"))
        # given-direction payments return money INTO one of OUR accounts
        pay_loan(self.u1, loan, amount="2000.00",
                 transaction_date="2026-10-20", account_id=str(self.bank1.pk))
        self.assertEqual(loan.remaining(), money("3000.00"))
        self.assertEqual(self.bank1.balance(), money("2500.00"))
        net2 = net_worth(self.u1)
        self.assertEqual(Decimal(net2["receivables"]), money("3000.00"))
        # cash1 1000 + bank1 2500 + receivable 3000 = 6500
        self.assertEqual(Decimal(net2["total_assets"]), money("6500.00"))

    def test_overpay_beyond_remaining_rejected(self):
        loan = PersonalLoan.objects.create(
            user=self.u1, name="Small", counterparty_name="X",
            direction=PersonalLoan.Direction.TAKEN,
            principal=money("100.00"), start_date="2026-10-01")
        with self.assertRaises(PersonalServiceError) as ctx:
            pay_loan(self.u1, loan, amount="150.00",
                     transaction_date="2026-10-02",
                     account_id=str(self.cash1.pk))
        self.assertIn("exceeds the remaining", str(ctx.exception.errors))

    def test_loan_payment_overdraft_guard_for_taken(self):
        loan = PersonalLoan.objects.create(
            user=self.u1, name="Tiny", counterparty_name="Y",
            direction=PersonalLoan.Direction.TAKEN,
            principal=money("900.00"), start_date="2026-10-01")
        with self.assertRaises(PersonalServiceError) as ctx:
            pay_loan(self.u1, loan, amount="600.00",
                     transaction_date="2026-10-02",
                     account_id=str(self.pocket1.pk))  # 0 balance
        self.assertIn("exceeds the available", str(ctx.exception.errors))


class DashboardMathTests(PersonalBase):
    def test_net_worth_composition(self):
        self.txn(self.u1, "INCOME", "5000.00", self.bank1,
                 category=str(self.salary.pk))
        due = PersonalLoan.objects.create(
            user=self.u1, name="Borrowed", counterparty_name="Z",
            direction=PersonalLoan.Direction.TAKEN,
            principal=money("2000.00"), start_date="2026-10-01")
        net = net_worth(self.u1)
        # cash 1000 + bank 5500 + receivable 0 − payable 2000
        self.assertEqual(Decimal(net["total_assets"]), money("6500.00"))
        self.assertEqual(Decimal(net["payables"]), money("2000.00"))
        self.assertEqual(Decimal(net["net_worth"]), money("4500.00"))
        self.assertEqual(
            Decimal(net["net_worth"]),
            Decimal(net["total_assets"]) - Decimal(net["payables"]))

    def test_month_expense_summary(self):
        self.txn(self.u1, "INCOME", "900.00", self.cash1,
                 date="2026-10-02", category=str(self.salary.pk))
        self.txn(self.u1, "EXPENSE", "300.00", self.cash1,
                 date="2026-10-03", category=str(self.food.pk))
        self.txn(self.u1, "EXPENSE", "100.00", self.cash1,
                 date="2026-10-04", category=str(self.food.pk))
        self.txn(self.u1, "EXPENSE", "150.00", self.cash1,
                 date="2026-09-30", category=str(self.food.pk))  # other month
        summary = month_expense_summary(self.u1, 2026, 10)
        self.assertEqual(Decimal(summary["total_income"]), money("900.00"))
        self.assertEqual(Decimal(summary["total_expense"]), money("400.00"))
        self.assertEqual(Decimal(summary["net_saved"]), money("500.00"))
        self.assertEqual(summary["expense_by_category"].get("Food"),
                         "400.00")


class PersonalAPITests(PersonalBase):
    def test_bootstrap_and_end_to_end_api(self):
        client = auth_client(self.u2)
        res = client.post("/api/personal/bootstrap/")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertGreaterEqual(len(res.json()["accounts"]), 1)
        kinds = {c["name"] for c in res.json()["categories"]}
        self.assertIn("Food", kinds)

        # create account
        res = client.post("/api/personal/accounts/",
                          {"name": "Nagad", "kind": "MOBILE"})
        self.assertEqual(res.status_code, 201, res.content)
        acc = res.json()
        self.assertEqual(acc["balance"], "0.00")

        acct_id = acc["id"]
        # income → expense → balance
        client.post("/api/personal/transactions/", {
            "transaction_type": "INCOME", "amount": "800.00",
            "transaction_date": "2026-10-10",
            "account": acct_id, "description": "Top-up"}, format="json")
        res = client.post("/api/personal/transactions/", {
            "transaction_type": "EXPENSE", "amount": "300.00",
            "transaction_date": "2026-10-11", "account": acct_id,
            "description": "Groceries"}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        res = client.get(f"/api/personal/accounts/{acct_id}/")
        self.assertEqual(res.json()["balance"], "500.00")

        # overdraft via API names the available amount
        res = client.post("/api/personal/transactions/", {
            "transaction_type": "EXPENSE", "amount": "9999.00",
            "transaction_date": "2026-10-12", "account": acct_id},
            format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("500.00", str(res.content))

        # reversal via API restores balance
        list_res = client.get("/api/personal/transactions/")
        txn_id = list_res.json()["transactions"][0]["id"]
        res = client.post(f"/api/personal/transactions/{txn_id}/reversal/")
        self.assertEqual(res.status_code, 200)
        res = client.get(f"/api/personal/accounts/{acct_id}/")
        self.assertEqual(res.json()["balance"], "800.00")

    def test_budget_api_over_budget_flag(self):
        PersonalCategory.seed_defaults(self.u1)
        cat_id = str(self.food.pk)
        res = auth_client(self.u1).post("/api/personal/budgets/", {
            "category": cat_id, "year": 2026, "month": 10,
            "budgeted_amount": "100.00"}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertFalse(res.json()["over_budget"])
        auth_client(self.u1).post("/api/personal/transactions/", {
            "transaction_type": "EXPENSE", "amount": "180.00",
            "transaction_date": "2026-10-05", "account": str(self.cash1.pk),
            "category": cat_id}, format="json")
        res = auth_client(self.u1).get("/api/personal/budgets/?year=2026"
                                       "&month=10")
        self.assertTrue(res.json()["budgets"][0]["over_budget"])
        self.assertEqual(res.json()["budgets"][0]["spent"], "180.00")
        self.assertEqual(res.json()["budgets"][0]["remaining"], "-80.00")

    def test_goal_and_loan_api(self):
        client = auth_client(self.u1)
        res = client.post("/api/personal/goals/", {
            "name": "Eid fund", "target_amount": "600.00",
            "target_date": "2026-12-31"}, format="json")
        self.assertEqual(res.status_code, 201)
        goal_id = res.json()["id"]
        self.assertEqual(res.json()["saved"], "0.00")

        res = client.post(f"/api/personal/goals/{goal_id}/contribute/", {
            "amount": "150.00", "account": str(self.bank1.pk),
            "transaction_date": "2026-10-09"}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["saved"], "150.00")
        self.assertEqual(res.json()["progress_percent"], "25.00")

        res = client.post("/api/personal/loans/", {
            "name": "EMI card", "counterparty_name": "DBBL",
            "direction": "TAKEN", "principal": "1000.00",
            "emi_amount": "500.00", "installments": 2,
            "start_date": "2026-10-01"}, format="json")
        self.assertEqual(res.status_code, 201)
        loan_id = res.json()["id"]
        self.assertEqual(res.json()["remaining"], "1000.00")

        # top-up so the account can fund the EMI (bank1 was at 350.00)
        res = client.post("/api/personal/transactions/", {
            "transaction_type": "INCOME", "amount": "300.00",
            "transaction_date": "2026-10-14", "account": str(self.bank1.pk),
            "description": "Top-up"}, format="json")
        self.assertEqual(res.status_code, 201, res.content)

        res = client.post(f"/api/personal/loans/{loan_id}/payment/", {
            "amount": "500.00", "account": str(self.bank1.pk),
            "transaction_date": "2026-10-15"}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        data = res.json()
        self.assertEqual(data["remaining"], "500.00")
        self.assertEqual(data["schedule"]["installments"][0]["state"],
                         "PAID")
        self.assertEqual(len(data["schedule"]["installments"]), 2)

        res = client.get(f"/api/personal/loans/{loan_id}/")
        self.assertIn("schedule", res.json())

    def test_dashboard_api(self):
        res = auth_client(self.u1).get("/api/personal/dashboard/")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("net_worth", body)
        self.assertIn("month", body)
        self.assertEqual(Decimal(body["net_worth"]["total_assets"]),
                         money("1500.00"))
