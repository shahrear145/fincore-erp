"""Phase 3 tests — transactions, due management, date memory (Section 23).

Covered here:
- Decimal-exact amount math for partial/full payments (allocation).
- Over-collection rejected; advance path allowed.
- Duplicate prevention via client_reference (offline-sync idempotency).
- Backdated transactions + locked-period rejection.
- Last-used date memory: persistence across restart (DB-backed) AND
  partner-specific memory that never overwrites another partner's date.
- Partner A cannot collect Partner B's due; ADMIN can.
- Customer due-limit enforcement + ADMIN bypass.
- Reversal rules: payment frees allocations; credit blocked while
  allocations exist; audit trail row exists.
- Partner list scoping: partner sees only their own transactions.
- Summary totals accuracy.
"""
from datetime import date
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.test import TestCase

from apps.core.middleware import AuditLog
from apps.customers.models import Customer, CustomerAssignment, PaymentAllocation
from apps.organizations.models import FinancialPeriod
from apps.tests_common import auth_client, make_user


def _bootstrap(partners=("One", "Two"), with_users=True):
    owner = make_user("towner")
    res = auth_client(owner).post("/api/businesses/", {"name": "T Traders"},
                                  format="json")
    biz = res.json()["id"]
    result = [owner, biz]
    for n, name in enumerate(partners):
        if with_users:
            user = make_user(f"tpartner{n + 1}")
            auth_client(owner).post(
                f"/api/businesses/{biz}/members/",
                {"email": user.email, "role": "PARTNER"}, format="json")
        else:
            user = None
        payload = {"name": name}
        if user:
            payload["linked_user"] = str(user.id)
        pid = auth_client(owner).post(
            f"/api/partners/?business={biz}", payload, format="json"
        ).json()["id"]
        from apps.partners.models import Partner
        result.append(Partner.objects.get(pk=pid))
    return result


def _customer(owner, biz, partner, opening="5000.00"):
    res = auth_client(owner).post(
        f"/api/customers/?business={biz}",
        {"name": f"Cust of {partner.name}", "opening_balance": opening,
         "partner_id": str(partner.id)}, format="json")
    return Customer.objects.get(pk=res.json()["id"])


def _txn(actor, biz, partner=None, customer=None, ttype="PAYMENT",
         amount="100.00", txdate="2026-09-10", **extra):
    body = {"transaction_type": ttype, "amount": amount,
            "transaction_date": txdate, **extra}
    if partner is not None:
        body["partner"] = str(partner.id)
    if customer is not None:
        body["customer"] = str(customer.id)
    return auth_client(actor).post(f"/api/transactions/?business={biz}",
                                   body, format="json")


class TransactionLedgerTests(TestCase):
    def setUp(self):
        self.owner, self.biz, self.p1, self.p2 = _bootstrap()
        self.p1u = self.p1.linked_user
        self.c1 = _customer(self.owner, self.biz, self.p1, "5000.00")

    def test_exact_decimal_partial_and_full_collection(self):
        pay = _txn(self.owner, self.biz, self.p1, self.c1, amount="123.25",
                   txdate="2026-09-11")
        self.assertEqual(pay.status_code, 201)
        a = CustomerAssignment.objects.get(customer=self.c1, partner=self.p1)
        self.assertEqual(a.current_due(), Decimal("4876.75"))
        pay2 = _txn(self.owner, self.biz, self.p1, self.c1, amount="4876.75",
                    txdate="2026-09-13")
        self.assertEqual(pay2.status_code, 201)
        self.assertEqual(a.current_due(), Decimal("0.00"))

    def test_allocation_oldest_first(self):
        _txn(self.owner, self.biz, self.p1, self.c1, ttype="CREDIT",
             amount="1000.00", txdate="2026-09-01")
        _txn(self.owner, self.biz, self.p1, self.c1, ttype="CREDIT",
             amount="2000.00", txdate="2026-09-05")
        a = CustomerAssignment.objects.get(customer=self.c1, partner=self.p1)
        self.assertEqual(a.current_due(), Decimal("8000.00"))
        pay = _txn(self.owner, self.biz, self.p1, self.c1, amount="1500.00",
                   txdate="2026-09-08")
        allocs = PaymentAllocation.objects.filter(
            collection_transaction_id=pay.json()["id"]) \
            .order_by("credit_entry__transaction_date")
        self.assertEqual(len(allocs), 2)
        first, second = allocs
        self.assertEqual(Decimal(str(first.amount)), Decimal("1000.00"))
        self.assertEqual(Decimal(str(second.amount)), Decimal("500.00"))

    def test_overcollection_rejected_advance_allowed(self):
        res = _txn(self.owner, self.biz, self.p1, self.c1, amount="6000.00",
                   txdate="2026-09-11")
        self.assertEqual(res.status_code, 400)
        self.assertIn("exceeds", str(res.json()))
        adv = _txn(self.owner, self.biz, self.p1, self.c1, ttype="ADVANCE",
                   amount="6000.00", txdate="2026-09-11")
        self.assertEqual(adv.status_code, 201)
        a = CustomerAssignment.objects.get(customer=self.c1, partner=self.p1)
        self.assertEqual(a.current_due(), Decimal("-1000.00"))

    def test_duplicate_prevented_by_client_reference(self):
        res1 = _txn(self.owner, self.biz, self.p1, self.c1, amount="50.00",
                    client_reference="dup-xyz")
        res2 = _txn(self.owner, self.biz, self.p1, self.c1, amount="50.00",
                    client_reference="dup-xyz")
        self.assertEqual(res1.status_code, 201)
        self.assertEqual(res2.status_code, 201)
        self.assertEqual(res2.json()["id"], res1.json()["id"])

    def test_backdated_ok_and_locked_period_rejected(self):
        ok = _txn(self.owner, self.biz, self.p1, self.c1, amount="10.00",
                  txdate="2026-09-01")
        self.assertEqual(ok.status_code, 201)
        period = FinancialPeriod.objects.create(
            business_id=self.biz, name="LOCK-SEP",
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 30))
        FinancialPeriod.objects.filter(pk=period.pk).update(
            is_locked=True, locked_at=None)
        period.refresh_from_db()
        blocked = _txn(self.owner, self.biz, self.p1, self.c1, amount="10.00",
                       txdate="2026-09-02")
        self.assertEqual(blocked.status_code, 403)

    def test_due_limit_enforced_admin_bypass(self):
        customer = _customer(self.owner, self.biz, self.p1, "0.00")
        from apps.customers.models import Customer as C
        C.objects.filter(pk=customer.pk).update(due_limit=Decimal("1000.00"))
        blocked = _txn(self.owner, self.biz, self.p1, customer, ttype="CREDIT",
                       amount="1500.00", txdate="2026-09-11")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("due limit", str(blocked.json()))
        allowed = _txn(self.owner, self.biz, self.p1, customer, ttype="CREDIT",
                       amount="1500.00", txdate="2026-09-11",
                       allow_beyond_limit=True)
        self.assertEqual(allowed.status_code, 201)


