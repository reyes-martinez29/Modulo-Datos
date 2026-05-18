"""
engines/duckdb_engine.py — Las 8 queries implementadas en DuckDB.

Decisión de diseño fundamental:
    DuckDB lee el Parquet DIRECTAMENTE con FROM '{path}'. No se carga el
    archivo en pandas primero y luego se registra en DuckDB. Hacer eso
    mediría pandas+DuckDB, no DuckDB solo.

    DuckDB puede leer Parquet nativo porque internamente usa su propio
    lector basado en Apache Arrow que hace column pruning y predicate
    pushdown — lee del disco solo las columnas y filas que necesita para
    ejecutar la query. Esta es su mayor ventaja sobre pandas en queries
    con filtros selectivos (especialmente Q5).

    Por qué .df() al final:
    duckdb.sql(...).df() convierte el resultado a pd.DataFrame.
    Esto es necesario para que benchmark.py pueda comparar resultados
    entre engines con la misma lógica de validación.

    Por qué una conexión por función en lugar de una global:
    Una conexión global podría interferir entre mediciones si el benchmark
    corre queries en paralelo o si hay estado residual entre runs.
    duckdb.sql() usa una conexión temporal por llamada — stateless,
    aislada, y sin overhead significativo para queries de lectura.

    EXPLAIN ANALYZE:
    Q3, Q5 y Q6 tienen una función auxiliar que retorna el plan de ejecución.
    El benchmark.py llama a estas funciones y guarda el plan en el JSON
    de resultados para que el reporte pueda interpretarlo.
"""

import duckdb
import pandas as pd


# ---------------------------------------------------------------------------
# Q1 — Conteo de transacciones por country_code
# ---------------------------------------------------------------------------

def q1(path: str) -> pd.DataFrame:
    """
    DuckDB hace column pruning automático: aunque el Parquet tiene 8 columnas,
    solo lee el bloque físico de country_code desde el disco.
    """
    return duckdb.sql(f"""
        SELECT
            country_code,
            COUNT(*) AS total
        FROM '{path}'
        GROUP BY country_code
        ORDER BY total DESC
    """).df()


# ---------------------------------------------------------------------------
# Q2 — Estadísticas de amount por category
# ---------------------------------------------------------------------------

def q2(path: str) -> pd.DataFrame:
    return duckdb.sql(f"""
        SELECT
            category,
            AVG(amount)  AS avg_amount,
            MIN(amount)  AS min_amount,
            MAX(amount)  AS max_amount
        FROM '{path}'
        GROUP BY category
        ORDER BY category
    """).df()


# ---------------------------------------------------------------------------
# Q3 — Top 10 usuarios por suma de amount
# ---------------------------------------------------------------------------

def q3(path: str) -> pd.DataFrame:
    return duckdb.sql(f"""
        SELECT
            user_id,
            SUM(amount)   AS total_amount,
            COUNT(*)      AS tx_count
        FROM '{path}'
        GROUP BY user_id
        ORDER BY total_amount DESC
        LIMIT 10
    """).df()


def explain_q3(path: str) -> str:
    """
    Retorna el plan de ejecución de Q3.

    Qué buscar en el plan:
    - HASH_GROUP_BY: DuckDB agrupó en memoria con una hash table.
    - TOP_N: optimización que evita ordenar los 50k grupos completos,
      solo mantiene los top 10 en una heap de tamaño fijo.
    - PARQUET_SCAN: confirma que lee el Parquet directo, no una tabla registrada.
    """
    rows = duckdb.sql(f"""
        EXPLAIN ANALYZE
        SELECT
            user_id,
            SUM(amount)  AS total_amount,
            COUNT(*)     AS tx_count
        FROM '{path}'
        GROUP BY user_id
        ORDER BY total_amount DESC
        LIMIT 10
    """).fetchall()
    return "\n".join(str(row[1]) for row in rows)


# ---------------------------------------------------------------------------
# Q4 — Transacciones fallidas por hora del día
# ---------------------------------------------------------------------------

def q4(path: str) -> pd.DataFrame:
    """
    EXTRACT(hour FROM timestamp::TIMESTAMP) convierte el campo a TIMESTAMP
    si viene como VARCHAR en el Parquet, y extrae la hora (0-23).

    GENERATE_SERIES garantiza que aparecen las 24 horas aunque alguna
    tenga 0 transacciones fallidas — LEFT JOIN desde el rango completo.
    """
    return duckdb.sql(f"""
        WITH failed AS (
            SELECT
                EXTRACT(hour FROM timestamp::TIMESTAMP) AS hour,
                COUNT(*) AS failed_count
            FROM '{path}'
            WHERE status = 'failed'
            GROUP BY hour
        ),
        all_hours AS (
            SELECT UNNEST(GENERATE_SERIES(0, 23)) AS hour
        )
        SELECT
            all_hours.hour,
            COALESCE(failed.failed_count, 0) AS failed_count
        FROM all_hours
        LEFT JOIN failed USING (hour)
        ORDER BY hour
    """).df().astype({"hour": int, "failed_count": int})


# ---------------------------------------------------------------------------
# Q5 — Transacciones recientes en MX o CO con amount > 500
# ---------------------------------------------------------------------------

