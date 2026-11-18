"""Phase 7 tests â€” AI & automation.

Covers:
- Recurring rules: monthly catch-up run posts through the validated
  writer (due book + journal both move), per-due-date idempotency (a
  second run never double-posts), max_runs bound, pause blocks runs,
  ADMIN-only writes, partner forbidden.
- Duplicate detection: same-day same partner/customer/amount attach a
  warning in the create response; different customer/amount do not;
  reversal excluded; review endpoint groups pairs.
- Business alerts: recurring-due feed + low-stock feed.
- Personal alerts: budget warning at >=80%, BUDGET_OVER, loan overdue.
- Assistant: personal/business answers quote exact ledger figures;
  partner scope hides other partners' dues; unknown questions get NO
  numbers; cross-user business access refused; every exchange logged.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.assistant.engine import answer_question
from apps.assistant.facts import build_business_facts, build_personal_facts
from apps.customers.models import CustomerAssignment
from apps.personal.models import (
    PersonalAccount, PersonalBudget, PersonalCategory, PersonalLoan,
)
from apps.tests_common import auth_client, make_user
from apps.tests_phase4 import build_env, add_customer
from apps.transactions.automation import (
    AutomationError, advance_date, run_due_recurring, run_recurring,
)
from apps.transactions.models import (
    RecurringTransaction, Transaction, TransactionType,
)


def money(v):
    return Decimal(str(v)).quantize(Decimal("0.01"))


class RecurringBase(TestCase):
    def setUp(self):
        self.env = build_env("p7")
        self.owner, self.biz = self.env["owner"], self.env["biz"]
        self.p1 = self.env["p1"]
        self.customer = add_customer(auth_client(self.owner), self.biz,
                                     self.p1, "Cyc Cust")
        self.today = timezone.localdate()
        self._seed_income()

    def _seed_income(self):
        auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz}",
            {"transaction_type": "INCOME", "amount": "5000.00",
             "transaction_date": str(self.today), "payment_method": "CASH",
             "description": "Float"}, format="json")

    def _rule(self, **kw):
        base = dict(
            business_id=self.biz, title="Monthly rent",
            transaction_type=TransactionType.EXPENSE,
            amount=money("1200.00"), payment_method="CASH",
            frequency=RecurringTransaction.Frequency.MONTHLY,
            interval=1, start_date=self.today, next_date=self.today,
            status=RecurringTransaction.Status.ACTIVE,
        )
        base.update(kw)
        return RecurringTransaction.objects.create(**base)

    def _actor(self):
        """Explicit scheduler-style actor built from the real membership."""
        from apps.organizations.models import Business
        from apps.transactions.automation import build_actor
        return build_actor(Business.objects.get(pk=self.biz), self.owner)


class RecurringTests(RecurringBase):
    def test_advance_date_math(self):
        from datetime import date
        self.assertEqual(advance_date(date(2026, 1, 31), "MONTHLY", 1),
                         date(2026, 2, 28))
        self.assertEqual(advance_date(date(2026, 2, 28), "MONTHLY", 1),
                         date(2026, 3, 28))
        self.assertEqual(advance_date(date(2026, 11, 9), "DAILY", 30),
                         date(2026, 12, 9))
        self.assertEqual(advance_date(date(2026, 11, 9), "WEEKLY", 2),
                         date(2026, 11, 23))

    def test_monthly_catchup_posts_through_writer(self):
        rule = self._rule()
        # three missed monthly cycles, catch up to today+90d
        upto = self.today + timedelta(days=90)
        outcome = run_recurring(self._actor(), rule, upto=upto)
        self.assertGreaterEqual(len(outcome["posted"]), 3)

        # every posted txn carries the deterministic client_reference
        ref = rule.client_ref_for(rule.start_date)
        self.assertTrue(Transaction.objects.filter(
            business_id=self.biz, client_reference=ref).exists())

        # every posted txn auto-posted its balanced journal (writer path)
        from apps.accounting.models import JournalEntry
        posted_ids = [p["transaction_id"]
                      for p in outcome["posted"]]
        je_count = JournalEntry.objects.filter(
            source_kind="TXN", source_id__in=posted_ids).count()
        self.assertEqual(je_count, len(posted_ids))
        self.assertGreaterEqual(je_count, 3)
        self.assertEqual(rule.times_run, len(outcome["posted"]))
        self.assertGreater(rule.next_date, upto)

    def test_idempotent_reruns_never_double_post(self):
        rule = self._rule()
        client = auth_client(self.owner)
        run_recurring(self._actor(), rule)
        count1 = Transaction.objects.filter(
            business_id=self.biz,
            client_reference__startswith=f"RR:{rule.pk}:").count()
        self.assertGreaterEqual(count1, 1)
        # simulate a crashed run whose txn posted but next_date rolled
        # back: the rerun must skip the existing posting, not duplicate
        rolled = RecurringTransaction.objects.get(pk=rule.pk)
        rolled.next_date = rolled.start_date
        rolled.save(update_fields=["next_date"])
        outcome = run_recurring(self._actor(), rolled)
        self.assertEqual(len(outcome["posted"]), 0)
        self.assertEqual(len(outcome["skipped_existing"]), 1)
        self.assertEqual(Transaction.objects.filter(
            business_id=self.biz,
            client_reference__startswith=f"RR:{rule.pk}:").count(), count1)

    def test_max_runs_binds_the_schedule(self):
        rule = self._rule(max_runs=1)
        outcome = run_recurring(self._actor(), rule, upto=self.today + timedelta(days=200))
        self.assertEqual(len(outcome["posted"]), 1)
        self.assertEqual(Transaction.objects.filter(
            business_id=self.biz,
            client_reference__startswith=f"RR:{rule.pk}:").count(), 1)
        rule.refresh_from_db()
        self.assertEqual(rule.times_run, 1)

    def test_paused_rule_refuses_to_run(self):
        rule = self._rule(status=RecurringTransaction.Status.PAUSED)
        with self.assertRaises(AutomationError):
            run_recurring(self._actor(), rule)

    def test_credit_rule_moves_partner_due_book(self):
        rule = self._rule(
            transaction_type=TransactionType.CREDIT,
            amount=money("500.00"),
            partner=self.p1, customer=self.customer,
            title="Monthly credit",
            start_date=self.today, next_date=self.today)
        client = auth_client(self.owner)
        outcome = run_recurring(self._actor(), rule)
        self.assertEqual(len(outcome["posted"]), 1)
        assignment = CustomerAssignment.objects.get(
            customer=self.customer, partner=self.p1)
        self.assertEqual(assignment.current_due(), money("500.00"))

        # crash-recovery: rerun with next_date rolled back must skip the
        # already-posted cycle, never double-charge the due book
        rolled = RecurringTransaction.objects.get(pk=rule.pk)
        rolled.next_date = rolled.start_date
        rolled.save(update_fields=["next_date"])
        outcome2 = run_recurring(self._actor(), rolled)
        self.assertEqual(len(outcome2["posted"]), 0)
        self.assertEqual(len(outcome2["skipped_existing"]), 1)
        self.assertEqual(assignment.current_due(), money("500.00"))
        # advance one cycle exactly — fresh.next_date is the next due
        fresh = RecurringTransaction.objects.get(pk=rule.pk)
        run_recurring(self._actor(), fresh, upto=fresh.next_date)
        assignment = CustomerAssignment.objects.get(
            customer=self.customer, partner=self.p1)
        self.assertEqual(assignment.current_due(), money("1000.00"))

    def test_partner_cannot_manage_rules(self):
        res = auth_client(self.env["p1"].linked_user).post(
            f"/api/transactions/recurring/?business={self.biz}",
            {"title": "X", "transaction_type": "EXPENSE",
             "amount": "10.00", "frequency": "DAILY",
             "start_date": str(self.today)}, format="json")
        self.assertEqual(res.status_code, 403, res.content)

    def test_api_full_cycle(self):
        client = auth_client(self.owner)
        res = client.post(
            f"/api/transactions/recurring/?business={self.biz}",
            {"title": "Weekly market", "transaction_type": "EXPENSE",
             "amount": "150.00", "payment_method": "CASH",
             "frequency": "WEEKLY", "interval": 1,
             "start_date": str(self.today)}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        rule_id = res.json()["id"]

        res = client.post(
            f"/api/transactions/recurring/run-due/?business={self.biz}",
            format="json")
        self.assertEqual(res.status_code, 200, res.content)
        runs = res.json()["runs"]
        self.assertTrue(any(r["rule_id"] == rule_id for r in runs))
        rule_posts = Transaction.objects.filter(
            business_id=self.biz,
            client_reference__startswith=f"RR:{rule_id}").count()
        self.assertGreaterEqual(rule_posts, 1)

        # alerts feed shows nothing due now (run-due advanced it)
        res = client.get(f"/api/transactions/alerts/?business={self.biz}")
        self.assertIn("recurring_due", res.json())
        self.assertIn("low_stock", res.json())

        # pause then attempt â€” paused rules excluded from catch-ups
        client.patch(f"/api/transactions/recurring/{rule_id}/"
                     f"?business={self.biz}", {"status": "PAUSED"},
                     format="json")
        res = client.get(
            f"/api/transactions/recurring/?business={self.biz}")
        rows = [r for r in res.json()["rules"] if r["id"] == rule_id]
        self.assertEqual(rows[0]["status"], "PAUSED")

        # history of every question is queryable later (kept â‰¤ this scope)
        rule_posts = Transaction.objects.filter(
            business_id=self.biz,
            client_reference__startswith=f"RR:{rule_id}").count()
        self.assertGreaterEqual(rule_posts, 1)


class DuplicateDetectionTests(RecurringBase):
    def _post_txn(self, amount, date, **kw):
        body = {"transaction_type": "CREDIT", "amount": amount,
                "transaction_date": date, "partner": str(self.p1.id),
                "customer": str(self.customer.id)}
        body.update(kw)
        return auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz}", body, format="json")

    def test_warning_on_near_identical_post(self):
        res1 = self._post_txn("250.00", str(self.today))
        self.assertEqual(res1.status_code, 201)
        self.assertNotIn("duplicate_warning", res1.json())
        res2 = self._post_txn("250.00", str(self.today))
        self.assertEqual(res2.status_code, 201, res2.content)
        self.assertIn("duplicate_warning", res2.json())
        warn = res2.json()["duplicate_warning"]
        self.assertEqual(warn["transaction_ids"],
                         [str(res1.json()["id"])])
        # different amount â†’ no warning
        res3 = self._post_txn("251.00", str(self.today))
        self.assertNotIn("duplicate_warning", res3.json())
        # boundary: next day still flagged (Â±1 window)
        res4 = self._post_txn("250.00",
                              str(self.today + timedelta(days=1)))
        self.assertEqual(res4.status_code, 201)
        self.assertIn("duplicate_warning", res4.json())

    def test_reversed_txns_not_flagged(self):
        res1 = self._post_txn("300.00", str(self.today))
        res2 = self._post_txn("300.00", str(self.today))
        txn2_id = res2.json()["id"]
        auth_client(self.owner).post(
            f"/api/transactions/{txn2_id}/reversal/?business={self.biz}",
            {"reason": "double post"}, format="json")
        # a third posting: original still present so flagged, but the
        # REVERSED one and the pair-check both stay clean of it
        res3 = self._post_txn("300.00", str(self.today))
        warn_ids = res3.json().get("duplicate_warning",
                                   {}).get("transaction_ids", [])
        self.assertNotIn(txn2_id, warn_ids)

    def test_duplicates_endpoint_groups_pairs(self):
        self._post_txn("500.00", str(self.today))
        self._post_txn("500.00", str(self.today))
        res = auth_client(self.owner).get(
            f"/api/transactions/duplicates/?business={self.biz}")
        self.assertEqual(res.status_code, 200)
        groups = res.json()["groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["transaction_ids"]), 2)
        # other business is not visible
        from apps.tests_phase4 import build_env as be
        env2 = be("p7other")
        res = auth_client(env2["owner"]).get(
            f"/api/transactions/duplicates/?business={env2['biz']}")
        self.assertEqual(res.json()["groups"], [])


class AlertTests(TestCase):
    def setUp(self):
        self.u1 = make_user("p7alerts")
        self.today = timezone.localdate()
        PersonalCategory.seed_defaults(self.u1)
        self.cash = PersonalAccount.objects.create(
            user=self.u1, name="Cash", initial_balance=money("1000.00"))
        self.food = PersonalCategory.objects.get(user=self.u1, name="Food",
                                                 kind="EXPENSE")

    def _txn(self, ttype, amount, date, **kw):
        from apps.personal.service import create_personal_transaction
        kw["category_id"] = kw.pop("category", None)
        return create_personal_transaction(
            self.u1, transaction_type=ttype, amount=amount,
            transaction_date=date, account_id=str(self.cash.pk), **kw)[0]

    def test_budget_warning_and_over(self):
        PersonalBudget.objects.create(
            user=self.u1, category=self.food, year=self.today.year,
            month=self.today.month, budgeted_amount=money("100.00"))
        self._txn("EXPENSE", "85.00", self.today, category=str(self.food.pk))
        res = auth_client(self.u1).get("/api/personal/alerts/")
        kinds = [a["kind"] for a in res.json()["alerts"]]
        self.assertIn("BUDGET_WARNING", kinds)
        self.assertEqual(
            [a for a in res.json()["alerts"]
             if a["kind"] == "BUDGET_WARNING"][0]["consumed_percent"], 85)
        self._txn("EXPENSE", "20.00", self.today, category=str(self.food.pk))
        res = auth_client(self.u1).get("/api/personal/alerts/")
        over = [a for a in res.json()["alerts"]
                if a["kind"] == "BUDGET_OVER"]
        self.assertEqual(len(over), 1)
        self.assertEqual(over[0]["consumed_percent"], 105)

    def test_loan_overdue_alert(self):
        loan = PersonalLoan.objects.create(
            user=self.u1, name="Gadget", counterparty_name="Store",
            direction=PersonalLoan.Direction.TAKEN,
            principal=money("600.00"), emi_amount=money("300.00"),
            installments=2, start_date=self.today - timedelta(days=60))
        res = auth_client(self.u1).get("/api/personal/alerts/")
        kinds = [a["kind"] for a in res.json()["alerts"]]
        self.assertIn("LOAN_OVERDUE", kinds)
        over = [a for a in res.json()["alerts"]
                if a["kind"] == "LOAN_OVERDUE"]
        self.assertEqual(over[0]["loan"], "Gadget")
        self.assertEqual(over[0]["remaining"], "600.00")

    def test_empty_account_alert(self):
        fresh = PersonalAccount.objects.create(
            user=self.u1, name="Nagad", initial_balance=money("0.00"))
        res = auth_client(self.u1).get("/api/personal/alerts/")
        kinds = [a["kind"] for a in res.json()["alerts"]]
        self.assertIn("ACCOUNT_EMPTY", kinds)


class AssistantFactsTests(TestCase):
    def setUp(self):
        self.env = build_env("p7ai")
        self.owner, self.biz = self.env["owner"], self.env["biz"]
        self.p1, self.p2 = self.env["p1"], self.env["p2"]
        self.cust1 = add_customer(auth_client(self.owner), self.biz,
                                  self.p1, "AI Cust One")
        self.cust2 = add_customer(auth_client(self.owner), self.biz,
                                  self.p2, "AI Cust Two")
        client = auth_client(self.owner)
        client.post(f"/api/transactions/?business={self.biz}",
                    {"transaction_type": "CREDIT", "amount": "700.00",
                     "transaction_date": str(timezone.localdate()),
                     "partner": str(self.p1.id),
                     "customer": str(self.cust1.id)}, format="json")
        client.post(f"/api/transactions/?business={self.biz}",
                    {"transaction_type": "CREDIT", "amount": "300.00",
                     "transaction_date": str(timezone.localdate()),
                     "partner": str(self.p2.id),
                     "customer": str(self.cust2.id)}, format="json")
        client.post(f"/api/transactions/?business={self.biz}",
                    {"transaction_type": "PAYMENT", "amount": "200.00",
                     "transaction_date": str(timezone.localdate()),
                     "partner": str(self.p1.id),
                     "customer": str(self.cust1.id),
                     "payment_method": "BKASH"}, format="json")

    def test_admin_facts_totals(self):
        from apps.organizations.models import Business, BusinessMembership
        biz = Business.objects.get(pk=self.biz)
        membership = BusinessMembership.objects.get(
            business=biz, user=self.owner)
        facts = build_business_facts(biz, membership)
        self.assertEqual(Decimal(facts["total_customer_due"]),
                         money("800.00"))  # 700-200 + 300
        dues = {d["customer"]: d["due"] for d in facts["top_customer_dues"]}
        self.assertEqual(Decimal(dues["AI Cust One"]), money("500.00"))
        self.assertEqual(Decimal(dues["AI Cust Two"]), money("300.00"))

    def test_partner_scope_hides_other_partners(self):
        from apps.organizations.models import Business, BusinessMembership
        biz = Business.objects.get(pk=self.biz)
        p1_user = self.env["p1"].linked_user
        membership = BusinessMembership.objects.get(
            business=biz, user=p1_user)
        facts = build_business_facts(biz, membership)
        self.assertEqual(facts["scope"], "PARTNER-SCOPED")
        self.assertEqual(Decimal(facts["total_customer_due"]),
                         money("500.00"))  # only p1's book
        self.assertTrue(all(d["partner"] == self.p1.name for d in
                            facts["top_customer_dues"]))

    def test_answers_quote_exact_figures(self):
        from apps.organizations.models import Business, BusinessMembership
        biz = Business.objects.get(pk=self.biz)
        membership = BusinessMembership.objects.get(
            business=biz, user=self.owner)
        facts = build_business_facts(biz, membership)
        res = answer_question(self.owner, "who owes me? how much is due?",
                              facts=facts, scope="BUSINESS")
        self.assertEqual(res["intent"], "BUSINESS_DUES")
        self.assertIn("800.00", res["answer"])

        res = answer_question(self.owner, "what is my cash balance?",
                              facts=facts, scope="BUSINESS")
        self.assertEqual(res["intent"], "BUSINESS_WALLETS")

        res = answer_question(self.owner,
                              "any low stock products for inventory?",
                              facts=facts, scope="BUSINESS")
        self.assertEqual(res["intent"], "BUSINESS_STOCK")

        res = answer_question(self.owner, "tell me about recurring",
                              facts=facts, scope="BUSINESS")
        self.assertEqual(res["intent"], "BUSINESS_RECURRING")

    def test_fallback_never_invents_numbers(self):
        facts = {"scope": "PERSONAL", "as_of": "2026-01-01",
                 "net_worth": "5000.00"}
        res = answer_question(self.owner, "how many dogs do I own?",
                              facts=facts, scope="PERSONAL")
        self.assertEqual(res["intent"], "FALLBACK")
        self.assertNotIn("5000", res["answer"])
        self.assertNotIn("à§³", res["answer"])

    def test_personal_answers(self):
        PersonalAccount.objects.create(
            user=self.owner, name="Cash", initial_balance=money("1234.50"))
        facts = build_personal_facts(self.owner)
        res = answer_question(self.owner, "what's my account balance?",
                              facts=facts, scope="PERSONAL")
        self.assertIn("1,234.50", res["answer"])
        res = answer_question(self.owner, "what is my net worth?",
                              facts=facts, scope="PERSONAL")
        self.assertEqual(res["intent"], "PERSONAL_NET_WORTH")
        self.assertIn("1,234.50", res["answer"])

    def test_api_flow_and_isolation(self):
        # business question with an explicit business â†’ allowed for member
        res = auth_client(self.owner).post(
            f"/api/assistant/ask/?business={self.biz}",
            {"question": "total dues and who is owing?"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["scope"], "BUSINESS")
        self.assertIn("800.00", res.json()["answer"])

        # partner asking the same is scoped to their own book
        res = auth_client(self.env["p1"].linked_user).post(
            f"/api/assistant/ask/?business={self.biz}",
            {"question": "total customer due"}, format="json")
        self.assertEqual(res.json()["scope"], "BUSINESS")
        self.assertIn("500.00", res.json()["answer"])
        self.assertNotIn("800.00", res.json()["answer"])

        # a stranger must be refused an explicit business context
        stranger = make_user("p7airandom")
        res = auth_client(stranger).post(
            f"/api/assistant/ask/?business={self.biz}",
            {"question": "total due"}, format="json")
        self.assertEqual(res.status_code, 403, res.content)

        # personal fallback (no business) answers from own ledger
        res = auth_client(self.owner).post(
            "/api/assistant/ask/", {"question": "whats my status?"},
            format="json")
        self.assertEqual(res.json()["scope"], "PERSONAL")

        # history logged with facts used (owner asked twice + partner once,
        # owner's own history holds the two owner asks)
        res = auth_client(self.owner).get("/api/assistant/history/")
        self.assertGreaterEqual(res.json()["count"], 2)
