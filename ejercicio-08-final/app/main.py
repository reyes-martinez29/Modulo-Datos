"""
app/main.py — API FastAPI del sistema de monitoreo de transacciones.

Reúne las piezas del módulo en un solo servicio:
    - Analytics (DuckDB sobre la vista unificada Parquet + SQLite)
    - Consultas por usuario con filtro de fecha (SQLite + idx_user_timestamp)
    - Detección de anomalías (módulo app.anomaly sobre SQLite)
    - Ingesta de CSV externo (pipeline E6 adaptado, invocado desde un endpoint)
    - Health enriquecido con métricas de rendimiento

Reglas de arquitectura que se aplican aquí:

1. Ningún endpoint abre una conexión a DuckDB. La conexión vive en el
   lifespan (regla heredada del E4: abrir DuckDB sobre Parquet cuesta ~88ms,
   así que se paga una sola vez al arrancar).

2. La tensión "tiempo real" vs cache se resuelve con invalidación dirigida:
   tanto POST /transactions/batch como POST /pipeline/ingest invalidan el
   prefijo "analytics:" del cache tras escribir. Así los datos recién
   ingeridos se reflejan de inmediato en /analytics/*, y el TTL de 300s
   queda solo como red de seguridad. El cache hace que analytics cumpla su
   SLA; la invalidación hace que no mienta sobre datos frescos.

3. La escritura concurrente a SQLite se serializa con un asyncio.Lock
   (heredado del E4). Tanto el batch como la ingesta de CSV escriben, así
   que ambos toman el lock para evitar que dos cargas simultáneas se pisen.

Variables de entorno (validadas por app.config al arrancar):
    PARQUET_PATH, DB_PATH, ANALYTICS_TTL, MAX_CSV_ROWS, DEFAULT_ANOMALY_THRESHOLD
"""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File

from app.anomaly import detect_failed_transaction_anomalies
from app.cache import cache
from app.config import Config
from app.db import (
    close_connections,
    get_uptime,
    init_connections,
    is_duckdb_connected,
    is_sqlite_connected,
    query_analytics_summary,
    query_top_merchants,
    query_user_exists,
    query_user_stats,
    query_user_transactions,
)
from app.models import (
    AnomalyResponse,
    BatchRequest,
    BatchResponse,
    HealthResponse,
    IngestReport,
    SummaryResponse,
    TopMerchantsResponse,
    UserStatsResponse,
    UserTransactionsResponse,
)
from pipeline.csv_source import CSVStructureError
from pipeline.pipeline import run_pipeline_csv

# ---------------------------------------------------------------------------
# Configuración — validada al importar (falla limpio si falta algo)
# ---------------------------------------------------------------------------

config = Config()

# Lock para serializar escrituras a SQLite (batch + ingesta CSV).
_write_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Lifespan — inicializa y cierra conexiones
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_connections(config.parquet_path, config.db_path)
    yield
    close_connections()


