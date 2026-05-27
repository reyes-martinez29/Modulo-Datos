"""
transactions/apps.py — Configuración de la app transactions.

Por qué NO inicializamos DuckDB aquí:
    AppConfig.ready() puede ejecutarse más de una vez en Django — con
    runserver (que usa autoreload y puede lanzar dos procesos), con
    management commands, y en ciertos contextos de testing.

    La conexión DuckDB vive en transactions/services/duckdb.py como
    un lazy singleton. Se inicializa la primera vez que una view llama
    get_duckdb_connection() — exactamente cuando se necesita, una sola vez
    por proceso, sin depender del ciclo de vida de AppConfig.
"""

from django.apps import AppConfig


class TransactionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name               = "transactions"
    verbose_name       = "Transacciones Financieras"