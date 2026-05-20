"""
app/main.py — API FastAPI del sistema de transacciones.

Estructura de este archivo:
    1. Configuración y lifespan (inicializa/cierra conexiones)
    2. Endpoints de analytics  → DuckDB + cache
    3. Endpoints de usuarios   → SQLite
    4. Endpoint de ingesta     → SQLite + Pydantic
    5. Endpoint de salud       → solo memoria

Regla de arquitectura que se aplica aquí:
    Ningún endpoint abre una conexión a base de datos.
    Todas las conexiones viven en app.state y se inicializan en el lifespan.
    Si se viola esta regla, cada request paga ~88ms de overhead de apertura
    de Parquet, lo que hace imposible cumplir los SLAs de /analytics/*.

Variables de entorno esperadas:
    PARQUET_PATH  — ruta al Parquet de E1 (default: ../../data/transactions_1m_parquet_snappy.parquet)
    DB_PATH       — ruta a la DB de E3  (default: ../../data/transactions.db)
    ANALYTICS_TTL — TTL del cache en segundos (default: 300)
"""

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException

from app.cache import cache
from app.db import (
    close_connections,
    get_uptime,
    init_connections,
    insert_batch,
    is_duckdb_connected,
    is_sqlite_connected,
    query_analytics_summary,
    query_top_merchants,
    query_user_exists,
    query_user_stats,
    query_user_transactions,
)
from app.models import (
    BatchRequest,
    BatchResponse,
    HealthResponse,
    SummaryResponse,
    TopMerchantsResponse,
    UserStatsResponse,
    UserTransactionsResponse,
)


# ---------------------------------------------------------------------------
# Configuración desde variables de entorno
# ---------------------------------------------------------------------------

PARQUET_PATH  = os.getenv("PARQUET_PATH",  "../../data/transactions_1m_parquet_snappy.parquet")
DB_PATH       = os.getenv("DB_PATH",       "../../data/transactions.db")
ANALYTICS_TTL = int(os.getenv("ANALYTICS_TTL", "300"))


