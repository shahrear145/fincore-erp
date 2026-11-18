"""ব্যবসার সেটিংস tests — নাম/ফোন/ইমেইল/ঠিকানা বদলানো (শুধু অ্যাডমিন).

Pinned rules:

- the business name can be changed from the app, by the business ADMIN;
- a partner-role user cannot change it (the books stay untouched);
- an empty name is refused (the old name survives);
- renaming is display-only: the slug and every record stay as they were,
  while the new name shows up across the UI (sidebar chip, page chip);
- the change is written to the audit log.
"""
from django.test import Client, TestCase

from apps.core.middleware import AuditLog
from apps.organizations.models import Business, BusinessMembership
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user


class BusinessSettingsTests(TestCase):
    def setUp(self):
        self.owner = make_user("bizowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Old Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        self.slug = self.biz.slug

        self.partner = Partner.objects.create(
            business=self.biz, name="Rahim", status="ACTIVE",
            created_by=self.owner)

        self.client = Client()
        self.client.force_login(self.owner)
        s = self.client.session
        s["active_business_id"] = str(self.biz.id)
        s["ui_lang"] = "bn"
        s.save()

    def _post(self, **fields):
        data = {"name": "New Traders", "contact_phone": "01711111111",
                "contact_email": "shop@example.com", "address": "Shop road"}
        data.update(fields)
        return self.client.post("/business/settings/", data)

    # --- page -------------------------------------------------------------

    def test_settings_page_renders_the_current_name(self):
        res = self.client.get("/business/settings/")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode()
        self.assertIn("Old Traders", body)
        self.assertIn('name="name"', body)
        self.assertIn('name="contact_phone"', body)
        # the internal slug never shows up on the page (it looks like a
        # leftover old name to the user)
        self.assertNotIn(self.slug, body)

    def test_settings_page_requires_login(self):
        res = Client().get("/business/settings/")
        self.assertEqual(res.status_code, 302)
        self.assertIn("login", res["Location"])

    # --- saving -----------------------------------------------------------

    def test_admin_can_rename_the_business(self):
        res = self._post()
        self.assertEqual(res.status_code, 302)
        self.biz.refresh_from_db()
        self.assertEqual(self.biz.name, "New Traders")
        self.assertEqual(self.biz.contact_phone, "01711111111")
        self.assertEqual(self.biz.contact_email, "shop@example.com")
        self.assertEqual(self.biz.address, "Shop road")

    def test_rename_keeps_the_slug_and_the_records(self):
        partner = self.partner
        self._post()
        self.biz.refresh_from_db()
        self.assertEqual(self.biz.slug, self.slug)      # stable identifier
        self.assertEqual(partner.business_id, self.biz.id)
        self.assertEqual(partner.name, "Rahim")

    def test_empty_name_is_refused(self):
        res = self._post(name="   ")
        self.assertEqual(res.status_code, 200)          # re-rendered with error
        self.biz.refresh_from_db()
        self.assertEqual(self.biz.name, "Old Traders")

    def test_change_is_audited(self):
        self._post()
        log = AuditLog.objects.filter(action="business_update").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.business_id, self.biz.id)
        self.assertEqual(log.previous.get("name"), "Old Traders")

    # --- scoping ----------------------------------------------------------

    def test_partner_user_cannot_rename(self):
        partner_user = make_user("bizpartner")
        BusinessMembership.objects.create(
            user=partner_user, business=self.biz,
            role=BusinessMembership.Role.PARTNER)
        client = Client()
        client.force_login(partner_user)
        s = client.session
        s["active_business_id"] = str(self.biz.id)
        s["ui_lang"] = "bn"
        s.save()

        res = client.post("/business/settings/", {"name": "Hijacked"})
        self.assertEqual(res.status_code, 302)
        self.assertNotIn("business/settings", res["Location"])
        self.biz.refresh_from_db()
        self.assertEqual(self.biz.name, "Old Traders")

    def test_other_business_is_not_reachable(self):
        other_owner = make_user("bizother")
        res = auth_client(other_owner).post(
            "/api/businesses/", {"name": "Another Shop"}, format="json")
        other = Business.objects.get(pk=res.json()["id"])

        client = Client()
        client.force_login(other_owner)
        s = client.session
        s["active_business_id"] = str(other.id)
        s.save()
        res = client.post("/business/settings/", {"name": "Mine Now"})
        self.assertEqual(res.status_code, 302)
        self.biz.refresh_from_db()
        self.assertEqual(self.biz.name, "Old Traders")
        other.refresh_from_db()
        self.assertEqual(other.name, "Mine Now")

    # --- the new name really shows up -------------------------------------

    def test_new_name_shows_in_the_ui(self):
        self._post()
        res = self.client.get(
            f"/customers/?partner={self.partner.id}&pdate=2026-11-09")
        self.assertEqual(res.status_code, 200)
        self.assertIn("New Traders", res.content.decode())

    def test_sidebar_link_only_for_admin(self):
        admin_body = self.client.get("/").content.decode()
        self.assertIn("business/settings", admin_body)

        partner_user = make_user("bizpartner2")
        BusinessMembership.objects.create(
            user=partner_user, business=self.biz,
            role=BusinessMembership.Role.PARTNER)
        client = Client()
        client.force_login(partner_user)
        s = client.session
        s["active_business_id"] = str(self.biz.id)
        s.save()
        partner_body = client.get("/").content.decode()
        self.assertNotIn("business/settings", partner_body)
