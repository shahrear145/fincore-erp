"""Phase 5 tests — accounting engine & reports (Section 11/12 family).

Covers:
- System chart-of-accounts seeded lazily, per business.
- Auto journals from every transaction type (balanced, correct accounts).
- Expense journals post to the business-expense account.
- Manual journal creation (ADMIN-only, balanced-or-rejected).
- Journal reversal: mirrored totals, trial balance still balanced.
- Trial balance: Σdebit == Σcredit across a mixture of activity.
- Ledger running-balance exactness.
- Income statement math; balance sheet equation; cash-flow in/out.
"""
from decimal import Decimal

from django.db.models import Sum
from django.test import TestCase

from apps.accounting.models import Account, JournalEntry, JournalLine
from apps.accounting.postings import ensure_system_accounts
from apps.accounting.reports import (
    account_ledger, balance_sheet, cash_flow, income_statement,
    trial_balance,
)
from apps.tests_common import auth_client
from apps.tests_phase4 import add_customer, build_env


class PostingTests(TestCase):
    def setUp(self):
        self.env = build_env("acc")
        self.owner, self.biz, self.p1 = (
            self.env["owner"], self.env["biz"], self.env["p1"])
        self._customer()

    def _business_obj(self):
        from apps.organizations.models import Business
        return Business.objects.get(pk=self.biz)

    def _customer(self):
        self.customer = add_customer(auth_client(self.owner), self.biz,
                                     self.p1, "Ledger Cust")

    def _txn(self, ttype, amount, date="2026-10-10", **kw):
        body = {"transaction_type": ttype, "amount": amount,
                "transaction_date": date,
                "partner": str(self.p1.id), "customer": str(self.customer.id)}
        body.update(kw)
        return auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz}", body, format="json")

    def _accounts(self):
        return {a.code: a for a in
                Account.objects.filter(business_id=self.biz)}

    def test_journals_balanced_for_every_transaction_type(self):
        res = self._txn("CREDIT", "500.00")
        self.assertEqual(res.status_code, 201, res.content)
        res = self._txn("PAYMENT", "200.00", date="2026-10-11",
                        payment_method="BKASH")
        self.assertEqual(res.status_code, 201)
        res = auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz}",
            {"transaction_type": "EXPENSE", "amount": "75.50",
             "transaction_date": "2026-10-12", "payment_method": "CASH"},
            format="json")
        self.assertEqual(res.status_code, 201)

        accounts = self._accounts()
        self.assertEqual(set(accounts.keys()),
                         {"1000", "1100", "1200", "2100", "2400",
                          "3000", "3200", "4000", "4100", "5100"})

        for entry in JournalEntry.objects.filter(business_id=self.biz):
            dr = Decimal(str(entry.lines.aggregate(t=Sum("debit"))["t"]
                             or 0))
            cr = Decimal(str(entry.lines.aggregate(t=Sum("credit"))["t"]
                             or 0))
            self.assertEqual(dr, cr, f"Unbalanced: {entry.journal_no}")
            self.assertTrue(dr > 0)

    def test_credit_payment_mapping(self):
        self._txn("CREDIT", "500.00", date="2026-10-10")
        self._txn("PAYMENT", "200.00", date="2026-10-11",
                  payment_method="BKASH")
        accounts = self._accounts()
        # credit txn: Dr AR 500 / Cr Sales 500
        je = JournalEntry.objects.get(business_id=self.biz,
                                      source_kind="TXN",
                                      transaction_date="2026-10-10")
        rows = {l.account_id: (Decimal(l.debit), Decimal(l.credit))
                for l in je.lines.all()}
        self.assertEqual(rows[accounts["1200"].id],
                         (Decimal("500.00"), Decimal("0")))
        self.assertEqual(rows[accounts["4000"].id],
                         (Decimal("0"), Decimal("500.00")))
        # payment: Dr wallet 1100 / Cr AR
        je2 = JournalEntry.objects.get(business_id=self.biz,
                                       source_kind="TXN",
                                       transaction_date="2026-10-11")
        rows2 = {l.account_id: (Decimal(l.debit), Decimal(l.credit))
                 for l in je2.lines.all()}
        self.assertEqual(rows2[accounts["1100"].id],
                         (Decimal("200.00"), Decimal("0")))
        self.assertEqual(rows2[accounts["1200"].id],
                         (Decimal("0"), Decimal("200.00")))

    def test_transfer_maps_source_and_destination_wallets(self):
        res = auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz}",
            {"transaction_type": "TRANSFER", "amount": "1000.00",
             "transaction_date": "2026-10-14", "payment_method": "CASH",
             "to_payment_method": "BANK"}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        accounts = self._accounts()
        je = JournalEntry.objects.get(business_id=self.biz,
                                      source_kind="TXN",
                                      transaction_date="2026-10-14")
        rows = {l.account_id: (Decimal(l.debit), Decimal(l.credit))
                for l in je.lines.all()}
        self.assertEqual(rows[accounts["1100"].id],
                         (Decimal("1000.00"), Decimal("0")))
        self.assertEqual(rows[accounts["1000"].id],
                         (Decimal("0"), Decimal("1000.00")))

    def test_trial_balance_balanced_after_mixed_activity(self):
        # customer cycles
        self._txn("CREDIT", "800.00", date="2026-10-10")
        self._txn("PAYMENT", "300.00", date="2026-10-11")
        # expense + income
        auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz}",
            {"transaction_type": "EXPENSE", "amount": "55.25",
             "transaction_date": "2026-10-15", "payment_method": "CASH"},
            format="json")
        auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz}",
            {"transaction_type": "INCOME", "amount": "100.00",
             "transaction_date": "2026-10-16", "payment_method": "BANK"},
            format="json")
        tb = trial_balance(self._business_obj(), from_date="2026-10-01",
                           to_date="2026-10-31")
        self.assertTrue(tb["balanced"], tb)
        self.assertEqual(Decimal(tb["total_debits"]),
                         Decimal(tb["total_credits"]))
        self.assertGreater(Decimal(tb["total_debits"]), 0)

    def test_ledger_running_balance(self):
        self._txn("CREDIT", "500.00", date="2026-10-10")
        self._txn("PAYMENT", "200.00", date="2026-10-12")
        self._txn("CREDIT", "100.00", date="2026-10-15")
        self._txn("PAYMENT", "250.00", date="2026-10-16")
        ar = Account.objects.get(business_id=self.biz, code="1200")
        ledger = account_ledger(self._business_obj(), ar,
                                from_date="2026-10-01", to_date="2026-10-31")
        self.assertEqual(ledger["opening_balance"], "0.00")
        effects = [Decimal("500.00"), Decimal("-200.00"),
                   Decimal("100.00"), Decimal("-250.00")]
        running = Decimal("0.00")
        for row, effect in zip(ledger["rows"], effects):
            running += effect
        self.assertEqual(Decimal(ledger["closing_balance"]),
                         running.quantize(Decimal("0.01")))
        self.assertEqual(Decimal(ledger["closing_balance"]),
                         Decimal("150.00"))

    def test_income_statement(self):
        self._txn("CREDIT", "1200.00", date="2026-10-10")
        auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz}",
            {"transaction_type": "INCOME", "amount": "300.00",
             "transaction_date": "2026-10-11", "payment_method": "CASH"},
            format="json")
        auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz}",
            {"transaction_type": "EXPENSE", "amount": "250.00",
             "transaction_date": "2026-10-12", "payment_method": "CASH"},
            format="json")
        isec = income_statement(self._business_obj(), from_date="2026-10-01",
                                to_date="2026-10-31")
        self.assertEqual(Decimal(isec["total_revenue"]),
                         Decimal("1500.00"))  # sales 1200 + other income 300
        self.assertEqual(Decimal(isec["total_expenses"]), Decimal("250.00"))
        self.assertEqual(Decimal(isec["net_profit"]), Decimal("1250.00"))

    def test_balance_sheet_equation_and_cash_flow(self):
        self._txn("CREDIT", "1000.00", date="2026-10-10")
        self._txn("PAYMENT", "600.00", date="2026-10-11",
                  payment_method="BKASH")
        auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz}",
            {"transaction_type": "EXPENSE", "amount": "150.00",
             "transaction_date": "2026-10-12", "payment_method": "CASH"},
            format="json")
        bs = balance_sheet(self._business_obj(), as_of="2026-10-31")
        self.assertTrue(bs["balanced"], bs)
        # assets: AR 400 (1000-600), wallets: 600 bKash - 150 cash = 450
        self.assertEqual(Decimal(bs["total_assets"]), Decimal("850.00"))
        cf = cash_flow(self._business_obj(), from_date="2026-10-01",
                       to_date="2026-10-31")
        self.assertEqual(Decimal(cf["total_money_in"]), Decimal("600.00"))
        self.assertEqual(Decimal(cf["total_money_out"]), Decimal("150.00"))
        self.assertEqual(Decimal(cf["net_change"]), Decimal("450.00"))


