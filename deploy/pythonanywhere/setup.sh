#!/usr/bin/env bash
# FinCore ERP — one-shot setup for PythonAnywhere (free "Beginner" plan).
#
# Run this in a PythonAnywhere Bash console after uploading the project to
# /home/<username>/fincore:
#
#   bash ~/fincore/deploy/pythonanywhere/setup.sh
#
# It is safe to re-run: it only creates things that are missing.

set -euo pipefail

PA_USER="${1:-$USER}"
PROJECT_ROOT="/home/${PA_USER}/fincore"
BACKEND="${PROJECT_ROOT}/backend"
VENV_NAME="fincore"
VENV_DIR="/home/${PA_USER}/.virtualenvs/${VENV_NAME}"
PYTHON_BIN="/usr/bin/python3.13"

# Fall back to the newest Python 3.1x that actually exists on this host.
if [ ! -x "${PYTHON_BIN}" ]; then
  PYTHON_BIN="$(ls -1 /usr/bin/python3.1[0-9] 2>/dev/null | sort -V | tail -1)"
fi
if [ -z "${PYTHON_BIN}" ] || [ ! -x "${PYTHON_BIN}" ]; then
  echo "!! No python3.1x found in /usr/bin — contact PythonAnywhere support."
  exit 1
fi
PY_VERSION="$(basename "${PYTHON_BIN}" | sed 's/^python//')"

echo "==> FinCore ERP setup for ${PA_USER}"
echo "    backend: ${BACKEND}"
echo "    python : ${PYTHON_BIN}"

if [ ! -d "${BACKEND}" ]; then
  echo "!! ${BACKEND} not found."
  echo "   Upload the project zip first, then run:"
  echo "   unzip fincore-pythonanywhere.zip -d ~/fincore"
  exit 1
fi

# --- 1. virtualenv ---------------------------------------------------------
if [ ! -d "${VENV_DIR}" ]; then
  echo "==> creating virtualenv ${VENV_NAME}"
  if command -v mkvirtualenv >/dev/null 2>&1; then
    mkvirtualenv --python="${PYTHON_BIN}" "${VENV_NAME}"
  else
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
  fi
fi
# shellcheck disable=SC1090
source "${VENV_DIR}/bin/activate"

# --- 2. dependencies -------------------------------------------------------
echo "==> installing dependencies (this takes a few minutes)"
python -m pip install --upgrade pip >/dev/null
python -m pip install -r \
  "${PROJECT_ROOT}/deploy/pythonanywhere/requirements-pythonanywhere.txt"

# --- 3. production .env ----------------------------------------------------
ENV_FILE="${BACKEND}/.env"
if [ ! -f "${ENV_FILE}" ]; then
  echo "==> creating ${ENV_FILE} from the production template"
  sed "s/YOURNAME/${PA_USER}/g" \
    "${PROJECT_ROOT}/deploy/pythonanywhere/env.production.example" > "${ENV_FILE}"
fi

# generate a real SECRET_KEY if the placeholder is still there
if grep -q "REPLACE-WITH-A-LONG-RANDOM-STRING" "${ENV_FILE}"; then
  KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(64))')"
  python - "${ENV_FILE}" "${KEY}" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
path.write_text(
    path.read_text().replace("REPLACE-WITH-A-LONG-RANDOM-STRING", key))
print("==> SECRET_KEY generated")
PY
fi

# --- 4. database + static files -------------------------------------------
cd "${BACKEND}"
mkdir -p "${BACKEND}/media"
echo "==> migrating the database"
python manage.py migrate --noinput

echo "==> collecting static files"
python manage.py collectstatic --noinput

echo "==> sanity check"
python manage.py check || true

cat <<EOF

============================================================
Setup finished. Finish these steps in the PythonAnywhere UI:

1) Web tab -> "Add a new web app"
     - Manual configuration -> Python ${PY_VERSION}
     - Virtualenv:         ${VENV_DIR}
     - Source code:        ${BACKEND}
     - Working directory:  ${BACKEND}

2) Web tab -> "WSGI configuration file"
     Replace everything with deploy/pythonanywhere/wsgi.py and set the
     PROJECT_DIR placeholder to ${PA_USER}. Save.

3) Web tab -> "Static files" (add two mappings)
     URL  /static/   ->   ${BACKEND}/staticfiles
     URL  /media/    ->   ${BACKEND}/media

4) Click the green "Reload" button, then open:
     https://${PA_USER}.pythonanywhere.com
============================================================
EOF
