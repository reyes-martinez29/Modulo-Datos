# Ejercicio 3 — La Capa Transaccional

## Comando único para regenerar todo desde cero

```bash
cd ejercicio-03-sqlite && \
python ingest.py --wal --chunk-size 20000 && \
python benchmark_queries.py
```

Esto crea la base de datos, ingesta 1M filas y corre el benchmark completo.
El resultado queda en `results/`.

---

## Descripción

Este ejercicio construye una capa transaccional sobre el mismo dataset de
1M transacciones del Ejercicio 1, optimizada para consultas por usuario
individual con SLAs de respuesta de 10-200ms.

A diferencia del Ejercicio 2 (DuckDB/polars para analytics sobre el archivo
completo), aquí el problema es diferente: el equipo de producto necesita
responder preguntas como "¿cuáles son las últimas 20 transacciones del usuario
X?" en menos de 50ms. DuckDB no puede hacer eso de forma consistente porque
tiene que escanear el Parquet completo. SQLite con los índices correctos lo
hace en 2-10ms usando B-Trees que navegan directamente al subconjunto de datos.

---

## Requisitos

```bash
pip install pandas duckdb
```

Python 3.11 o superior. El CSV de entrada debe existir en `data/` (generado
por el Ejercicio 1):

```
mi-modulo-datos/
├── data/
│   ├── transactions_1m.csv                          ← entrada
│   └── transactions_1m_parquet_snappy.parquet       ← para comparación DuckDB
└── ejercicio-03-sqlite/
    ├── schema.sql
    ├── ingest.py
    └── benchmark_queries.py
```

---

## Scripts disponibles

### `ingest.py` — Crea y llena la base de datos

```bash
# Con WAL mode (recomendado, más rápido)
python ingest.py --wal

# Sin WAL mode (para comparar)
python ingest.py --no-wal

# Cambiar chunk size
python ingest.py --wal --chunk-size 50000

# Ruta personalizada
python ingest.py --wal --db /ruta/custom/transactions.db
```

La ingesta de 1M filas con `--wal` tarda menos de 3 minutos.
Los resultados se guardan en `results/ingest_results.json`.

### `benchmark_queries.py` — Mide los 5 patrones de acceso

```bash
python benchmark_queries.py

# Con rutas personalizadas
python benchmark_queries.py \
  --db ../../data/transactions.db \
  --parquet ../../data/transactions_1m_parquet_snappy.parquet
```

Mide cada patrón con y sin índices, captura `EXPLAIN QUERY PLAN`, y compara
contra DuckDB. Los resultados se guardan en `results/benchmark_results.json`.

---

## Los 5 patrones de acceso

| Patrón | Descripción | SLA | Índice que lo sirve |
|--------|-------------|-----|---------------------|
| P1 | Lookup por `transaction_id` exacto | <10ms | `PRIMARY KEY` |
| P2 | Últimas 20 transacciones de un usuario | <50ms | `idx_user_timestamp` |
| P3 | Transacciones de un usuario en rango de fechas | <50ms | `idx_user_timestamp` |
| P4 | Suma de `amount` de un usuario en el último mes | <50ms | `idx_user_timestamp` |
| P5 | Usuarios de un país con más de N transacciones | <200ms | `idx_country_user` |

---

## Estructura de archivos

```
ejercicio-03-sqlite/
├── schema.sql              DDL con comentarios técnicos
├── schema_design.md        Justificación de cada decisión de diseño
├── ingest.py               Ingesta chunked con WAL mode
├── benchmark_queries.py    Benchmark de los 5 patrones
├── results/
│   ├── ingest_results.json     Tiempos de ingesta WAL vs no-WAL
│   └── benchmark_results.json  Métricas de cada patrón
└── README.md               Este archivo
```

El archivo `data/transactions.db` NO se incluye en el repositorio.
Regenerarlo con el comando de arriba tarda menos de 3 minutos.

---

## Notas sobre el diseño de índices

El schema tiene exactamente 3 índices:

1. **`PRIMARY KEY (transaction_id)`** — creado automáticamente por SQLite.
   Sirve P1 (lookup exacto en <10ms).

2. **`idx_user_timestamp (user_id, timestamp DESC)`** — índice compuesto.
   Sirve P2 (ORDER BY sin sort adicional gracias al DESC), P3 y P4
   (range scan por usuario + fecha).

3. **`idx_country_user (country_code, user_id)`** — índice compuesto.
   Sirve P5: dentro del índice, las filas de cada país ya están agrupadas
   por usuario, así que SQLite puede calcular los COUNTs sin hash table.

Ver `schema_design.md` para la justificación técnica completa de cada decisión.