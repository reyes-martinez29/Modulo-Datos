"""
transactions/views.py — Views de la API REST.

Arquitectura de backends (misma lógica que E4, diferente framework):

    DuckDB sobre Parquet → endpoints analíticos (público)
        - GET /analytics/summary
        - GET /analytics/top-merchants

    ORM de Django → endpoints transaccionales (requiere token)
        - GET /users/{user_id}/transactions
        - GET /users/{user_id}/stats
        - POST /transactions/batch

    Solo memoria → health (público)
        - GET /health

Por qué se mantiene la misma división:
    Los datos de analytics son los del Parquet estático del E1 —
    1M filas que DuckDB agrega con column pruning en <50ms.
    Los datos de usuario viven en la base Django que puede recibir
    nuevas transacciones vía batch. Son fuentes distintas con patrones
    de acceso distintos — la misma justificación del E4.

Autenticación:
    - AllowAny   → /health, /analytics/*
    - IsAuthenticated (TokenAuthentication) → /users/*, /batch

    TokenAuthentication de DRF verifica el header:
        Authorization: Token <token_value>
"""

import time
from typing import Optional

from django.db.models import Avg, Count, FloatField, Max, Sum
from django.db.models.functions import Coalesce
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from transactions.models import Transaction
from transactions.serializers import (
    BatchRequestSerializer,
    BatchResponseSerializer,
    MerchantSerializer,
    SummaryResponseSerializer,
    TransactionSerializer,
    UserStatsSerializer,
)
from transactions.services.duckdb import get_duckdb_connection

# Tiempo de arranque del servidor — usado por /health
_SERVER_START = time.monotonic()


# ---------------------------------------------------------------------------
# Paginador personalizado — mismo comportamiento que E4
# ---------------------------------------------------------------------------

class TransactionPagination(PageNumberPagination):
    """
    Paginación por número de página.
    page_size viene de settings.REST_FRAMEWORK['PAGE_SIZE'] (default 20).
    El cliente puede sobreescribir con ?page_size=N (max 100).
    """
    page_size_query_param = "page_size"
    max_page_size         = 100


# ---------------------------------------------------------------------------
# GET /health — solo memoria, siempre público
# ---------------------------------------------------------------------------

class HealthView(APIView):
    """
    Estado del servidor. Nunca consulta la base de datos.
    SLA: <50ms siempre.
    """
    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        return Response({
            "status":         "ok",
            "uptime_seconds": round(time.monotonic() - _SERVER_START, 2),
            "framework":      "django-rest-framework",
        })


# ---------------------------------------------------------------------------
# GET /analytics/summary — DuckDB, público
# ---------------------------------------------------------------------------

