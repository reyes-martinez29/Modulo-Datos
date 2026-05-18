# Python para Sistemas de Datos Modernos

Módulo práctico de 4 ejercicios conectados entre sí. El dataset que genera el
Ejercicio 1 es la entrada de todos los demás — no son ejercicios independientes
sino un sistema construido por partes.

---

## Estructura del repositorio

```
mi-modulo-datos/
├── data/                               ← datasets generados (en .gitignore)
│   ├── transactions_100k.csv
│   ├── transactions_500k.csv
│   ├── transactions_1m.csv             ← usado por E3 y E4
│   ├── transactions_1m_parquet.parquet
│   ├── transactions_1m_parquet_snappy.parquet   ← usado por E2, E3 y E4
│   ├── transactions_1m_parquet_gzip.parquet
│   └── transactions.db                ← generado por E3 (en .gitignore)
│
├── ejercicio-01-formatos/
├── ejercicio-02-consultas/
├── ejercicio-03-sqlite/
├── ejercicio-04-sistema/
│
├── pyproject.toml
└── README.md                           ← este archivo
```

La carpeta `data/` está en `.gitignore`. Ningún archivo de datos se sube al
repositorio. Cada ejercicio incluye instrucciones para regenerar los datos
desde cero.

---

## Dataset

El dataset es de transacciones financieras sintéticas con este schema fijo
(compartido por los 4 ejercicios):

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

Genera 1M de transacciones y compara el rendimiento de CSV, JSON Lines y
Parquet (sin compresión, Snappy, Gzip) en escritura, lectura completa, lectura
selectiva, tamaño en disco y consumo de RAM.

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

Los Parquet de 1M filas se conservan en `data/` porque los ejercicios
siguientes los necesitan. Los demás archivos temporales se borran al terminar
cada medición.

**Entregables:** `benchmark_cli.py`, `storage_benchmark/`, `results/*.json`, `report.md`

---

### E2 — El motor de consultas

Implementa 8 queries de negocio en tres engines (pandas, DuckDB, polars),
valida que los resultados son numéricamente equivalentes, mide tiempos y RAM,
e interpreta los planes de ejecución de DuckDB con EXPLAIN ANALYZE.

```bash
cd ejercicio-02-consultas

python benchmark.py --output results/
python generate_report.py
```

DuckDB lee el Parquet de E1 directamente — no se carga en pandas primero.

**Entregables:** `engines/`, `benchmark.py`, `results/benchmark_results.json`, `report.md`

---

### E3 — La capa transaccional

Ingesta el CSV de 1M filas en SQLite con transacciones explícitas por chunk,
diseña los índices para cumplir SLAs de 10-200ms por patrón de acceso, y
compara contra DuckDB patrón por patrón.

```bash
cd ejercicio-03-sqlite

# Regenerar base desde cero
python ingest.py --wal --chunk-size 20000
python ingest.py --no-wal --chunk-size 20000   # para comparar WAL vs DELETE

python benchmark_queries.py
python generate_report.py
```

El archivo `data/transactions.db` no está en el repositorio. Se regenera con
el comando de arriba en menos de 3 minutos.

**Entregables:** `schema.sql`, `schema_design.md`, `ingest.py`, `benchmark_queries.py`, `results/`, `README.md`

---

### E4 — El sistema completo

API FastAPI con 6 endpoints, arquitectura dual de backends (DuckDB para
analytics, SQLite para transaccional), caché con TTL configurable por endpoint,
validación con Pydantic, y suite de tests con pytest.

**En Proceso de contruccion**

**Entregables:** `app/`, `tests/test_api.py`, `benchmarks/latency_report.md`, `architecture_decision.md`, `README.md`

---

## Dependencias

```bash
uv add pandas pyarrow polars duckdb fastapi uvicorn pytest httpx matplotlib
```

O con pip:

```bash
pip install pandas pyarrow polars duckdb fastapi uvicorn pytest httpx matplotlib
```

Python 3.11 o superior. Se recomienda `uv` para gestión de entornos.

---

## Flujo de datos entre ejercicios

```
E1 generate_data.py
    └── data/transactions_1m.csv
    └── data/transactions_1m_parquet_snappy.parquet
              │
              ├── E2 benchmark.py          (lee Parquet con DuckDB/polars/pandas)
              │
              ├── E3 ingest.py             (ingesta CSV → SQLite)
              │       └── data/transactions.db
              │
              └── E4 app/                  (DuckDB sobre Parquet + SQLite)
```

El schema del dataset es fijo desde E1. No se modifica entre ejercicios.

---
