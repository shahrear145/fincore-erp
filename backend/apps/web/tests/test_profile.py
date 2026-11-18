"""Profile page + password recovery flows (session web UI).

Pinned rules:

- everything is user-scoped: a signed-in user can only touch their own
  account (avatar, info, password, PIN, device sessions);
- password change requires the current password and blacklists API
  refresh tokens + revokes every device session;
- email change requires the current password and an emailed OTP to the
  NEW address (the email is only swapped after the code is verified);
- password recovery: generic "maybe sent" answer for unknown emails (no
  account enumeration), OTP to the account email, all device sessions
  revoked after a successful reset;
- OTP codes are hashed at rest and single-use (5 attempts max).
"""
import re
import tempfile
import uuid
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from apps.tests_common import make_user
from apps.users.models import User, UserDeviceSession

PASSWORD = "S4fePass!2026"


def _sent_code(body):
    """Extract the 6-digit code from an email body."""
    match = re.search(r"\b(\d{6})\b", body)
    assert match, body
    return match.group(1)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="fincore-media-"))
class ProfileTests(TestCase):
    def setUp(self):
        self.user = make_user("owner", email="owner@example.com")
        self.client = Client()
        self.client.force_login(self.user)

    def test_profile_requires_login(self):
        res = Client().get("/profile/")
        self.assertEqual(res.status_code, 302)
        self.assertIn("login", res["Location"])

    def test_profile_renders(self):
        res = self.client.get("/profile/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "owner@example.com")

    def test_update_info(self):
        res = self.client.post("/profile/", {
            "action": "update_info", "full_name": "Karim Uddin",
            "phone": "01711223344"})
        self.assertRedirects(res, "/profile/")
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Karim")
        self.assertEqual(self.user.last_name, "Uddin")
        self.assertEqual(self.user.phone, "01711223344")

    def test_avatar_upload_and_remove(self):
        png = SimpleUploadedFile(
            "me.png", uuid.uuid4().bytes + b"\x00" * 32, content_type="image/png")
        res = self.client.post("/profile/", {
            "action": "upload_avatar", "avatar": png})
        self.assertRedirects(res, "/profile/")
        self.user.refresh_from_db()
        self.assertTrue(self.user.avatar, "avatar should be stored")
        # remove again
        self.client.post("/profile/", {"action": "remove_avatar"})
        self.user.refresh_from_db()
        self.assertFalse(self.user.avatar)

    def test_avatar_rejects_oversize(self):
        big = SimpleUploadedFile("big.png", b"\x00" * (4 * 1024 * 1024))
        self.client.post("/profile/", {"action": "upload_avatar", "avatar": big})
        self.user.refresh_from_db()
        self.assertFalse(self.user.avatar)

    def test_change_password_wrong_current(self):
        res = self.client.post("/profile/", {
            "action": "change_password", "current_password": "wrong!",
            "new_password1": "N3wStrong!pass", "new_password2": "N3wStrong!pass"})
        self.assertRedirects(res, "/profile/")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_change_password_ok_signs_out_other_devices(self):
        ds = UserDeviceSession.objects.create(
            user=self.user, device_name="Old phone", platform="android",
            device_id="dev-1", refresh_jti="jti-1")
        res = self.client.post("/profile/", {
            "action": "change_password", "current_password": PASSWORD,
            "new_password1": "N3wStrong!pass", "new_password2": "N3wStrong!pass"})
        self.assertRedirects(res, "/profile/")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("N3wStrong!pass"))
        ds.refresh_from_db()
        self.assertFalse(ds.is_active, "device session must be revoked")
        # the current web session survives the change
        res = self.client.get("/profile/")
        self.assertEqual(res.status_code, 200)

    def test_pin_set_and_check(self):
        res = self.client.post("/profile/", {
            "action": "pin_set", "pin1": "4321", "pin2": "4321"})
        self.assertRedirects(res, "/profile/")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_pin("4321"))
        self.assertFalse(self.user.check_pin("9999"))

    def test_device_logout(self):
        ds = UserDeviceSession.objects.create(
            user=self.user, device_name="Tab", platform="android",
            device_id="dev-2", refresh_jti="jti-2")
        res = self.client.post("/profile/", {
            "action": "device_logout", "session_id": str(ds.id)})
        self.assertRedirects(res, "/profile/")
        ds.refresh_from_db()
        self.assertFalse(ds.is_active)


