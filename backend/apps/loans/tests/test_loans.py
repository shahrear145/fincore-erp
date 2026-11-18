"""Loan & Investment tests — the main-account impact must stay exact.

Covers:
- a loan taken / capital invested ADDS to the main (cash) account;
- a repayment / withdrawal SUBTRACTS from it;
- per-party outstanding balances stay exact;
- more can never be returned than what the party still has with the business;
- the double-entry journal stays balanced and the money is never income;
- corrections reverse + repost (never a hard delete).
"""
from decimal import Decimal

from django.test import Client, TestCase

from apps.accounting.models import Account
from apps.accounting.postings import ensure_system_accounts
from apps.accounting.reports import balance_sheet, trial_balance
from apps.loans.models import LoanEntry, LoanParty
from apps.organizations.models import Business
from apps.tests_common import auth_client, make_user
from apps.transactions.models import Transaction, TransactionStatus, TransactionType


class LoanFlowTests(TestCase):
    def setUp(self):
        self.owner = make_user("loanowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Loan Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        ensure_system_accounts(self.biz, self.owner)
        self.client = Client()
        self.client.force_login(self.owner)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session.save()

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _book_url(kind):
        """Each book has its own page — a party only lives in its book."""
        return "/loans/" if kind == LoanParty.Kind.LOAN else "/investments/"

    def _party(self, name="করিম ভাই", kind=LoanParty.Kind.LOAN):
        self.client.post(self._book_url(kind), {
            "action": "add_party", "name": name, "kind": kind,
            "phone": "", "email": ""})
        return LoanParty.objects.get(business=self.biz, name=name)

    def _entry(self, party, kind, amount, tx_date, description=""):
        self.client.post(self._book_url(party.kind), {
            "action": "add_entry", "party_id": str(party.id), "kind": kind,
            "amount": amount, "transaction_date": tx_date,
            "description": description, "notes": ""})
        return LoanEntry.objects.filter(business=self.biz,
                                        description=description).first()

    def _txns(self, txn_type):
        return Transaction.objects.filter(business=self.biz,
                                          transaction_type=txn_type)

    # -- tests -----------------------------------------------------------
    def test_loan_taken_is_added_to_the_main_account(self):
        party = self._party()
        entry = self._entry(party, LoanEntry.Kind.RECEIVED, "10000.00",
                            "2026-11-01", "loan")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.transaction.transaction_type,
                         TransactionType.BORROW)
        self.assertEqual(entry.transaction.payment_method, "CASH")
        self.assertEqual(entry.signed_amount, Decimal("10000.00"))
        self.assertEqual(party.outstanding(), Decimal("10000.00"))

    def test_investment_received_is_added_and_withdrawal_subtracts(self):
        investor = self._party("রহিম ভাই", LoanParty.Kind.INVESTMENT)
        self._entry(investor, LoanEntry.Kind.RECEIVED, "5000.00",
                    "2026-11-02", "invest")
        self._entry(investor, LoanEntry.Kind.WITHDRAWAL, "2000.00",
                    "2026-11-06", "withdraw")
        self.assertEqual(investor.outstanding(), Decimal("3000.00"))
        self.assertEqual(self._txns(TransactionType.INVEST).count(), 1)
        self.assertEqual(self._txns(TransactionType.WITHDRAW).count(), 1)

    def test_repayment_subtracts_and_cannot_exceed_the_balance(self):
        party = self._party()
        self._entry(party, LoanEntry.Kind.RECEIVED, "10000.00",
                    "2026-11-01", "loan")
        self._entry(party, LoanEntry.Kind.REPAYMENT, "3000.00",
                    "2026-11-05", "repay")
        self.assertEqual(party.outstanding(), Decimal("7000.00"))
        self.assertEqual(self._txns(TransactionType.REPAY).count(), 1)

        # too much back — rejected, nothing written
        before = LoanEntry.objects.filter(business=self.biz).count()
        self.client.post("/loans/", {
            "action": "add_entry", "party_id": str(party.id),
            "kind": "REPAYMENT", "amount": "99999.00",
            "transaction_date": "2026-11-07", "description": "too much"})
        self.assertEqual(
            LoanEntry.objects.filter(business=self.biz).count(), before)
        party.refresh_from_db()
        self.assertEqual(party.outstanding(), Decimal("7000.00"))

    def test_high_level_summary_is_exact(self):
        lender = self._party("করিম ভাই")
        investor = self._party("রহিম ভাই", LoanParty.Kind.INVESTMENT)
        self._entry(lender, LoanEntry.Kind.RECEIVED, "10000.00",
                    "2026-11-01", "loan")
        self._entry(investor, LoanEntry.Kind.RECEIVED, "5000.00",
                    "2026-11-02", "invest")
        self._entry(lender, LoanEntry.Kind.REPAYMENT, "3000.00",
                    "2026-11-05", "repay")
        self._entry(investor, LoanEntry.Kind.WITHDRAWAL, "2000.00",
                    "2026-11-06", "withdraw")

        from apps.loans.service import summarize
        loan = summarize(self.biz, kind=LoanParty.Kind.LOAN)
        invest = summarize(self.biz, kind=LoanParty.Kind.INVESTMENT)

        # The two books are completely separate.
        self.assertEqual(loan["received"], Decimal("10000.00"))
        self.assertEqual(loan["returned"], Decimal("3000.00"))
        self.assertEqual(loan["outstanding"], Decimal("7000.00"))
        self.assertEqual(loan["net_main_account"], Decimal("7000.00"))
        self.assertEqual(invest["received"], Decimal("5000.00"))
        self.assertEqual(invest["returned"], Decimal("2000.00"))
        self.assertEqual(invest["outstanding"], Decimal("3000.00"))
        self.assertEqual(invest["net_main_account"], Decimal("3000.00"))

    def test_the_two_books_never_mix(self):
        lender = self._party("করিম ভাই")
        investor = self._party("রহিম ভাই", LoanParty.Kind.INVESTMENT)
        self._entry(lender, LoanEntry.Kind.RECEIVED, "10000.00",
                    "2026-11-01", "loan")
        self._entry(investor, LoanEntry.Kind.RECEIVED, "5000.00",
                    "2026-11-02", "invest")

        loan_html = self.client.get("/loans/").content.decode("utf-8")
        invest_html = self.client.get(
            "/investments/").content.decode("utf-8")

        # The loan page shows only the lender, the investment page only the
        # investor — no mixing at all.
        self.assertIn("করিম ভাই", loan_html)
        self.assertNotIn("রহিম ভাই", loan_html)
        self.assertIn("রহিম ভাই", invest_html)
        self.assertNotIn("করিম ভাই", invest_html)

        # And each page shows only its own totals.
        self.assertIn("৳10,000.00", loan_html)
        self.assertNotIn("৳5,000.00", loan_html)
        self.assertIn("৳5,000.00", invest_html)
        self.assertNotIn("৳10,000.00", invest_html)

    def test_a_party_cannot_be_used_in_the_other_book(self):
        lender = self._party("করিম ভাই")
        before = LoanEntry.objects.filter(business=self.biz).count()
        self.client.post("/investments/", {
            "action": "add_entry", "party_id": str(lender.id),
            "kind": "RECEIVED", "amount": "500.00",
            "transaction_date": "2026-11-03", "description": "cross"})
        self.assertEqual(
            LoanEntry.objects.filter(business=self.biz).count(), before)

    def test_adding_a_party_uses_the_book_it_was_added_in(self):
        self.client.post("/investments/", {
            "action": "add_party", "name": "সেলিম ভাই", "kind": "LOAN",
            "phone": "", "email": ""})
        party = LoanParty.objects.get(business=self.biz, name="সেলিম ভাই")
        # The kind is taken from the page, not from the form field.
        self.assertEqual(party.kind, LoanParty.Kind.INVESTMENT)

    def test_journal_stays_balanced_and_money_is_not_income(self):
        party = self._party()
        self._entry(party, LoanEntry.Kind.RECEIVED, "10000.00",
                    "2026-11-01", "loan")
        tb = trial_balance(self.biz, from_date="2026-01-01",
                           to_date="2026-12-31")
        self.assertTrue(tb["balanced"], tb)
        bs = balance_sheet(self.biz, as_of="2026-12-31")
        self.assertTrue(bs["balanced"], bs)
        codes = {r["code"]: r["amount"] for r in bs["liabilities"]}
        self.assertEqual(codes.get("2400"), "10000.00")
        # Loans are never revenue.
        self.assertNotIn("4000", {r["code"] for r in bs["liabilities"]})
        from apps.accounting.reports import income_statement
        isec = income_statement(self.biz, from_date="2026-01-01",
                                to_date="2026-12-31")
        self.assertEqual(Decimal(isec["total_revenue"]), Decimal("0.00"))

    def test_delete_removes_the_loan_entry_completely(self):
        party = self._party()
        self._entry(party, LoanEntry.Kind.RECEIVED, "10000.00",
                    "2026-11-01", "loan")
        repay = self._entry(party, LoanEntry.Kind.REPAYMENT, "3000.00",
                             "2026-11-05", "repay")
        self.assertEqual(party.outstanding(), Decimal("7000.00"))
        txn = repay.transaction
        self.client.post(self._book_url(party.kind), {
            "action": "delete_entry", "entry_id": str(repay.id),
            "reason": "cleanup"})
        repay.refresh_from_db() if LoanEntry.objects.filter(pk=repay.pk).exists() else None
        self.assertFalse(LoanEntry.objects.filter(pk=repay.pk).exists())
        self.assertFalse(LoanEntry.all_objects.filter(pk=repay.pk).exists())
        self.assertFalse(Transaction.objects.filter(pk=txn.pk).exists())
        party.refresh_from_db()
        self.assertEqual(party.outstanding(), Decimal("10000.00"))

    def test_edit_reverses_and_reposts(self):
        party = self._party()
        entry = self._entry(party, LoanEntry.Kind.RECEIVED, "10000.00",
                            "2026-11-01", "loan")
        self.client.post(self._book_url(party.kind), {
            "action": "edit_entry", "entry_id": str(entry.id),
            "kind": "RECEIVED", "amount": "12000.00",
            "transaction_date": "2026-11-02", "description": "loan fixed",
            "reason": "wrong amount"})
        entry.refresh_from_db()
        self.assertEqual(entry.status, LoanEntry.Status.REVERSED)
        party.refresh_from_db()
        self.assertEqual(party.outstanding(), Decimal("12000.00"))
        self.assertEqual(
            LoanEntry.objects.filter(business=self.biz,
                                     status=LoanEntry.Status.POSTED).count(),
            1)

    def test_date_filter_narrows_entries_and_totals(self):
        party = self._party()
        self._entry(party, LoanEntry.Kind.RECEIVED, "10000.00",
                    "2026-11-01", "nov")
        self._entry(party, LoanEntry.Kind.RECEIVED, "5000.00",
                    "2026-10-05", "oct")
        html = self.client.get(
            "/loans/?from=2026-11-01&to=2026-11-30").content.decode("utf-8")
        self.assertIn("nov", html)
        self.assertNotIn(">oct<", html)

    def test_page_shows_nav_and_summary(self):
        html = self.client.get("/loans/").content.decode("utf-8")
        self.assertIn("ঋণ", html)
        self.assertIn("মূল হিসাবে প্রভাব", html)
        invest_html = self.client.get(
            "/investments/").content.decode("utf-8")
        self.assertIn("বিনিয়োগ", invest_html)
        self.assertIn("মূল হিসাবে প্রভাব", invest_html)
        # the dashboard links to both books
        dash = self.client.get("/").content.decode("utf-8")
        self.assertIn("ঋণ", dash)
        self.assertIn("বিনিয়োগ", dash)

    def test_partner_cannot_post_loan_entries(self):
        party = self._party()
        partner_user = make_user("loanpartner")
        auth_client(self.owner).post(
            f"/api/businesses/{self.biz.id}/members/",
            {"email": partner_user.email, "role": "PARTNER"}, format="json")
        self.client.force_login(partner_user)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session.save()
        before = LoanEntry.objects.filter(business=self.biz).count()
        self.client.post("/loans/", {
            "action": "add_entry", "party_id": str(party.id),
            "kind": "RECEIVED", "amount": "1000.00",
            "transaction_date": "2026-11-01", "description": "nope"})
        self.assertEqual(
            LoanEntry.objects.filter(business=self.biz).count(), before)


