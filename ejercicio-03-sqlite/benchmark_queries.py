"""
benchmark_queries.py — Benchmark de los 5 patrones de acceso transaccional.

Uso:
    python benchmark_queries.py
    python benchmark_queries.py --db ../../data/transactions.db
    python benchmark_queries.py --parquet ../../data/transactions_1m_parquet_snappy.parquet

Para cada patrón mide:
    - Tiempo con índices (estado normal de la base de datos)
    - Tiempo sin índices (elimina los índices temporalmente para comparar)
    - EXPLAIN QUERY PLAN (captura el plan de ejecución de SQLite)
    - Tiempo equivalente en DuckDB sobre el Parquet de E1

El benchmark usa parámetros representativos para cada patrón:
    - P1: transaction_id real del dataset (no inventado)
    - P2/P3/P4: user_id con alta actividad (~20 transacciones promedio)
    - P5: country_code 'MX' con N=20

Por qué usar parámetros reales:
Si inventamos un user_id que no existe, la query termina en microsegundos
porque SQLite no encuentra nada — eso no mide el SLA real. Los parámetros
se extraen del propio dataset antes de correr el benchmark.

Entrega de ejercicio 3, solo por correccion del commit. 
"""

import argparse
import gc
import json
import sqlite3
import time
import tracemalloc
from pathlib import Path

import duckdb
import pandas as pd


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------

ROOT_DIR    = Path(__file__).parent.parent
DB_PATH     = ROOT_DIR / "data" / "transactions.db"
PARQUET_PATH = ROOT_DIR / "data" / "transactions_1m_parquet_snappy.parquet"
RESULTS_DIR = Path(__file__).parent / "results"

REPEATS = 5  # Más repeticiones que E1/E2 porque los tiempos son muy pequeños


# ---------------------------------------------------------------------------
# Definición de los 5 patrones
# ---------------------------------------------------------------------------

# Cada patrón tiene:
#   sql_with_idx    : query SQL para SQLite con índices
#   sql_no_idx      : misma query (los índices se eliminan externamente)
#   duckdb_sql      : query equivalente para DuckDB sobre Parquet
#   sla_ms          : SLA en milisegundos
#   description     : descripción del patrón

PATTERNS = {
    "P1": {
        "description": "Buscar transacción por transaction_id exacto",
        "sla_ms": 10,
        "sqlite_sql": """
            SELECT *
            FROM transactions
            WHERE transaction_id = :transaction_id
        """,
        "duckdb_sql": """
            SELECT *
            FROM '{parquet}'
            WHERE transaction_id = '{transaction_id}'
        """,
        "params_key": "transaction_id",
    },
    "P2": {
        "description": "Últimas 20 transacciones de un usuario",
        "sla_ms": 50,
        "sqlite_sql": """
            SELECT transaction_id, timestamp, amount, category, status
            FROM transactions
            WHERE user_id = :user_id
            ORDER BY timestamp DESC
            LIMIT 20
        """,
        "duckdb_sql": """
            SELECT transaction_id, timestamp, amount, category, status
            FROM '{parquet}'
            WHERE user_id = {user_id}
            ORDER BY timestamp DESC
            LIMIT 20
        """,
        "params_key": "user_id",
    },
    "P3": {
        "description": "Transacciones de un usuario en un rango de fechas",
        "sla_ms": 50,
        "sqlite_sql": """
            SELECT transaction_id, timestamp, amount, category, status
            FROM transactions
            WHERE user_id = :user_id
              AND timestamp BETWEEN :date_from AND :date_to
        """,
        "duckdb_sql": """
            SELECT transaction_id, timestamp, amount, category, status
            FROM '{parquet}'
            WHERE user_id = {user_id}
              AND timestamp BETWEEN '{date_from}' AND '{date_to}'
        """,
        "params_key": "user_id",
    },
    "P4": {
        "description": "Suma de amount de un usuario en el último mes",
        "sla_ms": 50,
        "sqlite_sql": """
            SELECT
                user_id,
                SUM(amount)  AS total_amount,
                COUNT(*)     AS tx_count
            FROM transactions
            WHERE user_id = :user_id
              AND timestamp >= :date_from
        """,
        "duckdb_sql": """
            SELECT
                user_id,
                SUM(amount)  AS total_amount,
                COUNT(*)     AS tx_count
            FROM '{parquet}'
            WHERE user_id = {user_id}
              AND timestamp::TIMESTAMP >= '{date_from}'::TIMESTAMP
            GROUP BY user_id
        """,
        "params_key": "user_id",
    },
    "P5": {
        "description": "Usuarios de un país con más de N transacciones",
        "sla_ms": 200,
        "sqlite_sql": """
            SELECT user_id, COUNT(*) AS tx_count
            FROM transactions
            WHERE country_code = :country_code
            GROUP BY user_id
            HAVING COUNT(*) > :min_tx
            ORDER BY tx_count DESC
        """,
        "duckdb_sql": """
            SELECT user_id, COUNT(*) AS tx_count
            FROM '{parquet}'
            WHERE country_code = '{country_code}'
            GROUP BY 1
            HAVING COUNT(*) > {min_tx}
            ORDER BY 2 DESC
        """,
        "params_key": "country_code",
    },
}


