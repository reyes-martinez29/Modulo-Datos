"""
tests/test_api.py — Suite de tests de la API Django REST Framework.

Cómo correr:
    python manage.py test tests                    # todos los tests
    python manage.py test tests.test_api.HealthTests   # una clase
    pytest tests/ -v                               # con pytest (requiere pytest-django)

Estrategia de testing:
    Se usa APIClient de DRF, que permite hacer requests directamente
    contra las views de Django sin necesitar un servidor real. Esto hace
    los tests rápidos y deterministas.

    El setUp de cada clase crea los datos mínimos necesarios usando
    el ORM directamente — no depende de que load_transactions haya corrido.
    Esto garantiza que los tests funcionan en una base limpia.

    Para los endpoints de analytics (DuckDB sobre Parquet), los tests
    verifican la estructura del response pero no los valores exactos,
    porque el Parquet puede no estar disponible en el entorno de CI.
    Si el Parquet no existe, esos tests se skipean con un mensaje claro.

Tests incluidos (mínimo 6 requeridos):
     1. test_health_ok                 — GET /health retorna 200 sin token
     2. test_analytics_summary_public  — GET /analytics/summary sin token retorna 200
     3. test_analytics_top_merchants   — GET /analytics/top-merchants respeta limit
     4. test_user_transactions_401     — GET /users/X/transactions sin token retorna 401
     5. test_user_transactions_ok      — GET /users/X/transactions con token retorna 200
     6. test_user_not_found            — GET /users/9999999/transactions retorna 404
     7. test_user_stats_ok             — GET /users/X/stats con token retorna estructura correcta
     8. test_user_stats_401            — GET /users/X/stats sin token retorna 401
     9. test_batch_401                 — POST /transactions/batch sin token retorna 401
    10. test_batch_invalid_schema      — batch con campo inválido retorna 422
    11. test_batch_ok                  — batch válido retorna 201 con conteos
    12. test_batch_deduplication       — mismo ID dos veces cuenta como duplicado
    13. test_batch_empty_list          — lista vacía retorna 422
    14. test_batch_over_limit          — más de 500 retorna 422
    15. test_obtain_token              — POST /auth/token/ retorna token con credenciales válidas
"""

import uuid
from pathlib import Path
from unittest import skip

from django.contrib.auth.models import User
from django.test import TestCase
from django.conf import settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from transactions.models import Transaction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_transaction(**kwargs) -> dict:
    """Retorna un dict con una transacción válida, con campos sobreescribibles."""
    defaults = {
        "transaction_id": str(uuid.uuid4()),
        "timestamp":      "2025-06-01 12:00:00",
        "user_id":        1234,
        "merchant_id":    567,
        "amount":         99.99,
        "category":       "Food",
        "country_code":   "MX",
        "status":         "completed",
    }
    defaults.update(kwargs)
    return defaults


def make_transaction_obj(**kwargs) -> Transaction:
    """Crea y retorna una instancia de Transaction en la base de test."""
    return Transaction.objects.create(**make_transaction(**kwargs))


# ---------------------------------------------------------------------------
# Tests de GET /health
# ---------------------------------------------------------------------------

class HealthTests(TestCase):

    def setUp(self):
        self.client = APIClient()

    def test_health_ok(self):
        """GET /health debe retornar 200 sin autenticación."""
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "ok")
        self.assertIn("uptime_seconds", response.data)

    def test_health_no_auth_required(self):
        """GET /health no debe requerir token — es un endpoint público."""
        # Sin ningún header de Authorization
        response = self.client.get("/api/v1/health")
        self.assertNotEqual(response.status_code, 401)


# ---------------------------------------------------------------------------
# Tests de /analytics/* — DuckDB, público
# ---------------------------------------------------------------------------

def parquet_available() -> bool:
    """Retorna True si el Parquet del E1 existe en la ruta configurada."""
    return Path(settings.PARQUET_PATH).exists()


