"""Diagnose the outbound email (Gmail SMTP) setup.

Usage on the server:

    python manage.py check_email                 # check config + login only
    python manage.py check_email --send you@x.com  # also send a test mail

Nothing is stored; the password is never printed.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.users.otp import _explain

OK = "✅"
BAD = "❌"


class Command(BaseCommand):
    help = "Check Gmail SMTP settings and (optionally) send a test email."

    def add_arguments(self, parser):
        parser.add_argument(
            "--send", metavar="EMAIL", default=None,
            help="Send a real test email to this address.",
        )

    def handle(self, *args, **options):
        backend = getattr(settings, "EMAIL_BACKEND", "")
        host = getattr(settings, "EMAIL_HOST", "")
        port = getattr(settings, "EMAIL_PORT", "")
        use_tls = getattr(settings, "EMAIL_USE_TLS", False)
        use_ssl = getattr(settings, "EMAIL_USE_SSL", False)
        user = getattr(settings, "EMAIL_HOST_USER", "") or ""
        pwd = getattr(settings, "EMAIL_HOST_PASSWORD", "") or ""
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "") or ""

        self.stdout.write("--- effective settings ---")
        self.stdout.write(f"EMAIL_BACKEND      : {backend}")
        self.stdout.write(f"EMAIL_HOST         : {host}")
        self.stdout.write(f"EMAIL_PORT         : {port}")
        self.stdout.write(f"EMAIL_USE_TLS      : {use_tls}")
        self.stdout.write(f"EMAIL_USE_SSL      : {use_ssl}")
        self.stdout.write(f"EMAIL_HOST_USER    : {user or '<EMPTY>'}")
        self.stdout.write(
            f"EMAIL_HOST_PASSWORD: {'<EMPTY>' if not pwd else '<set>'} "
            f"length={len(pwd)} spaces={' ' in pwd}"
        )
        self.stdout.write(f"DEFAULT_FROM_EMAIL : {from_email or '<EMPTY>'}")

        if "console" in backend:
            self.stdout.write(self.style.WARNING(
                "\nconsole backend is active — emails are printed, not sent. "
                "Set EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend"))
            return

        if not user or not pwd:
            self.stdout.write(self.style.ERROR(
                f"\n{BAD} EMAIL_HOST_USER / EMAIL_HOST_PASSWORD is empty in .env"))
            return

        self.stdout.write("\n--- SMTP login test ---")
        try:
            import smtplib
            import ssl

            if use_ssl:
                conn = smtplib.SMTP_SSL(host, int(port), timeout=30,
                                        context=ssl.create_default_context())
            else:
                conn = smtplib.SMTP(host, int(port), timeout=30)
            conn.ehlo()
            if use_tls and not use_ssl:
                conn.starttls(context=ssl.create_default_context())
                conn.ehlo()
            code, resp = conn.login(user, pwd)
            conn.quit()
            self.stdout.write(self.style.SUCCESS(
                f"{OK} LOGIN OK: {code} {resp.decode(errors='replace') if isinstance(resp, bytes) else resp}"))
        except Exception as exc:
            self.stdout.write(self.style.ERROR(
                f"{BAD} LOGIN FAILED: {type(exc).__name__}\n   {_explain(exc)}"))
            return

        target = options.get("send")
        if not target:
            self.stdout.write("\n(login works — run with --send you@example.com "
                              "to send a real test mail)")
            return

        self.stdout.write(f"\n--- sending test email to {target} ---")
        from django.core.mail import send_mail

        try:
            send_mail(
                "FinCore ERP — SMTP test",
                "This is a test message from FinCore ERP.\n"
                "If you can read this, email delivery works.",
                from_email or user,
                [target],
                fail_silently=False,
            )
        except Exception as exc:
            self.stdout.write(self.style.ERROR(
                f"{BAD} SEND FAILED: {type(exc).__name__}\n   {_explain(exc)}"))
            return
        self.stdout.write(self.style.SUCCESS(f"{OK} sent — check the inbox/spam"))