class DateMemoryTests(TestCase):
    def setUp(self):
        self.owner, self.biz, self.p1, self.p2 = _bootstrap()
        self.p1u, self.p2u = self.p1.linked_user, self.p2.linked_user

    def test_memory_set_and_partner_specific(self):
        res = _txn(self.p1u, self.biz, self.p1, ttype="INCOME",
                   amount="10.00", txdate="2026-09-10")
        self.assertEqual(res.status_code, 201)
        m1 = auth_client(self.p1u).get(
            f"/api/transactions/date-memory/?business={self.biz}"
            f"&partner={self.p1.id}").json()
        self.assertEqual(m1["last_date"], "2026-09-10")

        # Partner B still has NO memory — must not inherit partner A's date.
        m2 = auth_client(self.p2u).get(
            f"/api/transactions/date-memory/?business={self.biz}"
            f"&partner={self.p2.id}").json()
        self.assertIsNone(m2["last_date"])

        _txn(self.p2u, self.biz, self.p2, ttype="INCOME", amount="5.00",
             txdate="2026-09-15")
        m1 = auth_client(self.p1u).get(
            f"/api/transactions/date-memory/?business={self.biz}"
            f"&partner={self.p1.id}").json()
        m2 = auth_client(self.p2u).get(
            f"/api/transactions/date-memory/?business={self.biz}"
            f"&partner={self.p2.id}").json()
        self.assertEqual(m1["last_date"], "2026-09-10")   # A not overwritten
        self.assertEqual(m2["last_date"], "2026-09-15")

    def test_memory_persists_and_manual_set_and_reset(self):
        c = auth_client(self.p1u)
        _txn(self.p1u, self.biz, self.p1, ttype="INCOME", amount="1.00",
             txdate="2026-09-01")
        self.assertEqual(
            c.get(f"/api/transactions/date-memory/?business={self.biz}"
                  f"&partner={self.p1.id}").json()["last_date"],
            "2026-09-01")
        # Manual set (user picks another date in the UI)
        c.post(f"/api/transactions/date-memory/?business={self.biz}",
               {"date": "2026-08-30", "partner": str(self.p1.id)},
               format="json")
        self.assertEqual(
            c.get(f"/api/transactions/date-memory/?business={self.biz}"
                  f"&partner={self.p1.id}").json()["last_date"],
            "2026-08-30")
        # Reset to default
        c.delete(f"/api/transactions/date-memory/?business={self.biz}"
                 f"&partner={self.p1.id}")
        self.assertIsNone(
            c.get(f"/api/transactions/date-memory/?business={self.biz}"
                  f"&partner={self.p1.id}").json()["last_date"])

    def test_today_yesterday_buttons_are_just_date_sets(self):
        c = auth_client(self.p1u)
        c.post(f"/api/transactions/date-memory/?business={self.biz}",
               {"date": "2026-09-08", "partner": str(self.p1.id)},
               format="json")
        r = c.get(f"/api/transactions/date-memory/?business={self.biz}"
                  f"&partner={self.p1.id}").json()
        self.assertEqual(r["last_date"], "2026-09-08")


