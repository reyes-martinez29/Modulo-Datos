# Ejercicio 8 — Sistema de monitoreo de transacciones

Sistema completo de monitoreo para una fintech de LATAM. Integra analytics
sobre 1M+ transacciones, consultas por usuario con filtro de fecha, detección
de anomalías, e ingesta de CSV externo, todo empaquetado en Docker y levantable
con un solo comando.

## Arquitectura en una frase

SQLite es la fuente de verdad transaccional viva (histórico + todo lo nuevo);
DuckDB la consulta para analytics (columnar) y SQLite directo con índice
resuelve los lookups por usuario (sub-milisegundo). El Parquet del E1 es el
snapshot histórico que alimenta el setup inicial. Ver `decisions.md` para el
razonamiento completo.

## Los 9 endpoints

| Método | Path | Descripción | SLA |
|--------|------|-------------|-----|
| GET | `/health` | Estado + métricas (uptime, cache, filas en DB) | <50ms |
| GET | `/analytics/summary` | Totales globales por país y categoría | <500ms cold / <20ms warm |
| GET | `/analytics/top-merchants` | Top merchants por volumen, filtro por país | <500ms cold / <20ms warm |
| GET | `/analytics/anomalies` | Usuarios con más de N fallidas en 30 días | <100ms |
| GET | `/users/{id}/transactions` | Transacciones del usuario, paginadas, con filtro de fecha | <80ms |
| GET | `/users/{id}/stats` | Estadísticas del usuario | <80ms |
| POST | `/transactions/batch` | Insertar hasta 500 transacciones | <2s |
| POST | `/pipeline/ingest` | Ingestar un CSV externo vía el pipeline ETL | depende del tamaño |

## Prerequisitos

- Docker y Docker Compose v2 (`docker compose version` debe funcionar).
- El Parquet del E1 (`transactions_1m_parquet_snappy.parquet`).

## Paso 0 — Preparar `.env`, `.dockerignore` y los datos

```powershell
# Desde ejercicio-08-final/
Copy-Item .env.example .env

# .dockerignore debe vivir en la RAIZ del repo (docker-compose usa context: ..)
Copy-Item .dockerignore ..\.dockerignore -Force

New-Item -ItemType Directory -Force -Path .\data
Copy-Item ..\data\transactions_1m_parquet_snappy.parquet .\data\
```

```bash
# bash equivalente
cp .env.example .env
cp .dockerignore ../.dockerignore
mkdir -p ./data
cp ../data/transactions_1m_parquet_snappy.parquet ./data/
```

## Levantar el sistema desde cero

```bash
docker compose up --build
```

Esto construye la imagen, corre `setup` (copia el histórico del Parquet a
SQLite con los índices del E3, idempotente), y arranca `api` en el puerto 8000.

Si el build falla con un conflicto de tag entre `setup` y `api` construyéndose
en paralelo, construye primero y luego levanta:

```bash
docker compose build api
docker compose up
```

## Verificar que funciona

```bash
docker compose ps
curl http://localhost:8000/health
```

En PowerShell, usa `curl.exe` o `Invoke-WebRequest -UseBasicParsing` para
evitar la advertencia de parsing:

```powershell
curl.exe http://localhost:8000/health
curl.exe http://localhost:8000/analytics/summary
curl.exe "http://localhost:8000/analytics/anomalies?threshold=5"
curl.exe "http://localhost:8000/users/1/transactions?date_from=2025-01-01"
curl.exe http://localhost:8000/users/1/stats
```

## Probar el endpoint de ingesta de CSV

Prepara un CSV con las 8 columnas del schema (transaction_id en formato UUID4):

```
transaction_id,timestamp,user_id,merchant_id,amount,category,country_code,status
550e8400-e29b-41d4-a716-446655440000,2025-06-01 12:00:00,1,1,99.99,Food,MX,completed
```

Súbelo:

```bash
curl -X POST http://localhost:8000/pipeline/ingest \
  -F "file=@mi_archivo.csv"
```

Respuesta (reporte del pipeline con invariantes verificadas):

```json
{
  "rows_in_csv": 100, "extracted": 100, "parse_errors": 0,
  "valid": 95, "rejected": 5,
  "by_error": {"amount_out_of_range": 3, "invalid_category": 2},
  "inserted": 95, "duplicates": 0, "total_time_s": 0.12,
  "invariants": {"csv_eq_extracted_plus_parse_errors": true,
                 "extracted_eq_valid_plus_rejected": true,
                 "inserted_plus_duplicates_eq_valid": true}
}
```

Las filas rechazadas van a `./data/quarantine/YYYY-MM-DD.jsonl` con el motivo
exacto de cada rechazo, para auditarlas.

## Tests

```bash
# Dentro del entorno con las dependencias instaladas
pytest tests/ -v
```

La suite tiene 26 tests que cubren los 9 endpoints, la detección de anomalías
con distintos umbrales, el filtro de fecha, el pipeline CSV (válidas vs
rechazadas, invariantes, idempotencia, validación de estructura) y los códigos
de error. Los tests usan un dataset determinista con anomalías conocidas y no
requieren Docker.

## Ver logs y parar

```bash
docker compose logs -f api
docker compose down -v
```

## Variables de entorno

| Variable | Default | Descripción |
|----------|---------|-------------|
| `PARQUET_PATH` | `/data/transactions_1m_parquet_snappy.parquet` | Parquet histórico (requerida) |
| `DB_PATH` | `/data/transactions.db` | SQLite, fuente viva (requerida) |
| `ANALYTICS_TTL` | `300` | TTL del cache de analytics en segundos |
| `MAX_CSV_ROWS` | `100000` | Tope de filas para el endpoint de ingesta |
| `DEFAULT_ANOMALY_THRESHOLD` | `5` | Umbral por defecto del detector de anomalías |
| `QUARANTINE_DIR` | `/data/quarantine` | Directorio de filas rechazadas |

## Estructura

```
ejercicio-08-final/
├── app/
│   ├── main.py        FastAPI — 9 endpoints, lifespan, invalidación de cache
│   ├── db.py          DuckDB sobre SQLite (analytics) + SQLite con índice (usuarios)
│   ├── anomaly.py     detector de anomalías como módulo extensible
│   ├── cache.py       TTLCache del E4
│   ├── models.py      Pydantic: E4 + AnomalyResponse + IngestReport
│   └── config.py      validación de variables de entorno al arrancar
├── pipeline/
│   ├── csv_source.py  lee CSV externo, valida estructura (nuevo en E8)
│   ├── extract.py     normalización (del E6, intacto)
│   ├── transform.py   validación de negocio + cuarentena (del E6, intacto)
│   ├── load.py        INSERT OR IGNORE idempotente (del E6)
│   └── pipeline.py    orquestador CSV con invariantes
├── tests/
│   ├── conftest.py    dataset determinista con anomalías conocidas
│   └── test_api.py    26 tests
├── scripts/
│   ├── entrypoint.sh  valida env vars y arranca uvicorn
│   └── setup.py       copia Parquet → SQLite (Modelo A), idempotente
├── Dockerfile         multi-stage, preinstala sqlite_scanner de DuckDB
├── docker-compose.yml setup + api
├── requirements.txt
├── log_config.yaml    logs JSON para uvicorn
├── .env.example
├── .dockerignore
├── decisions.md
└── README.md
```