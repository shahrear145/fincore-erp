# FinCore ERP

**Personal & Business Accounting ERP** — one app for the shop ledger and your
personal pocket, built with Django and a server-rendered web UI.

A complete accounting system — from the shop's due books to personal finance, in
one place.

> Live on **PythonAnywhere** (free tier) behind login. Source is open; the
> production database is intentionally **not** in this repo.

---

## Highlights

- **Separate books for the shop** — cash, due, expenses/purchases, loans,
  investments: each with its own book, balance, totals and history.
- **Partner-wise due books** — an independent customer-by-customer book per
  partner, dated replay, overdue tracking, reminders (WhatsApp/email).
- **Super Calculator** — enter a date range and get total sell (cash + due),
  total expenses and profit/loss. The PDF breaks the result down step by step:
  cash account → partner books → expenses → summary → final result.
- **Position cockpit** — cash drawer, bank, dues, loan waterfall,
  as-of / two-date comparison, audit findings, health score.
- **PDF / Excel / Word export** — customer statements, super-calculator
  reports, position reports — all Bangla-safe via headless Chromium, with an
  fpdf2 fallback.
- **Audit-first design** — double-entry accounting engine, soft delete +
  reversal (financial rows are never hard-deleted), audit log on every write,
  Decimal money, partner-role RBAC enforced server-side.
- **PWA + offline sync** — Flutter client (separate repo) with client-generated
  idempotency keys; the server dedupes on `client_reference`.

## Tech stack

| Layer | Choice |
|---|---|
| Backend | Django 5, Django REST Framework, Python |
| Frontend | Server-rendered templates + vanilla JS, CSS custom properties (Aurora/Light/Dark themes), Font Awesome, Google Fonts |
| Money | `DecimalField` everywhere — never floats |
| Database | SQLite (PythonAnywhere free) · Postgres-ready (Docker path) |
| Auth | Session (web) + JWT (API), email OTP, Google OAuth, login history |
| Exports | openpyxl, python-docx, fpdf2, headless Chromium (Bangla-safe) |
| Tests | 375 tests — `python manage.py test apps --settings=config.testing` |
| Deploy | PythonAnywhere (see `deploy/pythonanywhere/`) · Docker Compose |

## Repository layout

```
backend/
├── config/            settings, urls, wsgi, asgi, celery, testing
├── manage.py
└── apps/
    ├── core/          base models, soft-delete, audit log, permissions
    ├── users/         auth, JWT, OTP, devices, login history, PIN
    ├── organizations/ businesses, memberships, financial periods
    ├── partners|customers|transactions   partner-wise dues & ledger
    ├── accounting/    double-entry postings, reports
    ├── cashbook|personal|loans|investments|ops
    └── web/           the session web UI + export engines
docs/                  architecture, ERD, setup, PythonAnywhere deploy
deploy/                Dockerfile, Compose, PythonAnywhere bundle
design-previews/       the two UI concepts that were evaluated
```

## Quick start (development)

```bash
cd backend
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # dev defaults work as-is
python manage.py migrate
python manage.py test apps --settings=config.testing   # full suite
python manage.py runserver
```

Open http://127.0.0.1:8000/ — Bangla UI by default; toggle English in the header.

## Deploying your own instance

- **PythonAnywhere free tier** — step-by-step: `docs/04-pythonanywhere-deploy.md`
  and `deploy/pythonanywhere/` (setup.sh creates the venv, runs migrate +
  collectstatic).
- **Docker** — `deploy/docker-compose.yml` (Postgres + Redis + API).

## Design previews

Two UI concepts were evaluated before the current one shipped:
`design-previews/index.html` (Dark Neon · Light Clean). The production UI uses
the same token system with an Aurora/Light/Dark theme switch.

## What is deliberately NOT in this repo

- Production database (`db.sqlite3`), backups, uploaded media — real business data stays private.
- `.env` with secrets (Google OAuth, email passwords, SECRET_KEY).
- The Flutter client app (separate repository).