# ---------------------------------------------------------------------------
# Extracción de parámetros representativos
# ---------------------------------------------------------------------------

def get_benchmark_params(conn: sqlite3.Connection) -> dict:
    """
    Extrae parámetros reales del dataset para el benchmark.

    Usar parámetros reales es crítico: un user_id que no existe produce
    resultados vacíos en microsegundos, lo que no mide el SLA real.

    Se elige un usuario con actividad típica (cerca del percentil 50 de
    transacciones), no el más activo ni el menos activo.
    """
    # Usuario con más transacciones — representa el peor caso del SLA
    # (más filas que recorrer = tiempo máximo del patrón)
    user_row = conn.execute("""
        SELECT user_id, COUNT(*) AS cnt
        FROM transactions
        GROUP BY user_id
        ORDER BY cnt DESC
        LIMIT 1
    """).fetchone()
    user_id = user_row[0]
    user_tx_count = user_row[1]

    # Un transaction_id real del dataset
    tx_id = conn.execute(
        "SELECT transaction_id FROM transactions LIMIT 1"
    ).fetchone()[0]

    # Rango de fechas: un mes de actividad del usuario seleccionado
    date_range = conn.execute("""
        SELECT MIN(timestamp), MAX(timestamp)
        FROM transactions
        WHERE user_id = ?
    """, (user_id,)).fetchone()

    # Para P3: usamos el primer mes del rango del usuario
    date_from = date_range[0][:10] + " 00:00:00"    # inicio del día
    date_to   = date_range[0][:7]  + "-28 23:59:59" # +~28 días

    # Para P4: último mes del dataset completo
    max_ts = conn.execute(
        "SELECT MAX(timestamp) FROM transactions"
    ).fetchone()[0]
    # Calcular 30 días antes del máximo manualmente (SQLite datetime)
    month_ago = conn.execute(
        "SELECT datetime(?, '-30 days')", (max_ts,)
    ).fetchone()[0]

    print(f"\n  Parámetros del benchmark:")
    print(f"    user_id:       {user_id} ({user_tx_count} transacciones)")
    print(f"    transaction_id: {tx_id[:20]}...")
    print(f"    date_from:     {date_from}")
    print(f"    date_to:       {date_to}")
    print(f"    month_ago:     {month_ago}")

    return {
        "transaction_id": tx_id,
        "user_id":        user_id,
        "date_from":      date_from,
        "date_to":        date_to,
        "month_ago":      month_ago,
        "country_code":   "MX",
        # min_tx: con distribución uniforme (1M filas / 15 países / 50k usuarios),
        # el promedio es ~1.3 tx por usuario por país. Un umbral de 20 no produce
        # resultados. Se usa 2 para obtener usuarios por encima del promedio
        # (al menos 3 transacciones en ese país) — un subconjunto realista.
        "min_tx":         2,
    }


# ---------------------------------------------------------------------------
# Medición de una query SQLite
# ---------------------------------------------------------------------------

