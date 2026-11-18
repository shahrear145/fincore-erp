"""বাকির খাতা pagination — সর্বোচ্চ ৩০ জন / page.

Pinned rules:

- the day-khata table shows at most 30 customers per page;
- ``khata_page`` outside the valid range clamps instead of erroring;
- row numbering continues across pages;
- the pager appears only when there is more than one page.
"""
from datetime import date

from django.test import Client, TestCase

from apps.organizations.models import Business
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user
from apps.transactions.models import TransactionType

D1 = date(2026, 11, 9)


class KhataPaginationTests(TestCase):
    def setUp(self):
        self.owner = make_user("paginationowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Pagination Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        self.partner = Partner.objects.create(
            business=self.biz, name="Karim", status="ACTIVE",
            created_by=self.owner)

        # 35 customers, each with one due on the same date.
        for index in range(1, 36):
            name = f"KTest{index:02d} Customer"
            res = auth_client(self.owner).post(
                f"/api/customers/?business={self.biz.id}",
                {"name": name, "partner_id": str(self.partner.id)},
                format="json")
            self.assertEqual(res.status_code, 201, res.content)
            customer_id = res.json()["id"]
            res = auth_client(self.owner).post(
                f"/api/transactions/?business={self.biz.id}",
                {"transaction_type": TransactionType.CREDIT,
                 "amount": "100.00",
                 "transaction_date": str(D1),
                 "partner": str(self.partner.id),
                 "customer": customer_id,
                 "payment_method": "CASH"},
                format="json")
            self.assertEqual(res.status_code, 201, res.content)

        self.client = Client()
        self.client.force_login(self.owner)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session["ui_lang"] = "bn"
        session.save()

    def _url(self, page=None):
        url = f"/customers/?partner={self.partner.id}&pdate={D1.isoformat()}"
        if page is not None:
            url += f"&khata_page={page}"
        return url

    def _rows(self, html):
        return html.count('class="krow"')

    def test_first_page_shows_thirty_rows_and_pager(self):
        res = self.client.get(self._url())
        self.assertEqual(res.status_code, 200)
        html = res.content.decode("utf-8")
        self.assertEqual(self._rows(html), 30)
        self.assertIn("1–30 / 35", html)
        self.assertIn("1 / 2", html)
        self.assertIn('class="kp-info"', html)

    def test_second_page_shows_the_rest(self):
        res = self.client.get(self._url(2))
        self.assertEqual(res.status_code, 200)
        html = res.content.decode("utf-8")
        self.assertEqual(self._rows(html), 5)
        self.assertIn("31–35 / 35", html)
        self.assertIn("KTest31 Customer", html)

    def test_out_of_range_page_clamps(self):
        res = self.client.get(self._url(99))
        self.assertEqual(res.status_code, 200)
        html = res.content.decode("utf-8")
        self.assertEqual(self._rows(html), 5)
        self.assertIn("31–35 / 35", html)
        res = self.client.get(self._url(-3))
        self.assertEqual(res.status_code, 200)
        html = res.content.decode("utf-8")
        self.assertEqual(self._rows(html), 30)
        self.assertIn("1–30 / 35", html)

    def test_pager_links_carry_the_page_parameter(self):
        res = self.client.get(self._url(1))
        html = res.content.decode("utf-8")
        self.assertIn("khata_page=2", html)