class AnalyticsTests(TestCase):

    def setUp(self):
        self.client = APIClient()

    def test_analytics_summary_public(self):
        """
        GET /analytics/summary debe ser accesible sin token.
        Si el Parquet no está disponible se skipea con mensaje claro.
        """
        if not parquet_available():
            self.skipTest(
                f"Parquet no disponible en {settings.PARQUET_PATH}. "
                "Corre el E1 para generarlo."
            )
        response = self.client.get("/api/v1/analytics/summary")
        self.assertEqual(response.status_code, 200)
        self.assertIn("total_transactions", response.data)
        self.assertIn("by_country",         response.data)
        self.assertIn("by_category",        response.data)

    def test_analytics_top_merchants_public(self):
        """GET /analytics/top-merchants debe ser público y respetar ?limit."""
        if not parquet_available():
            self.skipTest(f"Parquet no disponible en {settings.PARQUET_PATH}.")
        response = self.client.get("/api/v1/analytics/top-merchants?limit=5")
        self.assertEqual(response.status_code, 200)
        self.assertIn("merchants", response.data)
        self.assertEqual(response.data["limit"], 5)
        self.assertLessEqual(len(response.data["merchants"]), 5)

    def test_analytics_top_merchants_invalid_limit(self):
        """limit fuera de rango debe retornar 422."""
        if not parquet_available():
            self.skipTest(f"Parquet no disponible en {settings.PARQUET_PATH}.")
        response = self.client.get("/api/v1/analytics/top-merchants?limit=0")
        self.assertEqual(response.status_code, 422)


# ---------------------------------------------------------------------------
# Tests de /users/* — ORM, requiere token
# ---------------------------------------------------------------------------

