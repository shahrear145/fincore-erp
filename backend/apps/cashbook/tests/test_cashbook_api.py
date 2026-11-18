"""Cashbook / calculator API tests (additive JSON surface for the app).

These lock the contract the mobile app relies on:
- a member can read the cashbook overview of their own business,
- a non-member gets 403,
- the overview always carries the figures the calculator screen shows,
- an ADMIN can record a cash count and a stock snapshot.
"""
from datetime import date

from django.test import TestCase

from apps.tests_common import auth_client, make_user


class CashbookApiTests(TestCase):
    def setUp(self):
        self.admin = make_user("cashadmin")
        self.member = make_user("cashmember")
        self.outsider = make_user("cashoutsider")

        from apps.organizations.models import Business, BusinessMembership
        self.business = Business.objects.create(name="Cash Shop",
                                                owner=self.admin)
        BusinessMembership.objects.create(
            business=self.business, user=self.admin,
            role=BusinessMembership.Role.ADMIN, is_active=True)
        BusinessMembership.objects.create(
            business=self.business, user=self.member,
            role=BusinessMembership.Role.PARTNER, is_active=True)

        self.client = auth_client(self.admin)
        self.headers = {"HTTP_X_ACTIVE_BUSINESS": str(self.business.pk)}

    def test_member_can_read_the_overview(self):
        res = self.client.get("/api/cashbook/overview/", **self.headers)
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        for key in ("totals", "position", "cash_balance", "stock_value",
                    "total_due", "from", "to"):
            self.assertIn(key, body)
        self.assertIn("profit", body["totals"])
        self.assertIn("real_profit", body["totals"])

    def test_outsider_is_refused(self):
        res = auth_client(self.outsider).get("/api/cashbook/overview/",
                                            **self.headers)
        self.assertEqual(res.status_code, 403)

    def test_preset_changes_the_period(self):
        today = date.today()
        res = self.client.get("/api/cashbook/overview/?preset=today",
                              **self.headers)
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body["from"], today.isoformat())
        self.assertEqual(body["to"], today.isoformat())

    def test_day_rows_and_months(self):
        res = self.client.get("/api/cashbook/day-rows/", **self.headers)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn("rows", res.json())

        res = self.client.get("/api/cashbook/months/", **self.headers)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn("rows", res.json())

    def test_admin_records_a_cash_count(self):
        res = self.client.post("/api/cashbook/counts/",
                               {"counted_amount": "1500.00",
                                "book_amount": "1400.00",
                                "counted_on": date(2026, 1, 5).isoformat(),
                                "note": "দিনের গোনা"},
                               format="json", **self.headers)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["difference"], "100.00")

        res = self.client.get("/api/cashbook/counts/", **self.headers)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(len(res.json()["counts"]), 1)

    def test_admin_records_a_stock_snapshot(self):
        res = self.client.post("/api/cashbook/stock-snapshots/",
                               {"closing_value": "8000.00",
                                "snapshot_on": date(2026, 1, 5).isoformat()},
                               format="json", **self.headers)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["closing_value"], "8000.00")

        res = self.client.get("/api/cashbook/stock-snapshots/",
                              **self.headers)
        self.assertEqual(len(res.json()["snapshots"]), 1)

    def test_only_admin_may_write(self):
        res = auth_client(self.member).post(
            "/api/cashbook/counts/",
            {"counted_amount": "10.00", "book_amount": "10.00"},
            format="json", **self.headers)
        self.assertEqual(res.status_code, 403)
