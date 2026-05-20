"""
tests/test_api.py — Suite de tests del sistema de transacciones.

Cómo correr:
    pytest tests/                          # todos los tests
    pytest tests/ -v                       # con output detallado
    pytest tests/ -k "test_health"         # solo un test
    pytest tests/ --tb=short               # traceback corto en fallos

Estrategia de testing:
    Se usa TestClient de FastAPI (basado en httpx) que levanta la app
    en modo síncrono sin necesitar un servidor real. Esto hace los tests
    rápidos y deterministas.

    El fixture 'client' sobreescribe las variables de entorno para apuntar
    a los archivos reales de datos (el mismo Parquet y SQLite que usa el
    servidor en producción). Si los archivos no existen, los tests que
    dependen de la DB son skipeados automáticamente con un mensaje claro.

Tests incluidos (mínimo 8 requeridos por el enunciado):
     1. test_health_ok                   — GET /health retorna 200
     2. test_analytics_summary_ok        — estructura correcta del response
     3. test_analytics_top_merchants_ok  — respeta el parámetro limit
     4. test_analytics_top_merchants_country — filtro por country funciona
     5. test_user_transactions_ok        — usuario real retorna transacciones
     6. test_user_not_found              — usuario inexistente retorna 404
     7. test_user_stats_ok               — estructura de stats correcta
     8. test_batch_invalid_schema        — campo inválido retorna 422
     9. test_batch_ok                    — batch válido retorna 200 con conteos
    10. test_analytics_cache_warm        — segunda llamada cumple SLA warm (<20ms)
    11. test_pagination_out_of_range     — página fuera de rango retorna lista vacía
    12. test_health_sla                  — /health cumple SLA de <50ms
"""

import os
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Configurar rutas antes de importar la app
PARQUET_PATH = os.getenv(
    "PARQUET_PATH",
    str(Path(__file__).parent.parent.parent / "data" / "transactions_1m_parquet_snappy.parquet"),
)
DB_PATH = os.getenv(
    "DB_PATH",
    str(Path(__file__).parent.parent.parent / "data" / "transactions.db"),
)

os.environ["PARQUET_PATH"] = PARQUET_PATH
os.environ["DB_PATH"]      = DB_PATH

from app.main import app  # noqa: E402 — el import va después de setear env vars


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """
    Cliente de test que levanta la app completa con el lifespan real.
    scope="module" significa que la app se levanta una vez por módulo de test,
    no una vez por test — lo que hace la suite significativamente más rápida.
    """
    data_available = Path(PARQUET_PATH).exists() and Path(DB_PATH).exists()
    if not data_available:
        pytest.skip(
            "Datos no disponibles. Corre primero:\n"
            "  python generate_data.py --size 1m  (en ejercicio-01-formatos/)\n"
            "  python ingest.py --wal              (en ejercicio-03-sqlite/)"
        )

    with TestClient(app) as c:
        yield c


@pytest.fixture
def valid_transaction():
    """Una transacción válida para usar en tests de batch."""
    return {
        "transaction_id": str(uuid.uuid4()),
        "timestamp":      "2025-06-01T12:00:00",
        "user_id":        1234,
        "merchant_id":    567,
        "amount":         99.99,
        "category":       "Food",
        "country_code":   "MX",
        "status":         "completed",
    }


# ---------------------------------------------------------------------------
# Tests de /health
# ---------------------------------------------------------------------------

def test_health_ok(client):
    """El endpoint /health debe retornar 200 con los campos requeridos."""
    response = client.get("/health")

    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert "uptime_seconds"   in body
    assert "cache_hit_rate"   in body
    assert "cache_hits"       in body
    assert "cache_misses"     in body
    assert "duckdb_connected" in body
    assert "sqlite_connected" in body
    assert body["duckdb_connected"] is True
    assert body["sqlite_connected"] is True


def test_health_sla(client):
    """
    GET /health debe responder en menos de 50ms.

    Este test valida el SLA del endpoint más estricto del sistema.
    /health nunca toca la base de datos, por lo que si tarda más
    de 50ms hay un problema de arquitectura (probablemente alguna
    operación bloqueante en el endpoint).
    """
    SLA_MS = 50

    start    = time.perf_counter()
    response = client.get("/health")
    elapsed  = (time.perf_counter() - start) * 1000

    assert response.status_code == 200
    assert elapsed < SLA_MS, (
        f"/health tardó {elapsed:.1f}ms — SLA es <{SLA_MS}ms"
    )


