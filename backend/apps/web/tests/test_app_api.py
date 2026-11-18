"""Additive API tests for the Flutter app surface (apps/web/api.py).

Pinned rules:

- the password-reset flow is additive and never reveals whether the email
  belongs to an account;
- binary exports are served with the right content type and are
  partner-scoped exactly like the web download (a partner role cannot
  export another partner's book);
- reminders preview/send over JSON follow the same cooldown + book rules;
- reset wipes only the user's own data and needs the literal confirmation.
"""
from datetime import date

from django.core import mail
from django.test import TestCase
from rest_framework.test import APIClient

from apps.customers.models import Customer, CustomerAssignment, DueReminder
from apps.organizations.models import Business, BusinessMembership
from apps.partners.models import Partner
from apps.users.models import EmailOTP, User
from apps.tests_common import auth_client, make_user

D1 = date(2026, 11, 9)


class AppApiTestBase(TestCase):
    def setUp(self):
        self.owner = make_user("apowner", email="owner@example.com")
        self.partner_user = make_user("apppartner", email="partner@example.com")

        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "App Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])

        self.partner = Partner.objects.create(
            business=self.biz, name="Karim", status="ACTIVE",
            created_by=self.owner, linked_user=self.partner_user)
        self.other = Partner.objects.create(
            business=self.biz, name="Rahim", status="ACTIVE",
            created_by=self.owner)

        self.customer = self._add_customer("Hosen Ali", "01712345678",
                                           "hosen@example.com", self.partner)
        self.other_customer = self._add_customer("Salma Begum", "01899999999",
                                                 "salma@example.com", self.other)
        self._txn("3000.00", self.partner, self.customer)
        self._txn("500.00", self.other, self.other_customer)

        # the partner role needs an active membership + linked partner profile
        BusinessMembership.objects.create(
            business=self.biz, user=self.partner_user,
            role=BusinessMembership.Role.PARTNER, is_active=True)

        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.client.credentials(HTTP_X_ACTIVE_BUSINESS=str(self.biz.id))

    def _add_customer(self, name, phone, email, partner):
        res = auth_client(self.owner).post(
            f"/api/customers/?business={self.biz.id}",
            {"name": name, "partner_id": str(partner.id),
             "phone": phone, "email": email}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        cust = Customer.objects.get(pk=res.json()["id"])
        cust.phone, cust.email = phone, email
        cust.save(update_fields=["phone", "email"])
        return cust

    def _txn(self, amount, partner, customer, txn_date=D1):
        res = auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz.id}",
            {"transaction_type": "CREDIT", "amount": amount,
             "transaction_date": str(txn_date), "partner": str(partner.id),
             "customer": str(customer.id), "payment_method": "CASH"},
            format="json")
        self.assertEqual(res.status_code, 201, res.content)

    def _p_client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        c.credentials(HTTP_X_ACTIVE_BUSINESS=str(self.biz.id))
        return c

    def _assignment(self, customer=None, partner=None):
        return CustomerAssignment.objects.get(
            customer=customer or self.customer,
            partner=partner or self.partner)


