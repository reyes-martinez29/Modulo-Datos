"""
tests/conftest.py — Fixtures compartidas para la suite del E8.

Estrategia de testing:
    Los tests no dependen de Docker ni del dataset real de 1M filas. Cada
    test construye una base SQLite temporal con un dataset pequeño y
    controlado (incluyendo anomalías conocidas), de modo que los resultados
    esperados se puedan afirmar con exactitud.

    Para los endpoints de analytics, que en producción usan DuckDB con la
    extensión sqlite_scanner (ATTACH sobre la base SQLite), los tests usan
    un puente pandas: registran la tabla SQLite como una vista DuckDB. Es
    equivalente funcional al ATTACH y permite correr la suite en cualquier
    entorno sin descargar la extensión. La diferencia está documentada en
    decisions.md.
"""

import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta

import duckdb
import pandas as pd
import pytest

# Las env vars deben existir antes de importar app.config / app.main.
_tmp_parquet = tempfile.mktemp(suffix=".parquet")
_tmp_db = tempfile.mktemp(suffix=".db")


def _build_dataset(parquet_path: str, db_path: str) -> None:
    """
    Construye un dataset de prueba determinista:
        - Parquet histórico: 200 filas de 2024 (fuera de la ventana de 30 días)
        - SQLite: copia del histórico + transacciones recientes con anomalías
          conocidas (user 7 con 8 fallidas, user 42 con 6, user 99 con 2)
    """
    categories = ["Food", "Travel", "Electronics", "Health", "Entertainment",
                  "Retail", "Transport", "Education", "Services", "Other"]
    countries = ["MX", "CO", "BR", "AR", "CL", "PE", "EC",
                 "VE", "BO", "PY", "UY", "CR", "GT", "PA", "DO"]

    import random
    rng = random.Random(42)
    n_hist = 200
    hist = pd.DataFrame({
        "transaction_id": [f"txn-{i:08d}" for i in range(n_hist)],
        "timestamp": pd.date_range("2024-01-01", periods=n_hist, freq="1D").strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": [rng.randint(1, 100) for _ in range(n_hist)],
        "merchant_id": [rng.randint(1, 50) for _ in range(n_hist)],
        "amount": [round(rng.uniform(1, 5000), 2) for _ in range(n_hist)],
        "category": [rng.choice(categories) for _ in range(n_hist)],
        "country_code": [rng.choice(countries) for _ in range(n_hist)],
        "status": [rng.choice(["completed", "failed", "pending"]) for _ in range(n_hist)],
    })
    hist.to_parquet(parquet_path)

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE transactions (
            transaction_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL,
            user_id INTEGER NOT NULL, merchant_id INTEGER NOT NULL,
            amount REAL NOT NULL, category TEXT NOT NULL,
            country_code TEXT NOT NULL, status TEXT NOT NULL)
    """)
    hist.to_sql("transactions", conn, index=False, if_exists="append")

    now = datetime.now()
    recientes = []
    for i in range(8):  # user 7: 8 fallidas recientes
        recientes.append((f"recent-f7-{i:03d}", (now - timedelta(days=i * 3)).strftime("%Y-%m-%d %H:%M:%S"),
                          7, 100, 99.99, "Retail", "MX", "failed"))
    for i in range(6):  # user 42: 6 fallidas recientes
        recientes.append((f"recent-f42-{i:03d}", (now - timedelta(days=i * 4)).strftime("%Y-%m-%d %H:%M:%S"),
                          42, 200, 150.0, "Travel", "CO", "failed"))
    for i in range(2):  # user 99: 2 fallidas (NO anómalo con N=5)
        recientes.append((f"recent-f99-{i:03d}", (now - timedelta(days=i * 5)).strftime("%Y-%m-%d %H:%M:%S"),
                          99, 300, 50.0, "Food", "BR", "failed"))
    conn.executemany(
        "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)", recientes
    )
    conn.execute("CREATE INDEX idx_user_timestamp ON transactions(user_id, timestamp DESC)")
    conn.commit()
    conn.close()


# Construir el dataset una vez al cargar el módulo, antes de importar la app.
_build_dataset(_tmp_parquet, _tmp_db)
os.environ["PARQUET_PATH"] = _tmp_parquet
os.environ["DB_PATH"] = _tmp_db
os.environ["ANALYTICS_TTL"] = "300"
os.environ["DEFAULT_ANOMALY_THRESHOLD"] = "5"


@pytest.fixture(scope="session")
def client():
    """
    TestClient de la app FastAPI con el puente pandas para analytics.

    Parchea init_connections y _unified_cte de app.db para usar una vista
    pandas en lugar del ATTACH de sqlite_scanner (no disponible sin red).
    """
    import app.db as db

    def patched_init(parquet_path, db_path):
        import time
        db._parquet_path = parquet_path
        db._db_path = db_path
        db._start_time = time.monotonic()
        conn = duckdb.connect(":memory:")
        sconn = sqlite3.connect(db_path)
        txn_df = pd.read_sql("SELECT * FROM transactions", sconn)
        sconn.close()
        conn.register("txn_table_global", txn_df)
        db._duckdb_conn = conn

    def patched_cte():
        return """
            WITH unified AS (
                SELECT transaction_id,timestamp,user_id,merchant_id,amount,category,country_code,status
                FROM txn_table_global
            )
        """

    db._analytics_cte = patched_cte

    import app.main as main
    main.init_connections = patched_init

    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def valid_csv_row():
    """Genera una fila CSV válida con UUID4 único."""
    def _make():
        return f"{uuid.uuid4()},2025-06-10 10:00:00,5,5,75.50,Retail,CO,completed"
    return _make


CSV_HEADER = "transaction_id,timestamp,user_id,merchant_id,amount,category,country_code,status"