class UserTests(TestCase):

    def setUp(self):
        self.client = APIClient()

        # Crear usuario y token para los tests autenticados
        self.user  = User.objects.create_user(
            username="testuser",
            password="testpass123",
        )
        self.token = Token.objects.create(user=self.user)

        # Crear transacciones de prueba para user_id=42
        for i in range(5):
            make_transaction_obj(
                transaction_id = str(uuid.uuid4()),
                user_id        = 42,
                amount         = 100.0 + i,
                status         = "completed" if i % 2 == 0 else "failed",
            )

    def test_user_transactions_401(self):
        """GET /users/42/transactions sin token debe retornar 401."""
        response = self.client.get("/api/v1/users/42/transactions")
        self.assertEqual(response.status_code, 401)

    def test_user_transactions_ok(self):
        """GET /users/42/transactions con token debe retornar las transacciones."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        response = self.client.get("/api/v1/users/42/transactions")
        self.assertEqual(response.status_code, 200)
        # La respuesta de DRF paginado tiene 'results'
        results = response.data.get("results", response.data)
        self.assertGreater(len(results), 0)
        # Cada transacción debe tener los campos esperados
        tx = results[0]
        self.assertIn("transaction_id", tx)
        self.assertIn("amount",         tx)
        self.assertIn("status",         tx)

    def test_user_not_found(self):
        """
        GET /users/9999999/transactions con token debe retornar 404.
        Regla de negocio: user_id sin transacciones = usuario no encontrado.
        """
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        response = self.client.get("/api/v1/users/9999999/transactions")
        self.assertEqual(response.status_code, 404)

    def test_user_stats_401(self):
        """GET /users/42/stats sin token debe retornar 401."""
        response = self.client.get("/api/v1/users/42/stats")
        self.assertEqual(response.status_code, 401)

    def test_user_stats_ok(self):
        """GET /users/42/stats con token debe retornar la estructura correcta."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        response = self.client.get("/api/v1/users/42/stats")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["user_id"],           42)
        self.assertIn("total_amount",      response.data)
        self.assertIn("transaction_count", response.data)
        self.assertIn("top_category",      response.data)
        self.assertIn("top_country",       response.data)
        self.assertGreater(response.data["transaction_count"], 0)

    def test_user_stats_not_found(self):
        """GET /users/9999999/stats con token debe retornar 404."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        response = self.client.get("/api/v1/users/9999999/stats")
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Tests de POST /transactions/batch
# ---------------------------------------------------------------------------

class BatchTests(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.user   = User.objects.create_user(
            username="batchuser",
            password="testpass123",
        )
        self.token  = Token.objects.create(user=self.user)

    def _auth(self):
        """Configura el cliente con el token del usuario de test."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")

    def test_batch_401(self):
        """POST /transactions/batch sin token debe retornar 401."""
        response = self.client.post(
            "/api/v1/transactions/batch",
            {"transactions": [make_transaction()]},
            format="json",
        )
        self.assertEqual(response.status_code, 401)

    def test_batch_ok(self):
        """Un batch válido debe insertarse y retornar 201 con conteos."""
        self._auth()
        transactions = [make_transaction() for _ in range(3)]
        response = self.client.post(
            "/api/v1/transactions/batch",
            {"transactions": transactions},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["received"], 3)
        self.assertEqual(
            response.data["inserted"] + response.data["duplicates_skipped"],
            3,
        )

    def test_batch_deduplication(self):
        """
        Insertar el mismo transaction_id dos veces debe contar como duplicado,
        no como error ni como doble inserción.
        """
        self._auth()
        fixed_id = str(uuid.uuid4())
        payload  = {"transactions": [make_transaction(transaction_id=fixed_id)]}

        # Primera inserción
        r1 = self.client.post("/api/v1/transactions/batch", payload, format="json")
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r1.data["inserted"], 1)

        # Segunda inserción — debe ser duplicado
        r2 = self.client.post("/api/v1/transactions/batch", payload, format="json")
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(r2.data["duplicates_skipped"], 1)
        self.assertEqual(r2.data["inserted"], 0)

    def test_batch_invalid_amount(self):
        """amount negativo debe retornar 422."""
        self._auth()
        response = self.client.post(
            "/api/v1/transactions/batch",
            {"transactions": [make_transaction(amount=-50.0)]},
            format="json",
        )
        self.assertEqual(response.status_code, 422)

    def test_batch_invalid_category(self):
        """category fuera del set válido debe retornar 422."""
        self._auth()
        response = self.client.post(
            "/api/v1/transactions/batch",
            {"transactions": [make_transaction(category="Gambling")]},
            format="json",
        )
        self.assertEqual(response.status_code, 422)

    def test_batch_invalid_country(self):
        """country_code inválido debe retornar 422."""
        self._auth()
        response = self.client.post(
            "/api/v1/transactions/batch",
            {"transactions": [make_transaction(country_code="ZZ")]},
            format="json",
        )
        self.assertEqual(response.status_code, 422)

    def test_batch_missing_field(self):
        """Campo requerido faltante debe retornar 422."""
        self._auth()
        tx = make_transaction()
        del tx["amount"]
        response = self.client.post(
            "/api/v1/transactions/batch",
            {"transactions": [tx]},
            format="json",
        )
        self.assertEqual(response.status_code, 422)

    def test_batch_empty_list(self):
        """Lista vacía debe retornar 422 por el validador del serializer."""
        self._auth()
        response = self.client.post(
            "/api/v1/transactions/batch",
            {"transactions": []},
            format="json",
        )
        self.assertEqual(response.status_code, 422)

    def test_batch_over_limit(self):
        """Más de 500 transacciones debe retornar 422."""
        self._auth()
        transactions = [make_transaction() for _ in range(501)]
        response = self.client.post(
            "/api/v1/transactions/batch",
            {"transactions": transactions},
            format="json",
        )
        self.assertEqual(response.status_code, 422)


# ---------------------------------------------------------------------------
# Tests de autenticación
# ---------------------------------------------------------------------------

class AuthTests(TestCase):

    def setUp(self):
        self.client = APIClient()
        User.objects.create_user(username="authuser", password="securepass123")

    def test_obtain_token_ok(self):
        """POST /api/v1/auth/token/ con credenciales válidas retorna token."""
        response = self.client.post(
            "/api/v1/auth/token/",
            {"username": "authuser", "password": "securepass123"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("token", response.data)
        self.assertGreater(len(response.data["token"]), 10)

    def test_obtain_token_invalid(self):
        """
        Credenciales incorrectas deben retornar 422.

        obtain_auth_token de DRF lanza ValidationError cuando las credenciales
        son incorrectas, lo que nuestro custom_exception_handler convierte de
        400 a 422 — consistente con el comportamiento del resto de la API.
        """
        response = self.client.post(
            "/api/v1/auth/token/",
            {"username": "authuser", "password": "wrongpass"},
            format="json",
        )
        self.assertEqual(response.status_code, 422)