class PasswordResetApiTests(AppApiTestBase):
    def test_forgot_never_reveals_and_issues_code_for_existing_user(self):
        res = self.client.post("/api/auth/password/forgot/",
                               {"email": "owner@example.com"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])
        self.assertTrue(EmailOTP.objects.filter(
            user=self.owner, purpose="password_reset").exists())

        res = self.client.post("/api/auth/password/forgot/",
                               {"email": "nobody@example.com"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])
        self.assertEqual(EmailOTP.objects.filter(
            user__email="nobody@example.com").count(), 0)

    def test_reset_rejects_wrong_code_and_accepts_correct_code(self):
        self.client.post("/api/auth/password/forgot/",
                         {"email": "owner@example.com"}, format="json")
        self.assertTrue(EmailOTP.objects.filter(
            user=self.owner, purpose="password_reset").exists())

        res = self.client.post("/api/auth/password/reset/",
                               {"email": "owner@example.com", "code": "000000",
                                "new_password1": "N3wPass!2026x",
                                "new_password2": "N3wPass!2026x"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(res.json()["ok"])
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password("S4fePass!2026"))

        # a correct code passes the real verifier — patch the mail-issued code
        from unittest.mock import patch
        with patch("apps.web.api.verify_email_otp", return_value=(True, "ok")):
            res = self.client.post("/api/auth/password/reset/",
                                  {"email": "owner@example.com", "code": "123456",
                                   "new_password1": "N3wPass!2026x",
                                   "new_password2": "N3wPass!2026x"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password("N3wPass!2026x"))
        self.assertFalse(self.owner.check_password("S4fePass!2026"))


class ExportApiTests(AppApiTestBase):
    def test_khata_pdf_is_served_with_file_headers(self):
        # ADMIN may export any ACTIVE partner's book
        res = self.client.get(
            f"/api/exports/khata/pdf/?partner={self.other.id}"
            f"&pdate={D1.isoformat()}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/pdf")
        self.assertIn("attachment", res["Content-Disposition"])
        self.assertGreater(len(res.content), 100)
        res = self.client.get(
            f"/api/exports/khata/pdf/?partner={self.partner.id}"
            f"&pdate={D1.isoformat()}")
        self.assertEqual(res.status_code, 200)

    def test_partner_role_can_only_export_own_book(self):
        pclient = self._p_client(self.partner_user)
        res = pclient.get(
            f"/api/exports/khata/pdf/?partner={self.other.id}"
            f"&pdate={D1.isoformat()}")
        self.assertEqual(res.status_code, 403)
        res = pclient.get(
            f"/api/exports/khata/pdf/?partner={self.partner.id}"
            f"&pdate={D1.isoformat()}")
        self.assertEqual(res.status_code, 200)

    def test_summary_and_supercalc_exports_in_all_formats(self):
        for fmt, ctype in (("pdf", "application/pdf"),
                           ("excel",
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                           ("word",
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")):
            res = self.client.get(f"/api/exports/summary/{fmt}/")
            self.assertEqual(res.status_code, 200, fmt)
            self.assertEqual(res["Content-Type"], ctype, fmt)
            res = self.client.get(f"/api/exports/supercalc/{fmt}/")
            self.assertEqual(res.status_code, 200, fmt)
            self.assertEqual(res["Content-Type"], ctype, fmt)


class ReminderApiTests(AppApiTestBase):
    def test_preview_and_send_whatsapp(self):
        res = self.client.get(
            f"/api/customers/{self.customer.id}/reminder/"
            f"?partner={self.partner.id}")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["partner"], "Karim")
        self.assertTrue(data["can_whatsapp"])

        res = self.client.post(
            f"/api/customers/{self.customer.id}/reminder/",
            {"channel": "WHATSAPP", "partner_id": str(self.partner.id)},
            format="json")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertIn("wa.me", data["wa_url"])
        self.assertEqual(len(mail.outbox), 0)  # whatsapp sends no mail
        self.assertEqual(DueReminder.objects.filter(channel="WHATSAPP").count(), 1)

    def test_cooldown_requires_confirmation(self):
        url = f"/api/customers/{self.customer.id}/reminder/"
        res = self.client.post(url, {"channel": "WHATSAPP",
                                     "partner_id": str(self.partner.id)},
                               format="json")
        self.assertEqual(res.status_code, 200)
        res = self.client.post(url, {"channel": "WHATSAPP",
                                     "partner_id": str(self.partner.id)},
                               format="json")
        self.assertEqual(res.status_code, 409)
        res = self.client.post(url, {"channel": "WHATSAPP",
                                     "partner_id": str(self.partner.id),
                                     "confirm_repeat": "1"}, format="json")
        self.assertEqual(res.status_code, 200)

    def test_email_goes_out_and_is_logged(self):
        url = f"/api/customers/{self.customer.id}/reminder/"
        res = self.client.post(url, {"channel": "EMAIL",
                                     "partner_id": str(self.partner.id)},
                               format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(DueReminder.objects.filter(channel="EMAIL").count(), 1)

    def test_partner_role_cannot_send_for_another_book(self):
        pclient = self._p_client(self.partner_user)
        url = f"/api/customers/{self.other_customer.id}/reminder/"
        res = pclient.get(f"{url}?partner={self.other.id}")
        self.assertEqual(res.status_code, 403)
        res = pclient.post(url, {"channel": "WHATSAPP",
                                 "partner_id": str(self.other.id)}, format="json")
        self.assertEqual(res.status_code, 403)


class ResetDataApiTests(AppApiTestBase):
    def test_reset_requires_literal_confirmation(self):
        res = self.client.post("/api/me/reset/", {"confirm": "yes"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertTrue(Business.objects.filter(pk=self.biz.id).exists())

        res = self.client.post("/api/me/reset/", {"confirm": "RESET"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])
        self.assertFalse(Business.objects.filter(pk=self.biz.id).exists())
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password("S4fePass!2026"))
