# FinCore ERP

**Personal & Business Accounting ERP** — one app for the shop ledger and your
personal pocket, built with Django and a server-rendered web UI.

হিসাব রাখার সম্পূর্ণ একটা সিস্টেম — দোকানের বাকি খাতা থেকে শুরু করে ব্যক্তিগত হিসাব পর্যন্ত।

> Live on **PythonAnywhere** (free tier) behind login. Source is open; the
> production database is intentionally **not** in this repo.

---

## Highlights

- **দোকান হিসাব, আলাদা বই** — নগদ, বাকি, খরচ/ক্রয়, ঋণ, বিনিয়োগ: প্রতিটার নিজস্ব বই,
  ব্যালেন্স, টোটাল ও হিস্ট্রি।
- **পার্টনারভিত্তিক বাকি খাতা** — প্রতিটি পার্টনারের জন্য গ্রাহকভিত্তিক আলাদা খাতা,
  তারিখ অনুযায়ী রিপ্লে, ওভারটাইম আদায়, রিমাইন্ডার (WhatsApp/ইমেইল)।
- **সুপার ক্যালকুলেটর** — তারিখ রেঞ্জ দিয়ে মোট সেল (নগদ + বাকি), মোট খরচ,
  লাভ/লোকসান। PDF-এ ধাপে ধাপে রিপোর্ট: নগদের হিসাব → পার্টনার খাতা → খরচ →
  সংক্ষিপ্ত বিবরণ → চূড়ান্ত ফল।
- **পজিশন ককপিট** — ক্যাশ ড্রয়ার, ব্যাংক, বাকি, ঋণের জলপ্রবাহ (waterfall),
  as-of/দুই তারিখ তুলনা, অডিট ফাইন্ডিং, স্বাস্থ্য স্কোর।
- **PDF / Excel / Word export** — গ্রাহক স্টেটমেন্ট (বাকি চালান), সুপার ক্যালকুলেটর,
  পজিশন রিপোর্ট — সবগুলোই বাংলা-শাপশট PDF (headless Chromium) বা fpdf2 fallback।
- **অডিট-ফার্স্ট ডিজাইন** — double-entry accounting engine, soft-delete + reversal
  (never hard-delete financial rows), audit log on every write, Decimal money,
  partner-role RBAC enforced on the backend.
- **PWA + offline sync** — Flutter client (separate repo) with client-generated
  idempotency keys; server dedupes on `client_reference`.

## Tech stack

| Layer | Choice |
|---|---|
| Backend | Django 5, Django REST Framework, Python |
| Frontend | Server-rendered templates + vanilla JS, CSS custom properties (themes: Aurora/Light/Dark), Font Awesome, Google Fonts |
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

Open http://127.0.0.1:8000/ — Bangla UI by default; toggle EN in the header.

## Deploying your own instance

- **PythonAnywhere free tier** — step-by-step: `docs/04-pythonanywhere-deploy.md`
  and `deploy/pythonanywhere/` (setup.sh creates venv, runs migrate + collectstatic).
- **Docker** — `deploy/docker-compose.yml` (Postgres + Redis + API).

## Design previews

Two UI concepts were evaluated before the current one shipped:
`design-previews/index.html` (Dark Neon · Light Clean). The production UI uses
the same token system with an Aurora/Light/Dark theme switch.

## What is deliberately NOT in this repo

- Production database (`db.sqlite3`), backups, uploaded media — real business data stays private.
- `.env` with secrets (Google OAuth, email passwords, SECRET_KEY).
- The Flutter client app (separate repository).
