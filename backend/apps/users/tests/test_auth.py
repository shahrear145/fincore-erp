"""Auth + audit + login-history tests."""
from django.test import TestCase
from rest_framework.test import APIClient

from apps.tests_common import auth_client, make_user


class AuthFlowTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = make_user("owner1", "owner1@x.com")

    def test_register_creates_user(self):
        res = self.client.post("/api/auth/register/", {
            "username": "newbie", "email": "newbie@x.com",
            "password": "Str0ngPass!x", "password2": "Str0ngPass!x",
        }, format="json")
        self.assertEqual(res.status_code, 201)

    def test_register_rejects_weak_password(self):
        res = self.client.post("/api/auth/register/", {
            "username": "weakpw", "email": "weak@x.com",
            "password": "12345678", "password2": "12345678",
        }, format="json")
        self.assertEqual(res.status_code, 400)

    def test_login_returns_access_refresh_and_user(self):
        res = self.client.post("/api/auth/login/", {
            "username": "owner1", "password": "S4fePass!2026",
        }, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertIn("access", res.data)
        self.assertIn("refresh", res.data)
        self.assertEqual(res.data["user"]["username"], "owner1")

    def test_login_by_email(self):
        res = self.client.post("/api/auth/login/", {
            "username": "owner1@x.com", "password": "S4fePass!2026",
        }, format="json")
        self.assertEqual(res.status_code, 200)

    def test_failed_login_recorded(self):
        res = self.client.post("/api/auth/login/", {
            "username": "owner1", "password": "wrong-password",
        }, format="json")
        self.assertEqual(res.status_code, 401)
        self.user.refresh_from_db()
        self.assertTrue(self.user.login_events.filter(result="failed").exists())

    def test_me_requires_auth(self):
        res = self.client.get("/api/me/")
        self.assertEqual(res.status_code, 401)
        client = auth_client(self.user)
        res = client.get("/api/me/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["username"], "owner1")

    def test_pin_set_and_check(self):
        client = auth_client(self.user)
        res = client.post("/api/auth/pin/", {"pin": "123456"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_pin("123456"))
        self.assertFalse(self.user.check_pin("654321"))

    def test_change_password_blacklists_sessions(self):
        client = auth_client(self.user)
        res = client.post("/api/auth/password/change/", {
            "current_password": "S4fePass!2026", "new_password": "N3wStrong!pass",
        }, format="json")
        self.assertEqual(res.status_code, 200)
        ok = self.client.post("/api/auth/login/", {
            "username": "owner1", "password": "N3wStrong!pass",
        }, format="json")
        self.assertEqual(ok.status_code, 200)
        bad = self.client.post("/api/auth/login/", {
            "username": "owner1", "password": "S4fePass!2026",
        }, format="json")
        self.assertEqual(bad.status_code, 401)
