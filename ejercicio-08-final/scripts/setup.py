"""
scripts/setup.py — Servicio 'setup' de docker-compose para el E8.

Implementa el Modelo A de la arquitectura de datos: SQLite es la fuente de
verdad transaccional viva. Este script copia el historico del Parquet (1M
transacciones) a SQLite UNA SOLA VEZ al levantar el sistema. A partir de ahi,
el pipeline CSV y el endpoint batch escriben en SQLite, que queda con el
historico completo mas todo lo nuevo.

El Parquet es el snapshot historico inmutable; su rol es alimentar este setup
inicial. En runtime, analytics consulta SQLite via DuckDB (no el Parquet),
asi que no hay doble conteo.

Idempotencia: si DB_PATH ya existe, no hace nada y termina con codigo 0.
Esto permite que 'depends_on: service_completed_successfully' funcione sin
recrear la base en cada arranque.

La copia Parquet -> SQLite se hace en chunks de 20000 filas (mismo tamano que
ingest.py del E3) para no cargar 1M tuplas en memoria de una sola vez.
"""

import os
import sqlite3
import sys

import duckdb


def main() -> int:
    parquet_path = os.environ.get("PARQUET_PATH")
    db_path = os.environ.get("DB_PATH")

    if not parquet_path:
        print("ERROR: PARQUET_PATH no esta definida.", file=sys.stderr)
        return 1
    if not db_path:
        print("ERROR: DB_PATH no esta definida.", file=sys.stderr)
        return 1

    if os.path.exists(db_path):
        print(f"setup: {db_path} ya existe -- nada que hacer (idempotente).")
        return 0

    if not os.path.exists(parquet_path):
        print(f"ERROR: PARQUET_PATH='{parquet_path}' no existe.", file=sys.stderr)
        return 1

    print(f"setup: leyendo {parquet_path} con DuckDB...")
    conn = duckdb.connect(":memory:")
    conn.execute(
        f"CREATE VIEW transactions AS SELECT * FROM read_parquet('{parquet_path}')"
    )
    row_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    print(f"setup: {row_count} filas en el Parquet historico.")

    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    print(f"setup: creando {db_path} (SQLite, fuente de verdad viva)...")
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

    print("setup: copiando historico Parquet -> SQLite en chunks de 20000...")
    CHUNK = 20_000
    offset = 0
    total = 0
    while True:
        rows = conn.execute(f"""
            SELECT transaction_id, CAST(timestamp AS VARCHAR), user_id, merchant_id,
                   amount, category, country_code, status
            FROM transactions
            LIMIT {CHUNK} OFFSET {offset}
        """).fetchall()
        if not rows:
            break
        sqlite_conn.executemany(
            """INSERT OR IGNORE INTO transactions
               (transaction_id, timestamp, user_id, merchant_id,
                amount, category, country_code, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        total += len(rows)
        offset += CHUNK
        print(f"setup: {total} filas copiadas...")

    # Indices del E3 — mismos nombres, para lookups por usuario sub-milisegundo.
    print("setup: creando indices idx_user_timestamp e idx_country_user...")
    sqlite_conn.execute(
        "CREATE INDEX idx_user_timestamp ON transactions(user_id, timestamp DESC)"
    )
    sqlite_conn.execute(
        "CREATE INDEX idx_country_user ON transactions(country_code, user_id)"
    )
    sqlite_conn.commit()

    inserted = sqlite_conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    sqlite_conn.close()
    conn.close()

    print(f"setup: completado. {inserted} filas en {db_path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())