app = FastAPI(
    title="Sistema de Monitoreo de Transacciones",
    description=(
        "Monitoreo de transacciones financieras con analytics unificado "
        "(histórico en Parquet + reciente en SQLite vía DuckDB), detección "
        "de anomalías e ingesta de CSV externo."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_transactions() -> int:
    """Cuenta filas en la tabla transaccional. Usado por /health."""
    conn = sqlite3.connect(config.db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Endpoint de desarrollo — limpiar cache
# ---------------------------------------------------------------------------

@app.post("/dev/cache/clear", summary="(Dev) Limpiar el cache analítico")
async def dev_clear_cache():
    cache.invalidate_prefix("analytics:")
    return {"status": "ok", "cleared_prefix": "analytics:"}


# ---------------------------------------------------------------------------
# Analytics — DuckDB sobre la vista unificada Parquet + SQLite
# ---------------------------------------------------------------------------

@app.get("/analytics/summary", response_model=SummaryResponse,
         summary="Totales globales (histórico + reciente)")
async def get_analytics_summary():
    """
    Totales sobre la vista unificada. Cacheado con TTL; se invalida cuando
    entra una escritura (batch o ingesta CSV), así refleja datos frescos.
    """
    cache_key = cache.make_key("analytics", "summary")
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    result = query_analytics_summary()
    cache.set(cache_key, result, ttl=config.analytics_ttl)
    return result


@app.get("/analytics/top-merchants", response_model=TopMerchantsResponse,
         summary="Top merchants por volumen")
async def get_top_merchants(limit: int = 10, country: Optional[str] = None):
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit debe estar entre 1 y 100")

    if country:
        country = country.upper()

    cache_key = cache.make_key("analytics", "top-merchants", f"limit={limit}", f"country={country}")
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    merchants = query_top_merchants(limit=limit, country=country)
    result = {"merchants": merchants, "limit": limit, "country": country}
    cache.set(cache_key, result, ttl=config.analytics_ttl)
    return result


# ---------------------------------------------------------------------------
# Detección de anomalías — nuevo en el E8
# ---------------------------------------------------------------------------

@app.get("/analytics/anomalies", response_model=AnomalyResponse,
         summary="Usuarios con más de N transacciones fallidas en 30 días")
async def get_anomalies(threshold: Optional[int] = None, window_days: int = 30):
    """
    Detecta usuarios con más de `threshold` transacciones fallidas en los
    últimos `window_days` días. Si no se pasa threshold, usa el default
    configurable (DEFAULT_ANOMALY_THRESHOLD).

    Backend: SQLite con idx_user_timestamp — filtra por status y ventana de
    fecha, agrupa por usuario. No usa la vista unificada porque el histórico
    del Parquet queda fuera de la ventana de 30 días.
    """
    if threshold is None:
        threshold = config.default_anomaly_threshold
    if threshold < 0:
        raise HTTPException(status_code=422, detail="threshold debe ser >= 0")
    if window_days < 1 or window_days > 365:
        raise HTTPException(status_code=422, detail="window_days debe estar entre 1 y 365")

    users = detect_failed_transaction_anomalies(
        db_path=config.db_path, threshold=threshold, window_days=window_days
    )

    return {
        "threshold": threshold,
        "window_days": window_days,
        "anomalous_users": users,
        "total_flagged": len(users),
    }


# ---------------------------------------------------------------------------
# Usuarios — SQLite, con filtro de fecha nuevo en el E8
# ---------------------------------------------------------------------------

@app.get("/users/{user_id}/transactions", response_model=UserTransactionsResponse,
         summary="Transacciones de un usuario con paginación y filtro de fecha")
async def get_user_transactions(
    user_id: int,
    page: int = 1,
    page_size: int = 20,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    """
    Transacciones de un usuario, más recientes primero. El E8 agrega los
    filtros opcionales date_from y date_to (formato 'YYYY-MM-DD HH:MM:SS' o
    'YYYY-MM-DD'). El índice idx_user_timestamp cubre el filtro de usuario,
    el orden y el rango de fechas.
    """
    if page < 1:
        raise HTTPException(status_code=422, detail="page debe ser >= 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(status_code=422, detail="page_size debe estar entre 1 y 100")

    if not query_user_exists(user_id):
        raise HTTPException(status_code=404, detail=f"Usuario {user_id} no encontrado")

    transactions = query_user_transactions(
        user_id, page=page, page_size=page_size,
        date_from=date_from, date_to=date_to,
    )

    return {
        "user_id": user_id,
        "page": page,
        "page_size": page_size,
        "date_from": date_from,
        "date_to": date_to,
        "transactions": transactions,
    }


@app.get("/users/{user_id}/stats", response_model=UserStatsResponse,
         summary="Estadísticas de un usuario")
async def get_user_stats(user_id: int):
    stats = query_user_stats(user_id)
    if stats is None:
        raise HTTPException(status_code=404, detail=f"Usuario {user_id} no encontrado")
    return stats


# ---------------------------------------------------------------------------
# Ingesta transaccional — batch (del E4) e ingesta CSV (nuevo en el E8)
# ---------------------------------------------------------------------------

@app.post("/transactions/batch", response_model=BatchResponse,
          summary="Insertar un batch de transacciones")
async def post_transactions_batch(request: BatchRequest):
    """
    Inserta hasta 500 transacciones validadas con Pydantic. Serializa la
    escritura con el lock e invalida el cache analítico tras insertar.
    """
    rows = [
        {
            "transaction_id": t.transaction_id,
            "timestamp": t.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "user_id": t.user_id,
            "merchant_id": t.merchant_id,
            "amount": t.amount,
            "category": t.category,
            "country_code": t.country_code,
            "status": t.status,
        }
        for t in request.transactions
    ]

    async with _write_lock:
        # Deduplicación + inserción en una transacción. Se hace inline aquí
        # (no vía pipeline) porque estos datos ya vienen validados por Pydantic.
        conn = sqlite3.connect(config.db_path)
        try:
            ids = [r["transaction_id"] for r in rows]
            placeholders = ",".join("?" * len(ids))
            existing = {
                row[0] for row in conn.execute(
                    f"SELECT transaction_id FROM transactions WHERE transaction_id IN ({placeholders})",
                    ids,
                ).fetchall()
            }
            new_rows = [r for r in rows if r["transaction_id"] not in existing]
            with conn:
                conn.executemany(
                    """INSERT OR IGNORE INTO transactions
                       (transaction_id, timestamp, user_id, merchant_id,
                        amount, category, country_code, status)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    [
                        (r["transaction_id"], r["timestamp"], r["user_id"], r["merchant_id"],
                         r["amount"], r["category"], r["country_code"], r["status"])
                        for r in new_rows
                    ],
                )
            inserted = len(new_rows)
            duplicates = len(existing)
        finally:
            conn.close()

    if inserted > 0:
        cache.invalidate_prefix("analytics:")

    return {"received": len(rows), "inserted": inserted, "duplicates_skipped": duplicates}


@app.post("/pipeline/ingest", response_model=IngestReport,
          summary="Ingestar un CSV externo a través del pipeline ETL")
async def post_pipeline_ingest(file: UploadFile = File(...)):
    """
    Recibe un CSV vía multipart/form-data, lo procesa con el pipeline ETL
    (read → extract → transform → load) y devuelve el reporte con las
    invariantes verificadas. Las filas inválidas van a cuarentena con su
    motivo de rechazo; solo las válidas se insertan.

    Serializa la escritura con el mismo lock que el batch, e invalida el
    cache analítico si se insertó al menos una fila.
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=422, detail="El archivo debe ser un .csv")

    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="El CSV debe estar codificado en UTF-8")

    async with _write_lock:
        try:
            report = run_pipeline_csv(
                csv_text=text,
                db_path=config.db_path,
                quarantine_dir=config.quarantine_dir,
                max_rows=config.max_csv_rows,
            )
        except CSVStructureError as e:
            # Error de estructura del archivo → 422 con el motivo exacto
            raise HTTPException(status_code=422, detail=str(e))

    if report["inserted"] > 0:
        cache.invalidate_prefix("analytics:")

    return report


# ---------------------------------------------------------------------------
# Health — enriquecido con métricas de rendimiento
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse, summary="Estado del sistema")
async def get_health():
    """
    Estado y métricas. Reporta uptime, hit rate del cache, estado de
    conexiones y número de transacciones en la base — esta última es la
    métrica de monitoreo nueva del E8: su crecimiento vs lo esperado es una
    señal de salud del pipeline de ingesta.

    No consulta DuckDB ni hace agregaciones pesadas. El COUNT sobre la tabla
    es O(1) en SQLite porque mantiene el conteo de filas internamente.
    """
    return {
        "status": "ok",
        "uptime_seconds": round(get_uptime(), 2),
        "cache_hit_rate": round(cache.hit_rate, 4),
        "cache_hits": cache.hits,
        "cache_misses": cache.misses,
        "duckdb_connected": is_duckdb_connected(),
        "sqlite_connected": is_sqlite_connected(),
        "transactions_in_db": _count_transactions(),
    }