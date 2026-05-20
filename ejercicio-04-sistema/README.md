# Ejercicio 4 — El Sistema Completo

API FastAPI con arquitectura dual DuckDB (analytics) + SQLite (transaccional),
caché con TTL configurable, validación con Pydantic y suite de tests automatizados.

---

## Prerequisitos

Los datos deben existir en `data/` antes de arrancar el servidor.
Si no los tienes, generarlos desde los ejercicios anteriores:

```bash
# Generar el dataset (ejercicio-01-formatos/)
python generate_data.py --size 1m

# Correr el benchmark para generar el Parquet (ejercicio-01-formatos/)
python benchmark_cli.py --size 1m

# Ingestar a SQLite (ejercicio-03-sqlite/)
python ingest.py --wal --chunk-size 20000
```

Instalar dependencias:

```bash
pip install fastapi uvicorn httpx pytest duckdb numpy
```

---

## Cómo arrancar el servidor

```bash
cd ejercicio-04-sistema

# Con rutas por defecto (../../data/)
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# Con rutas explícitas
PARQUET_PATH=/ruta/al/parquet DB_PATH=/ruta/al/db uvicorn app.main:app --reload
```

La API estará disponible en:
- `http://127.0.0.1:8000` — endpoints
- `http://127.0.0.1:8000/docs` — documentación interactiva (Swagger)

---

## Variables de entorno

| Variable | Default | Descripción |
|----------|---------|-------------|
| `PARQUET_PATH` | `../../data/transactions_1m_parquet_snappy.parquet` | Parquet de E1 para DuckDB |
| `DB_PATH` | `../../data/transactions.db` | SQLite de E3 para transaccional |
| `ANALYTICS_TTL` | `300` | TTL del cache en segundos |

---

## Endpoints

| Método | Path | Backend | SLA | Cache |
|--------|------|---------|-----|-------|
| GET | `/analytics/summary` | DuckDB | <500ms cold / <20ms warm | TTL=300s |
| GET | `/analytics/top-merchants` | DuckDB | <500ms cold / <20ms warm | TTL=300s |
| GET | `/users/{user_id}/transactions` | SQLite | <80ms | No |
| GET | `/users/{user_id}/stats` | SQLite | <80ms | No |
| POST | `/transactions/batch` | SQLite | <2s para 500 registros | No |
| GET | `/health` | Memoria | <50ms siempre | No |

### Ejemplos de uso

```bash
# Resumen global
curl http://127.0.0.1:8000/analytics/summary

# Top 5 merchants en México
curl "http://127.0.0.1:8000/analytics/top-merchants?limit=5&country=MX"

# Últimas transacciones del usuario 2076
curl http://127.0.0.1:8000/users/2076/transactions

# Página 2 con 10 transacciones por página
curl "http://127.0.0.1:8000/users/2076/transactions?page=2&page_size=10"

# Stats del usuario 2076
curl http://127.0.0.1:8000/users/2076/stats

# Insertar un batch
curl -X POST http://127.0.0.1:8000/transactions/batch \
  -H "Content-Type: application/json" \
  -d '{"transactions": [{"transaction_id": "abc-123", "timestamp": "2025-06-01T12:00:00", "user_id": 1, "merchant_id": 1, "amount": 99.99, "category": "Food", "country_code": "MX", "status": "completed"}]}'

# Estado del sistema
curl http://127.0.0.1:8000/health
```

---

## Tests

```bash
cd ejercicio-04-sistema

# Todos los tests
pytest tests/ -v

# Solo tests de un endpoint
pytest tests/ -k "test_health" -v
pytest tests/ -k "test_batch" -v

# Con reporte de cobertura
pytest tests/ --tb=short
```

Los tests requieren que los datos existan en `data/`. Si no están disponibles,
pytest los skipea automáticamente con un mensaje explicativo.

La suite incluye 18 tests que cubren:
- Happy path de cada endpoint (health, summary, top-merchants, transactions, stats, batch)
- Usuario inexistente en /transactions y /stats (404)
- Batch con schema inválido (422) — amount negativo, category inválida, campo faltante
- Batch vacío y batch de más de 500 (422)
- Deduplicación de transaction_id — segunda inserción cuenta como duplicado
- Filtro por country_code en top-merchants
- Limit inválido en top-merchants (422)
- Paginación fuera de rango (lista vacía, no error)
- SLA de /health (<50ms)
- Cache warm de /analytics/summary (<20ms)
- Endpoint /dev/cache/clear funciona correctamente

---

## Benchmark de latencia

El servidor debe estar corriendo antes de correr el benchmark.

```bash
# Terminal 1: arrancar el servidor
uvicorn app.main:app --host 127.0.0.1 --port 8000

# Terminal 2: primera corrida (ID automático con timestamp)
python benchmarks/latency_benchmark.py

# Segunda corrida con ID descriptivo
python benchmarks/latency_benchmark.py --run-id post-cache-fix

# Tercera corrida con más requests para mayor precisión estadística
python benchmarks/latency_benchmark.py --run-id final --requests 200

# Generar el reporte comparativo de TODAS las corridas guardadas
python benchmarks/latency_benchmark.py --report
```

Por qué correr varias corridas:
una sola corrida puede estar afectada por el estado del sistema en ese momento
(cold start del Parquet en el page cache del SO, carga del CPU, scheduling).
Con 3-5 corridas se puede calcular el promedio y la varianza de cada percentil
y determinar si el sistema cumple el SLA de forma consistente, no solo por suerte.

Archivos generados:
- `results/latency_<run_id>.json` — datos de cada corrida individual
- `results/latency_runs_index.jsonl` — índice de todas las corridas (una línea por corrida)
- `benchmarks/latency_report.md` — reporte comparativo generado con `--report`

---

## Diagrama de arquitectura

```
                    ┌─────────────┐
                    │ HTTP Client │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │   FastAPI   │  lifespan inicializa conexiones al arrancar
                    └──────┬──────┘
                           │
          ┌────────────────┼────────────────┐
          │                │                │
   ┌──────▼──────┐  ┌──────▼──────┐  ┌─────▼──────┐
   │  /analytics │  │   /users    │  │  /health   │
   │  (DuckDB)   │  │  (SQLite)   │  │ (memoria)  │
   └──────┬──────┘  └──────┬──────┘  └────────────┘
          │                │
   ┌──────▼──────┐  ┌──────▼──────┐
   │  cache.py   │  │    db.py    │
   │  TTL 300s   │  │  conexiones │
   └──────┬──────┘  └──────┬──────┘
          │                │
   ┌──────▼──────┐  ┌──────▼──────┐
   │   Parquet   │  │   SQLite    │
   │  (E1, 1M)  │  │  (E3, 1M)  │
   └─────────────┘  └─────────────┘
```

---

## Estructura de archivos

```
ejercicio-04-sistema/
├── app/
│   ├── __init__.py
│   ├── main.py               API, lifespan y endpoints
│   ├── db.py                 Conexiones y queries
│   ├── cache.py              Cache TTL en memoria
│   └── models.py             Modelos Pydantic
├── tests/
│   ├── __init__.py
│   └── test_api.py           Suite de 14 tests
├── benchmarks/
│   ├── latency_benchmark.py  p50/p95/p99 cold vs warm
│   └── latency_report.md     Generado automáticamente
├── results/                  JSON de resultados (generado automáticamente)
├── architecture_decision.md  Justificación de cada decisión de backend
└── README.md                 Este archivo
```