def q5(path: str) -> pd.DataFrame:
    """
    Predicate pushdown en acción:
    DuckDB empuja los filtros (amount > 500, country_code IN (...), timestamp >= cutoff)
    al lector de Parquet. Gracias al column pruning y row group filtering,
    puede descartar row groups enteros que no cumplen la condición de fecha
    sin leerlos del disco. Esto es lo que hace Q5 el caso donde DuckDB
    gana más claramente sobre pandas y polars.

    La subconsulta calcula max(timestamp) del dataset completo para que el
    período de 30 días sea relativo al rango del dataset, no a hoy.
    """
    return duckdb.sql(f"""
        WITH bounds AS (
            SELECT MAX(timestamp::TIMESTAMP) AS max_ts
            FROM '{path}'
        )
        SELECT
            transaction_id,
            timestamp::TIMESTAMP  AS timestamp,
            user_id,
            merchant_id,
            amount,
            category,
            country_code,
            status
        FROM '{path}', bounds
        WHERE
            amount > 500
            AND country_code IN ('MX', 'CO')
            AND timestamp::TIMESTAMP >= (bounds.max_ts - INTERVAL '30 days')
        ORDER BY timestamp, transaction_id
    """).df()


def explain_q5(path: str) -> str:
    """
    Qué buscar en el plan de Q5:
    - FILTER: los predicados que DuckDB aplica antes de materializar filas.
    - Estadísticas de filas: 'rows=' antes vs después del filtro muestra
      cuántas filas se descartaron sin leer.
    - PARQUET_SCAN con 'Filters:' en el plan indica que el filtro se hizo
      en el nivel del lector, no después de cargar todo a memoria.
    """
    rows = duckdb.sql(f"""
        EXPLAIN ANALYZE
        WITH bounds AS (
            SELECT MAX(timestamp::TIMESTAMP) AS max_ts
            FROM '{path}'
        )
        SELECT *
        FROM '{path}', bounds
        WHERE
            amount > 500
            AND country_code IN ('MX', 'CO')
            AND timestamp::TIMESTAMP >= (bounds.max_ts - INTERVAL '30 days')
    """).fetchall()
    return "\n".join(str(row[1]) for row in rows)


# ---------------------------------------------------------------------------
# Q6 — Por country_code, la category con más transacciones
# ---------------------------------------------------------------------------

def q6(path: str) -> pd.DataFrame:
    """
    Estrategia en SQL: window function RANK() para encontrar el argmax.

    Por qué RANK() en lugar de MAX() + JOIN:
    MAX(tx_count) te da el valor máximo pero no sabes a qué categoría pertenece.
    RANK() numera las categorías por conteo descendente dentro de cada país,
    y luego nos quedamos solo con rank = 1 (la categoría top).

    Desempate: cuando dos categorías tienen el mismo conteo, ORDER BY category
    establece un desempate alfabético determinista. Los otros engines deben
    usar el mismo criterio de desempate para que la validación pase.
    """
    return duckdb.sql(f"""
        WITH counts AS (
            SELECT
                country_code,
                category,
                COUNT(*)     AS tx_count,
                AVG(amount)  AS avg_amount
            FROM '{path}'
            GROUP BY country_code, category
        ),
        ranked AS (
            SELECT
                country_code,
                category,
                tx_count,
                avg_amount,
                RANK() OVER (
                    PARTITION BY country_code
                    ORDER BY tx_count DESC, category ASC
                ) AS rnk
            FROM counts
        )
        SELECT
            country_code,
            category,
            tx_count,
            avg_amount
        FROM ranked
        WHERE rnk = 1
        ORDER BY country_code
    """).df()


def explain_q6(path: str) -> str:
    """
    Qué buscar en el plan de Q6:
    - WINDOW: la operación de ventana que calcula RANK().
    - HASH_GROUP_BY: dos niveles — uno para contar por (país, categoría),
      otro para la ventana.
    - El plan muestra si DuckDB optimizó la ventana en un solo pase o
      materializó el resultado intermedio.
    """
    rows = duckdb.sql(f"""
        EXPLAIN ANALYZE
        WITH counts AS (
            SELECT
                country_code,
                category,
                COUNT(*)     AS tx_count,
                AVG(amount)  AS avg_amount
            FROM '{path}'
            GROUP BY country_code, category
        ),
        ranked AS (
            SELECT *,
                RANK() OVER (
                    PARTITION BY country_code
                    ORDER BY tx_count DESC, category ASC
                ) AS rnk
            FROM counts
        )
        SELECT country_code, category, tx_count, avg_amount
        FROM ranked WHERE rnk = 1
        ORDER BY country_code
    """).fetchall()
    return "\n".join(str(row[1]) for row in rows)


# ---------------------------------------------------------------------------
# Q7 — Usuarios con más de 5 transacciones fallidas
# ---------------------------------------------------------------------------

def q7(path: str) -> pd.DataFrame:
    """
    HAVING filtra los grupos después del GROUP BY, equivalente al
    .query("failed_count > 5") de pandas pero ejecutado en el motor SQL
    antes de materializar el resultado completo.
    """
    return duckdb.sql(f"""
        SELECT
            user_id,
            COUNT(*) AS failed_count
        FROM '{path}'
        WHERE status = 'failed'
        GROUP BY user_id
        HAVING COUNT(*) > 5
        ORDER BY user_id
    """).df()


# ---------------------------------------------------------------------------
# Q8 — Monto promedio diario por category
# ---------------------------------------------------------------------------

def q8(path: str) -> pd.DataFrame:
    """
    DATE_TRUNC('day', timestamp) trunca al inicio del día (medianoche).
    STRFTIME convierte a string 'YYYY-MM-DD' para que la comparación
    con pandas y polars sea directa sin conversiones de tipo datetime.
    """
    return duckdb.sql(f"""
        SELECT
            STRFTIME(DATE_TRUNC('day', timestamp::TIMESTAMP), '%Y-%m-%d') AS day,
            category,
            AVG(amount) AS avg_amount
        FROM '{path}'
        GROUP BY day, category
        ORDER BY day, category
    """).df()