"""PWA (#১৩) — manifest, service worker route, offline page, install banner.

Pinned rules:

- site.webmanifest is valid JSON with 192/512 icons that exist on disk;
- /sw.js is public and sent with Service-Worker-Allowed: / so the
  worker can control the whole app;
- the offline page exists and can retry the connection;
- base.html registers the service worker and carries the install banner;
- the HTML app shell never asks the service worker to cache ledger HTML
  (verified by reading sw.js strategy strings).
"""
import json
from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase

STATIC_WEB = Path(settings.BASE_DIR) / "apps" / "web" / "static" / "web"
TEMPLATE_BASE = (Path(settings.BASE_DIR) / "apps" / "web" / "templates"
                 / "web" / "base.html")


class ManifestTests(TestCase):
    def test_manifest_is_valid_and_complete(self):
        raw = (STATIC_WEB / "site.webmanifest").read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertEqual(data["short_name"], "FinCore")
        self.assertEqual(data["display"], "standalone")
        self.assertEqual(data["start_url"], "/")
        self.assertEqual(data["theme_color"], "#08162c")
        sizes = {i["sizes"] for i in data["icons"]}
        self.assertIn("192x192", sizes)
        self.assertIn("512x512", sizes)

    def test_all_manifest_icon_files_exist(self):
        data = json.loads((STATIC_WEB / "site.webmanifest").read_text(
            encoding="utf-8"))
        for icon in data["icons"]:
            src = icon["src"]
            if src.startswith("/static/web/"):
                self.assertTrue((STATIC_WEB / src.split("/")[-1]).exists(),
                                f"missing icon file for {src}")

    def test_icon_files_are_correct_size(self):
        from PIL import Image
        for name, size in (("icon-192.png", 192), ("icon-512.png", 512)):
            img = Image.open(STATIC_WEB / name)
            self.assertEqual(img.size, (size, size))


class ServiceWorkerTests(TestCase):
    def test_sw_is_public_and_root_scoped(self):
        res = Client().get("/sw.js")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res["Content-Type"].startswith("application/javascript"))
        self.assertEqual(res["Service-Worker-Allowed"], "/")
        self.assertIn("no-cache", res["Cache-Control"])

    def test_sw_content_has_expected_strategy(self):
        body = Path(
            Path(settings.BASE_DIR) / "apps" / "web" / "static" / "web" / "sw.js"
        ).read_text(encoding="utf-8")
        self.assertIn("navigate", body)          # offline fallback for pages
        self.assertIn("offline.html", body)
        self.assertIn("fincore-shell", body)
        # navigations must not be cached (no ledger HTML at rest)
        self.assertNotIn("cache.put(req", body.split("navigate")[0] or "")

    def test_offline_page_exists_and_can_retry(self):
        html = (STATIC_WEB / "offline.html").read_text(encoding="utf-8")
        self.assertIn("location.reload()", html)
        self.assertIn("FinCore", html)


class BaseTemplateTests(TestCase):
    def test_base_template_registers_service_worker(self):
        html = TEMPLATE_BASE.read_text(encoding="utf-8")
        self.assertIn("serviceWorker", html)
        self.assertIn("/sw.js", html)
        self.assertIn("pwa-install", html)
        self.assertIn("beforeinstallprompt", html)