class PartnerScopeTests(TestCase):
    def setUp(self):
        self.owner, self.biz, self.p1, self.p2 = _bootstrap()
        self.p1u = self.p1.linked_user
        self.c1 = _customer(self.owner, self.biz, self.p1, "1000.00")
        self.c2 = None
        res = auth_client(self.owner).post(
            f"/api/customers/?business={self.biz}",
            {"name": "Cust of P2", "opening_balance": "900.00",
             "partner_id": str(self.p2.id)}, format="json")
        from apps.customers.models import Customer as C
        self.c2 = C.objects.get(pk=res.json()["id"])

    def test_partner_cannot_collect_other_partners_due(self):
        res = _txn(self.p1u, self.biz, self.p2, self.c2, amount="100.00",
                   txdate="2026-09-11")
        self.assertEqual(res.status_code, 403)

    def test_admin_collects_exactly_one_partners_due(self):
        res = _txn(self.owner, self.biz, self.p2, self.c2, amount="100.00",
                   txdate="2026-09-11")
        self.assertEqual(res.status_code, 201)
        a1 = CustomerAssignment.objects.get(customer=self.c1, partner=self.p1)
        a2 = CustomerAssignment.objects.get(customer=self.c2, partner=self.p2)
        self.assertEqual(a1.current_due(), Decimal("1000.00"))
        self.assertEqual(a2.current_due(), Decimal("800.00"))

    def test_partner_sees_only_own_transactions(self):
        _txn(self.owner, self.biz, self.p1, self.c1, amount="10.00",
             txdate="2026-09-10")
        _txn(self.owner, self.biz, self.p2, self.c2, amount="20.00",
             txdate="2026-09-10")
        mine = auth_client(self.p1u).get(
            f"/api/transactions/?business={self.biz}").json()
        self.assertEqual(mine["count"], 1)
        self.assertEqual(
            Decimal(str(mine["results"][0]["amount"])), Decimal("10.00"))

    def test_receipt_generated_with_allocations(self):
        _txn(self.owner, self.biz, self.p1, self.c1, ttype="CREDIT",
             amount="500.00", txdate="2026-09-01")
        res = _txn(self.owner, self.biz, self.p1, self.c1, amount="400.00",
                   txdate="2026-09-11")
        txn_id = res.json()["id"]
        self.assertTrue(res.json()["receipt_no"].startswith("RCP-"))
        receipt = auth_client(self.owner).get(
            f"/api/transactions/{txn_id}/receipt/?business={self.biz}").json()
        self.assertEqual(receipt["receipt_no"], res.json()["receipt_no"])
        self.assertEqual(Decimal(str(receipt["amount"])), Decimal("400.00"))
        self.assertEqual(len(receipt["allocations"]), 1)

    def test_reversal_frees_and_restores(self):
        res = _txn(self.owner, self.biz, self.p1, self.c1, amount="400.00",
                   txdate="2026-09-11")
        txn_id = res.json()["id"]
        a = CustomerAssignment.objects.get(customer=self.c1, partner=self.p1)
        rev = auth_client(self.owner).post(
            f"/api/transactions/{txn_id}/reversal/?business={self.biz}",
            {"reason": "wrong entry"}, format="json")
        self.assertEqual(rev.status_code, 200)
        self.assertEqual(a.current_due(), Decimal("1000.00"))
        self.assertGreater(
            AuditLog.objects.filter(resource_id=str(txn_id),
                                    action="reversal").count(), 0)

    def test_credit_blocked_while_allocated(self):
        res = _txn(self.owner, self.biz, self.p1, self.c1, ttype="CREDIT",
                   amount="500.00", txdate="2026-09-01")
        _txn(self.owner, self.biz, self.p1, self.c1, amount="200.00",
             txdate="2026-09-02")
        auth_client(self.owner).post(
            f"/api/transactions/{res.json()['id']}/reversal/?business={self.biz}",
            {"reason": "should fail"}, format="json")
        rev = auth_client(self.owner).post(
            f"/api/transactions/{res.json()['id']}/reversal/?business={self.biz}",
            {"reason": "should fail"}, format="json")
        self.assertEqual(rev.status_code, 400)
        self.assertIn("allocated", str(rev.json()))

    def test_summary_totals(self):
        _txn(self.owner, self.biz, self.p1, self.c1, ttype="CREDIT",
             amount="1000.00", txdate="2026-09-01")
        _txn(self.owner, self.biz, self.p1, self.c1, amount="400.00",
             txdate="2026-09-02")
        _txn(self.owner, self.biz, None, None, ttype="EXPENSE",
             amount="150.00", txdate="2026-09-03")
        summary = auth_client(self.owner).get(
            f"/api/transactions/summary/?business={self.biz}"
            "&from=2026-09-01&to=2026-09-30").json()
        self.assertEqual(
            Decimal(str(summary["totals_by_type"]["CREDIT"])), Decimal("1000.00"))
        self.assertEqual(
            Decimal(str(summary["totals_by_type"]["PAYMENT"])), Decimal("400.00"))
        self.assertEqual(
            Decimal(str(summary["totals_by_type"]["EXPENSE"])), Decimal("150.00"))
        self.assertEqual(len(summary["daily_series"]), 3)
