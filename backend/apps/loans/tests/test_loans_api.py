"""Loan / investment book API tests.

The web UI (`web:loans_view`) is unchanged; this API is an additional JSON
surface for the mobile app. These tests lock the contract the app relies on:

- a member can read the book of their own business,
- a non-member gets 403,
- ADMIN can add a lender / investor and post an entry,
- the two books (loan vs investment) never mix,
- an entry can be reversed.
"""
from datetime import date

from django.test import TestCase

from apps.tests_common import auth_client, make_user


class LoanBookApiTests(TestCase):
    def setUp(self):
        self.admin = make_user("loanadmin")
        self.member = make_user("loanmember")
        self.outsider = make_user("loanoutsider")

        from apps.organizations.models import Business, BusinessMembership
        self.business = Business.objects.create(name="Loan Shop", owner=self.admin)
        BusinessMembership.objects.create(
            business=self.business, user=self.admin,
            role=BusinessMembership.Role.ADMIN, is_active=True)
        BusinessMembership.objects.create(
            business=self.business, user=self.member,
            role=BusinessMembership.Role.PARTNER, is_active=True)

        self.client = auth_client(self.admin)

    def _headers(self, business=None):
        return {"HTTP_X_ACTIVE_BUSINESS": str(business or self.business.pk)}

    def test_member_can_read_the_book(self):
        res = self.client.get("/api/loans/book/", **self._headers())
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body["kind"], "LOAN")
        self.assertIn("summary", body)
        self.assertEqual(body["parties"], [])

    def test_outsider_is_refused(self):
        res = auth_client(self.outsider).get("/api/loans/book/",
                                            **self._headers())
        self.assertEqual(res.status_code, 403)

    def test_admin_adds_party_and_entry(self):
        res = self.client.post("/api/loans/parties/",
                               {"name": "Karim", "kind": "LOAN",
                                "phone": "01700000000"},
                               format="json", **self._headers())
        self.assertEqual(res.status_code, 201, res.content)
        party_id = res.json()["id"]

        res = self.client.post("/api/loans/entries/",
                               {"party": party_id, "kind": "RECEIVED",
                                "amount": "5000.00",
                                "transaction_date": date(2026, 1, 5).isoformat(),
                                "description": "à¦¬à§à¦¯à¦¾à¦‚à¦• à¦¥à§‡à¦•à§‡ à¦§à¦¾à¦°"},
                               format="json", **self._headers())
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["amount"], "5000.00")

        res = self.client.get("/api/loans/book/", **self._headers())
        body = res.json()
        self.assertEqual(len(body["parties"]), 1)
        self.assertEqual(body["parties"][0]["outstanding"], "5000.00")
        self.assertEqual(body["summary"]["total_in"], "5000.00")
        self.assertEqual(len(body["entries"]), 1)

    def test_the_two_books_never_mix(self):
        res = self.client.post("/api/loans/parties/",
                               {"name": "Rina", "kind": "INVESTMENT"},
                               format="json", **self._headers())
        self.assertEqual(res.status_code, 201, res.content)
        party_id = res.json()["id"]

        res = self.client.post("/api/loans/entries/",
                               {"party": party_id, "kind": "RECEIVED",
                                "amount": "2000.00",
                                "transaction_date": date(2026, 1, 6).isoformat()},
                               format="json", **self._headers())
        self.assertEqual(res.status_code, 201, res.content)

        loan_book = self.client.get(
            "/api/loans/book/?kind=LOAN", **self._headers())
        self.assertEqual(loan_book.json()["parties"], [])

        invest_book = self.client.get(
            "/api/loans/book/?kind=INVESTMENT", **self._headers())
        self.assertEqual(len(invest_book.json()["parties"]), 1)
        self.assertEqual(invest_book.json()["summary"]["total_in"], "2000.00")

    def test_entry_can_be_reversed(self):
        res = self.client.post("/api/loans/parties/",
                               {"name": "Salam", "kind": "LOAN"},
                               format="json", **self._headers())
        party_id = res.json()["id"]
        res = self.client.post("/api/loans/entries/",
                               {"party": party_id, "kind": "RECEIVED",
                                "amount": "1000.00",
                                "transaction_date": date(2026, 1, 7).isoformat()},
                               format="json", **self._headers())
        entry_id = res.json()["id"]

        res = self.client.post(f"/api/loans/entries/{entry_id}/reverse/",
                               {"reason": "à¦­à§à¦² à¦•à¦°à§‡"},
                               format="json", **self._headers())
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["status"], "REVERSED")

        book = self.client.get("/api/loans/book/", **self._headers()).json()
        self.assertEqual(book["summary"]["outstanding"], "0.00")

    def test_only_admin_may_write(self):
        res = auth_client(self.member).post(
            "/api/loans/parties/", {"name": "X", "kind": "LOAN"},
            format="json", **self._headers())
        self.assertEqual(res.status_code, 403)


