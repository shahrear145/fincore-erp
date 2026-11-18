"""ঋণের কিস্তি প্ল্যান (loan installment schedule) tests.

Pinned rules:

- an admin creates a plan with a first due date and a number of
  installments; the rows sum exactly to the total (the last row absorbs
  the rounding remainder) and are spread over consecutive months;
- checking a row off posts the matching REPAYMENT entry (and its cash
  transaction) and links it to the row;
- undoing a check-off reverses that entry and frees the row;
- the plan can only be deleted while nothing is paid;
- partner-role users cannot touch the schedule;
- the loans page renders the schedule section.
"""
from decimal import Decimal

from django.test import Client, TestCase

from apps.accounting.postings import ensure_system_accounts
from apps.loans.models import LoanEntry, LoanInstallment, LoanParty
from apps.organizations.models import Business, BusinessMembership
from apps.tests_common import auth_client, make_user
from apps.transactions.models import TransactionType


class LoanScheduleTests(TestCase):
    def setUp(self):
        self.owner = make_user("schedulowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Schedule Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        ensure_system_accounts(self.biz, self.owner)
        self.client = Client()
        self.client.force_login(self.owner)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session["ui_lang"] = "bn"
        session.save()

        # one lender + one RECEIVED loan with a monthly plan drawn from it
        self._post("add_party", {"name": "করিম ভাই", "kind": "LOAN",
                                 "phone": "", "email": ""})
        self.party = LoanParty.objects.get(
            business=self.biz, name="করিম ভাই")
        self._post("add_entry", {
            "party_id": str(self.party.id), "kind": "RECEIVED",
            "amount": "1000.00", "transaction_date": "2026-11-01",
            "description": "ঋণ নেওয়া", "notes": "",
            "contract_total": "1500.00"})

    # -- helpers ---------------------------------------------------------
    def _post(self, action, payload):
        payload = dict(payload)
        payload["action"] = action
        self.client.post("/loans/", payload)

    def _monthly_amounts(self, total, count):
        base = (Decimal(total) / Decimal(count)).quantize(Decimal("0.01"))
        amounts = [base] * (count - 1)
        amounts.append(Decimal(total) - sum(amounts))
        return amounts

    # -- creation ---------------------------------------------------------
    def test_create_schedule_sums_exactly_to_the_total(self):
        self._post("add_schedule", {
            "party_id": str(self.party.id), "total": "1000.00",
            "count": "3", "first_due": "2026-12-01", "notes": ""})
        rows = list(LoanInstallment.objects.filter(
            business=self.biz, party=self.party).order_by("sequence"))
        self.assertEqual(len(rows), 3)
        amounts = [r.amount for r in rows]
        self.assertEqual(amounts, self._monthly_amounts("1000.00", 3))
        self.assertEqual(sum(amounts), Decimal("1000.00"))
        self.assertEqual([r.due_date.isoformat() for r in rows],
                         ["2026-12-01", "2027-01-01", "2027-02-01"])
        self.assertEqual([r.is_paid for r in rows], [False, False, False])

    def test_create_schedule_with_month_end_due_clamps(self):
        self._post("add_schedule", {
            "party_id": str(self.party.id), "total": "300.00",
            "count": "3", "first_due": "2026-01-31", "notes": ""})
        rows = list(LoanInstallment.objects.filter(
            business=self.biz, party=self.party).order_by("sequence"))
        self.assertEqual([r.due_date.isoformat() for r in rows],
                         ["2026-01-31", "2026-02-28", "2026-03-31"])

    def test_lender_with_an_existing_schedule_cannot_get_another(self):
        for _ in range(2):
            self._post("add_schedule", {
                "party_id": str(self.party.id), "total": "900.00",
                "count": "3", "first_due": "2026-12-01", "notes": ""})
        self.assertEqual(LoanInstallment.objects.filter(
            business=self.biz, party=self.party).count(), 3)

    def test_schedule_only_for_the_loan_book(self):
        # The investments page rejects a schedule action entirely.
        self.client.post("/investments/", {
            "action": "add_schedule", "party_id": str(self.party.id),
            "total": "900.00", "count": "3", "first_due": "2026-12-01",
            "notes": ""})
        self.assertEqual(LoanInstallment.objects.filter(
            business=self.biz, party=self.party).count(), 0)

    # -- check-off / uncheck ------------------------------------------------
    def test_check_off_posts_a_repayment_and_links_it(self):
        self._post("add_schedule", {
            "party_id": str(self.party.id), "total": "1000.00",
            "count": "2", "first_due": "2026-12-01", "notes": ""})
        first = LoanInstallment.objects.filter(
            business=self.biz, party=self.party).order_by("sequence").first()
        self._post("pay_installment", {"installment_id": str(first.id)})
        first.refresh_from_db()
        self.assertTrue(first.is_paid)
        self.assertIsNotNone(first.paid_entry)
        self.assertEqual(first.paid_entry.kind, LoanEntry.Kind.REPAYMENT)
        self.assertEqual(first.paid_entry.amount, first.amount)
        self.assertEqual(first.paid_entry.status, LoanEntry.Status.POSTED)

    def test_uncheck_reverses_the_repayment_entry(self):
        self._post("add_schedule", {
            "party_id": str(self.party.id), "total": "1000.00",
            "count": "2", "first_due": "2026-12-01", "notes": ""})
        first = LoanInstallment.objects.filter(
            business=self.biz, party=self.party).order_by("sequence").first()
        self._post("pay_installment", {"installment_id": str(first.id)})
        first.refresh_from_db()
        entry_id = first.paid_entry_id
        self._post("unpay_installment", {"installment_id": str(first.id)})
        first.refresh_from_db()
        self.assertFalse(first.is_paid)
        self.assertIsNone(first.paid_entry)
        entry = LoanEntry.objects.get(pk=entry_id)
        self.assertEqual(entry.status, LoanEntry.Status.REVERSED)

    # -- deletion rules -------------------------------------------------------
    def test_delete_blocked_while_anything_is_paid(self):
        self._post("add_schedule", {
            "party_id": str(self.party.id), "total": "1000.00",
            "count": "2", "first_due": "2026-12-01", "notes": ""})
        first = LoanInstallment.objects.filter(
            business=self.biz, party=self.party).order_by("sequence").first()
        self._post("pay_installment", {"installment_id": str(first.id)})
        self._post("delete_schedule", {"party_id": str(self.party.id)})
        self.assertEqual(LoanInstallment.objects.filter(
            business=self.biz, party=self.party).count(), 2)

    def test_delete_works_when_nothing_is_paid(self):
        self._post("add_schedule", {
            "party_id": str(self.party.id), "total": "1000.00",
            "count": "2", "first_due": "2026-12-01", "notes": ""})
        self._post("delete_schedule", {"party_id": str(self.party.id)})
        self.assertEqual(LoanInstallment.objects.filter(
            business=self.biz, party=self.party).count(), 0)

    # -- permissions & page ---------------------------------------------------
    def test_partner_role_cannot_create_a_schedule(self):
        partner_user = make_user("schedulebob")
        BusinessMembership.objects.create(
            user=partner_user, business=self.biz,
            role=BusinessMembership.Role.PARTNER, is_active=True)
        other_client = Client()
        other_client.force_login(partner_user)
        session = other_client.session
        session["active_business_id"] = str(self.biz.id)
        session.save()
        other_client.post("/loans/", {
            "action": "add_schedule", "party_id": str(self.party.id),
            "total": "900.00", "count": "3", "first_due": "2026-12-01",
            "notes": ""})
        self.assertEqual(LoanInstallment.objects.filter(
            business=self.biz, party=self.party).count(), 0)

    def test_loans_page_shows_the_schedule_section(self):
        self._post("add_schedule", {
            "party_id": str(self.party.id), "total": "1000.00",
            "count": "4", "first_due": "2026-12-01", "notes": ""})
        res = self.client.get("/loans/")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode("utf-8")
        self.assertIn("কিস্তি প্ল্যান", body)
        self.assertIn("শোধিত", body)

    def test_loans_page_shows_create_form_for_lenders_without_a_plan(self):
        res = self.client.get("/loans/")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode("utf-8")
        self.assertIn("নতুন কিস্তি প্ল্যান", body)
        self.assertIn("কিস্তির সংখ্যা", body)