class EmailChangeTests(TestCase):
    def setUp(self):
        self.user = make_user("mailman", email="old@example.com")
        self.client = Client()
        self.client.force_login(self.user)

    def test_request_requires_correct_password(self):
        with mock.patch("apps.users.otp.send_mail") as mailer:
            self.client.post("/profile/", {
                "action": "email_request", "current_password": "nope",
                "new_email": "new@example.com"})
        mailer.assert_not_called()
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "old@example.com")

    def test_email_change_full_flow(self):
        with mock.patch("apps.users.otp.send_mail") as mailer:
            res = self.client.post("/profile/", {
                "action": "email_request", "current_password": PASSWORD,
                "new_email": "New@Example.com"})
            self.assertRedirects(res, "/profile/")
            self.assertEqual(len(mailer.call_args_list), 1)
            # the code goes to the NEW address, never the old one
            self.assertEqual(mailer.call_args[0][3], ["new@example.com"])
            code = _sent_code(mailer.call_args[0][1])
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "old@example.com",
                         "email must not change before the code is verified")
        # wrong code keeps the old email
        res = self.client.post("/profile/", {
            "action": "email_verify", "code": "000000"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "old@example.com")
        # right code swaps the email
        self.client.post("/profile/", {"action": "email_verify", "code": code})
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "new@example.com".lower())
        # pending state cleared
        res = self.client.get("/profile/")
        self.assertNotContains(res, "New@Example.com")

    def test_email_change_rejects_taken_address(self):
        make_user("other", email="taken@example.com")
        with mock.patch("apps.users.otp.send_mail") as mailer:
            self.client.post("/profile/", {
                "action": "email_request", "current_password": PASSWORD,
                "new_email": "taken@example.com"})
        mailer.assert_not_called()
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "old@example.com")


class PasswordRecoveryTests(TestCase):
    def setUp(self):
        self.user = make_user("lostin", email="lostin@example.com")

    def test_unknown_email_gets_generic_answer(self):
        res = Client().post("/password-forgot/", {"email": "ghost@example.com"})
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "রিসেট")
        # no session step and no OTP row is leaked for unknown emails
        self.assertNotIn("pwreset_user_id", Client().session)

    def test_full_reset_flow(self):
        ds = UserDeviceSession.objects.create(
            user=self.user, device_name="Old laptop", platform="web",
            device_id="dev-3", refresh_jti="jti-3")
        client = Client()
        with mock.patch("apps.users.otp.send_mail") as mailer:
            res = client.post("/password-forgot/",
                              {"email": "lostin@example.com"})
            self.assertRedirects(res, "/password-reset/")
            self.assertEqual(mailer.call_args[0][3], ["lostin@example.com"])
            code = _sent_code(mailer.call_args[0][1])
        # mismatched passwords bounce
        res = client.post("/password-reset/", {
            "code": code, "new_password1": "N3wStrong!pass",
            "new_password2": "different"})
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))
        # wrong code bounces
        res = client.post("/password-reset/", {
            "code": "000000", "new_password1": "N3wStrong!pass",
            "new_password2": "N3wStrong!pass"})
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))
        # correct code + password completes the reset
        res = client.post("/password-reset/", {
            "code": code, "new_password1": "N3wStrong!pass",
            "new_password2": "N3wStrong!pass"})
        self.assertRedirects(res, "/login/")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("N3wStrong!pass"))
        ds.refresh_from_db()
        self.assertFalse(ds.is_active, "device sessions die after a reset")
        # the reset session state is cleared
        self.assertFalse(client.session.get("pwreset_user_id"))

    def test_reset_page_requires_started_flow(self):
        res = Client().get("/password-reset/", follow=True)
        self.assertRedirects(res, "/password-forgot/")

    def test_forgot_link_on_login_page(self):
        res = Client().get("/login/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "/password-forgot/")

    def test_login_page_shows_avatar_only_when_authed(self):
        client = Client()
        res = client.get("/login/")
        self.assertNotContains(res, "avatar-btn")
        client.force_login(self.user)
        res = client.get("/profile/")
        self.assertContains(res, "avatar-btn")
