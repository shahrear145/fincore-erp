"""Dashboard period filter tests.

Pinned rules:

- the dashboard defaults to month-to-date (unchanged behaviour);
- every preset resolves to the right range and the page says so;
- a transaction outside the chosen range never leaks into the KPI cards;
- custom ranges accept any two dates, reversed input is swapped, and
  garbage input falls back to the default month instead of erroring.
"""
from datetime import date, timedelta

from django.test import Client, TestCase
from django.utils import timezone

from apps.organizations.models import Business
from apps.partners.models import Partner
from apps.personal.service import (
    create_personal_transaction, ensure_default_accounts_and_categories,
)
from apps.tests_common import auth_client, make_user
from apps.transactions.models import Transaction


class DashboardPeriodTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.owner = make_user("periodowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Period Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        self.partner = Partner.objects.create(
            business=self.biz, name="Karim", status="ACTIVE",
            created_by=self.owner)
        self.client = Client()
        self.client.force_login(self.owner)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session.save()

    # -- helpers ---------------------------------------------------------
    def _income(self, amount, day):
        """Post a real business income through the writer (with its journal).

        The dashboard's Revenue card reads the accounting engine, so a raw
        Transaction row without a journal would always show 0.00.
        """
        res = auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz.id}",
            {"transaction_type": "INCOME", "amount": str(amount),
             "transaction_date": day.isoformat(), "payment_method": "CASH",
             "description": "period test"},
            format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return Transaction.objects.get(pk=res.json()["id"])

    def _page(self, query=""):
        res = self.client.get("/" + query)
        self.assertEqual(res.status_code, 200)
        return res.content.decode("utf-8")

    # -- tests -----------------------------------------------------------
    def test_default_is_month_to_date(self):
        body = self._page()
        self.assertIn("period-bar", body)
        self.assertIn("এই মাস", body)
        self.assertIn(self.today.strftime("%d %b"), body)

    def test_today_preset_hides_older_transactions(self):
        self._income("111.11", self.today)
        self._income("222.22", self.today - timedelta(days=1))
        body = self._page("?preset=today")
        self.assertIn("আজ", body)
        # the Revenue card reads the accounting engine for today only
        self.assertIn("111.11", body)
        self.assertNotIn("222.22", body)

    def test_month_preset_shows_both(self):
        first = self.today.replace(day=1)
        second = self.today
        if first == second:
            self.skipTest("run on a day after the 1st to compare two dates")
        self._income("111.11", second)
        self._income("222.22", first)
        body = self._page("?preset=month")
        self.assertIn("এই মাস", body)
        # both days are inside the month, so revenue is their exact sum
        self.assertIn("333.33", body)
        # and the narrower today preset sees only today's part
        today_body = self._page("?preset=today")
        self.assertIn("111.11", today_body)
        self.assertNotIn("222.22", today_body)

    def test_personal_totals_follow_the_range(self):
        ensure_default_accounts_and_categories(self.owner)
        account = self.owner.personal_accounts.first()
        create_personal_transaction(
            self.owner, transaction_type="INCOME", amount="55.55",
            transaction_date=self.today, account_id=account.id)
        create_personal_transaction(
            self.owner, transaction_type="INCOME", amount="66.66",
            transaction_date=self.today - timedelta(days=1),
            account_id=account.id)
        body = self._page("?preset=today")
        self.assertIn("55.55", body)
        self.assertNotIn("66.66", body)

    def test_custom_range_swaps_reversed_dates(self):
        first = self.today.replace(day=1)
        body = self._page(
            f"?preset=custom&from={self.today.isoformat()}"
            f"&to={first.isoformat()}")
        # the earlier date is shown first even though it was typed last
        self.assertIn(f"{first:%d %b} – {self.today:%d %b}", body)
        # and the date inputs were normalised to the same order
        self.assertIn(f'name="from" value="{first.isoformat()}"', body)
        self.assertIn(f'name="to" value="{self.today.isoformat()}"', body)

    def test_garbage_input_falls_back_to_month(self):
        body = self._page("?preset=bogus&from=not-a-date&to=also-bad")
        self.assertIn("এই মাস", body)

    def test_last_month_preset_resolves_previous_month(self):
        body = self._page("?preset=last_month")
        self.assertIn("গত মাস", body)
        first_this = self.today.replace(day=1)
        last_end = first_this - timedelta(days=1)
        self.assertIn(last_end.strftime("%d %b"), body)
