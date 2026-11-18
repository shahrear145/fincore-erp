"""Payment-reminder tests — WhatsApp / email hand-off for customers with a due.

Pinned rules:

- the reminder text is built server-side from the right name, business,
  partner and amount (never from the browser);
- a reminder only ever touches ONE partner's book — a partner-role user
  cannot preview or send for another partner's customer (404, no leak);
- WhatsApp hands over a `wa.me` link with the message pre-typed; email
  really goes out through SMTP and is recorded;
- every attempt is logged (sent or failed) with the amount snapshot;
- the same customer is not chased again within the cooldown window unless
  the user explicitly confirms it;
- a customer without a due, a phone number or an email is refused.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.test import Client, TestCase

from apps.customers.models import Customer, CustomerAssignment, DueReminder
from apps.customers.reminders import (
    COOLDOWN_DAYS, build_reminder_message, build_whatsapp_url, format_money,
    normalize_bd_phone,
)
from apps.organizations.models import Business, BusinessMembership
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user

D1 = date(2026, 11, 9)


class ReminderTestBase(TestCase):
    def setUp(self):
        self.owner = make_user("remowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Reminder Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])

        self.partner = Partner.objects.create(
            business=self.biz, name="Karim", status="ACTIVE",
            created_by=self.owner)
        self.other = Partner.objects.create(
            business=self.biz, name="Rahim", status="ACTIVE",
            created_by=self.owner)

        self.customer = self._add_customer("Hosen Ali", "01712345678",
                                           "hosen@example.com", self.partner)
        self.other_customer = self._add_customer(
            "Salma Begum", "01899999999", "salma@example.com", self.other)

        # one due of 3000 for the owner book, one due of 500 for the other
        self._txn("3000.00", self.partner, self.customer)
        self._txn("500.00", self.other, self.other_customer)

        self.client = Client()
        self.client.force_login(self.owner)
        self._activate(self.biz)

    # --- helpers -----------------------------------------------------------

    def _add_customer(self, name, phone, email, partner):
        res = auth_client(self.owner).post(
            f"/api/customers/?business={self.biz.id}",
            {"name": name, "partner_id": str(partner.id),
             "phone": phone, "email": email},
            format="json")
        self.assertEqual(res.status_code, 201, res.content)
        cust = Customer.objects.get(pk=res.json()["id"])
        cust.phone = phone
        cust.email = email
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
        return res

    def _activate(self, biz):
        s = self.client.session
        s["active_business_id"] = str(biz.id)
        s["ui_lang"] = "bn"
        s.save()

    def _assignment(self, customer=None, partner=None):
        customer = customer or self.customer
        partner = partner or self.partner
        return CustomerAssignment.objects.get(customer=customer, partner=partner)

    def _preview_url(self, assignment):
        return f"/customers/reminder/{assignment.id}/preview/"

    def _send_url(self, assignment):
        return f"/customers/reminder/{assignment.id}/send/"


class ReminderPreviewTests(ReminderTestBase):
    def test_preview_builds_message_from_server_data(self):
        res = self.client.get(self._preview_url(self._assignment()))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["name"], "Hosen Ali")
        self.assertEqual(data["amount"], "৳3,000.00")
        self.assertEqual(data["partner"], "Karim")
        self.assertEqual(data["business"], "Reminder Traders")
        self.assertIn("Hosen Ali", data["message"])
        self.assertIn("৳3,000.00", data["message"])
        self.assertIn("Karim", data["message"])
        self.assertTrue(data["can_whatsapp"])
        self.assertTrue(data["can_email"])
        self.assertFalse(data["cooldown_active"])

    def test_preview_requires_login(self):
        res = Client().get(self._preview_url(self._assignment()))
        self.assertEqual(res.status_code, 401)
        self.assertFalse(res.json()["ok"])

    def test_preview_unknown_assignment_is_404(self):
        res = self.client.get(
            "/customers/reminder/00000000-0000-0000-0000-000000000000/preview/")
        self.assertEqual(res.status_code, 404)

    def test_preview_is_scoped_to_own_book(self):
        # a partner-role user must not see or touch another partner's customer
        partner_user = make_user("rempartner")
        BusinessMembership.objects.create(
            user=partner_user, business=self.biz,
            role=BusinessMembership.Role.PARTNER)
        own = Partner.objects.create(
            business=self.biz, name="Own Book", status="ACTIVE",
            created_by=self.owner, linked_user=partner_user)
        own_customer = self._add_customer(
            "Nijer Customer", "01711111111", "nijer@example.com", own)
        self._txn("1200.00", own, own_customer)

        client = Client()
        client.force_login(partner_user)
        s = client.session
        s["active_business_id"] = str(self.biz.id)
        s["ui_lang"] = "bn"
        s.save()

        other_asn = self._assignment(self.other_customer, self.other)
        res = client.get(self._preview_url(other_asn))
        self.assertEqual(res.status_code, 404)
        self.assertNotIn("Salma Begum", res.content.decode())
        self.assertNotIn("500.00", res.content.decode())

        own_asn = self._assignment(own_customer, own)
        res = client.get(self._preview_url(own_asn))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["name"], "Nijer Customer")

    def test_preview_other_business_is_404(self):
        other_owner = make_user("remotherbiz")
        res = auth_client(other_owner).post(
            "/api/businesses/", {"name": "Another Shop"}, format="json")
        other_biz = Business.objects.get(pk=res.json()["id"])
        other_customer = self._add_customer(
            "Foreign Person", "01722222222", "f@example.com", self.other)
        # move the assignment's customer into the other business
        asn = self._assignment(other_customer, self.other)
        asn.customer.business = other_biz
        asn.customer.save(update_fields=["business"])
        res = self.client.get(self._preview_url(asn))
        self.assertEqual(res.status_code, 404)


class ReminderSendTests(ReminderTestBase):
    def test_whatsapp_send_logs_and_returns_wa_link(self):
        asn = self._assignment()
        res = self.client.post(self._send_url(asn), {"channel": "WHATSAPP"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["channel"], "WHATSAPP")
        self.assertTrue(data["wa_url"].startswith("https://wa.me/8801712345678?text="))

        log = DueReminder.objects.get()
        self.assertEqual(log.channel, DueReminder.Channel.WHATSAPP)
        self.assertEqual(log.status, DueReminder.Status.SENT)
        self.assertEqual(log.amount, Decimal("3000.00"))
        self.assertEqual(log.customer, self.customer)
        self.assertEqual(log.partner, self.partner)
        self.assertEqual(log.recipient, "8801712345678")
        self.assertEqual(log.sent_by, self.owner)
        self.assertIn("Hosen Ali", log.message)
        self.assertEqual(len(mail.outbox), 0)   # whatsapp sends no mail

    def test_email_send_sends_mail_and_logs(self):
        asn = self._assignment()
        res = self.client.post(self._send_url(asn), {"channel": "EMAIL"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ["hosen@example.com"])
        self.assertIn("Hosen Ali", sent.body)
        self.assertIn("৳3,000.00", sent.body)

        log = DueReminder.objects.get()
        self.assertEqual(log.channel, DueReminder.Channel.EMAIL)
        self.assertEqual(log.status, DueReminder.Status.SENT)
        self.assertEqual(log.recipient, "hosen@example.com")

    def test_email_failure_is_logged_as_failed(self):
        asn = self._assignment()
        with patch("apps.customers.reminders.send_mail",
                   side_effect=RuntimeError("smtp down")):
            res = self.client.post(self._send_url(asn), {"channel": "EMAIL"})
        self.assertEqual(res.status_code, 502)
        self.assertFalse(res.json()["ok"])
        log = DueReminder.objects.get()
        self.assertEqual(log.status, DueReminder.Status.FAILED)
        self.assertIn("smtp down", log.error)

    def test_send_without_phone_is_refused(self):
        self.customer.phone = ""
        self.customer.save(update_fields=["phone"])
        asn = self._assignment()
        res = self.client.post(self._send_url(asn), {"channel": "WHATSAPP"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"], "no_phone")
        self.assertEqual(DueReminder.objects.count(), 0)

    def test_send_without_email_is_refused(self):
        self.customer.email = ""
        self.customer.save(update_fields=["email"])
        asn = self._assignment()
        res = self.client.post(self._send_url(asn), {"channel": "EMAIL"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"], "no_email")
        self.assertEqual(DueReminder.objects.count(), 0)

    def test_send_without_due_is_refused(self):
        # a customer with an assignment but no due entries at all
        plain = self._add_customer("No Due", "01733333333", "nodue@example.com",
                                   self.partner)
        asn = self._assignment(plain, self.partner)
        res = self.client.post(self._send_url(asn), {"channel": "WHATSAPP"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"], "no_due")
        self.assertEqual(DueReminder.objects.count(), 0)

    def test_bad_channel_is_refused(self):
        res = self.client.post(self._send_url(self._assignment()),
                               {"channel": "CARRIER_PIGEON"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"], "bad_channel")

    def test_send_requires_login(self):
        res = Client().post(self._send_url(self._assignment()),
                            {"channel": "WHATSAPP"})
        self.assertEqual(res.status_code, 401)

    def test_send_is_scoped_to_own_book(self):
        partner_user = make_user("rempartner2")
        BusinessMembership.objects.create(
            user=partner_user, business=self.biz,
            role=BusinessMembership.Role.PARTNER)
        own = Partner.objects.create(
            business=self.biz, name="Own Book", status="ACTIVE",
            created_by=self.owner, linked_user=partner_user)
        client = Client()
        client.force_login(partner_user)
        s = client.session
        s["active_business_id"] = str(self.biz.id)
        s.save()

        other_asn = self._assignment(self.other_customer, self.other)
        res = client.post(self._send_url(other_asn), {"channel": "WHATSAPP"})
        self.assertEqual(res.status_code, 404)
        self.assertEqual(DueReminder.objects.count(), 0)


class ReminderCooldownTests(ReminderTestBase):
    def test_cooldown_blocks_a_second_reminder(self):
        asn = self._assignment()
        res = self.client.post(self._send_url(asn), {"channel": "WHATSAPP"})
        self.assertEqual(res.status_code, 200)

        res = self.client.post(self._send_url(asn), {"channel": "WHATSAPP"})
        self.assertEqual(res.status_code, 409)
        body = res.json()
        self.assertEqual(body["code"], "cooldown")
        self.assertEqual(body["cooldown_days"], COOLDOWN_DAYS)
        self.assertEqual(DueReminder.objects.count(), 1)

    def test_cooldown_can_be_confirmed_explicitly(self):
        asn = self._assignment()
        self.client.post(self._send_url(asn), {"channel": "WHATSAPP"})
        res = self.client.post(self._send_url(asn),
                               {"channel": "WHATSAPP", "confirm_repeat": "1"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(DueReminder.objects.count(), 2)

    def test_preview_reports_cooldown_after_a_reminder(self):
        asn = self._assignment()
        self.client.post(self._send_url(asn), {"channel": "WHATSAPP"})
        data = self.client.get(self._preview_url(asn)).json()
        self.assertTrue(data["cooldown_active"])
        self.assertIsNotNone(data["last_reminder"])
        self.assertEqual(data["last_reminder"]["channel"], "WHATSAPP")


class ReminderUiTests(ReminderTestBase):
    def test_khata_row_shows_the_bell_button(self):
        res = self.client.get(
            f"/customers/?partner={self.partner.id}&pdate={D1.isoformat()}")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode()
        self.assertIn("data-reminder-btn", body)
        self.assertIn(str(self._assignment().id), body)
        self.assertIn("reminder-modal", body)

    def test_history_drawer_lists_sent_reminders(self):
        asn = self._assignment()
        self.client.post(self._send_url(asn), {"channel": "WHATSAPP"})
        res = self.client.get(
            f"/customers/?partner={self.partner.id}&pdate={D1.isoformat()}"
            f"&history={self.customer.id}&partial=history")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode()
        self.assertIn("WhatsApp", body)
        self.assertIn("remowner", body)

    def test_no_bell_button_when_there_is_no_due(self):
        plain = self._add_customer("Settled", "01744444444", "s@example.com",
                                   self.partner)
        asn = self._assignment(plain, self.partner)
        res = self.client.get(
            f"/customers/?partner={self.partner.id}&pdate={D1.isoformat()}")
        self.assertEqual(res.status_code, 200)
        # the settled customer has no row in this day's khata at all
        self.assertNotIn(str(asn.id), res.content.decode())


class ReminderServiceTests(TestCase):
    def test_normalize_phone(self):
        self.assertEqual(normalize_bd_phone("01712345678"), "8801712345678")
        self.assertEqual(normalize_bd_phone("+8801712345678"), "8801712345678")
        self.assertEqual(normalize_bd_phone("008801712345678"), "8801712345678")
        self.assertEqual(normalize_bd_phone("8801712345678"), "8801712345678")
        self.assertEqual(normalize_bd_phone("01712 345 678"), "8801712345678")
        self.assertEqual(normalize_bd_phone(""), "")
        self.assertEqual(normalize_bd_phone("12345"), "")
        self.assertEqual(normalize_bd_phone(None), "")

    def test_format_money(self):
        self.assertEqual(format_money(Decimal("2000")), "৳2,000.00")
        self.assertEqual(format_money(Decimal("1234.5")), "৳1,234.50")
        self.assertEqual(format_money(Decimal("-50")), "-৳50.00")

    def test_whatsapp_url_encodes_the_message(self):
        url = build_whatsapp_url("8801712345678", "প্রিয় হোসেন আলী, বাকি ৳3,000.00")
        self.assertTrue(url.startswith("https://wa.me/8801712345678?text="))
        self.assertNotIn(" ", url.split("text=")[1])

    def test_message_is_bilingual(self):
        class Obj:
            name = "Hosen Ali"
        class Biz:
            name = "Reminder Traders"
        class Partner:
            name = "Karim"
        bn = build_reminder_message(customer=Obj(), business=Biz(),
                                     partner=Partner(), amount=Decimal("3000"))
        en = build_reminder_message(customer=Obj(), business=Biz(),
                                     partner=Partner(), amount=Decimal("3000"),
                                     lang="en")
        self.assertIn("Hosen Ali", bn)
        self.assertIn("Reminder Traders", bn)
        self.assertIn("৳3,000.00", bn)
        self.assertIn("Dear Hosen Ali", en)
        self.assertIn("outstanding balance", en)
