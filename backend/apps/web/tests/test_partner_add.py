""""পার্টনার যোগ করুন" — web flow regression tests.

Two separate bugs are pinned here:

1. The submit button of the add-partner popover did nothing in the browser.
   Server-side the POST always worked; the popover is absolutely positioned
   inside a `.card`, and `.card` carries `backdrop-filter`, which creates a
   stacking context. The popover's `z-index` therefore only counted *inside*
   its own card, so the next card (the empty state) painted on top of the
   button and swallowed the click. `app.css` now lifts the card while its
   `<details>` is open, and `base.html` keeps the `card-open` fallback class in
   sync for browsers without `:has()`.

2. A business could exist with no `BusinessMembership` for its owner. Every
   screen resolves the active business through memberships, so such a business
   (and all of its partners/customers/transactions) was invisible in the web
   UI. The repair migration and the admin hook are covered in
   `apps/organizations/tests/test_business.py`.
"""
import re
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.test import Client, SimpleTestCase, TestCase

from apps.organizations.models import Business, BusinessMembership
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user


class AddPartnerWebTests(TestCase):
    def setUp(self):
        self.owner = make_user("partnerowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Add Partner Shop"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])

        self.client = Client()
        self.client.force_login(self.owner)
        s = self.client.session
        s["active_business_id"] = str(self.biz.id)
        s["ui_lang"] = "bn"
        s.save()

    def _add(self, name="করিম", phone="01711111111"):
        return self.client.post(
            "/customers/",
            {"action": "add_partner", "partner_name": name,
             "partner_phone": phone},
        )

    def test_customers_page_offers_the_add_partner_form_to_admin(self):
        res = self.client.get("/customers/")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode()
        self.assertIn('value="add_partner"', body)
        self.assertIn('name="partner_name"', body)

    def test_admin_can_add_a_partner_through_the_web_form(self):
        res = self._add()
        self.assertEqual(res.status_code, 302)
        partner = Partner.objects.get(business=self.biz, name="করিম")
        self.assertEqual(partner.phone, "01711111111")
        self.assertEqual(partner.status, Partner.Status.ACTIVE)
        self.assertEqual(res["Location"], f"/customers/?partner={partner.id}")

        page = self.client.get(res["Location"])
        self.assertEqual(page.status_code, 200)
        self.assertIn("করিম", page.content.decode())

    def test_blank_name_is_refused_and_nothing_is_created(self):
        res = self._add(name="   ")
        self.assertEqual(res.status_code, 302)
        self.assertEqual(Partner.objects.filter(business=self.biz).count(), 0)

    def test_partner_role_cannot_add_partners(self):
        other = make_user("partneronly")
        BusinessMembership.objects.create(
            user=other, business=self.biz,
            role=BusinessMembership.Role.PARTNER,
        )
        c = Client()
        c.force_login(other)
        s = c.session
        s["active_business_id"] = str(self.biz.id)
        s.save()

        res = c.post("/customers/", {
            "action": "add_partner", "partner_name": "চোরাই পার্টনার"})
        self.assertEqual(res.status_code, 302)
        self.assertFalse(Partner.objects.filter(name="চোরাই পার্টনার").exists())

        page = c.get("/customers/").content.decode()
        self.assertNotIn('value="add_partner"', page)


class PartnerPopoverStackingTests(SimpleTestCase):
    """The CSS/JS contract that keeps the popover clickable."""

    def _read(self, path):
        return Path(path).read_text(encoding="utf-8")

    def test_card_is_lifted_above_the_next_card_while_details_is_open(self):
        css_path = finders.find("web/app.css")
        self.assertIsNotNone(css_path)
        css = self._read(css_path)
        rule = re.search(
            r"\.card:has\(details\[open\]\)\s*,\s*\.card\.card-open\s*\{([^}]*)\}",
            css,
        )
        self.assertIsNotNone(
            rule, "app.css must lift a card that contains an open <details>")
        declarations = rule.group(1)
        self.assertIn("position:relative", declarations.replace(" ", ""))
        self.assertRegex(declarations.replace(" ", ""), r"z-index:\d+")

    def test_base_template_keeps_the_card_open_class_in_sync(self):
        base = Path(settings.BASE_DIR) / "apps/web/templates/web/base.html"
        html = self._read(base)
        self.assertIn("'toggle'", html)
        self.assertIn("card-open", html)
        self.assertIn("closest('.card')", html)
