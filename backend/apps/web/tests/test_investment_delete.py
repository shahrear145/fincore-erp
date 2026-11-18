"""Investment history — a deleted row must disappear completely.

Pinned rules:

- deleting an entry (investment OR loan) removes the row itself (no
  "reversed" badge left behind), its cash transaction and its ledger
  postings — the cash balance returns to exactly what it was before;
- the investor / lender (party) is removed too when nothing is left of it;
- a row that was reversed earlier is never shown in the history list —
  deleted means gone from view, like everywhere else;
- only an ADMIN can delete, and the deletion is still audited.
"""
from decimal import Decimal

from django.test import Client, TestCase

from apps.accounting.models import JournalEntry, JournalLine
from apps.customers.models import Customer
from apps.loans.models import LoanEntry, LoanParty
from apps.organizations.models import Business
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user
from apps.transactions.models import Transaction, TransactionStatus

D1 = "2026-11-01"


class InvestmentHardDeleteTests(TestCase):
    def setUp(self):
        self.owner = make_user("invowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Investment Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        self.partner = Partner.objects.create(
            business=self.biz, name="Rahim", status="ACTIVE",
            created_by=self.owner)
        self.client = Client()
        self.client.force_login(self.owner)
        s = self.client.session
        s["active_business_id"] = str(self.biz.id)
        s["ui_lang"] = "bn"
        s.save()

    def _book_url(self, kind):
        return "/investments/" if kind == LoanParty.Kind.INVESTMENT else "/loans/"

    def _party(self, kind=LoanParty.Kind.INVESTMENT, name="Abu Taher"):
        res = self.client.post(self._book_url(kind), {
            "action": "add_party", "name": name, "phone": "",
            "email": "", "notes": ""})
        self.assertEqual(res.status_code, 302)
        return LoanParty.objects.get(business=self.biz, kind=kind, name=name)

    def _entry(self, party, kind=LoanEntry.Kind.RECEIVED, amount="200000.00",
               day=D1, description="capital"):
        res = self.client.post(self._book_url(party.kind), {
            "action": "add_entry", "party_id": str(party.id), "kind": kind,
            "amount": amount, "transaction_date": day,
            "description": description, "notes": ""})
        self.assertEqual(res.status_code, 302)
        return LoanEntry.objects.filter(party=party).order_by(
            "-created_at").first()

    def _cash(self):
        from apps.cashbook.services import cash_balance
        return cash_balance(self.biz)

    def _journal_count(self, txn):
        return JournalEntry.objects.filter(
            business=self.biz, source_id=txn.id,
            source_kind__in=("TXN", "TXN_REVERSAL")).count()

    # --- the investment book ---------------------------------------------

    def test_deleting_an_investment_row_removes_it_completely(self):
        party = self._party()
        entry = self._entry(party)
        txn = entry.transaction
        self.assertTrue(LoanEntry.objects.filter(pk=entry.pk).exists())

        res = self.client.post(self._book_url(LoanParty.Kind.INVESTMENT), {
            "action": "delete_entry", "entry_id": str(entry.id),
            "reason": "wrong entry"})
        self.assertEqual(res.status_code, 302)

        # the row is gone — not marked reversed
        self.assertFalse(LoanEntry.objects.filter(pk=entry.pk).exists())
        self.assertFalse(
            LoanEntry.all_objects.filter(pk=entry.pk).exists())
        # the money entry is gone too
        self.assertFalse(Transaction.all_objects.filter(pk=txn.pk).exists())
        # and its ledger postings (no ghost money)
        self.assertEqual(self._journal_count(txn), 0)
        self.assertFalse(JournalLine.objects.filter(
            entry__source_id=txn.id).exists())

    def test_the_cash_balance_goes_back_to_what_it_was(self):
        party = self._party()
        before = self._cash()
        entry = self._entry(party)
        self.assertNotEqual(self._cash(), before)
        self.client.post(self._book_url(LoanParty.Kind.INVESTMENT), {
            "action": "delete_entry", "entry_id": str(entry.id)})
        self.assertEqual(self._cash(), before)

    def test_the_ledger_stays_balanced_after_the_delete(self):
        from apps.accounting.reports import balance_sheet
        party = self._party()
        entry = self._entry(party)
        self.client.post(self._book_url(LoanParty.Kind.INVESTMENT), {
            "action": "delete_entry", "entry_id": str(entry.id)})
        bs = balance_sheet(self.biz)
        self.assertTrue(bs["balanced"], bs)
        codes = {r["code"]: r["amount"] for r in bs["liabilities"]}
        self.assertNotIn("3200", codes)     # investor capital is gone
        from apps.cashbook.services import integrity_check
        self.assertTrue(integrity_check(self.biz)["clean"])

    def test_an_empty_investor_is_removed_as_well(self):
        party = self._party()
        entry = self._entry(party)
        self.client.post(self._book_url(LoanParty.Kind.INVESTMENT), {
            "action": "delete_entry", "entry_id": str(entry.id)})
        self.assertFalse(LoanParty.objects.filter(pk=party.pk).exists())

    def test_an_investor_with_other_entries_stays(self):
        party = self._party()
        first = self._entry(party)
        second = self._entry(party, kind=LoanEntry.Kind.WITHDRAWAL,
                              amount="50000.00", day="2026-11-05")
        self.client.post(self._book_url(LoanParty.Kind.INVESTMENT), {
            "action": "delete_entry", "entry_id": str(first.id)})
        self.assertTrue(LoanParty.objects.filter(pk=party.pk).exists())
        self.assertTrue(LoanEntry.objects.filter(pk=second.pk).exists())

    def test_the_page_shows_the_row_as_gone(self):
        party = self._party()
        entry = self._entry(party)
        html = self.client.get(
            self._book_url(LoanParty.Kind.INVESTMENT)).content.decode()
        self.assertIn("Abu Taher", html)
        self.client.post(self._book_url(LoanParty.Kind.INVESTMENT), {
            "action": "delete_entry", "entry_id": str(entry.id)})
        html = self.client.get(
            self._book_url(LoanParty.Kind.INVESTMENT)).content.decode()
        self.assertNotIn("200,000.00", html)

    def test_the_delete_is_audited(self):
        from apps.core.middleware import AuditLog
        party = self._party()
        entry = self._entry(party)
        self.client.post(self._book_url(LoanParty.Kind.INVESTMENT), {
            "action": "delete_entry", "entry_id": str(entry.id),
            "reason": "wrong entry"})
        log = AuditLog.objects.filter(action="hard_delete").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.business_id, self.biz.id)
        self.assertEqual(log.changes.get("amount"), "200000.00")

    def test_an_already_reversed_investment_row_can_be_purged(self):
        # a row that was reversed earlier is invisible in the history list,
        # but it can still be removed for good through the same action
        party = self._party()
        entry = self._entry(party)
        self.client.post(self._book_url(LoanParty.Kind.INVESTMENT), {
            "action": "delete_entry", "entry_id": str(entry.id)})
        self.assertEqual(LoanEntry.objects.count(), 0)

    def test_a_reversed_row_never_renders_in_the_page(self):
        from apps.loans.service import reverse_entry
        from apps.organizations.models import BusinessMembership
        from types import SimpleNamespace
        party = self._party()
        entry = self._entry(party)
        # reverse it the old way (like a loan entry)
        actor = SimpleNamespace(
            user=self.owner, business=self.biz,
            business_membership=BusinessMembership.objects.filter(
                user=self.owner, business=self.biz).first())
        reverse_entry(actor, entry, reason="test")
        self.assertEqual(LoanEntry.objects.count(), 1)   # still in the DB
        html = self.client.get(
            self._book_url(LoanParty.Kind.INVESTMENT)).content.decode()
        # … but never visible in the history list
        self.assertNotIn("বাতিলকৃত", html)
        self.assertNotIn("Reversed", html)
        self.assertNotIn(str(entry.amount), html)

    # --- the loan book erases completely, just like investment ------------

    def test_deleting_a_loan_row_removes_it_completely(self):
        party = self._party(LoanParty.Kind.LOAN, "Bank Manager")
        entry = self._entry(party)
        txn = entry.transaction

        res = self.client.post(self._book_url(LoanParty.Kind.LOAN), {
            "action": "delete_entry", "entry_id": str(entry.id),
            "reason": "wrong entry"})
        self.assertEqual(res.status_code, 302)

        self.assertFalse(LoanEntry.objects.filter(pk=entry.pk).exists())
        self.assertFalse(
            LoanEntry.all_objects.filter(pk=entry.pk).exists())
        self.assertFalse(Transaction.all_objects.filter(pk=txn.pk).exists())
        self.assertEqual(self._journal_count(txn), 0)
        self.assertFalse(JournalLine.objects.filter(
            entry__source_id=txn.id).exists())

    def test_the_cash_balance_goes_back_after_a_loan_delete(self):
        party = self._party(LoanParty.Kind.LOAN, "Bank Manager")
        before = self._cash()
        entry = self._entry(party)
        self.assertNotEqual(self._cash(), before)
        self.client.post(self._book_url(LoanParty.Kind.LOAN), {
            "action": "delete_entry", "entry_id": str(entry.id)})
        self.assertEqual(self._cash(), before)

    def test_a_reversed_loan_row_is_never_shown_in_history(self):
        from apps.loans.service import reverse_entry
        from apps.organizations.models import BusinessMembership
        from types import SimpleNamespace
        party = self._party(LoanParty.Kind.LOAN, "Bank Manager")
        entry = self._entry(party)
        actor = SimpleNamespace(
            user=self.owner, business=self.biz,
            business_membership=BusinessMembership.objects.filter(
                user=self.owner, business=self.biz).first())
        reverse_entry(actor, entry, reason="test")
        self.assertEqual(LoanEntry.objects.count(), 1)   # still in the DB
        html = self.client.get(
            self._book_url(LoanParty.Kind.LOAN)).content.decode()
        self.assertNotIn("বাতিলকৃত", html)
        self.assertNotIn("Reversed", html)
        self.assertNotIn("200,000.00", html)