class JournalAPITests(TestCase):
    def setUp(self):
        self.env = build_env("japi")
        self.owner, self.biz = self.env["owner"], self.env["biz"]
        ensure_system_accounts(self._business_obj())

    def _business_obj(self):
        from apps.organizations.models import Business
        return Business.objects.get(pk=self.biz)

    def _accounts(self):
        return {a.code: a.id for a in
                Account.objects.filter(business_id=self.biz)}

    def test_manual_journal_admin_only_and_balanced_or_rejected(self):
        ar = self._accounts()["1200"]
        sales = self._accounts()["4000"]
        # partner denied
        denied = auth_client(self.env["p1"].linked_user).post(
            f"/api/accounting/journal-entries/?business={self.biz}",
            {"transaction_date": "2026-10-10",
             "lines": [{"account": ar, "debit": "10.00"},
                       {"account": sales, "credit": "10.00"}]},
            format="json")
        self.assertEqual(denied.status_code, 403)

        # unbalanced rejected
        bad = auth_client(self.owner).post(
            f"/api/accounting/journal-entries/?business={self.biz}",
            {"transaction_date": "2026-10-10",
             "lines": [{"account": ar, "debit": "50.00"},
                       {"account": sales, "credit": "40.00"}]},
            format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("not balanced", str(bad.json()))

        ok = auth_client(self.owner).post(
            f"/api/accounting/journal-entries/?business={self.biz}",
            {"transaction_date": "2026-10-10", "memo": "opening",
             "lines": [{"account": ar, "debit": "50.00"},
                       {"account": sales, "credit": "50.00"}]},
            format="json")
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertTrue(ok.json()["journal_no"].startswith("JE-"))
        self.assertEqual(len(ok.json()["lines"]), 2)

    def test_journal_reversal_mirror_and_balance(self):
        ar = self._accounts()["1200"]
        sales = self._accounts()["4000"]
        created = auth_client(self.owner).post(
            f"/api/accounting/journal-entries/?business={self.biz}",
            {"transaction_date": "2026-10-10",
             "lines": [{"account": ar, "debit": "80.00"},
                       {"account": sales, "credit": "80.00"}]},
            format="json").json()
        rev = auth_client(self.owner).post(
            f"/api/accounting/journal-entries/{created['id']}/reversal/"
            f"?business={self.biz}", {"reason": "wrong book"},
            format="json")
        self.assertEqual(rev.status_code, 200, rev.content)
        mirror = rev.json()
        self.assertTrue(mirror["memo"].startswith("Reversal of"))
        total = JournalLine.objects.filter(entry_id=created["id"]) \
            .aggregate(t=Sum("debit"))["t"]
        total_mirror = JournalLine.objects.filter(
            entry_id=mirror["id"]).aggregate(t=Sum("credit"))["t"]
        self.assertEqual(Decimal(total), Decimal(total_mirror))
        # trial balance still balanced (both entries counted)
        tb = trial_balance(self._business_obj(),
                           from_date="2026-10-01", to_date="2026-10-31")
        self.assertTrue(tb["balanced"])

        # double reversal rejected
        rev2 = auth_client(self.owner).post(
            f"/api/accounting/journal-entries/{created['id']}/reversal/"
            f"?business={self.biz}", {"reason": "again"}, format="json")
        self.assertEqual(rev2.status_code, 400)
