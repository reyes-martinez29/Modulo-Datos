"""
scripts/setup.py — Servicio 'setup' de docker-compose.

Corre UNA SOLA VEZ antes de que arranque 'api'. Su trabajo es asegurar que
exista la base SQLite (DB_PATH) generada a partir del Parquet (PARQUET_PATH),
con el mismo schema e índice idx_user_timestamp que diseñó el E3 y que
ingest.py usa en el flujo normal del repo.

Por qué este script existe (y no se reutiliza ingest.py directamente):
    ingest.py vive en ejercicio-03-sqlite/ y depende de argumentos/paths
    propios de ese ejercicio (--wal, --chunk-size, rutas relativas al
    repo en disco del desarrollador). Dentro del contenedor el contexto
    es distinto: solo existen PARQUET_PATH y DB_PATH como variables de
    entorno y un volumen compartido. Replicar la lógica mínima aquí
    (leer Parquet -> escribir SQLite -> crear índice -> WAL) evita
    acoplar el contenedor a la estructura de carpetas del repo completo.

Idempotencia:
    Si DB_PATH ya existe (por ejemplo, en una segunda corrida de
    'docker compose up'), el script no hace nada y termina con código 0.
    Esto permite que 'depends_on: condition: service_completed_successfully'
    funcione de forma predecible sin recrear la base en cada arranque.
"""

import os
import sqlite3
import sys

import duckdb


def main() -> int:
    parquet_path = os.environ.get("PARQUET_PATH")
    db_path = os.environ.get("DB_PATH")

    if not parquet_path:
        print("ERROR: PARQUET_PATH no está definida.", file=sys.stderr)
        return 1
    if not db_path:
        print("ERROR: DB_PATH no está definida.", file=sys.stderr)
        return 1

    if os.path.exists(db_path):
        print(f"setup: {db_path} ya existe -- nada que hacer (idempotente).")
        return 0

    if not os.path.exists(parquet_path):
        print(f"ERROR: PARQUET_PATH='{parquet_path}' no existe.", file=sys.stderr)
        print(
            "Genera el Parquet con ejercicio-01-formatos/benchmark_cli.py "
            "antes de levantar el contenedor.",
            file=sys.stderr,
        )
        return 1

    print(f"setup: leyendo {parquet_path} con DuckDB...")
    conn = duckdb.connect(":memory:")
    conn.execute(
        f"CREATE VIEW transactions AS SELECT * FROM read_parquet('{parquet_path}')"
    )
    row_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    print(f"setup: {row_count} filas encontradas en el Parquet.")

    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    print(f"setup: creando {db_path}...")
    sqlite_conn = sqlite3.connect(db_path)
    sqlite_conn.execute("PRAGMA journal_mode=WAL")

    sqlite_conn.execute("""
        CREATE TABLE transactions (
            transaction_id TEXT PRIMARY KEY,
            timestamp      TEXT NOT NULL,
            user_id        INTEGER NOT NULL,
            merchant_id    INTEGER NOT NULL,
            amount         REAL NOT NULL,
            category       TEXT NOT NULL,
            country_code   TEXT NOT NULL,
            status         TEXT NOT NULL
        )
    """)

    print("setup: copiando filas del Parquet a SQLite en chunks de 20000...")
    # Chunking explícito -- igual que ingest.py del E3 (--chunk-size 20000) y
    # load_transactions del E5 (bulk_create batch_size=10000). Traer 1M filas
    # con un solo fetchall() cargaría ~1M tuplas de 8 columnas en memoria de
    # Python de una sola vez -- innecesario y arriesgado en un contenedor con
    # límites de memoria. DuckDB soporta LIMIT/OFFSET sobre la vista del
    # Parquet para paginar la lectura.
    CHUNK_SIZE = 20_000
    offset = 0
    total_inserted = 0
    while True:
        rows = conn.execute(f"""
            SELECT transaction_id, CAST(timestamp AS VARCHAR), user_id, merchant_id,
                   amount, category, country_code, status
            FROM transactions
            LIMIT {CHUNK_SIZE} OFFSET {offset}
        """).fetchall()

        if not rows:
            break

        sqlite_conn.executemany(
            """
            INSERT OR IGNORE INTO transactions
                (transaction_id, timestamp, user_id, merchant_id,
                 amount, category, country_code, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        total_inserted += len(rows)
        offset += CHUNK_SIZE
        print(f"setup: {total_inserted} filas procesadas...")

    print("setup: creando idx_user_timestamp (mismo índice diseñado en E3)...")
    sqlite_conn.execute(
        "CREATE INDEX idx_user_timestamp ON transactions(user_id, timestamp DESC)"
    )

    sqlite_conn.commit()

    inserted = sqlite_conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    sqlite_conn.close()
    conn.close()

    print(f"setup: completado. {inserted} filas insertadas en {db_path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())