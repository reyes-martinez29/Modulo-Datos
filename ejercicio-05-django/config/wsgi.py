"""
config/wsgi.py — Punto de entrada WSGI para servidores de producción.

Para desarrollo se usa:
    python manage.py runserver

Para producción se usaría un servidor WSGI como gunicorn:
    gunicorn config.wsgi:application
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()