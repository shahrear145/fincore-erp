"""Shared test factories helper (no fake financial data — these run in an
isolated test database only)."""
from rest_framework.test import APIClient


def make_user(username, email=None, password="S4fePass!2026"):
    from apps.users.models import User

    return User.objects.create_user(
        username=username, email=email or f"{username}@example.com", password=password
    )


def auth_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def login_jwt(client, identifier, password="S4fePass!2026"):
    res = client.post(
        "/api/auth/login/",
        {"username": identifier, "password": password},
        format="json",
    )
    assert res.status_code == 200, res.content
    return res.json()["access"]