def measure_sqlite(
    conn: sqlite3.Connection,
    sql: str,
    params: dict,
    repeats: int = REPEATS,
) -> dict:
    """
    Mide el tiempo promedio de una query SQLite.

    gc.collect() antes de cada run para evitar que la recolección de
    basura de Python interfiera con las mediciones.

    Los primeros runs pueden ser más lentos por el page cache de SQLite.
    Después de 1-2 runs, el cache está caliente y los tiempos se estabilizan.
    Reportamos el promedio de todos los runs incluyendo el primero, porque
    en producción la primera consulta también tiene que cumplir el SLA.
    """
    times = []
    result = None

    for _ in range(repeats):
        gc.collect()
        t0 = time.perf_counter()
        cursor = conn.execute(sql, params)
        result = cursor.fetchall()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        times.append(elapsed_ms)

    return {
        "avg_ms":   round(sum(times) / len(times), 3),
        "min_ms":   round(min(times), 3),
        "max_ms":   round(max(times), 3),
        "runs_ms":  [round(t, 3) for t in times],
        "rows":     len(result) if result else 0,
    }


# ---------------------------------------------------------------------------
# Captura de EXPLAIN QUERY PLAN
# ---------------------------------------------------------------------------

def get_explain_plan(conn: sqlite3.Connection, sql: str, params: dict) -> str:
    """
    Captura el plan de ejecución de SQLite para una query.

    EXPLAIN QUERY PLAN retorna filas con:
        (id, parent, notused, detail)

    El campo 'detail' describe la operación: SCAN (full scan), SEARCH (usa
    índice), USE TEMP B-TREE (sort sin índice), etc.

    La presencia de 'USING INDEX' en el detail confirma que SQLite está
    usando el índice diseñado para ese patrón.
    """
    plan_rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    return "\n".join(row[3] for row in plan_rows)


# ---------------------------------------------------------------------------
# Gestión de índices para el benchmark sin índices
# ---------------------------------------------------------------------------

def drop_custom_indexes(conn: sqlite3.Connection) -> None:
    """Elimina los índices creados en schema.sql (no el PRIMARY KEY)."""
    conn.execute("DROP INDEX IF EXISTS idx_user_timestamp")
    conn.execute("DROP INDEX IF EXISTS idx_country_user")
    conn.commit()
    print("  Índices eliminados para benchmark sin índices.")


