"""UI theme tests — the picker, the library and the saved choice.

Pinned rules:

- the page renders the user's saved theme on <html data-theme="…">;
- the avatar menu offers every theme in the library (aurora + classics + standard);
- picking a theme saves it on the account (so it follows every device)
  and comes back on the next page;
- an unknown theme id is refused and changes nothing;
- the theme library files are actually served (no missing CSS).
"""
from django.test import Client, TestCase

from apps.organizations.models import Business
from apps.tests_common import auth_client, make_user
from apps.users.models import User
from apps.web.context_processors import THEME_CHOICES, THEME_IDS


class ThemeTests(TestCase):
    def setUp(self):
        self.user = make_user("themeuser")
        res = auth_client(self.user).post(
            "/api/businesses/", {"name": "Theme Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        self.client = Client()
        self.client.force_login(self.user)
        s = self.client.session
        s["active_business_id"] = str(self.biz.id)
        s["ui_lang"] = "bn"
        s.save()

    def _html(self, path="/"):
        res = self.client.get(path)
        self.assertEqual(res.status_code, 200)
        return res.content.decode("utf-8")

    def test_default_theme_is_aurora(self):
        self.assertEqual(User.objects.get(pk=self.user.pk).theme, "aurora")
        html = self._html()
        self.assertIn('data-theme="aurora"', html)

    def test_every_theme_is_offered_in_the_menu(self):
        html = self._html()
        for theme_id, label, _c1, _c2 in THEME_CHOICES:
            self.assertIn(f'value="{theme_id}"', html)
            self.assertIn(label, html)
        # twelve designed/premium (aurora, classics, neon, clean) + five standard (flat)
        self.assertEqual(len([t for t in THEME_CHOICES if t[0].startswith("flat-")]), 5)
        self.assertEqual(len([t for t in THEME_CHOICES if not t[0].startswith("flat-")]), 12)

    def test_picking_a_theme_saves_it_on_the_account(self):
        res = self.client.post("/set-theme/", {"theme": "midnight", "next": "/"})
        self.assertEqual(res.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.theme, "midnight")
        # and the next page wears it
        self.assertIn('data-theme="midnight"', self._html())

    def test_a_theme_survives_the_next_request(self):
        self.client.post("/set-theme/", {"theme": "royal"})
        self.client.post("/set-theme/", {"theme": "flat-teal"})
        html = self._html("/summary/")
        self.assertIn('data-theme="flat-teal"', html)

    def test_unknown_theme_is_refused(self):
        res = self.client.post("/set-theme/", {"theme": "not-a-theme"})
        self.assertEqual(res.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.theme, "aurora")

    def test_theme_switch_needs_login(self):
        res = Client().post("/set-theme/", {"theme": "midnight"})
        self.assertEqual(res.status_code, 302)
        self.assertIn("login", res["Location"])

    def test_theme_stylesheets_are_served(self):
        from django.contrib.staticfiles import finders
        for css, marker in (("web/premium.css", "--t-"), ("web/themes.css", "--t-"), ("web/modern.css", "--m-")):
            path = finders.find(css)
            self.assertIsNotNone(path, css)
            with open(path, encoding="utf-8") as fh:
                body = fh.read()
            self.assertIn("{", body)
            self.assertIn(marker, body)

    def test_each_theme_defines_its_own_colours(self):
        from django.contrib.staticfiles import finders
        body = ""
        for css in ("web/themes.css", "web/modern.css"):
            with open(finders.find(css), encoding="utf-8") as fh:
                body += fh.read()
        for theme_id in THEME_IDS:
            self.assertIn(f'[data-theme="{theme_id}"]', body)
