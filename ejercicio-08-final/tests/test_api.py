"""
tests/test_api.py — Suite de tests del sistema de monitoreo.

Cubre los casos críticos del negocio, no solo el happy path:
    - Los 9 endpoints responden y con la forma correcta
    - Detección de anomalías con distintos umbrales (la lógica de negocio
      central): el umbral discrimina correctamente quién es anómalo
    - Filtro de fecha en el historial de usuario
    - Pipeline CSV: válidas vs rechazadas, invariantes, idempotencia
    - Validación de estructura del CSV (columna faltante, no-CSV)
    - Códigos de error correctos (404 usuario inexistente, 422 datos inválidos)

Los tests usan un dataset determinista construido en conftest.py con
anomalías conocidas, de modo que los resultados esperados son exactos.
"""

import io
import uuid

from tests.conftest import CSV_HEADER


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["duckdb_connected"] is True
    assert body["sqlite_connected"] is True
    assert body["transactions_in_db"] > 0


def test_health_has_performance_metrics(client):
    body = client.get("/health").json()
    assert "uptime_seconds" in body
    assert "cache_hit_rate" in body
    assert "transactions_in_db" in body


# ---------------------------------------------------------------------------
# Analytics — vista unificada
# ---------------------------------------------------------------------------

def test_analytics_summary_unified(client):
    r = client.get("/analytics/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["total_transactions"] == 216
    assert len(body["by_country"]) == 15
    assert len(body["by_category"]) == 10


def test_top_merchants_ok(client):
    r = client.get("/analytics/top-merchants?limit=3")
    assert r.status_code == 200
    merchants = r.json()["merchants"]
    assert len(merchants) <= 3
    amounts = [m["total_amount"] for m in merchants]
    assert amounts == sorted(amounts, reverse=True)


def test_top_merchants_country_filter(client):
    r = client.get("/analytics/top-merchants?limit=5&country=mx")
    assert r.status_code == 200
    assert r.json()["country"] == "MX"


def test_top_merchants_invalid_limit(client):
    assert client.get("/analytics/top-merchants?limit=0").status_code == 422
    assert client.get("/analytics/top-merchants?limit=999").status_code == 422


# ---------------------------------------------------------------------------
# Anomalías — la lógica de negocio central
# ---------------------------------------------------------------------------

def test_anomalies_threshold_5(client):
    r = client.get("/analytics/anomalies?threshold=5")
    assert r.status_code == 200
    body = r.json()
    flagged = {u["user_id"] for u in body["anomalous_users"]}
    assert 7 in flagged
    assert 42 in flagged
    assert 99 not in flagged
    assert body["total_flagged"] == 2


def test_anomalies_threshold_7(client):
    r = client.get("/analytics/anomalies?threshold=7")
    flagged = {u["user_id"] for u in r.json()["anomalous_users"]}
    assert flagged == {7}


def test_anomalies_threshold_high(client):
    r = client.get("/analytics/anomalies?threshold=10")
    assert r.json()["total_flagged"] == 0


def test_anomalies_default_threshold(client):
    r = client.get("/analytics/anomalies")
    assert r.status_code == 200
    assert r.json()["threshold"] == 5


def test_anomalies_invalid_window(client):
    assert client.get("/analytics/anomalies?window_days=0").status_code == 422
    assert client.get("/analytics/anomalies?window_days=999").status_code == 422


# ---------------------------------------------------------------------------
# Usuarios — con filtro de fecha
# ---------------------------------------------------------------------------

def test_user_stats_ok(client):
    r = client.get("/users/7/stats")
    assert r.status_code == 200
    assert r.json()["transaction_count"] > 0


def test_user_stats_not_found(client):
    assert client.get("/users/999999/stats").status_code == 404


def test_user_transactions_date_filter(client):
    from datetime import datetime, timedelta
    date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    r = client.get(f"/users/7/transactions?date_from={date_from}")
    assert r.status_code == 200
    body = r.json()
    assert body["date_from"] == date_from
    assert len(body["transactions"]) == 8


def test_user_transactions_not_found(client):
    assert client.get("/users/999999/transactions").status_code == 404


def test_user_transactions_invalid_pagination(client):
    assert client.get("/users/7/transactions?page=0").status_code == 422
    assert client.get("/users/7/transactions?page_size=999").status_code == 422


# ---------------------------------------------------------------------------
# Batch (del E4)
# ---------------------------------------------------------------------------

def test_batch_insert_and_dedup(client):
    tid = str(uuid.uuid4())
    payload = {"transactions": [{
        "transaction_id": tid, "timestamp": "2025-06-01T12:00:00",
        "user_id": 1, "merchant_id": 1, "amount": 99.99,
        "category": "Food", "country_code": "MX", "status": "completed"}]}
    r1 = client.post("/transactions/batch", json=payload)
    assert r1.status_code == 200
    assert r1.json()["inserted"] == 1
    r2 = client.post("/transactions/batch", json=payload)
    assert r2.json()["inserted"] == 0
    assert r2.json()["duplicates_skipped"] == 1


def test_batch_invalid_amount(client):
    payload = {"transactions": [{
        "transaction_id": str(uuid.uuid4()), "timestamp": "2025-06-01T12:00:00",
        "user_id": 1, "merchant_id": 1, "amount": -50.0,
        "category": "Food", "country_code": "MX", "status": "completed"}]}
    assert client.post("/transactions/batch", json=payload).status_code == 422


def test_batch_invalid_category(client):
    payload = {"transactions": [{
        "transaction_id": str(uuid.uuid4()), "timestamp": "2025-06-01T12:00:00",
        "user_id": 1, "merchant_id": 1, "amount": 99.99,
        "category": "Gambling", "country_code": "MX", "status": "completed"}]}
    assert client.post("/transactions/batch", json=payload).status_code == 422


def test_batch_over_500(client):
    one = {"transaction_id": str(uuid.uuid4()), "timestamp": "2025-06-01T12:00:00",
           "user_id": 1, "merchant_id": 1, "amount": 99.99,
           "category": "Food", "country_code": "MX", "status": "completed"}
    payload = {"transactions": [one] * 501}
    assert client.post("/transactions/batch", json=payload).status_code == 422


# ---------------------------------------------------------------------------
# Pipeline CSV — el componente nuevo del E8
# ---------------------------------------------------------------------------

def _csv_bytes(rows):
    return io.BytesIO(("\n".join([CSV_HEADER] + rows)).encode("utf-8"))


def test_ingest_csv_valid_and_invalid(client):
    rows = [f"{uuid.uuid4()},2025-06-10 10:00:00,5,5,75.50,Retail,CO,completed" for _ in range(4)]
    rows.append(f"{uuid.uuid4()},2025-06-10 10:00:00,5,5,-10.0,Retail,CO,completed")
    r = client.post("/pipeline/ingest", files={"file": ("t.csv", _csv_bytes(rows), "text/csv")})
    assert r.status_code == 200
    body = r.json()
    assert body["rows_in_csv"] == 5
    assert body["valid"] == 4
    assert body["rejected"] == 1
    assert body["inserted"] == 4


def test_ingest_csv_invariants(client):
    rows = [f"{uuid.uuid4()},2025-06-10 10:00:00,5,5,75.50,Retail,CO,completed" for _ in range(3)]
    r = client.post("/pipeline/ingest", files={"file": ("t.csv", _csv_bytes(rows), "text/csv")})
    assert all(r.json()["invariants"].values())


def test_ingest_csv_idempotent(client):
    tid = str(uuid.uuid4())
    rows = [f"{tid},2025-06-10 10:00:00,5,5,75.50,Retail,CO,completed"]
    r1 = client.post("/pipeline/ingest", files={"file": ("t.csv", _csv_bytes(rows), "text/csv")})
    assert r1.json()["inserted"] == 1
    r2 = client.post("/pipeline/ingest", files={"file": ("t.csv", _csv_bytes(rows), "text/csv")})
    assert r2.json()["inserted"] == 0
    assert r2.json()["duplicates"] == 1


def test_ingest_csv_missing_column(client):
    bad = io.BytesIO(
        b"transaction_id,timestamp,user_id,merchant_id,category,country_code,status\n"
        b"x,2025-06-10 10:00:00,5,5,Retail,CO,completed"
    )
    r = client.post("/pipeline/ingest", files={"file": ("bad.csv", bad, "text/csv")})
    assert r.status_code == 422
    assert "amount" in r.json()["detail"]


def test_ingest_rejects_non_csv(client):
    r = client.post("/pipeline/ingest",
                    files={"file": ("data.txt", io.BytesIO(b"hola"), "text/plain")})
    assert r.status_code == 422


def test_ingest_empty_csv(client):
    empty = io.BytesIO(CSV_HEADER.encode("utf-8"))
    r = client.post("/pipeline/ingest", files={"file": ("empty.csv", empty, "text/csv")})
    assert r.status_code == 422