def test_dev_cache_clear_ok(client):
    """POST /dev/cache/clear debe responder 200 y limpiar cache analítico."""
    response = client.post("/dev/cache/clear")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["cleared_prefix"] == "analytics:"


# ---------------------------------------------------------------------------
# Tests de /analytics/summary
# ---------------------------------------------------------------------------

def test_analytics_summary_ok(client):
    """GET /analytics/summary debe retornar los campos correctos."""
    response = client.get("/analytics/summary")

    assert response.status_code == 200

    body = response.json()
    assert "total_transactions" in body
    assert "total_amount"       in body
    assert "avg_amount"         in body
    assert "by_country"         in body
    assert "by_category"        in body

    # Con 1M transacciones el total debe ser exactamente 1,000,000
    assert body["total_transactions"] == 1_000_000

    # Debe haber exactamente 15 países y 10 categorías
    assert len(body["by_country"])  == 15
    assert len(body["by_category"]) == 10

    # Cada país debe tener los campos esperados
    country = body["by_country"][0]
    assert "country_code"       in country
    assert "total_transactions" in country
    assert "total_amount"       in country


def test_analytics_cache_warm(client):
    """
    La segunda llamada a /analytics/summary debe cumplir el SLA warm (<20ms).

    El primer request llena el cache (puede tardar hasta 500ms cold).
    El segundo request debe leer del cache y tardar <20ms.
    """
    WARM_SLA_MS = 20

    # Primera llamada — puede ser cold (hasta 500ms)
    client.get("/analytics/summary")

    # Segunda llamada — debe ser warm (<20ms)
    start    = time.perf_counter()
    response = client.get("/analytics/summary")
    elapsed  = (time.perf_counter() - start) * 1000

    assert response.status_code == 200
    assert elapsed < WARM_SLA_MS, (
        f"Cache warm tardó {elapsed:.1f}ms — SLA es <{WARM_SLA_MS}ms. "
        "El cache no está funcionando correctamente."
    )


# ---------------------------------------------------------------------------
# Tests de /analytics/top-merchants
# ---------------------------------------------------------------------------

def test_analytics_top_merchants_ok(client):
    """GET /analytics/top-merchants debe respetar el parámetro limit."""
    response = client.get("/analytics/top-merchants?limit=5")

    assert response.status_code == 200

    body = response.json()
    assert "merchants" in body
    assert body["limit"] == 5
    assert len(body["merchants"]) == 5

    # Los merchants deben estar ordenados por total_amount descendente
    amounts = [m["total_amount"] for m in body["merchants"]]
    assert amounts == sorted(amounts, reverse=True)


def test_analytics_top_merchants_country(client):
    """El filtro por country_code debe retornar solo merchants de ese país."""
    response = client.get("/analytics/top-merchants?limit=10&country=MX")

    assert response.status_code == 200

    body = response.json()
    assert body["country"] == "MX"
    assert len(body["merchants"]) > 0


def test_analytics_top_merchants_invalid_limit(client):
    """Un limit fuera de rango debe retornar 422."""
    response = client.get("/analytics/top-merchants?limit=0")
    assert response.status_code == 422

    response = client.get("/analytics/top-merchants?limit=999")
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Tests de /users/{user_id}/transactions
# ---------------------------------------------------------------------------

def test_user_transactions_ok(client):
    """Un usuario real debe retornar sus transacciones correctamente."""
    # user_id=2076 existe en el dataset (verificado en E3)
    response = client.get("/users/2076/transactions")

    assert response.status_code == 200

    body = response.json()
    assert body["user_id"]   == 2076
    assert body["page"]      == 1
    assert body["page_size"] == 20
    assert isinstance(body["transactions"], list)
    assert len(body["transactions"]) > 0

    # Cada transacción debe tener los campos correctos
    tx = body["transactions"][0]
    assert "transaction_id" in tx
    assert "timestamp"      in tx
    assert "amount"         in tx
    assert "category"       in tx
    assert "status"         in tx


def test_user_not_found(client):
    """Un user_id que no existe debe retornar 404."""
    response = client.get("/users/9999999/transactions")
    assert response.status_code == 404
    assert "no encontrado" in response.json()["detail"].lower()


