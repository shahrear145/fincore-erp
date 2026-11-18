"""Google OAuth 2.0 (web app) + Drive v3 REST client, standard library only.

No google-api-python-client dependency; every call goes through urllib.
Scope: drive.file — only the files this app created, nothing else.
"""
import json
import urllib.parse
import urllib.request
import urllib.error
import uuid
import hashlib
import base64

from django.conf import settings


AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"


class DriveError(Exception):
    """Raised with a Bangla, user-safe message."""
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def client_id() -> str:
    return getattr(settings, "GOOGLE_CLIENT_ID", "").strip()


def client_secret() -> str:
    return getattr(settings, "GOOGLE_CLIENT_SECRET", "").strip()


def redirect_uri() -> str:
    base = getattr(settings, "SITE_BASE_URL", "").rstrip("/")
    if not base:
        base = "http://127.0.0.1:8000"
    return base + "/drive/callback/"


def is_configured() -> bool:
    return bool(client_id() and client_secret())


def authorization_url(state: str) -> str:
    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/drive.file",
        "access_type": "offline",
        "prompt": "consent",                 # always yields a refresh token
        "state": state,
    }
    return AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)


def _post_form(url: str, data: dict, timeout: int = 30) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore") if e.fp else ""
        try:
            info = json.loads(raw)
            raise DriveError(info.get("error_description",
                                      info.get("error", "OAuth error")))
        except Exception:
            raise DriveError(f"গুগল থেকে সাড়া পাওয়া যায়নি ({e.code})।")
    return json.loads(raw) if raw else {}


def exchange_code(code: str) -> dict:
    return _post_form(TOKEN_ENDPOINT, {
        "client_id": client_id(),
        "client_secret": client_secret(),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri(),
    })


def refresh_access_token(refresh_token: str) -> dict:
    return _post_form(TOKEN_ENDPOINT, {
        "client_id": client_id(),
        "client_secret": client_secret(),
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })


def _drive(path_suffix: str, *, method="GET", token=None, json_body=None,
           form_body=None, multipart=False, timeout=60):
    url = DRIVE_API + path_suffix
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/json"}
    data = None
    if multipart and json_body is not None and form_body is not None:
        boundary = uuid.uuid4().hex
        json_bytes = json.dumps(json_body).encode("utf-8")
        fields = [
            (# JSON metadata part
                f"--{boundary}\r\n"
                'Content-Type: application/json; charset=UTF-8\r\n\r\n').encode("utf-8")
            + json_bytes + b"\r\n",
            (# file bytes
                f"--{boundary}\r\n"
                f"Content-Type: {form_body[0]}\r\n\r\n").encode("utf-8")
            + form_body[1] + b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
        data = b"".join(fields)
        url = UPLOAD_API + path_suffix + f"?uploadType=multipart"
        headers["Content-Type"] = (
            f"multipart/related; boundary={boundary}")
    elif json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif form_body is not None:
        data = form_body
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            if raw:
                return json.loads(raw)
            return {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore") if e.fp else ""
        try:
            info = json.loads(raw)
            raise DriveError(info.get("error", {}).get(
                "message", "Google Drive error"))
        except Exception:
            raise DriveError(f"গুগল ড্রাইভে সমস্যা ({e.code})।")


def find_folder(name: str, *, token: str):
    escaped = name.replace("'", "\\'")
    q = ("mimeType='application/vnd.google-apps.folder' and trashed=false "
         f"and name='{escaped}'")
    params = urllib.parse.urlencode({"q": q, "fields": "files(id,name)"})
    data = _drive(f"/files?{params}", token=token)
    items = data.get("files", [])
    return items[0] if items else None


def create_folder(name: str, *, parent=None, token: str):
    body = {"name": name,
            "mimeType": "application/vnd.google-apps.folder"}
    if parent:
        body["parents"] = [parent]
    return _drive("/files?fields=id,name", method="POST",
                  json_body=body, token=token)


def find_file(name: str, *, parent: str, token: str):
    q = (f"name='{name.replace(chr(39), chr(92) + chr(39))}' and "
         f"'{parent}' in parents and trashed=false")
    params = urllib.parse.urlencode({"q": q, "fields": "files(id,name)"})
    data = _drive(f"/files?{params}", token=token)
    items = data.get("files", [])
    return items[0] if items else None


def upload_file(name: str, payload: bytes, *, parent: str,
                token: str, mime="application/json"):
    body = {"name": name, "parents": [parent],
            "mimeType": mime}
    params = urllib.parse.urlencode(
        {"fields": "id,name,size,modifiedTime"})
    return _drive(f"/files?{params}", method="POST",
                  json_body=body, form_body=(mime, payload),
                  multipart=True, token=token, timeout=120)


def download_file(file_id: str, *, token: str) -> bytes:
    req = urllib.request.Request(
        f"{DRIVE_API}/files/{file_id}?alt=media",
        headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise DriveError(f"গুগল ড্রাইভ থেকে ডাউনলোড করা যায়নি ({e.code})।")


def update_file_contents(file_id: str, payload: bytes, *, token: str,
                         mime="application/json") -> None:
    url = f"{UPLOAD_API}/files/{file_id}?uploadType=media"
    req = urllib.request.Request(
        url, data=payload,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": mime + "; charset=utf-8"},
        method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        raise DriveError(f"ফাইল আপডেট করা যায়নি ({e.code})।")
