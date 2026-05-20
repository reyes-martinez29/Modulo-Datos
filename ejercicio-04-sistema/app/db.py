"""
app/db.py — Conexiones a bases de datos y todas las queries del sistema.

Decisión de arquitectura fundamental:
    Las conexiones a DuckDB y SQLite se abren UNA SOLA VEZ al arrancar el
    servidor (en el lifespan de FastAPI definido en main.py) y se cierran
    al apagar. Nunca se abren dentro de los endpoints.

    Por qué esto importa: abrir una conexión a DuckDB sobre Parquet cuesta
    ~88ms (medido en E3). Si cada request abre su propia conexión, ese costo
    se paga en cada request y hace imposible cumplir los SLAs. Con conexión
    global, el costo se paga una vez al arrancar.

Separación de responsabilidades:
    Este archivo tiene dos secciones bien diferenciadas:
    1. Inicialización de conexiones (init_connections / close_connections)
    2. Funciones de query — una por operación de datos

    Las funciones de query son puras en el sentido de que reciben todos los
    parámetros que necesitan y retornan datos. No tienen efectos secundarios
    ni acceden al cache — eso es responsabilidad de los endpoints en main.py.

Backend por operación:
    DuckDB  → analytics_summary, analytics_top_merchants
              Justificación: agregaciones sobre 1M filas en columnas.
              DuckDB con Parquet hace column pruning y vectorización —
              es el engine correcto para este tipo de query (demostrado en E2).

    SQLite  → user_transactions, user_stats, batch_insert
              Justificación: lookups por user_id con índice B-Tree.
              SQLite con idx_user_timestamp responde en <1ms (demostrado en E3).
              Para escritura: SQLite es la base transaccional del sistema.

SQLite y concurrencia:
    FastAPI es asíncrono pero las operaciones de SQLite son síncronas.
    Se usa check_same_thread=False con WAL mode para permitir lecturas
    concurrentes. Las escrituras (batch insert) usan un lock asyncio para
    evitar conflictos entre requests simultáneos que intenten escribir.
"""

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Optional

import duckdb


# ---------------------------------------------------------------------------
# Estado global de conexiones
# ---------------------------------------------------------------------------
# Estas variables se asignan en init_connections() al arrancar el servidor.
# Los endpoints las usan directamente — no crean conexiones propias.

_duckdb_conn: Optional[duckdb.DuckDBPyConnection] = None
_sqlite_conn: Optional[sqlite3.Connection]         = None
_sqlite_write_lock: Optional[asyncio.Lock]         = None

# Tiempo de arranque — usado por /health para calcular uptime
_start_time: float = 0.0


# ---------------------------------------------------------------------------
# Inicialización y cierre (llamados desde el lifespan en main.py)
# ---------------------------------------------------------------------------

def init_connections(parquet_path: str, db_path: str) -> None:
    """
    Abre y configura las conexiones a DuckDB y SQLite.

    Se llama UNA SOLA VEZ desde el lifespan de FastAPI al arrancar el servidor.
    Después de esta función, _duckdb_conn y _sqlite_conn están listos para usar.

    Parámetros
    ----------
    parquet_path : ruta al archivo Parquet de E1 (para DuckDB)
    db_path      : ruta a la base SQLite de E3 (para operaciones transaccionales)
    """
    global _duckdb_conn, _sqlite_conn, _sqlite_write_lock, _start_time

    if not Path(parquet_path).exists():
        raise FileNotFoundError(
            f"Parquet no encontrado: {parquet_path}\n"
            "Corre primero el benchmark del Ejercicio 1 para generarlo."
        )
    if not Path(db_path).exists():
        raise FileNotFoundError(
            f"Base de datos SQLite no encontrada: {db_path}\n"
            "Corre primero ingest.py del Ejercicio 3."
        )

    # DuckDB: conexión de solo lectura sobre el Parquet de E1
    # read_only=False para que DuckDB pueda crear su propio caché interno,
    # pero en la práctica solo hacemos SELECT sobre el Parquet.
    _duckdb_conn = duckdb.connect(database=":memory:")
    # Registrar el Parquet como vista — todas las queries usan 'transactions'
    _duckdb_conn.execute(
        f"CREATE VIEW transactions AS SELECT * FROM read_parquet('{parquet_path}')"
    )

    # SQLite: check_same_thread=False porque FastAPI puede llamar desde
    # diferentes hilos del thread pool. WAL mode ya está activo en la DB
    # (configurado por ingest.py en E3).
    _sqlite_conn = sqlite3.connect(db_path, check_same_thread=False)
    _sqlite_conn.row_factory = sqlite3.Row  # permite acceso por nombre de columna
    _sqlite_conn.execute("PRAGMA journal_mode=WAL")
    _sqlite_conn.execute("PRAGMA cache_size=-65536")   # 64MB de caché
    _sqlite_conn.execute("PRAGMA temp_store=MEMORY")

    # Lock para serializar escrituras concurrentes a SQLite
    _sqlite_write_lock = asyncio.Lock()

    _start_time = time.monotonic()