def recreate_custom_indexes(conn: sqlite3.Connection) -> None:
    """Recrea los índices después del benchmark sin índices."""
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_timestamp
        ON transactions (user_id, timestamp DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_country_user
        ON transactions (country_code, user_id)
    """)
    conn.commit()
    print("  Índices recreados.")


# ---------------------------------------------------------------------------
# Benchmark por patrón
# ---------------------------------------------------------------------------

def benchmark_pattern(
    pid: str,
    pattern: dict,
    conn_with: sqlite3.Connection,
    conn_without: sqlite3.Connection,
    params: dict,
    parquet_path: str,
) -> dict:
    """
    Ejecuta un patrón de acceso en SQLite (con y sin índices) y en DuckDB.

    Parámetros
    ----------
    pid            : identificador del patrón ("P1" ... "P5")
    pattern        : dict con sql, sla_ms, description
    conn_with      : conexión con índices
    conn_without   : conexión sin índices
    params         : parámetros del benchmark (user_id, dates, etc.)
    parquet_path   : ruta al Parquet de E1 para DuckDB

    Retorna
    -------
    dict con todas las métricas del patrón.
    """
    print(f"\n  [{pid}] {pattern['description']}")
    print(f"  SLA: <{pattern['sla_ms']}ms")

    # --- Parámetros SQLite (named parameters con :nombre) ---
    sqlite_params = {
        "transaction_id": params["transaction_id"],
        "user_id":        params["user_id"],
        "date_from":      params["date_from"],
        "date_to":        params["date_to"],
        "month_ago":      params["month_ago"],
        "country_code":   params["country_code"],
        "min_tx":         params["min_tx"],
    }

    # --- SQLite CON índices ---
    metrics_with = measure_sqlite(conn_with, pattern["sqlite_sql"], sqlite_params)
    plan_with    = get_explain_plan(conn_with, pattern["sqlite_sql"], sqlite_params)
    sla_ok       = metrics_with["avg_ms"] <= pattern["sla_ms"]
    print(f"  Con índices:    {metrics_with['avg_ms']:.2f}ms avg "
          f"(min={metrics_with['min_ms']:.2f}ms) "
          f"{'✓ SLA OK' if sla_ok else '✗ SLA MISS'}")

    # --- SQLite SIN índices ---
    metrics_without = measure_sqlite(conn_without, pattern["sqlite_sql"], sqlite_params)
    plan_without    = get_explain_plan(conn_without, pattern["sqlite_sql"], sqlite_params)
    speedup         = round(metrics_without["avg_ms"] / metrics_with["avg_ms"], 1) \
                      if metrics_with["avg_ms"] > 0 else 0
    print(f"  Sin índices:    {metrics_without['avg_ms']:.2f}ms avg "
          f"({speedup}x más lento)")

    # --- DuckDB sobre Parquet ---
    duckdb_sql = pattern["duckdb_sql"].format(
        parquet=parquet_path,
        transaction_id=params["transaction_id"],
        user_id=params["user_id"],
        date_from=params["date_from"],
        date_to=params["date_to"],
        month_ago=params["month_ago"],
        country_code=params["country_code"],
        min_tx=params["min_tx"],
    )

    duckdb_times = []
    duckdb_rows  = 0
    for _ in range(REPEATS):
        gc.collect()
        t0     = time.perf_counter()
        result = duckdb.sql(duckdb_sql).fetchall()
        duckdb_times.append((time.perf_counter() - t0) * 1000)
        duckdb_rows = len(result)

    duckdb_avg = round(sum(duckdb_times) / len(duckdb_times), 3)
    winner     = "SQLite" if metrics_with["avg_ms"] < duckdb_avg else "DuckDB"
    print(f"  DuckDB:         {duckdb_avg:.2f}ms avg → ganador: {winner}")

    return {
        "description":    pattern["description"],
        "sla_ms":         pattern["sla_ms"],
        "sqlite_with_idx": {
            **metrics_with,
            "sla_ok":    sla_ok,
            "plan":      plan_with,
        },
        "sqlite_no_idx": {
            **metrics_without,
            "plan":      plan_without,
            "speedup_vs_with": speedup,
        },
        "duckdb": {
            "avg_ms":  duckdb_avg,
            "min_ms":  round(min(duckdb_times), 3),
            "max_ms":  round(max(duckdb_times), 3),
            "runs_ms": [round(t, 3) for t in duckdb_times],
            "rows":    duckdb_rows,
        },
        "winner": winner,
    }


# ---------------------------------------------------------------------------
# Benchmark principal
# ---------------------------------------------------------------------------

def run_benchmark(db_path: Path, parquet_path: Path) -> dict:
    """
    Ejecuta los 5 patrones de acceso en SQLite y DuckDB.

    Usa dos conexiones SQLite separadas:
      - conn_with:    la base de datos normal con todos los índices
      - conn_without: la misma base de datos después de DROP INDEX

    Por qué dos conexiones en lugar de una:
    Al hacer DROP INDEX y luego CREATE INDEX en la misma conexión, SQLite
    puede mantener páginas del índice anterior en su page cache, lo que
    puede hacer que el benchmark "sin índices" sea más rápido de lo real.
    Abriendo una segunda conexión nos aseguramos de que el page cache
    esté frío para el benchmark sin índices.
    """
    if not db_path.exists():
        raise FileNotFoundError(
            f"No se encontró {db_path}\n"
            "Corre primero: python ingest.py --wal"
        )

    if not parquet_path.exists():
        raise FileNotFoundError(
            f"No se encontró {parquet_path}\n"
            "El Parquet se genera en el Ejercicio 1."
        )

    print(f"\n{'='*55}")
    print("Benchmark de patrones de acceso transaccional")
    print(f"  SQLite:  {db_path}")
    print(f"  Parquet: {parquet_path}")
    print(f"  Repeats: {REPEATS} por medición")
    print(f"{'='*55}")

    # Conexión principal (con índices)
    conn_with = sqlite3.connect(str(db_path))
    conn_with.execute("PRAGMA cache_size = -65536")
    conn_with.execute("PRAGMA temp_store = MEMORY")

    # ANALYZE actualiza las estadísticas del query planner de SQLite.
    # Sin ANALYZE, SQLite puede ignorar los índices en tablas recién
    # pobladas porque no tiene información sobre la distribución de datos.
    # Esto ocurre especialmente después de una ingesta masiva — el planner
    # no sabe cuántas filas hay por user_id y prefiere el full scan.
    print("\nActualizando estadísticas del query planner (ANALYZE)...")
    conn_with.execute("ANALYZE")
    conn_with.commit()
    print("  ANALYZE completado.")

    # Extraer parámetros reales del dataset
    params = get_benchmark_params(conn_with)

    # --- Benchmark CON índices y capturas de EXPLAIN ---
    print(f"\n{'─'*55}")
    print("Fase 1: benchmark con índices")
    print(f"{'─'*55}")

    results = {}

    # -----------------------------------------------------------------------
    # FASE 1: medir CON índices
    # -----------------------------------------------------------------------
    # IMPORTANTE: medimos CON índices PRIMERO, antes de tocarlos.
    # Si elimináramos los índices antes de esta fase (como hace conn_without
    # más abajo), el DROP INDEX afectaría al archivo .db compartido y ambas
    # conexiones verían la misma base sin índices. SQLite no tiene índices
    # por conexión — son parte del archivo.
    print("\n" + "─"*55)
    print("Fase 1: midiendo CON índices...")

    results_with = {}
    for pid, pattern in PATTERNS.items():
        results_with[pid] = measure_sqlite(
            conn_with,
            pattern["sqlite_sql"],
            {
                "transaction_id": params["transaction_id"],
                "user_id":        params["user_id"],
                "date_from":      params["date_from"],
                "date_to":        params["date_to"],
                "month_ago":      params["month_ago"],
                "country_code":   params["country_code"],
                "min_tx":         params["min_tx"],
            },
        )
        plan_with = get_explain_plan(
            conn_with,
            pattern["sqlite_sql"],
            {
                "transaction_id": params["transaction_id"],
                "user_id":        params["user_id"],
                "date_from":      params["date_from"],
                "date_to":        params["date_to"],
                "month_ago":      params["month_ago"],
                "country_code":   params["country_code"],
                "min_tx":         params["min_tx"],
            },
        )
        results_with[pid]["plan"] = plan_with
        sla_ok = results_with[pid]["avg_ms"] <= pattern["sla_ms"]
        results_with[pid]["sla_ok"] = sla_ok
        print(f"  [{pid}] {results_with[pid]['avg_ms']:.2f}ms "
              f"{'✓ SLA OK' if sla_ok else '✗ SLA MISS'}")

    conn_with.close()

    # -----------------------------------------------------------------------
    # FASE 2: eliminar índices y medir SIN índices
    # -----------------------------------------------------------------------
    # Abrimos una conexión nueva DESPUÉS de la fase 1 para asegurarnos de que
    # el page cache de la fase 1 no interfiere.
    print("\n" + "─"*55)
    print("Fase 2: eliminando índices y midiendo SIN índices...")

    conn_without = sqlite3.connect(str(db_path))
    conn_without.execute("PRAGMA cache_size = -65536")
    conn_without.execute("PRAGMA temp_store = MEMORY")
    drop_custom_indexes(conn_without)

    results_without = {}
    for pid, pattern in PATTERNS.items():
        results_without[pid] = measure_sqlite(
            conn_without,
            pattern["sqlite_sql"],
            {
                "transaction_id": params["transaction_id"],
                "user_id":        params["user_id"],
                "date_from":      params["date_from"],
                "date_to":        params["date_to"],
                "month_ago":      params["month_ago"],
                "country_code":   params["country_code"],
                "min_tx":         params["min_tx"],
            },
        )
        plan_without = get_explain_plan(
            conn_without,
            pattern["sqlite_sql"],
            {
                "transaction_id": params["transaction_id"],
                "user_id":        params["user_id"],
                "date_from":      params["date_from"],
                "date_to":        params["date_to"],
                "month_ago":      params["month_ago"],
                "country_code":   params["country_code"],
                "min_tx":         params["min_tx"],
            },
        )
        results_without[pid]["plan"] = plan_without
        speedup = round(results_without[pid]["avg_ms"] / results_with[pid]["avg_ms"], 1)                   if results_with[pid]["avg_ms"] > 0 else 0
        results_without[pid]["speedup_vs_with"] = speedup
        print(f"  [{pid}] {results_without[pid]['avg_ms']:.2f}ms "
              f"({speedup}x más lento que con índices)")

    recreate_custom_indexes(conn_without)
    conn_without.close()

    # -----------------------------------------------------------------------
    # FASE 3: DuckDB sobre Parquet
    # -----------------------------------------------------------------------
    print("\n" + "─"*55)
    print("Fase 3: midiendo DuckDB sobre Parquet...")

    results_duckdb = {}
    for pid, pattern in PATTERNS.items():
        duckdb_sql = pattern["duckdb_sql"].format(
            parquet       = str(parquet_path),
            transaction_id= params["transaction_id"],
            user_id       = params["user_id"],
            date_from     = params["date_from"],
            date_to       = params["date_to"],
            month_ago     = params["month_ago"],
            country_code  = params["country_code"],
            min_tx        = params["min_tx"],
        )
        duckdb_times = []
        duckdb_rows  = 0
        for _ in range(REPEATS):
            gc.collect()
            t0     = time.perf_counter()
            result = duckdb.sql(duckdb_sql).fetchall()
            duckdb_times.append((time.perf_counter() - t0) * 1000)
            duckdb_rows = len(result)
        duckdb_avg = round(sum(duckdb_times) / len(duckdb_times), 3)
        results_duckdb[pid] = {
            "avg_ms":  duckdb_avg,
            "min_ms":  round(min(duckdb_times), 3),
            "max_ms":  round(max(duckdb_times), 3),
            "runs_ms": [round(t, 3) for t in duckdb_times],
            "rows":    duckdb_rows,
        }
        winner = "SQLite" if results_with[pid]["avg_ms"] < duckdb_avg else "DuckDB"
        print(f"  [{pid}] {duckdb_avg:.2f}ms → ganador: {winner}")

    # -----------------------------------------------------------------------
    # Ensamblar resultado final
    # -----------------------------------------------------------------------
    for pid in PATTERNS:
        results[pid] = {
            "description":     PATTERNS[pid]["description"],
            "sla_ms":          PATTERNS[pid]["sla_ms"],
            "sqlite_with_idx": results_with[pid],
            "sqlite_no_idx":   results_without[pid],
            "duckdb":          results_duckdb[pid],
            "winner":          "SQLite" if results_with[pid]["avg_ms"] < results_duckdb[pid]["avg_ms"] else "DuckDB",
        }

    return {
        "db_path":      str(db_path),
        "parquet_path": str(parquet_path),
        "repeats":      REPEATS,
        "params":       {k: str(v) for k, v in params.items()},
        "patterns":     results,
    }


# ---------------------------------------------------------------------------
# Resumen en consola
# ---------------------------------------------------------------------------

def print_summary(results: dict) -> None:
    """Imprime tabla resumen de SLAs y ganadores."""
    print(f"\n{'='*65}")
    print("RESUMEN — SQLite vs DuckDB")
    print(f"{'='*65}")
    header = (f"{'Pat':<5} {'SLA(ms)':>8} {'c/idx(ms)':>10} "
              f"{'s/idx(ms)':>10} {'DuckDB(ms)':>11} {'SLA':>5} {'Ganador':>8}")
    print(header)
    print("-" * len(header))

    for pid, pdata in results["patterns"].items():
        wi  = pdata["sqlite_with_idx"]["avg_ms"]
        wo  = pdata["sqlite_no_idx"]["avg_ms"]
        dk  = pdata["duckdb"]["avg_ms"]
        sla = "✓" if pdata["sqlite_with_idx"]["sla_ok"] else "✗"
        print(
            f"{pid:<5} {pdata['sla_ms']:>8} {wi:>10.2f} "
            f"{wo:>10.2f} {dk:>11.2f} {sla:>5} {pdata['winner']:>8}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark de patrones de acceso SQLite vs DuckDB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python benchmark_queries.py
  python benchmark_queries.py --db ../../data/transactions.db
        """,
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help=f"Ruta a la base SQLite (default: {DB_PATH})",
    )
    parser.add_argument(
        "--parquet",
        type=Path,
        default=PARQUET_PATH,
        help=f"Ruta al Parquet de E1 (default: {PARQUET_PATH})",
    )
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    results = run_benchmark(args.db, args.parquet)
    print_summary(results)

    out_path = RESULTS_DIR / "benchmark_results.json"
    out_path.write_text(
        json.dumps(results, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\nResultados guardados en {out_path}")


if __name__ == "__main__":
    main()