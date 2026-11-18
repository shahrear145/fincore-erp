"""Fernet helpers for at-rest secrets (derived from SECRET_KEY like ops)."""
import hashlib
import base64


def fernet():
    from cryptography.fernet import Fernet
    from django.conf import settings
    raw = hashlib.sha256(
        (settings.SECRET_KEY + ":drivesync").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt(plaintext: str) -> str:
    return fernet().encrypt(plaintext.encode()).decode()


def decrypt(token_enc: str) -> str:
    return fernet().decrypt(token_enc.encode()).decode()