def close_connections() -> None:
    """
    Cierra las conexiones. Llamado desde el lifespan al apagar el servidor.
    """
    global _duckdb_conn, _sqlite_conn
    if _duckdb_conn:
        _duckdb_conn.close()
        _duckdb_conn = None
    if _sqlite_conn:
        _sqlite_conn.close()
        _sqlite_conn = None


def get_uptime() -> float:
    """Segundos transcurridos desde que arrancó el servidor."""
    return time.monotonic() - _start_time


def is_duckdb_connected() -> bool:
    return _duckdb_conn is not None


def is_sqlite_connected() -> bool:
    return _sqlite_conn is not None


# ---------------------------------------------------------------------------
# Queries — DuckDB (analytics sobre Parquet)
# ---------------------------------------------------------------------------

def query_analytics_summary() -> dict:
    """
    Totales globales del dataset: conteo, monto total, promedio,
    y breakdown por país y por categoría.

    Por qué DuckDB: necesita agregar sobre todo el dataset (1M filas).
    DuckDB con Parquet hace column pruning — solo lee las columnas necesarias
    del archivo, no las 8 columnas completas. En E2 este tipo de query
    tardó ~130ms en DuckDB vs ~2s en pandas.

    Retorna un dict con la estructura de SummaryResponse.
    """
    assert _duckdb_conn, "Conexión DuckDB no inicializada"

    # Query principal: totales globales
    totals = _duckdb_conn.execute("""
        SELECT
            COUNT(*)        AS total_transactions,
            SUM(amount)     AS total_amount,
            AVG(amount)     AS avg_amount
        FROM transactions
    """).fetchone()

    # Breakdown por país
    by_country = _duckdb_conn.execute("""
        SELECT
            country_code,
            COUNT(*)    AS total_transactions,
            SUM(amount) AS total_amount
        FROM transactions
        GROUP BY country_code
        ORDER BY total_transactions DESC
    """).fetchall()

    # Breakdown por categoría
    by_category = _duckdb_conn.execute("""
        SELECT
            category,
            COUNT(*)    AS total_transactions,
            AVG(amount) AS avg_amount
        FROM transactions
        GROUP BY category
        ORDER BY total_transactions DESC
    """).fetchall()

    return {
        "total_transactions": totals[0],
        "total_amount":       round(totals[1], 2),
        "avg_amount":         round(totals[2], 4),
        "by_country": [
            {
                "country_code":       r[0],
                "total_transactions": r[1],
                "total_amount":       round(r[2], 2),
            }
            for r in by_country
        ],
        "by_category": [
            {
                "category":           r[0],
                "total_transactions": r[1],
                "avg_amount":         round(r[2], 4),
            }
            for r in by_category
        ],
    }


