"""
transactions/urls.py — Rutas de la app transactions.

Se usa URLconf manual (no Router de DRF) porque las views son APIView,
no ViewSets. El Router de DRF está diseñado para ViewSets con acciones
estándar (list, create, retrieve, update, destroy). Con APIView puras
es más claro y explícito declarar cada path directamente.

Paths registrados:
    GET  /health                          → HealthView
    GET  /analytics/summary               → AnalyticsSummaryView
    GET  /analytics/top-merchants         → AnalyticsTopMerchantsView
    GET  /users/<user_id>/transactions    → UserTransactionsView
    GET  /users/<user_id>/stats           → UserStatsView
    POST /transactions/batch              → TransactionBatchView
"""

from django.urls import path
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.reverse import reverse

from transactions.views import (
    AnalyticsSummaryView,
    AnalyticsTopMerchantsView,
    HealthView,
    TransactionBatchView,
    UserStatsView,
    UserTransactionsView,
)

@api_view(["GET"])
@permission_classes([AllowAny])
def api_root(request, format=None):
    """Vista raíz — lista todos los endpoints disponibles."""
    return Response({
        "health":          reverse("health",          request=request),
        "analytics": {
            "summary":       reverse("analytics-summary",       request=request),
            "top-merchants": reverse("analytics-top-merchants", request=request),
        },
        "users": {
            "transactions":  request.build_absolute_uri("/api/v1/users/2076/transactions"),
            "stats":         request.build_absolute_uri("/api/v1/users/2076/stats"),
            "nota":          "reemplaza 2076 con cualquier user_id (1-50000)",
        },
        "batch":           reverse("transactions-batch", request=request),
        "auth": {
            "token":         request.build_absolute_uri("/api/v1/auth/token/"),
            "login":         request.build_absolute_uri("/api/v1/api-auth/login/"),
        },
    })


urlpatterns = [
    # Vista raíz — lista todos los endpoints
    path("", api_root, name="api-root"),

    # Salud del sistema — público, sin autenticación
    path(
        "health",
        HealthView.as_view(),
        name="health",
    ),

    # Analytics — DuckDB sobre Parquet, públicos
    path(
        "analytics/summary",
        AnalyticsSummaryView.as_view(),
        name="analytics-summary",
    ),
    path(
        "analytics/top-merchants",
        AnalyticsTopMerchantsView.as_view(),
        name="analytics-top-merchants",
    ),

    # Endpoints de usuario — ORM de Django, requieren token
    path(
        "users/<int:user_id>/transactions",
        UserTransactionsView.as_view(),
        name="user-transactions",
    ),
    path(
        "users/<int:user_id>/stats",
        UserStatsView.as_view(),
        name="user-stats",
    ),

    # Ingesta — ORM de Django, requiere token
    path(
        "transactions/batch",
        TransactionBatchView.as_view(),
        name="transactions-batch",
    ),
]