# ---------------------------------------------------------------------------
# Lifespan — inicializa y cierra conexiones al arrancar y apagar el servidor
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Contexto de vida del servidor.

    Todo lo que está antes del yield ocurre al arrancar.
    Todo lo que está después ocurre al apagar.

    Las conexiones a DuckDB y SQLite se abren aquí una sola vez.
    Los endpoints las usan a través de las funciones de db.py — nunca
    abren sus propias conexiones.
    """
    init_connections(PARQUET_PATH, DB_PATH)
    print(f"Servidor listo — DuckDB: {PARQUET_PATH} | SQLite: {DB_PATH}")
    yield
    close_connections()
    print("Conexiones cerradas.")


# ---------------------------------------------------------------------------
# Aplicación
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Sistema de Transacciones Financieras",
    description="API con arquitectura dual DuckDB (analytics) + SQLite (transaccional).",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Endpoint de desarrollo — limpiar cache
# ---------------------------------------------------------------------------

@app.post(
    "/dev/cache/clear",
    summary="(Dev) Limpiar el cache analítico",
)
async def dev_clear_cache():
    """
    Invalida el cache en memoria. Usado por el benchmark de latencia para
    simular condiciones cold sin reiniciar el servidor entre requests.

    No toca la base de datos — solo elimina entradas del cache.
    """
    cache.invalidate_prefix("analytics:")
    return {"status": "ok", "cleared_prefix": "analytics:"}


# ---------------------------------------------------------------------------
# Endpoints de analytics — DuckDB + cache
# ---------------------------------------------------------------------------

@app.get(
    "/analytics/summary",
    response_model=SummaryResponse,
    summary="Totales globales del dataset",
)
async def get_analytics_summary():
    """
    Retorna conteo total, monto total, promedio global y breakdown
    por país y categoría.

    Backend: DuckDB sobre Parquet — agrega 1M filas en columnas.
    Cache: TTL de ANALYTICS_TTL segundos (default 300s).
           Cold: <500ms | Warm: <20ms
    """
    cache_key = cache.make_key("analytics", "summary")
    cached    = cache.get(cache_key)

    if cached is not None:
        return cached

    result = query_analytics_summary()
    cache.set(cache_key, result, ttl=ANALYTICS_TTL)
    return result


@app.get(
    "/analytics/top-merchants",
    response_model=TopMerchantsResponse,
    summary="Top merchants por volumen de transacciones",
)
async def get_top_merchants(
    limit:   int           = 10,
    country: Optional[str] = None,
):
    """
    Top N merchants ordenados por monto total. Acepta filtro opcional por país.

    Parámetros:
        limit   — cuántos merchants retornar (default 10, max 100)
        country — filtrar por country_code (ej. MX, CO, BR)

    Backend: DuckDB — GROUP BY merchant_id sobre 1M filas con column pruning.
    Cache: la key incluye limit y country para evitar colisiones entre queries
           con distintos parámetros.
           Cold: <500ms | Warm: <20ms
    """
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit debe estar entre 1 y 100")

    if country:
        country = country.upper()

    cache_key = cache.make_key(
        "analytics", "top-merchants", f"limit={limit}", f"country={country}"
    )
    cached = cache.get(cache_key)

    if cached is not None:
        return cached

    merchants = query_top_merchants(limit=limit, country=country)
    result    = {"merchants": merchants, "limit": limit, "country": country}
    cache.set(cache_key, result, ttl=ANALYTICS_TTL)
    return result


# ---------------------------------------------------------------------------
# Endpoints de usuarios — SQLite con índice B-Tree
# ---------------------------------------------------------------------------

@app.get(
    "/users/{user_id}/transactions",
    response_model=UserTransactionsResponse,
    summary="Últimas transacciones de un usuario con paginación",
)
async def get_user_transactions(
    user_id:   int,
    page:      int = 1,
    page_size: int = 20,
):
    """
    Retorna las transacciones de un usuario ordenadas de más reciente a más antigua.
    Soporta paginación con page y page_size.

    Retorna 404 si el usuario no existe en la base de datos.
    Retorna lista vacía si la página está fuera del rango del usuario.

    Backend: SQLite con idx_user_timestamp — <1ms por el índice B-Tree (medido en E3).
             SLA: <80ms
    """
    if page < 1:
        raise HTTPException(status_code=422, detail="page debe ser mayor o igual a 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(status_code=422, detail="page_size debe estar entre 1 y 100")

    if not query_user_exists(user_id):
        raise HTTPException(status_code=404, detail=f"Usuario {user_id} no encontrado")

    transactions = query_user_transactions(user_id, page=page, page_size=page_size)

    return {
        "user_id":      user_id,
        "page":         page,
        "page_size":    page_size,
        "transactions": transactions,
    }


@app.get(
    "/users/{user_id}/stats",
    response_model=UserStatsResponse,
    summary="Estadísticas de un usuario",
)
async def get_user_stats(user_id: int):
    """
    Retorna monto total, conteo, categoría más frecuente y país más frecuente.

    Retorna 404 si el usuario no existe.

    Backend: SQLite con idx_user_timestamp — GROUP BY filtrado por user_id.
             SLA: <80ms
    """
    stats = query_user_stats(user_id)

    if stats is None:
        raise HTTPException(status_code=404, detail=f"Usuario {user_id} no encontrado")

    return stats


# ---------------------------------------------------------------------------
# Endpoint de ingesta — SQLite + Pydantic
# ---------------------------------------------------------------------------

@app.post(
    "/transactions/batch",
    response_model=BatchResponse,
    summary="Insertar un batch de transacciones",
)
async def post_transactions_batch(request: BatchRequest):
    """
    Recibe hasta 500 transacciones, valida el schema con Pydantic,
    deduplica por transaction_id e inserta las nuevas en SQLite.

    Si cualquier campo es inválido FastAPI retorna 422 automáticamente
    antes de que este endpoint ejecute nada.

    Después de un insert exitoso invalida el cache de analytics para que
    los próximos requests a /analytics/* reflejen los datos actualizados.

    Backend: SQLite — base transaccional de escritura del sistema.
             SLA: <2s para 500 registros.
    """
    transactions = [
        {
            "transaction_id": t.transaction_id,
            "timestamp":      t.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "user_id":        t.user_id,
            "merchant_id":    t.merchant_id,
            "amount":         t.amount,
            "category":       t.category,
            "country_code":   t.country_code,
            "status":         t.status,
        }
        for t in request.transactions
    ]

    result = await insert_batch(transactions)

    if result["inserted"] > 0:
        cache.invalidate_prefix("analytics:")

    return result


# ---------------------------------------------------------------------------
# Endpoint de salud — solo memoria, nunca toca la DB
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Estado del sistema",
)
async def get_health():
    """
    Retorna uptime, estado de conexiones y hit rate del cache desde el arranque.

    NUNCA consulta la base de datos. Solo lee estado en memoria.
    Por eso siempre responde en <50ms independientemente de la carga.

    SLA: <50ms siempre.
    """
    return {
        "status":           "ok",
        "uptime_seconds":   round(get_uptime(), 2),
        "cache_hit_rate":   round(cache.hit_rate, 4),
        "cache_hits":       cache.hits,
        "cache_misses":     cache.misses,
        "duckdb_connected": is_duckdb_connected(),
        "sqlite_connected": is_sqlite_connected(),
    }