# Ejercicio 5 — El Backend con Estructura

API REST con Django y Django REST Framework que reimplementa los 6 endpoints
del E4 usando el ORM de Django, autenticación por token y el panel de
administración de Django.

---

## Prerequisitos

Los datos deben existir antes de cargar transacciones:

```bash
# Ejercicio 1 — generar el Parquet de 1M filas (si no lo tienes)
cd ejercicio-01-formatos
python generate_data.py --size 1m
python benchmark_cli.py --size 1m
```

Instalar dependencias con uv (desde la raíz del módulo):

```bash
uv add django djangorestframework pytest-django
uv sync
```

---

## Configuración del entorno

Copia el archivo de ejemplo y ajusta las rutas si es necesario:

```bash
cp .env.example .env
```

El `.env` por defecto funciona sin cambios si tu estructura de carpetas
es la estándar del módulo. Las variables disponibles:

```env
PARQUET_PATH=../../data/transactions_1m_parquet_snappy.parquet
DB_NAME=data/transactions_django.db
DJANGO_DEBUG=True
DJANGO_SECRET_KEY=django-insecure-dev-key-cambiar-en-produccion
```

El `settings.py` carga el `.env` automáticamente — no hace falta exportar
nada en la terminal.

---

## Levantar el sistema desde cero

```bash
cd ejercicio-05-django

# 1. Generar y aplicar migraciones (crea la tabla transactions y los índices)
python manage.py makemigrations transactions
python manage.py migrate

# 2. Crear superusuario para el Admin
python manage.py createsuperuser

# 3. Cargar las transacciones desde el Parquet del E1
python manage.py load_transactions

# 4. Obtener un token para la API
python manage.py drf_create_token <username>

# 5. Levantar el servidor
python manage.py runserver
```

La API estará disponible en:
- `http://127.0.0.1:8000/api/v1/` — endpoints
- `http://127.0.0.1:8000/admin/` — panel de administración

---

## Obtener un token de autenticación

```bash
# Opción A — management command (necesita createsuperuser primero)
python manage.py drf_create_token <username>

# Opción B — via API con cualquier usuario registrado
# PowerShell:
Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/auth/token/" `
  -Method POST `
  -ContentType "application/json" `
  -Body '{"username": "tu_usuario", "password": "tu_contraseña"}' `
  -UseBasicParsing | Select-Object -ExpandProperty Content
# Respuesta: {"token": "abc123..."}
```

El token no va en el `.env` — es una credencial de usuario que vive en la
base de datos. Si recreas la base, vuelve a generarlo con `drf_create_token`.

---

## Endpoints

| Método | Path | Auth | Backend | Descripción |
|--------|------|:----:|---------|-------------|
| GET | `/api/v1/health` | No | Memoria | Estado del servidor |
| GET | `/api/v1/analytics/summary` | No | DuckDB | Totales globales |
| GET | `/api/v1/analytics/top-merchants` | No | DuckDB | Top merchants por volumen |
| GET | `/api/v1/users/{id}/transactions` | Token | ORM | Transacciones de un usuario |
| GET | `/api/v1/users/{id}/stats` | Token | ORM | Estadísticas de un usuario |
| POST | `/api/v1/transactions/batch` | Token | ORM | Insertar hasta 500 transacciones |

### Ejemplos de uso en PowerShell

```powershell
$TOKEN = "el_token_de_drf_create_token"
$H = @{"Authorization" = "Token $TOKEN"}

# Endpoints públicos
(Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/health" -UseBasicParsing).Content
(Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/analytics/summary" -UseBasicParsing).Content
(Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/analytics/top-merchants?limit=5&country=MX" -UseBasicParsing).Content

# Endpoints con token
(Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/users/2076/transactions" -Headers $H -UseBasicParsing).Content
(Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/users/2076/transactions?page=2&page_size=10" -Headers $H -UseBasicParsing).Content
(Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/users/2076/stats" -Headers $H -UseBasicParsing).Content

# Batch insert
$body = '{"transactions": [{"transaction_id": "550e8400-e29b-41d4-a716-446655440000", "timestamp": "2025-06-01T12:00:00", "user_id": 1, "merchant_id": 1, "amount": 99.99, "category": "Food", "country_code": "MX", "status": "completed"}]}'
(Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/transactions/batch" -Method POST -Headers $H -ContentType "application/json" -Body $body -UseBasicParsing).Content
```

---

## Tests

```bash
# Con el test runner de Django (recomendado)
python manage.py test tests -v 2

# Con pytest
pytest tests/ -v
```

La suite incluye **22 tests** que cubren:

- Health endpoint público y sin latencia de DB
- Analytics summary y top-merchants públicos, con validación de `limit`
- 401 sin token en `/users/*` y `/transactions/batch`
- 404 para usuario sin transacciones
- 422 para batch inválido — amount negativo, category inválida, country_code
  inválido, campo faltante, lista vacía y más de 500 items
- Deduplicación por `transaction_id` — segunda inserción cuenta como duplicado
- Obtención de token con credenciales correctas (200) e incorrectas (422)

Los tests de analytics se skipean automáticamente si el Parquet no está
disponible, con un mensaje claro de cómo generarlo.

---

## Decisión de arquitectura: DuckDB + ORM

Los endpoints `/analytics/*` usan DuckDB directamente sobre el Parquet del E1.
La razón es la misma del E4: DuckDB hace column pruning y vectorización,
resolviendo agregaciones sobre 1M filas en ~40ms. El ORM de Django sobre
SQLite haría un full scan sin ventaja columnar.

Los endpoints de usuario y el batch usan el ORM porque los datos son
transaccionales — escritura y lectura por usuario individual donde los
índices `idx_user_timestamp` e `idx_country_user` (réplica exacta del E3)
reducen el trabajo a las filas del usuario en microsegundos.

La conexión DuckDB es un lazy singleton en `transactions/services/duckdb.py`
con `threading.Lock` — no en `AppConfig.ready()` para evitar inicializaciones
múltiples con el autoreload de Django.

---

## Estructura de archivos

```
ejercicio-05-django/
├── config/
│   ├── __init__.py
│   ├── settings.py              carga .env, DATABASES, DRF, PARQUET_PATH
│   ├── urls.py                  rutas raíz + /api/v1/auth/token/
│   └── wsgi.py
├── transactions/
│   ├── __init__.py
│   ├── apps.py                  AppConfig — DuckDB es lazy, no en ready()
│   ├── models.py                Transaction con Meta.indexes del E3
│   ├── serializers.py           validación entrada/salida
│   ├── views.py                 6 views: DuckDB para analytics, ORM para el resto
│   ├── urls.py                  6 paths explícitos
│   ├── admin.py                 list_display, list_filter, search_fields
│   ├── exceptions.py            custom_exception_handler: 400 → 422
│   ├── migrations/
│   │   ├── __init__.py
│   │   └── 0001_initial.py      CREATE TABLE + 2 índices del E3
│   ├── services/
│   │   ├── __init__.py
│   │   └── duckdb.py            singleton lazy con threading.Lock
│   └── management/commands/
│       ├── __init__.py
│       └── load_transactions.py bulk_create desde Parquet, ignore_conflicts
├── tests/
│   ├── __init__.py
│   └── test_api.py              22 tests
├── data/
│   └── .gitkeep                 la DB se genera con migrate + load_transactions
├── manage.py
├── .env                         NO va en git
├── .env.example                 sí va en git
├── .gitignore
└── README.md
```