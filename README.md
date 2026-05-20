# Python para Sistemas de Datos Modernos

Módulo práctico de 4 ejercicios conectados entre sí. El dataset que genera el
Ejercicio 1 es la entrada de todos los demás — no son ejercicios independientes
sino un sistema construido por partes.

---

## Estructura del repositorio

```
mi-modulo-datos/
├── data/                                         ← datasets generados (en .gitignore)
│   ├── transactions_100k.csv
│   ├── transactions_500k.csv
│   ├── transactions_1m.csv                       ← usado por E3 y E4
│   ├── transactions_1m_parquet.parquet
│   ├── transactions_1m_parquet_snappy.parquet    ← usado por E2, E3 y E4
│   ├── transactions_1m_parquet_gzip.parquet
│   └── transactions.db                           ← generado por E3 (en .gitignore)
│
├── ejercicio-01-formatos/
├── ejercicio-02-consultas/
├── ejercicio-03-sqlite/
├── ejercicio-04-sistema/
│
├── pyproject.toml
└── README.md
```

La carpeta `data/` está en `.gitignore`. Ningún archivo de datos se sube al
repositorio. Cada ejercicio incluye instrucciones para regenerar los datos
desde cero.

---

## Dataset

Schema fijo compartido por los 4 ejercicios:

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `transaction_id` | string (UUID4) | Único por fila |
| `timestamp` | datetime | Último año, distribución uniforme |
| `user_id` | entero | Entre 1 y 50,000 |
| `merchant_id` | entero | Entre 1 y 10,000 |
| `amount` | float | Entre 0.01 y 5,000.00 |
| `category` | string | 10 valores: Food, Travel, Electronics... |
| `country_code` | string | 15 países: MX, CO, BR, AR, CL... |
| `status` | string | completed (85%), failed (10%), pending (5%) |

---

## Ejercicios

### E1 — Formatos bajo la lupa

Genera 1M de transacciones y compara CSV, JSON Lines y Parquet (sin compresión,
Snappy, Gzip) en escritura, lectura completa, lectura selectiva, tamaño en disco
y consumo de RAM. El resultado principal: Parquet+Snappy es entre 3x y 300x más
eficiente que CSV dependiendo de la operación.

```bash
cd ejercicio-01-formatos

python generate_data.py --size 100k --validate
python generate_data.py --size 500k --validate
python generate_data.py --size 1m   --validate

python benchmark_cli.py --size 100k
python benchmark_cli.py --size 500k
python benchmark_cli.py --size 1m

python generate_charts.py
python generate_report.py
```

Los Parquet de 1M filas se conservan en `data/` porque los ejercicios siguientes
los necesitan. Los demás archivos temporales se borran al terminar cada medición.

**Entregables:** `benchmark_cli.py`, `storage_benchmark/`, `results/*.json`, `report.md`

---

### E2 — El motor de consultas

Implementa 8 queries de negocio en tres engines (pandas, DuckDB, polars), valida
equivalencia numérica entre los tres, mide tiempos y RAM, e interpreta los planes
de ejecución con EXPLAIN ANALYZE. El resultado principal: polars gana en 6 de 8
queries, DuckDB domina en operaciones con timestamps.

```bash
cd ejercicio-02-consultas

python benchmark.py --output results/
python generate_report.py
```

DuckDB lee el Parquet de E1 directamente — no se carga en pandas primero.

**Entregables:** `engines/`, `benchmark.py`, `results/benchmark_results.json`, `report.md`

---

### E3 — La capa transaccional

Ingesta 1M filas en SQLite con transacciones explícitas por chunk, diseña tres
índices para cumplir SLAs de 10-200ms por patrón de acceso, y compara contra
DuckDB patrón por patrón. El resultado principal: SQLite con índices gana en los
5 patrones transaccionales, con speedups de hasta 1355x entre con y sin índice.

```bash
cd ejercicio-03-sqlite

python ingest.py --wal --chunk-size 20000
python ingest.py --no-wal --chunk-size 20000   # para comparar WAL vs DELETE

python benchmark_queries.py
python generate_report.py
```

El archivo `data/transactions.db` no está en el repositorio. Se regenera en
menos de 3 minutos con el comando de arriba.

**Entregables:** `schema.sql`, `schema_design.md`, `ingest.py`, `benchmark_queries.py`, `results/`, `README.md`

---

### E4 — El sistema completo

API FastAPI con 6 endpoints, arquitectura dual DuckDB (analytics) + SQLite
(transaccional), cache con TTL configurable, validación Pydantic, 18 tests con
pytest y benchmark de latencia con p50/p95/p99 cold vs warm. El resultado
principal: cold p99 máximo de 55ms (<500ms SLA), warm p99 máximo de 1.39ms
(<20ms SLA), 60x de speedup del cache medido en 3 corridas.

```bash
cd ejercicio-04-sistema

# Variables de entorno (rutas por defecto apuntan a ../../data/)
export PARQUET_PATH=../../data/transactions_1m_parquet_snappy.parquet
export DB_PATH=../../data/transactions.db

# Arrancar el servidor
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# En otra terminal: tests
pytest tests/ -v

# Benchmark de latencia (con el servidor corriendo)
python benchmarks/latency_benchmark.py --run-id baseline
python benchmarks/latency_benchmark.py --report
```

**Entregables:** `app/`, `tests/test_api.py`, `benchmarks/latency_report.md`, `architecture_decision.md`, `README.md`

---

## Dependencias

```bash
# Con uv (recomendado)
uv add pandas pyarrow polars duckdb fastapi uvicorn pytest httpx matplotlib numpy

# Con pip
pip install pandas pyarrow polars duckdb fastapi uvicorn pytest httpx matplotlib numpy
```

Python 3.11 o superior.

---

## Flujo de datos entre ejercicios

```
E1 generate_data.py
    ├── data/transactions_1m.csv                       → E3 ingest.py → data/transactions.db
    └── data/transactions_1m_parquet_snappy.parquet    → E2 benchmark.py
                                                        → E3 benchmark_queries.py (comparación)
                                                        → E4 app/ (DuckDB analytics)

data/transactions.db                                   → E4 app/ (SQLite transaccional)
```

El schema del dataset es fijo desde E1 y no se modifica entre ejercicios.

---
