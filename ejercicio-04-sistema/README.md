# Ejercicio 4 — Sistema Completo (FastAPI + DuckDB + SQLite)

API de transacciones con arquitectura dual:

- **DuckDB sobre Parquet** para endpoints analíticos (`/analytics/*`)
- **SQLite** para endpoints transaccionales por usuario y batch insert
- **Cache en memoria con TTL** para cumplir SLAs *warm* en analytics

## Requisitos previos (E1 y E3)

Este ejercicio depende de artefactos generados en ejercicios anteriores:

- Parquet de 1M transacciones (Ejercicio 1)
- Base SQLite ya ingestada e indexada (Ejercicio 3)

Por defecto, la app busca:

- `../../data/transactions_1m_parquet_snappy.parquet`
- `../../data/transactions.db`

Si esos archivos no existen, el servidor y los tests que dependen de datos fallarán/serán skipeados.

## Variables de entorno

- `PARQUET_PATH` — ruta al Parquet (default: `../../data/transactions_1m_parquet_snappy.parquet`)
- `DB_PATH` — ruta a la SQLite DB (default: `../../data/transactions.db`)
- `ANALYTICS_TTL` — TTL del cache en segundos (default: `300`)

### Ejemplo (PowerShell)

Las rutas pueden ser **relativas al directorio actual** (cwd). Elige una de estas dos formas para evitar errores.

#### Opción A (recomendada): ejecutar `uvicorn` dentro de `ejercicio-04-sistema/`

```powershell
$env:PARQUET_PATH = "../data/transactions_1m_parquet_snappy.parquet"
$env:DB_PATH      = "../data/transactions.db"
$env:ANALYTICS_TTL = "300"
```

#### Opción B: ejecutar `uvicorn` desde la raíz del repo

```powershell
$env:PARQUET_PATH = "./data/transactions_1m_parquet_snappy.parquet"
$env:DB_PATH      = "./data/transactions.db"
$env:ANALYTICS_TTL = "300"
```

## Cómo arrancar el servidor

### Opción A: arrancar desde `ejercicio-04-sistema/`

```powershell
cd ejercicio-04-sistema
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### Opción B: arrancar desde la raíz del repo

```powershell
uvicorn app.main:app --app-dir ejercicio-04-sistema --host 127.0.0.1 --port 8000
```

Docs interactivos:

- http://127.0.0.1:8000/docs

## Endpoints (spec)

- `GET /analytics/summary`
- `GET /analytics/top-merchants?limit=N&country=XX`
- `GET /users/{user_id}/transactions?page=N&page_size=M`
- `GET /users/{user_id}/stats`
- `POST /transactions/batch`
- `GET /health`

### Endpoint de desarrollo (solo benchmark)

- `POST /dev/cache/clear` — invalida el cache analítico en memoria (`analytics:`)

Nota: en un despliegue real este endpoint debe omitirse o protegerse.

## Cómo correr los tests

Desde la raíz del repo:

```powershell
python -m pytest ejercicio-04-sistema/tests -q
```

Si falta Parquet/DB, los tests que dependen de datos se skipean con un mensaje que indica qué generar.

## Benchmark de latencia (p50/p95/p99)

1) Levanta el servidor (en otro terminal):

```powershell
cd ejercicio-04-sistema
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

2) Corre el benchmark desde la raíz del repo:

```powershell
python ejercicio-04-sistema/benchmarks/latency_benchmark.py
```

### Registrar 3 corridas (recomendado)

Para dejar evidencia de estabilidad, corre el benchmark 3 veces usando `--run-id`.
Esto guarda outputs distintos sin sobrescribir archivos.

```powershell
python ejercicio-04-sistema/benchmarks/latency_benchmark.py --run-id run1
python ejercicio-04-sistema/benchmarks/latency_benchmark.py --run-id run2
python ejercicio-04-sistema/benchmarks/latency_benchmark.py --run-id run3
```

Salida:

- JSON: `ejercicio-04-sistema/results/latency_results_<run-id>.json`
- Markdown: `ejercicio-04-sistema/benchmarks/latency_report_<run-id>.md`
- Índice (append-only): `ejercicio-04-sistema/results/latency_runs_index.jsonl`

El benchmark compara:

- **Cold**: limpia cache antes de cada request con `POST /dev/cache/clear`
- **Warm**: requests consecutivos con cache activo

## Diagrama de arquitectura (ASCII)

```text
                ┌───────────────────────────┐
Client ─HTTP───▶│        FastAPI API         │
                │    (ejercicio-04-sistema)  │
                └─────────────┬─────────────┘
                              │
                              │  /health
                              │  (solo memoria)
                              │
               ┌──────────────┴──────────────┐
               │                             │
     /analytics/* (OLAP)              /users/*, /transactions/batch (OLTP)
               │                             │
        ┌──────▼───────┐                   ┌─▼────────────────┐
        │  Cache TTL     │                   │   SQLite DB       │
        │  (memoria)     │                   │  transactions.db  │
        └──────┬────────┘                   └────────┬─────────┘
               │                                       │
        ┌──────▼────────┐                              │
        │     DuckDB      │                              │
        │  (in-memory)    │                              │
        └──────┬────────┘                              │
               │                                       │
        ┌──────▼──────────────────────────┐            │
        │ Parquet (E1): transactions_1m... │            │
        └─────────────────────────────────┘            │
                                                        │
                (La ingesta escribe a SQLite y luego invalida cache)
```

## Decisión de arquitectura

Ver el documento de justificación endpoint → backend en:

- `ejercicio-04-sistema/architecture_decision.md`