class AnalyticsSummaryView(APIView):
    """
    Totales globales: conteo, monto total, promedio, breakdown por país
    y por categoría.

    Backend: DuckDB sobre el Parquet del E1. La conexión es un singleton
    lazy — se inicializa la primera vez que se llama get_duckdb_connection()
    y se reutiliza en todas las requests siguientes.
    """
    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        conn = get_duckdb_connection()

        totals = conn.execute("""
            SELECT
                COUNT(*)    AS total_transactions,
                SUM(amount) AS total_amount,
                AVG(amount) AS avg_amount
            FROM transactions
        """).fetchone()

        by_country = conn.execute("""
            SELECT
                country_code,
                COUNT(*)    AS total_transactions,
                SUM(amount) AS total_amount
            FROM transactions
            GROUP BY country_code
            ORDER BY total_transactions DESC
        """).fetchall()

        by_category = conn.execute("""
            SELECT
                category,
                COUNT(*)    AS total_transactions,
                AVG(amount) AS avg_amount
            FROM transactions
            GROUP BY category
            ORDER BY total_transactions DESC
        """).fetchall()

        data = {
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

        serializer = SummaryResponseSerializer(data)
        return Response(serializer.data)


# ---------------------------------------------------------------------------
# GET /analytics/top-merchants — DuckDB, público
# ---------------------------------------------------------------------------

class AnalyticsTopMerchantsView(APIView):
    """
    Top N merchants por volumen. Acepta ?limit=N y ?country=XX.
    Backend: DuckDB sobre Parquet.
    """
    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        # Validar parámetros de query
        try:
            limit = int(request.query_params.get("limit", 10))
            if not 1 <= limit <= 100:
                raise ValueError
        except (TypeError, ValueError):
            return Response(
                {"detail": "limit debe ser un entero entre 1 y 100"},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        country: Optional[str] = request.query_params.get("country")
        if country:
            country = country.upper()

        conn = get_duckdb_connection()

        if country:
            rows = conn.execute("""
                SELECT
                    merchant_id,
                    SUM(amount) AS total_amount,
                    COUNT(*)    AS transaction_count
                FROM transactions
                WHERE country_code = ?
                GROUP BY merchant_id
                ORDER BY total_amount DESC
                LIMIT ?
            """, [country, limit]).fetchall()
        else:
            rows = conn.execute("""
                SELECT
                    merchant_id,
                    SUM(amount) AS total_amount,
                    COUNT(*)    AS transaction_count
                FROM transactions
                GROUP BY merchant_id
                ORDER BY total_amount DESC
                LIMIT ?
            """, [limit]).fetchall()

        merchants = [
            {
                "merchant_id":       r[0],
                "total_amount":      round(r[1], 2),
                "transaction_count": r[2],
            }
            for r in rows
        ]

        return Response({
            "merchants": MerchantSerializer(merchants, many=True).data,
            "limit":     limit,
            "country":   country,
        })


# ---------------------------------------------------------------------------
# GET /users/{user_id}/transactions — ORM, requiere token
# ---------------------------------------------------------------------------

class UserTransactionsView(APIView):
    """
    Últimas transacciones de un usuario con paginación.

    Retorna 404 si el user_id no tiene ninguna transacción en la base.
    Esta es la misma regla de negocio del E4: ausencia de transacciones
    equivale a usuario no encontrado.

    Backend: ORM de Django con el índice idx_user_timestamp.
    SLA: <80ms.
    """
    permission_classes = [IsAuthenticated]
    pagination_class   = TransactionPagination

    def get(self, request: Request, user_id: int) -> Response:
        # Verificar que el usuario tiene transacciones antes de paginar
        if not Transaction.objects.filter(user_id=user_id).exists():
            raise NotFound(detail=f"Usuario {user_id} no encontrado")

        queryset = (
            Transaction.objects
            .filter(user_id=user_id)
            .order_by("-timestamp")
        )

        # Paginación manual para poder usar la clase de paginación
        paginator = TransactionPagination()
        page      = paginator.paginate_queryset(queryset, request, view=self)

        if page is not None:
            serializer = TransactionSerializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)

        serializer = TransactionSerializer(queryset, many=True)
        return Response(serializer.data)


# ---------------------------------------------------------------------------
# GET /users/{user_id}/stats — ORM, requiere token
# ---------------------------------------------------------------------------

class UserStatsView(APIView):
    """
    Estadísticas de un usuario: total, conteo, categoría y país más frecuentes.

    Usa tres queries ORM con el índice idx_user_timestamp — todas filtradas
    por user_id, por lo que el índice reduce el trabajo a las filas del usuario.

    Retorna 404 si el user_id no existe.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request: Request, user_id: int) -> Response:
        # Totales
        totals = (
            Transaction.objects
            .filter(user_id=user_id)
            .aggregate(
                total_amount      = Coalesce(Sum("amount"), 0.0, output_field=FloatField()),
                transaction_count = Count("transaction_id"),
            )
        )

        if totals["transaction_count"] == 0:
            raise NotFound(detail=f"Usuario {user_id} no encontrado")

        # Categoría más frecuente
        top_cat = (
            Transaction.objects
            .filter(user_id=user_id)
            .values("category")
            .annotate(cnt=Count("category"))
            .order_by("-cnt")
            .first()
        )

        # País más frecuente
        top_country = (
            Transaction.objects
            .filter(user_id=user_id)
            .values("country_code")
            .annotate(cnt=Count("country_code"))
            .order_by("-cnt")
            .first()
        )

        data = {
            "user_id":           user_id,
            "total_amount":      round(totals["total_amount"], 2),
            "transaction_count": totals["transaction_count"],
            "top_category":      top_cat["category"] if top_cat else None,
            "top_country":       top_country["country_code"] if top_country else None,
        }

        serializer = UserStatsSerializer(data)
        return Response(serializer.data)


# ---------------------------------------------------------------------------
# POST /transactions/batch — ORM, requiere token
# ---------------------------------------------------------------------------

class TransactionBatchView(APIView):
    """
    Inserta hasta 500 transacciones con deduplicación por transaction_id.

    Flujo:
        1. BatchRequestSerializer valida el payload — retorna 422 si inválido
           (via custom_exception_handler en exceptions.py)
        2. Extrae los transaction_id del batch y consulta cuáles ya existen
        3. Filtra solo las nuevas
        4. Transaction.objects.bulk_create con ignore_conflicts=True
        5. Invalida el cache analítico (si existiera — aquí no hay cache,
           pero se registra el patrón para E8)
        6. Retorna conteo de received, inserted y duplicates_skipped

    Por qué bulk_create y no create() en loop:
        Con 500 filas, create() en loop hace 500 INSERT individuales.
        bulk_create hace un solo INSERT con todos los valores — óptimo
        para lotes. ignore_conflicts=True maneja duplicados sin excepciones.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        serializer = BatchRequestSerializer(data=request.data)
        # is_valid(raise_exception=True) lanza ValidationError → 422
        serializer.is_valid(raise_exception=True)

        validated = serializer.validated_data["transactions"]
        received  = len(validated)

        # Detectar duplicados consultando cuáles IDs ya existen
        ids       = [t["transaction_id"] for t in validated]
        existing  = set(
            Transaction.objects
            .filter(transaction_id__in=ids)
            .values_list("transaction_id", flat=True)
        )

        new_transactions = [t for t in validated if t["transaction_id"] not in existing]
        duplicates       = received - len(new_transactions)

        if new_transactions:
            objs = [
                Transaction(
                    transaction_id = t["transaction_id"],
                    timestamp      = t["timestamp"].strftime("%Y-%m-%d %H:%M:%S"),
                    user_id        = t["user_id"],
                    merchant_id    = t["merchant_id"],
                    amount         = t["amount"],
                    category       = t["category"],
                    country_code   = t["country_code"],
                    status         = t["status"],
                )
                for t in new_transactions
            ]
            # ignore_conflicts=True: si hay duplicados que se colaron por
            # concurrencia, SQLite los ignora sin lanzar excepción
            Transaction.objects.bulk_create(objs, ignore_conflicts=True)

        result = {
            "received":          received,
            "inserted":          len(new_transactions),
            "duplicates_skipped": duplicates,
        }
        return Response(
            BatchResponseSerializer(result).data,
            status=status.HTTP_201_CREATED,
        )