def test_pagination_out_of_range(client):
    """
    Una página fuera del rango del usuario debe retornar lista vacía,
    no un error 500 ni 404. El usuario existe pero esa página no tiene datos.
    """
    # user_id=2076 tiene 43 transacciones. Con page_size=20, page=100 está vacía.
    response = client.get("/users/2076/transactions?page=100&page_size=20")

    assert response.status_code == 200
    assert response.json()["transactions"] == []


# ---------------------------------------------------------------------------
# Tests de /users/{user_id}/stats
# ---------------------------------------------------------------------------

def test_user_stats_ok(client):
    """GET /users/{user_id}/stats debe retornar la estructura correcta."""
    response = client.get("/users/2076/stats")

    assert response.status_code == 200

    body = response.json()
    assert body["user_id"]           == 2076
    assert "total_amount"            in body
    assert "transaction_count"       in body
    assert "top_category"            in body
    assert "top_country"             in body
    assert body["transaction_count"] > 0
    assert body["total_amount"]      > 0


def test_user_stats_not_found(client):
    """Un user_id inexistente en /stats también debe retornar 404."""
    response = client.get("/users/9999999/stats")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Tests de POST /transactions/batch
# ---------------------------------------------------------------------------

def test_batch_invalid_schema(client):
    """
    Un batch con schema inválido debe retornar 422 con detalle del error.

    Casos que deben fallar:
    - amount negativo
    - category fuera del set válido
    - country_code inválido
    - campo requerido faltante
    """
    # amount negativo
    response = client.post("/transactions/batch", json={
        "transactions": [{
            "transaction_id": str(uuid.uuid4()),
            "timestamp":      "2025-06-01T12:00:00",
            "user_id":        1,
            "merchant_id":    1,
            "amount":         -50.0,
            "category":       "Food",
            "country_code":   "MX",
            "status":         "completed",
        }]
    })
    assert response.status_code == 422

    # category inválida
    response = client.post("/transactions/batch", json={
        "transactions": [{
            "transaction_id": str(uuid.uuid4()),
            "timestamp":      "2025-06-01T12:00:00",
            "user_id":        1,
            "merchant_id":    1,
            "amount":         50.0,
            "category":       "Gambling",
            "country_code":   "MX",
            "status":         "completed",
        }]
    })
    assert response.status_code == 422

    # campo faltante (sin amount)
    response = client.post("/transactions/batch", json={
        "transactions": [{
            "transaction_id": str(uuid.uuid4()),
            "timestamp":      "2025-06-01T12:00:00",
            "user_id":        1,
            "merchant_id":    1,
            "category":       "Food",
            "country_code":   "MX",
            "status":         "completed",
        }]
    })
    assert response.status_code == 422


def test_batch_ok(client, valid_transaction):
    """Un batch válido debe insertarse correctamente."""
    # Crear 3 transacciones con IDs únicos para evitar duplicados con runs anteriores
    transactions = [
        {**valid_transaction, "transaction_id": str(uuid.uuid4())}
        for _ in range(3)
    ]

    response = client.post("/transactions/batch", json={"transactions": transactions})

    assert response.status_code == 200

    body = response.json()
    assert body["received"] == 3
    assert body["inserted"] + body["duplicates_skipped"] == 3


def test_batch_deduplication(client, valid_transaction):
    """
    Insertar el mismo transaction_id dos veces debe contar como duplicado,
    no como error ni como doble inserción.
    """
    fixed_id  = str(uuid.uuid4())
    payload   = {"transactions": [{**valid_transaction, "transaction_id": fixed_id}]}

    # Primera inserción
    r1 = client.post("/transactions/batch", json=payload)
    assert r1.status_code == 200
    assert r1.json()["inserted"] == 1

    # Segunda inserción — debe ser duplicado
    r2 = client.post("/transactions/batch", json=payload)
    assert r2.status_code == 200
    assert r2.json()["duplicates_skipped"] == 1
    assert r2.json()["inserted"] == 0


def test_batch_empty_list(client):
    """Un batch con lista vacía debe retornar 422 por el validador de Pydantic."""
    response = client.post("/transactions/batch", json={"transactions": []})
    assert response.status_code == 422


def test_batch_over_limit(client, valid_transaction):
    """Un batch de más de 500 transacciones debe retornar 422."""
    transactions = [
        {**valid_transaction, "transaction_id": str(uuid.uuid4())}
        for _ in range(501)
    ]
    response = client.post("/transactions/batch", json={"transactions": transactions})
    assert response.status_code == 422