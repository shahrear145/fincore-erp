"""Google Sign-In helpers — minimal, stdlib-only OAuth 2.0 web flow.

Separate from the Drive OAuth client on purpose: login uses the
`openid email profile` scope and the same OAuth client.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid

from django.conf import settings

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"


class GoogleSignInError(Exception):
    pass


def client_id() -> str:
    return getattr(settings, "GOOGLE_CLIENT_ID", "").strip()


def client_secret() -> str:
    return getattr(settings, "GOOGLE_CLIENT_SECRET", "").strip()


def redirect_uri() -> str:
    base = getattr(settings, "SITE_BASE_URL", "").rstrip("/")
    if not base:
        base = "http://127.0.0.1:8000"
    return base + "/auth/google/callback/"


def is_configured() -> bool:
    return bool(client_id() and client_secret())


def authorization_url(state: str) -> str:
    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account",
        "state": state,
    }
    return AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        try:
            info = json.loads(raw)
            raise GoogleSignInError(info.get("error_description",
                                             info.get("error", "Google OAuth error")))
        except Exception:
            raise GoogleSignInError(f"Google-এর সাথে যোগাযোগ ব্যর্থ ({e.code})।")
    return json.loads(raw)


def exchange_code(code: str) -> dict:
    return _post_form(TOKEN_ENDPOINT, {
        "client_id": client_id(),
        "client_secret": client_secret(),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri(),
    })


def fetch_profile(access_token: str) -> dict:
    req = urllib.request.Request(
        USERINFO_ENDPOINT,
        headers={"Authorization": f"Bearer {access_token}",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise GoogleSignInError(f"Google প্রোফাইল আনতে ব্যর্থ ({e.code})।")


def make_state() -> str:
    return uuid.uuid4().hex