class LoanContractTotalTests(TestCase):
    """The contracted total to repay (principal + interest).

    The card must show the real ২,১০,০০০ still owed while the cash
    account only ever saw the ১,৯০,০০০ actually received — and a
    repayment that includes the interest must be allowed.
    """

    def setUp(self):
        self.owner = make_user("loanctowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Contract Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        ensure_system_accounts(self.biz, self.owner)
        self.client = Client()
        self.client.force_login(self.owner)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session.save()

    @staticmethod
    def _book_url(kind):
        return "/loans/" if kind == LoanParty.Kind.LOAN else "/investments/"

    def _party(self, name="সুদ ভাই", kind=LoanParty.Kind.LOAN):
        self.client.post(self._book_url(kind), {
            "action": "add_party", "name": name, "kind": kind,
            "phone": "", "email": ""})
        return LoanParty.objects.get(business=self.biz, name=name)

    def _entry(self, party, kind, amount, tx_date, description="",
               contract_total=None):
        payload = {
            "action": "add_entry", "party_id": str(party.id), "kind": kind,
            "amount": amount, "transaction_date": tx_date,
            "description": description, "notes": ""}
        if contract_total:
            payload["contract_total"] = contract_total
        self.client.post(self._book_url(party.kind), payload)
        return LoanEntry.objects.filter(
            business=self.biz, description=description).first()

    def test_contract_raises_liability_but_not_cash(self):
        party = self._party()
        entry = self._entry(party, LoanEntry.Kind.RECEIVED, "190000.00",
                            "2026-11-01", "rin", contract_total="210000.00")
        self.assertEqual(entry.contract_total, Decimal("210000.00"))
        # cash only saw what was actually received
        self.assertEqual(entry.transaction.amount, Decimal("190000.00"))
        self.assertEqual(party.received_total(), Decimal("190000.00"))
        self.assertEqual(party.payable_total(), Decimal("210000.00"))
        self.assertEqual(party.interest_total(), Decimal("20000.00"))
        self.assertEqual(party.outstanding(), Decimal("210000.00"))

    def test_full_interest_including_repayment_is_allowed(self):
        party = self._party()
        self._entry(party, LoanEntry.Kind.RECEIVED, "190000.00",
                    "2026-11-01", "rin", contract_total="210000.00")
        repay = self._entry(party, LoanEntry.Kind.REPAYMENT, "210000.00",
                            "2026-12-01", "ferot")
        self.assertIsNotNone(repay)
        self.assertEqual(repay.transaction.amount, Decimal("210000.00"))
        self.assertEqual(party.outstanding(), Decimal("0.00"))

    def test_contract_below_the_loan_is_rejected(self):
        party = self._party()
        before = LoanEntry.objects.filter(business=self.biz).count()
        self.client.post("/loans/", {
            "action": "add_entry", "party_id": str(party.id),
            "kind": "RECEIVED", "amount": "190000.00",
            "contract_total": "189999.00",
            "transaction_date": "2026-11-01", "description": "bad"})
        self.assertEqual(
            LoanEntry.objects.filter(business=self.biz).count(), before)

    def test_contract_only_applies_to_loan_taken(self):
        party = self._party()
        self._entry(party, LoanEntry.Kind.RECEIVED, "100000.00",
                    "2026-11-01", "rin")
        before = LoanEntry.objects.filter(business=self.biz).count()
        self.client.post("/loans/", {
            "action": "add_entry", "party_id": str(party.id),
            "kind": "REPAYMENT", "amount": "5000.00",
            "contract_total": "110000.00",
            "transaction_date": "2026-11-05", "description": "repay"})
        self.assertEqual(
            LoanEntry.objects.filter(business=self.biz).count(), before)

    def test_plain_loan_without_contract_behaves_as_before(self):
        party = self._party()
        self._entry(party, LoanEntry.Kind.RECEIVED, "10000.00",
                    "2026-11-01", "rin")
        self.assertEqual(party.outstanding(), Decimal("10000.00"))
        repay = self._entry(party, LoanEntry.Kind.REPAYMENT, "10001.00",
                            "2026-11-05", "too-much")
        self.assertIsNone(repay)

    def test_card_shows_the_contracted_total(self):
        party = self._party()
        self._entry(party, LoanEntry.Kind.RECEIVED, "190000.00",
                    "2026-11-01", "rin", contract_total="210000.00")
        html = self.client.get("/loans/").content.decode("utf-8")
        self.assertIn("দিতে হবে", html)
        self.assertIn("সুদ", html)
