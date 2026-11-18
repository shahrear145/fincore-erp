# Development Environment Setup

## 1. One-time environment prep (Windows, verified)

```powershell
# Python 3.12 (installed via winget in this project)
winget install --id Python.Python.3.12 --scope user
# Then open a NEW terminal so PATH refreshes
python --version        # expect 3.12.x
```

## 2. Backend (Django + DRF)

```powershell
cd E:\app\backend
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env          # dev: DEBUG=True is enough
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py test apps --settings=config.testing   # full suite
.\.venv\Scripts\python manage.py createsuperuser    # for /admin/
.\.venv\Scripts\python manage.py runserver
```

- Default dev database: SQLite at `backend/db.sqlite3` (auto-created).
- Always run the tests with `--settings=config.testing`: it turns DRF
  throttling off, otherwise the suite rate-limits itself (HTTP 429) after a
  few hundred API calls.
- Smoke-test the API:
  `POST http://127.0.0.1:8000/api/auth/register/` with
  `{"username":"u","email":"u@x.com","password":"Str0ngPass!x","password2":"Str0ngPass!x"}`

## 3. Production-like stack (PostgreSQL + Redis + API)

```powershell
cd E:\app\deploy
docker compose up -d --build
# API on http://localhost:8000  | Postgres on localhost:5432 | Redis on 6379
```

`deploy/docker-compose.yml` starts: PostgreSQL 16, Redis 7, the Django app
(gunicorn), one Celery worker, and adminer (dev only, behind port 8081).
Configure S3-compatible storage by filling the `S3_*` variables in `.env`
(MinIO compatible; e.g. `S3_ENDPOINT=http://your-host:9000`).

## 4. Database switch to PostgreSQL

In `backend/.env` set:

```
POSTGRES_DB=fincore
POSTGRES_USER=fincore
POSTGRES_PASSWORD=<strong password>
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
REDIS_URL=redis://localhost:6379/0
```

then `migrate` again — migrations are portable across SQLite/PostgreSQL.

## 5. Flutter client (Phase 2+)

```powershell
flutter --version          # install Flutter SDK 3.22+ if missing
flutter create --org com.yourname fincore_client        # once
cd fincore_client
flutter pub add riverpod dio drift shared_preferences fl_chart jwt_decoder
flutter run -d chrome      # web target
flutter run                # android target (needs device/emulator)
```

Point the client to the API base URL via `--dart-define=API_BASE=...`
(Android emulator uses `10.0.2.2` to reach localhost).

## 6. Environment variables reference

See `backend/.env.example` — every variable is documented inline. Secrets
live only in `.env` (git-ignored); never in source code.

## 7. Operational runbook (production)

- HTTPS termination at nginx/Caddy in front of gunicorn.
- `manage.py collectstatic` then serve `/static/` from the container volume.
- Nightly: Celery beat task dumps PostgreSQL → gzipped pg_dump → encrypted
  upload to S3 (`AES256`), retention configurable (Phase 8).
- Token blacklist cleanup: `celery beat` periodic flush of expired blacklist entries.

## 8. Web UI, OTP email and baki khata exports

The session UI is served by the `web` app at `/`; `/admin/`, `/health/`
and `/api/*` are matched first. Run it with:

```powershell
cd backend
.venv\Scripts\python manage.py runserver 127.0.0.1:8000
```

Email OTP registration needs working `EMAIL_*` settings in `backend/.env`.
For local testing without a mailbox:

```powershell
$env:EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend"
.venv\Scripts\python manage.py runserver 127.0.0.1:8000
```

The verification code is printed in the runserver console.

### Baki khata export dependencies

`requirements.txt` includes `openpyxl`, `python-docx`, `fpdf2` and
`uharfbuzz`. PDF export prefers headless Edge/Chrome; if the browser is
not auto-detected, point `FINCORE_PDF_BROWSER` at its executable:

```powershell
$env:FINCORE_PDF_BROWSER="C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
```

Without a browser the fpdf2 fallback still produces the PDF using the
bundled Noto Sans Bengali+Latin font in `apps/web/fonts/` (SIL OFL 1.1;
see the README there). No system font installation is required.

