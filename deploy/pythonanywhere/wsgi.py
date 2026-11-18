"""WSGI configuration for FinCore ERP on PythonAnywhere.

How to use it
-------------
1. Open the PythonAnywhere "Web" tab.
2. Click the link "WSGI configuration file" of your web app.
3. Delete everything in that file and paste the contents of this file.
4. Replace the placeholder in PROJECT_DIR below with your PythonAnywhere
   username (the only occurrence in this file).
5. Save, then click the green "Reload" button.

The virtualenv is selected in the Web tab itself ("Virtualenv" field), so
this file only has to put the project on sys.path.
"""
import os
import sys

PROJECT_DIR = "/home/__USERNAME__/fincore/backend"
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