def query_top_merchants(
    limit: int = 10,
    country: Optional[str] = None,
) -> list[dict]:
    """
    Top N merchants por volumen total de amount.
    Opcionalmente filtrado por country_code.

    Por qué DuckDB: aggregation sobre 1M filas con GROUP BY merchant_id
    (hasta 10,000 merchants). DuckDB puede hacer esto con column pruning
    (solo lee merchant_id, amount y opcionalmente country_code del Parquet).

    Parámetros
    ----------
    limit   : cuántos merchants retornar (default 10)
    country : si se proporciona, filtra por ese country_code
    """
    assert _duckdb_conn, "Conexión DuckDB no inicializada"

    if country:
        sql = """
            SELECT
                merchant_id,
                SUM(amount)  AS total_amount,
                COUNT(*)     AS transaction_count
            FROM transactions
            WHERE country_code = ?
            GROUP BY merchant_id
            ORDER BY total_amount DESC
            LIMIT ?
        """
        rows = _duckdb_conn.execute(sql, [country.upper(), limit]).fetchall()
    else:
        sql = """
            SELECT
                merchant_id,
                SUM(amount)  AS total_amount,
                COUNT(*)     AS transaction_count
            FROM transactions
            GROUP BY merchant_id
            ORDER BY total_amount DESC
            LIMIT ?
        """
        rows = _duckdb_conn.execute(sql, [limit]).fetchall()

    return [
        {
            "merchant_id":       r[0],
            "total_amount":      round(r[1], 2),
            "transaction_count": r[2],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Queries — SQLite (transaccional por usuario)
# ---------------------------------------------------------------------------

def query_user_transactions(
    user_id: int,
    page: int = 1,
    page_size: int = 20,
) -> list[dict]:
    """
    Últimas transacciones de un usuario con paginación.

    Por qué SQLite: lookup por user_id con el índice idx_user_timestamp.
    En E3 este patrón tardó 0.15ms con índice vs 108ms sin índice.
    DuckDB tarda ~98ms por el overhead de bootstrap sobre Parquet.

    La paginación se implementa con OFFSET en SQLite. Para page=1 retorna
    las transacciones más recientes; para page=2 las siguientes, etc.
    ORDER BY timestamp DESC garantiza que page=1 son siempre las más recientes.

    Retorna lista vacía si el usuario no tiene transacciones en esa página.
    El endpoint diferencia entre usuario inexistente y página sin datos.
    """
    assert _sqlite_conn, "Conexión SQLite no inicializada"

    offset = (page - 1) * page_size

    rows = _sqlite_conn.execute("""
        SELECT
            transaction_id,
            timestamp,
            amount,
            category,
            status,
            merchant_id
        FROM transactions
        WHERE user_id = ?
        ORDER BY timestamp DESC
        LIMIT ? OFFSET ?
    """, (user_id, page_size, offset)).fetchall()

    return [
        {
            "transaction_id": r["transaction_id"],
            "timestamp":      r["timestamp"],
            "amount":         r["amount"],
            "category":       r["category"],
            "status":         r["status"],
            "merchant_id":    r["merchant_id"],
        }
        for r in rows
    ]


def query_user_exists(user_id: int) -> bool:
    """
    Verifica si un usuario tiene al menos una transacción en la base.
    Usado por los endpoints de usuario para retornar 404 correctamente.
    """
    assert _sqlite_conn, "Conexión SQLite no inicializada"

    row = _sqlite_conn.execute(
        "SELECT 1 FROM transactions WHERE user_id = ? LIMIT 1",
        (user_id,),
    ).fetchone()

    return row is not None


def query_user_stats(user_id: int) -> Optional[dict]:
    """
    Estadísticas de un usuario: total, conteo, categoría y país más frecuentes.

    Por qué SQLite: todas las queries están filtradas por user_id, por lo que
    el índice idx_user_timestamp lleva directamente a las filas del usuario.
    DuckDB necesitaría escanear el Parquet completo para encontrar las filas
    de un usuario que representa el 0.004% del dataset.

    Retorna None si el usuario no existe.
    """
    assert _sqlite_conn, "Conexión SQLite no inicializada"

    # Totales
    totals = _sqlite_conn.execute("""
        SELECT
            SUM(amount) AS total_amount,
            COUNT(*)    AS transaction_count
        FROM transactions
        WHERE user_id = ?
    """, (user_id,)).fetchone()

    if totals["transaction_count"] == 0:
        return None

    # Categoría más frecuente
    top_cat = _sqlite_conn.execute("""
        SELECT category, COUNT(*) AS cnt
        FROM transactions
        WHERE user_id = ?
        GROUP BY category
        ORDER BY cnt DESC
        LIMIT 1
    """, (user_id,)).fetchone()

    # País más frecuente
    top_country = _sqlite_conn.execute("""
        SELECT country_code, COUNT(*) AS cnt
        FROM transactions
        WHERE user_id = ?
        GROUP BY country_code
        ORDER BY cnt DESC
        LIMIT 1
    """, (user_id,)).fetchone()

    return {
        "user_id":           user_id,
        "total_amount":      round(totals["total_amount"], 2),
        "transaction_count": totals["transaction_count"],
        "top_category":      top_cat["category"] if top_cat else None,
        "top_country":       top_country["country_code"] if top_country else None,
    }


# ---------------------------------------------------------------------------
# Escritura — SQLite (POST /transactions/batch)
# ---------------------------------------------------------------------------

async def insert_batch(transactions: list[dict]) -> dict:
    """
    Inserta un batch de transacciones en SQLite con deduplicación.

    Flujo:
        1. Extraer los transaction_id del batch
        2. Consultar cuáles ya existen en la DB (duplicados)
        3. Insertar solo los que no existen con INSERT OR IGNORE
        4. Retornar conteo de insertados y duplicados saltados

    Por qué INSERT OR IGNORE en lugar de filtrar antes:
        Es más eficiente dejar que SQLite maneje los duplicados con la
        restricción de PRIMARY KEY. El INSERT OR IGNORE no falla ni lanza
        excepción — simplemente ignora las filas duplicadas.

    Por qué asyncio.Lock:
        FastAPI puede recibir dos batch requests simultáneos. Sin lock,
        ambos podrían pasar la verificación de duplicados al mismo tiempo
        y luego intentar insertar las mismas filas, generando conflictos.
        El lock serializa las escrituras sin bloquear el event loop.

    Parámetros
    ----------
    transactions : lista de dicts con los campos del schema

    Retorna
    -------
    dict con received, inserted, duplicates_skipped
    """
    assert _sqlite_conn, "Conexión SQLite no inicializada"
    assert _sqlite_write_lock, "Write lock no inicializado"

    received = len(transactions)

    async with _sqlite_write_lock:
        # Verificar cuáles transaction_id ya existen
        ids = [t["transaction_id"] for t in transactions]
        placeholders = ",".join("?" * len(ids))
        existing = {
            row[0]
            for row in _sqlite_conn.execute(
                f"SELECT transaction_id FROM transactions WHERE transaction_id IN ({placeholders})",
                ids,
            ).fetchall()
        }

        # Filtrar solo los nuevos
        new_transactions = [t for t in transactions if t["transaction_id"] not in existing]
        duplicates = received - len(new_transactions)

        if new_transactions:
            rows = [
                (
                    t["transaction_id"],
                    t["timestamp"],
                    t["user_id"],
                    t["merchant_id"],
                    t["amount"],
                    t["category"],
                    t["country_code"],
                    t["status"],
                )
                for t in new_transactions
            ]

            with _sqlite_conn:  # transacción explícita — commit al salir del with
                _sqlite_conn.executemany(
                    """
                    INSERT OR IGNORE INTO transactions
                        (transaction_id, timestamp, user_id, merchant_id,
                         amount, category, country_code, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

    return {
        "received":          received,
        "inserted":          len(new_transactions),
        "duplicates_skipped": duplicates,
    }