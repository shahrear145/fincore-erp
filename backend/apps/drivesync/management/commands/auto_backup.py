"""Daily automated backup of every local user.

Usage:
    python manage.py auto_backup
    python manage.py auto_backup --keep 30
    python manage.py auto_backup --email

Writes one .json file per user to  ``<BASE_DIR>/backups/``  (or
``--output <dir>``). Old files beyond ``--keep`` (default 30) are deleted.
Optionally emails the snapshot as an attachment to each user.

Free hosting note: PythonAnywhere's *scheduled tasks* are a paid-plan
feature. Without one, the site still backs itself up — every page that
calls ``apps.drivesync.autobackup.maybe_run_auto_backup`` snapshots at
most once per 24h when traffic exists. This command remains for
paid plans / manual runs.
"""

from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.drivesync import autobackup


class Command(BaseCommand):
    help = "প্রতিটি ইউজারের ডেটার JSON স্ন্যাপশট তৈরি করে (ব্যাকআপ)।"

    def add_arguments(self, parser):
        parser.add_argument("--output", default=None,
                            help="লক্ষ্য ডিরেক্টরি (ডিফল্ট: BASE_DIR/backups)")
        parser.add_argument("--keep", type=int, default=30,
                            help="প্রতি ইউজারের জন্য কতটি সাম্প্রতিকতম ফাইল রাখা হবে (ডিফল্ট ৩০)")
        parser.add_argument("--email", action="store_true",
                            help="স্ন্যাপশট ইমেইল অ্যাটাচমেন্টেও পাঠাবে")

    def handle(self, *args, **options):
        out_dir = (Path(options["output"])
                   if options["output"]
                   else Path(getattr(settings, "AUTO_BACKUP_DIR", None)
                             or (Path(settings.BASE_DIR) / "backups")))
        out_dir.mkdir(parents=True, exist_ok=True)

        if options["email"]:
            # same per-user write loop, plus the attachment email
            from django.contrib.auth import get_user_model
            from django.core.mail import EmailMessage
            from apps.drivesync.service import build_snapshot
            import json

            User = get_user_model()
            stamp_files = []
            created = 0
            for user in User.objects.filter(is_active=True):
                payload = json.dumps(
                    build_snapshot(user), ensure_ascii=False, indent=1
                ).encode("utf-8")
                stamp = timezone.now().strftime("%Y%m%d-%H%M%S-%f")
                filename = f"{user.id}-{user.username}-{stamp}.json"
                (out_dir / filename).write_bytes(payload)
                created += 1
                stamp_files.append((filename, payload, user))
                try:
                    email = EmailMessage(
                        subject=f"FinCore backup - {stamp}",
                        body="দৈনিক ব্যাকআপ জমা দেওয়া হয়েছে।\n"
                             "ফাইলে আপনার ব্যক্তিগত ও ব্যবসার সব ডেটার স্ন্যাপশট আছে।",
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        to=[user.email] if user.email else [],
                        attachments=[(filename, payload, "application/json")],
                    )
                    email.send(fail_silently=True)
                except Exception:
                    pass
                self.stdout.write(self.style.SUCCESS(
                    f"  OK {filename} ({len(payload):,} bytes)"))
            autobackup._prune(out_dir, max(1, int(options["keep"])))
        else:
            created = autobackup.write_all_snapshots(
                out_dir, keep=max(1, int(options["keep"])))
            self.stdout.write(self.style.SUCCESS(
                f"  {created} user(s) backed up to {out_dir}"))

        # stamp the sentinel so the lazy path doesn't duplicate right after
        try:
            (out_dir / autobackup.SENTINEL_NAME).write_text(
                str(int(timezone.now().timestamp())), encoding="utf-8")
        except OSError:
            pass

        self.stdout.write(self.style.SUCCESS(
            f"Backup complete - keep {max(1, int(options['keep']))} per user."))
