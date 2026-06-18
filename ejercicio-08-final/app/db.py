"""
app/db.py — Capa de acceso a datos del sistema de monitoreo.

Arquitectura de datos del E8 (la decisión central del ejercicio):

    SQLite es la fuente de verdad transaccional VIVA. El servicio setup
    copia el histórico del Parquet (1M transacciones) a SQLite una sola vez
    al arrancar el sistema; a partir de ahí, todo lo nuevo — lo que ingesta
    el pipeline desde CSV y lo del endpoint /transactions/batch — se escribe
    en SQLite. Por tanto SQLite contiene en todo momento el histórico
    completo más todo lo reciente.

    El Parquet es el snapshot histórico INMUTABLE. Su rol es alimentar el
    setup inicial (es la forma columnar y compacta de transportar 1M filas);
    no se consulta en el path de request.

    Por qué este modelo y no "Parquet congelado + SQLite solo lo nuevo":
        La alternativa sería dejar el histórico solo en Parquet y SQLite
        solo con lo nuevo, haciendo UNION ALL en analytics. Pero entonces
        los lookups por usuario (/users/{id}/...) y la detección de
        anomalías tendrían que escanear el Parquet de 1M filas para
        encontrar las transacciones de un usuario — destruyendo el
        idx_user_timestamp que resuelve esos lookups en ~0.15ms (medido en
        E3). Ese índice es la pieza más valiosa del lado transaccional; un
        modelo que lo inutilice optimiza analytics a costa de degradar todo
        lo demás. Como SQLite ya tiene el histórico completo, analytics no
        necesita el Parquet en runtime: DuckDB consulta la tabla SQLite
        directamente (vía la extensión sqlite_scanner), aprovechando su
        motor de ejecución columnar y vectorizado sobre esa tabla.

    Resultado: una sola fuente en runtime (SQLite), consultada con la
    herramienta adecuada según el caso — DuckDB para agregaciones analíticas
    (columnar, vectorizado), SQLite directo con índice para lookups por
    usuario (sub-milisegundo). Sin doble conteo, sin escaneos de Parquet en
    el path de request.

La conexión DuckDB vive en el lifespan de FastAPI, no en los endpoints —
misma regla que el E4: abrir la conexión y adjuntar la base cuesta tiempo,
así que se paga una sola vez al arrancar.
"""

import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

import duckdb

# ---------------------------------------------------------------------------
# Estado global — inicializado una sola vez en el lifespan
# ---------------------------------------------------------------------------

_duckdb_conn: Optional[duckdb.DuckDBPyConnection] = None
_parquet_path: Optional[str] = None
_db_path: Optional[str] = None
_start_time: float = time.monotonic()


def init_connections(parquet_path: str, db_path: str) -> None:
    """
    Inicializa la conexión DuckDB y verifica que las fuentes existen.

    Se llama una sola vez desde el lifespan de FastAPI. La conexión DuckDB
    queda con la base SQLite adjuntada (sqlite_scanner) en modo solo lectura,
    de modo que las queries de analytics consulten la tabla transaccional sin
    reabrir nada en el path de request. El parquet_path se recibe y se valida
    que existe (es el snapshot que alimentó el setup), pero analytics no lo
    consulta en runtime — SQLite ya contiene el histórico completo.
    """
    global _duckdb_conn, _parquet_path, _db_path, _start_time

    if not Path(parquet_path).exists():
        raise FileNotFoundError(
            f"Parquet histórico no encontrado: {parquet_path}. "
            "Genera el dataset del E1 o verifica PARQUET_PATH."
        )
    if not Path(db_path).exists():
        raise FileNotFoundError(
            f"Base SQLite no encontrada: {db_path}. "
            "El servicio setup debe generarla antes de arrancar la API."
        )

    _parquet_path = parquet_path
    _db_path = db_path
    _start_time = time.monotonic()

    # Conexión DuckDB en memoria. La extensión sqlite_scanner permite
    # adjuntar la base SQLite y consultarla como si fueran tablas DuckDB.
    _duckdb_conn = duckdb.connect(":memory:")
    _duckdb_conn.execute("INSTALL sqlite; LOAD sqlite;")
    _duckdb_conn.execute(
        f"ATTACH '{db_path}' AS txn_db (TYPE sqlite, READ_ONLY);"
    )


def close_connections() -> None:
    """Cierra la conexión DuckDB al apagar el servidor."""
    global _duckdb_conn
    if _duckdb_conn is not None:
        _duckdb_conn.close()
        _duckdb_conn = None


def get_uptime() -> float:
    """Segundos transcurridos desde init_connections. No toca la DB."""
    return time.monotonic() - _start_time


def is_duckdb_connected() -> bool:
    return _duckdb_conn is not None


def is_sqlite_connected() -> bool:
    """Verifica que la base SQLite es accesible. Hace una query trivial."""
    if _db_path is None:
        return False
    try:
        conn = sqlite3.connect(_db_path)
        conn.execute("SELECT 1")
        conn.close()
        return True
    except sqlite3.Error:
        return False


# ---------------------------------------------------------------------------
# Helper — fuente de analytics
# ---------------------------------------------------------------------------

def _analytics_cte() -> str:
    """
    Devuelve el CTE base de las queries de analytics.

    En el Modelo A, SQLite contiene el histórico completo más todo lo
    reciente, así que analytics consulta únicamente la tabla SQLite
    adjuntada (txn_db.transactions). DuckDB aplica su motor columnar y
    vectorizado sobre esa tabla — no se hace UNION con el Parquet porque eso
    duplicaría el histórico (que ya está en SQLite).
    """
    return """
        WITH unified AS (
            SELECT transaction_id, timestamp, user_id, merchant_id,
                   amount, category, country_code, status
            FROM txn_db.transactions
        )
    """


