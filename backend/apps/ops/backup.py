"""Encrypted backup service (Phase 8).

Backups are Fernet-encrypted (symmetric, authenticated) with a key
derived from BACKUP_ENCRYPTION_KEY (env) or Django's SECRET_KEY hash.
Storage target: S3-compatible bucket when configured, else local
`backups/` under the project — both produce a BackupRecord with the
SHA-256 of the *encrypted* blob for integrity verification.

Restore is explicit (ADMIN-only, confirmed) and never runs automatically.
"""
import hashlib
import os
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.utils import timezone

from apps.ops.models import BackupRecord


def _fernet():
    from cryptography.fernet import Fernet

    raw = getattr(settings, "BACKUP_ENCRYPTION_KEY", "") or ""
    if raw:
        # accept a raw 32-byte urlsafe-base64 key or derive one
        try:
            key = raw.encode() if isinstance(raw, str) else raw
            return Fernet(key)
        except Exception:
            pass
    # deterministic derivation from SECRET_KEY (stable per deployment)
    digest = hashlib.sha256(
        (settings.SECRET_KEY + ":backup").encode()).digest()
    import base64
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def create_backup(user, note: str = "") -> BackupRecord:
    record = BackupRecord.objects.create(
        created_by=user, status=BackupRecord.Status.PENDING,
        encrypted=True, note=note or "")
    try:
        from io import StringIO, BytesIO

        buf = StringIO()
        call_command("dumpdata", "--natural-foreign", "--natural-primary",
                     "--indent", "2",
                     exclude=["contenttypes", "auth.permission",
                              "sessions.session",
                              "admin.logentry"],
                     stdout=buf)
        plaintext = buf.getvalue().encode("utf-8")
        ciphertext = _fernet().encrypt(plaintext)
        sha = hashlib.sha256(ciphertext).hexdigest()
        ts = timezone.now().strftime("%Y%m%d-%H%M%S")
        name = f"fincore-{ts}-{str(record.pk)[:8]}.json.fernet"

        bucket = getattr(settings, "BACKUP_S3_BUCKET", "") or ""
        if bucket:
            _upload_s3(ciphertext, bucket, name)
            record.storage = "s3"
            record.location = f"s3://{bucket}/{name}"
        else:
            base = Path(getattr(settings, "BACKUP_LOCAL_DIR",
                                settings.BASE_DIR / "backups"))
            base.mkdir(parents=True, exist_ok=True)
            path = base / name
            path.write_bytes(ciphertext)
            record.storage = "local"
            record.location = str(path)

        record.size_bytes = len(ciphertext)
        record.sha256 = sha
        record.status = BackupRecord.Status.COMPLETED
        record.save(update_fields=["storage", "location", "size_bytes",
                                   "sha256", "status"])
    except Exception as exc:
        record.status = BackupRecord.Status.FAILED
        record.error = str(exc)[:2000]
        record.save(update_fields=["status", "error"])
    return record


def _upload_s3(data: bytes, bucket: str, key: str):
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=getattr(settings, "BACKUP_S3_ENDPOINT", None) or None,
        aws_access_key_id=getattr(settings, "BACKUP_S3_KEY", None) or None,
        aws_secret_access_key=getattr(settings, "BACKUP_S3_SECRET", None)
        or None,
        region_name=getattr(settings, "BACKUP_S3_REGION", "us-east-1"),
    )
    client.put_object(Bucket=bucket, Key=key, Body=data,
                      ContentType="application/octet-stream",
                      ServerSideEncryption="AES256")


def verify_backup(record: BackupRecord) -> dict:
    """Integrity check: read the blob, confirm SHA-256, confirm decrypts."""
    try:
        if record.storage == "s3":
            import boto3
            bucket = getattr(settings, "BACKUP_S3_BUCKET", "")
            key = record.location.replace(f"s3://{bucket}/", "", 1)
            client = boto3.client(
                "s3",
                endpoint_url=getattr(settings, "BACKUP_S3_ENDPOINT", None)
                or None,
                aws_access_key_id=getattr(settings, "BACKUP_S3_KEY", None)
                or None,
                aws_secret_access_key=getattr(settings, "BACKUP_S3_SECRET",
                                              None) or None,
                region_name=getattr(settings, "BACKUP_S3_REGION",
                                    "us-east-1"))
            blob = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        else:
            blob = Path(record.location).read_bytes()
        sha = hashlib.sha256(blob).hexdigest()
        ok_hash = sha == record.sha256
        decrypted = False
        try:
            _fernet().decrypt(blob)
            decrypted = True
        except Exception:
            decrypted = False
        return {"sha256_match": ok_hash, "decrypts": decrypted,
                "valid": ok_hash and decrypted}
    except Exception as exc:
        return {"valid": False, "error": str(exc)}
