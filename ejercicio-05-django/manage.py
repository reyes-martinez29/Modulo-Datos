#!/usr/bin/env python
"""
manage.py — Punto de entrada de Django para comandos de administración.

Uso:
    python manage.py runserver
    python manage.py migrate
    python manage.py createsuperuser
    python manage.py load_transactions --parquet ../../data/transactions_1m_parquet_snappy.parquet
    python manage.py drf_create_token <username>
"""

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "No se pudo importar Django. Verifica que está instalado y "
            "disponible en el entorno virtual activo."
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()