# ---------------------------------------------------------------------------
# Analytics — DuckDB sobre la vista unificada
# ---------------------------------------------------------------------------

def query_analytics_summary() -> dict:
    """
    Totales globales sobre la vista unificada (histórico + reciente).

    Tres agregaciones: totales globales, breakdown por país, breakdown por
    categoría. DuckDB aplica column pruning sobre el Parquet (solo lee las
    columnas que la query necesita) y lee la tabla SQLite completa, que es
    pequeña comparada con el histórico.
    """
    cte = _analytics_cte()

    totals = _duckdb_conn.execute(cte + """
        SELECT COUNT(*), ROUND(SUM(amount), 2), ROUND(AVG(amount), 4)
        FROM unified
    """).fetchone()

    by_country = _duckdb_conn.execute(cte + """
        SELECT country_code, COUNT(*) AS n, ROUND(SUM(amount), 2) AS total
        FROM unified
        GROUP BY country_code
        ORDER BY n DESC
    """).fetchall()

    by_category = _duckdb_conn.execute(cte + """
        SELECT category, COUNT(*) AS n, ROUND(AVG(amount), 4) AS avg_amt
        FROM unified
        GROUP BY category
        ORDER BY n DESC
    """).fetchall()

    return {
        "total_transactions": totals[0],
        "total_amount": totals[1],
        "avg_amount": totals[2],
        "by_country": [
            {"country_code": c, "total_transactions": n, "total_amount": t}
            for c, n, t in by_country
        ],
        "by_category": [
            {"category": cat, "total_transactions": n, "avg_amount": a}
            for cat, n, a in by_category
        ],
    }


def query_top_merchants(limit: int = 10, country: Optional[str] = None) -> list[dict]:
    """
    Top N merchants por volumen total sobre la vista unificada.
    Filtro opcional por país, parametrizado para evitar inyección.
    """
    cte = _analytics_cte()

    if country:
        rows = _duckdb_conn.execute(cte + """
            SELECT merchant_id, ROUND(SUM(amount), 2) AS total, COUNT(*) AS n
            FROM unified
            WHERE country_code = ?
            GROUP BY merchant_id
            ORDER BY total DESC
            LIMIT ?
        """, [country.upper(), limit]).fetchall()
    else:
        rows = _duckdb_conn.execute(cte + """
            SELECT merchant_id, ROUND(SUM(amount), 2) AS total, COUNT(*) AS n
            FROM unified
            GROUP BY merchant_id
            ORDER BY total DESC
            LIMIT ?
        """, [limit]).fetchall()

    return [
        {"merchant_id": m, "total_amount": t, "transaction_count": n}
        for m, t, n in rows
    ]


# ---------------------------------------------------------------------------
# Usuarios — SQLite con idx_user_timestamp (solo datos transaccionales)
# ---------------------------------------------------------------------------
# Nota: los endpoints de usuario consultan SOLO SQLite, no la vista unificada.
# El setup inicial copia el histórico del Parquet a SQLite, así que el
# histórico de un usuario también está en SQLite. Esto mantiene los lookups
# por usuario en <1ms con el índice, sin pagar el costo de abrir el Parquet.

def query_user_exists(user_id: int) -> bool:
    conn = sqlite3.connect(_db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM transactions WHERE user_id = ? LIMIT 1", [user_id]
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def query_user_transactions(
    user_id: int,
    page: int = 1,
    page_size: int = 20,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> list[dict]:
    """
    Transacciones de un usuario, más recientes primero, con paginación.

    El E8 agrega filtros de fecha opcionales (date_from, date_to) sobre el
    endpoint heredado del E4. El índice idx_user_timestamp (user_id,
    timestamp DESC) cubre tanto el filtro por usuario como el ordenamiento
    y el rango de fechas, porque el orden lexicográfico de los timestamps
    ISO8601 coincide con el orden cronológico.
    """
    conn = sqlite3.connect(_db_path)
    try:
        clauses = ["user_id = ?"]
        params: list = [user_id]

        if date_from:
            clauses.append("timestamp >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("timestamp <= ?")
            params.append(date_to)

        where = " AND ".join(clauses)
        offset = (page - 1) * page_size
        params.extend([page_size, offset])

        rows = conn.execute(f"""
            SELECT transaction_id, timestamp, amount, category, status, merchant_id
            FROM transactions
            WHERE {where}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
        """, params).fetchall()

        return [
            {
                "transaction_id": r[0], "timestamp": r[1], "amount": r[2],
                "category": r[3], "status": r[4], "merchant_id": r[5],
            }
            for r in rows
        ]
    finally:
        conn.close()


def query_user_stats(user_id: int) -> Optional[dict]:
    """Monto total, conteo, categoría y país más frecuentes de un usuario."""
    conn = sqlite3.connect(_db_path)
    try:
        agg = conn.execute("""
            SELECT COUNT(*), ROUND(SUM(amount), 2)
            FROM transactions WHERE user_id = ?
        """, [user_id]).fetchone()

        if agg[0] == 0:
            return None

        top_cat = conn.execute("""
            SELECT category FROM transactions WHERE user_id = ?
            GROUP BY category ORDER BY COUNT(*) DESC LIMIT 1
        """, [user_id]).fetchone()

        top_country = conn.execute("""
            SELECT country_code FROM transactions WHERE user_id = ?
            GROUP BY country_code ORDER BY COUNT(*) DESC LIMIT 1
        """, [user_id]).fetchone()

        return {
            "user_id": user_id,
            "total_amount": agg[1],
            "transaction_count": agg[0],
            "top_category": top_cat[0] if top_cat else None,
            "top_country": top_country[0] if top_country else None,
        }
    finally:
        conn.close()