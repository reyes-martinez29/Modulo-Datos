"""
config/settings.py — Configuración del proyecto Django para el E5.

Decisiones de configuración documentadas:

1. BASE DE DATOS
   Django usa su propia SQLite (db.sqlite3) gestionada por migraciones.
   No reutilizamos la base del E3 porque el enunciado pide "base gestionada
   por el ORM de Django" con migraciones propias. La base del E3 sigue
   existiendo para comparación y el E4 la usa — son sistemas separados.

2. DUCKDB
   La ruta al Parquet del E1 viene de la variable de entorno PARQUET_PATH.
   La conexión DuckDB no se inicializa aquí — vive en un lazy singleton
   en transactions/services/duckdb.py para evitar el problema de
   AppConfig.ready() ejecutándose múltiples veces.

3. AUTENTICACIÓN
   TokenAuthentication de DRF. Requiere rest_framework.authtoken en
   INSTALLED_APPS y sus migraciones aplicadas (crea la tabla authtoken_token).

4. STATUS 422
   DRF devuelve 400 para errores de serializer por defecto. El enunciado
   y los ejercicios anteriores esperan 422 para schema inválido. Se resuelve
   con un custom exception handler que convierte 400 → 422.

5. PAGINACIÓN
   PageNumberPagination con page_size=20 — mismo comportamiento que E4.
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Carga de .env (si existe) — opcional para desarrollo local
# ---------------------------------------------------------------------------
# Si existe un archivo .env en la raíz del proyecto, carga sus variables
# antes de que settings.py las lea con os.getenv(). Esto evita tener que
# exportar variables en cada terminal.
#
# No es obligatorio: si las variables ya están exportadas en el entorno
# (como en producción o CI), el .env se ignora sin error.

def _load_dotenv(base_dir: Path) -> None:
    """Lee el .env y carga cada línea como variable de entorno."""
    env_file = base_dir / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # Ignorar comentarios y líneas vacías
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip().strip('"').strip("'")
            # Solo setear si no está ya definida en el entorno
            # (el entorno real tiene prioridad sobre el .env)
            os.environ.setdefault(key, value)

# ---------------------------------------------------------------------------
# Rutas base
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent

# Cargar .env después de definir BASE_DIR
_load_dotenv(BASE_DIR)

# ---------------------------------------------------------------------------
# Seguridad
# ---------------------------------------------------------------------------

SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-key-change-in-production-abc123",
)

DEBUG = os.getenv("DJANGO_DEBUG", "True") == "True"

ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

# ---------------------------------------------------------------------------
# Apps instaladas
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # DRF
    "rest_framework",
    "rest_framework.authtoken",   # tabla authtoken_token — necesaria para TokenAuthentication
    # App del módulo
    "transactions",
]

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# ---------------------------------------------------------------------------
# URLs y WSGI
# ---------------------------------------------------------------------------

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

# ---------------------------------------------------------------------------
# Templates (necesario para Django Admin)
# ---------------------------------------------------------------------------

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME":   BASE_DIR / os.getenv("DB_NAME", "data/transactions_django.db"),
    }
}

# ---------------------------------------------------------------------------
# Internacionalización
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "es-mx"
TIME_ZONE     = "UTC"
USE_I18N      = True
USE_TZ        = True   # Django usa timezone-aware datetimes internamente

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

STATIC_URL  = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# ---------------------------------------------------------------------------
# Default primary key
# ---------------------------------------------------------------------------

# Usamos CharField como PK en Transaction (UUID), así que este default
# solo afecta a las tablas de Django (auth, authtoken, etc.)
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    # Autenticación global: Token + Session (session para el Admin)
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    # Por defecto los endpoints son públicos — cada view declara su permiso
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    # Paginación: mismo comportamiento que E4 (page + page_size)
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    # Exception handler personalizado: convierte 400 → 422 para errores
    # de validación de serializer, alineado con el comportamiento de E4.
    "EXCEPTION_HANDLER": "transactions.exceptions.custom_exception_handler",
}

# ---------------------------------------------------------------------------
# Configuración del proyecto — variables de datos
# ---------------------------------------------------------------------------

# Ruta al Parquet del E1 — usada por el singleton DuckDB
PARQUET_PATH = os.getenv(
    "PARQUET_PATH",
    str(BASE_DIR.parent / "data" / "transactions_1m_parquet_snappy.parquet"),
)