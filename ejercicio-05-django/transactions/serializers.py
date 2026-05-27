"""
transactions/serializers.py — Serializers de entrada y salida.

Dos grupos:

1. Serializers de salida (read):
   Definen qué campos retorna cada endpoint y en qué formato.
   TransactionSerializer — para listas de transacciones de usuario.
   UserStatsSerializer   — para las estadísticas de un usuario.

2. Serializers de entrada (write):
   Validan el payload del POST /transactions/batch.
   TransactionInSerializer — una transacción individual del batch.
   BatchRequestSerializer  — el payload completo (lista de transacciones).

Decisión sobre los campos de validación:
    Los mismos VALID_* que usa models.py. Se importan del modelo para
    garantizar que la validación del serializer y las constantes del
    modelo siempre están sincronizadas — una sola fuente de verdad.

Decisión sobre el status 422:
    El serializer lanza ValidationError cuando los datos no son válidos.
    DRF convierte eso a HTTP 400 por defecto. El custom_exception_handler
    en exceptions.py lo convierte a 422. El serializer no necesita
    saber nada sobre el status code — es responsabilidad del handler.
"""

from datetime import datetime, timezone

from rest_framework import serializers

from transactions.models import (
    VALID_CATEGORIES,
    VALID_COUNTRIES,
    VALID_STATUSES,
    Transaction,
)


# ---------------------------------------------------------------------------
# Serializers de salida
# ---------------------------------------------------------------------------

class TransactionSerializer(serializers.ModelSerializer):
    """
    Serializer de solo lectura para una transacción.
    Usado en GET /users/{id}/transactions.
    """

    class Meta:
        model  = Transaction
        fields = [
            "transaction_id",
            "timestamp",
            "amount",
            "category",
            "status",
            "merchant_id",
        ]


class UserStatsSerializer(serializers.Serializer):
    """
    Serializer para las estadísticas de un usuario.
    Usado en GET /users/{id}/stats.

    No es ModelSerializer porque las stats son resultado de agregaciones
    del ORM (Sum, Count, etc.), no instancias directas del modelo.
    """

    user_id           = serializers.IntegerField()
    total_amount      = serializers.FloatField()
    transaction_count = serializers.IntegerField()
    top_category      = serializers.CharField(allow_null=True)
    top_country       = serializers.CharField(allow_null=True)


# ---------------------------------------------------------------------------
# Serializers de entrada — POST /transactions/batch
# ---------------------------------------------------------------------------

class TransactionInSerializer(serializers.Serializer):
    """
    Valida una transacción individual del batch.

    Los validadores de campo son explícitos — no se delega en el modelo
    porque el batch puede recibir datos de fuentes externas que no coinciden
    con las restricciones del modelo Django.
    """

    transaction_id = serializers.CharField(min_length=10, max_length=36)
    timestamp      = serializers.DateTimeField()
    user_id        = serializers.IntegerField(min_value=1, max_value=50_000)
    merchant_id    = serializers.IntegerField(min_value=1, max_value=10_000)
    amount         = serializers.FloatField(min_value=0.01, max_value=5_000.0)
    category       = serializers.CharField()
    country_code   = serializers.CharField(min_length=2, max_length=2)
    status         = serializers.CharField()

    def validate_category(self, value: str) -> str:
        if value not in VALID_CATEGORIES:
            raise serializers.ValidationError(
                f"'{value}' no es una categoría válida. "
                f"Opciones: {sorted(VALID_CATEGORIES)}"
            )
        return value

    def validate_status(self, value: str) -> str:
        if value not in VALID_STATUSES:
            raise serializers.ValidationError(
                f"'{value}' no es un status válido. "
                f"Opciones: {sorted(VALID_STATUSES)}"
            )
        return value

    def validate_country_code(self, value: str) -> str:
        value = value.upper()
        if value not in VALID_COUNTRIES:
            raise serializers.ValidationError(
                f"'{value}' no es un country_code válido. "
                f"Opciones: {sorted(VALID_COUNTRIES)}"
            )
        return value

    def validate_amount(self, value: float) -> float:
        # FloatField ya valida min/max, pero redondeamos para consistencia
        return round(value, 2)


class BatchRequestSerializer(serializers.Serializer):
    """
    Valida el payload completo del POST /transactions/batch.

    Límite de 500 transacciones — igual que E4. Si la lista tiene
    más de 500 elementos, DRF retorna 422 (via custom_exception_handler)
    antes de que la view procese nada.
    """

    transactions = serializers.ListField(
        child     = TransactionInSerializer(),
        min_length = 1,
        max_length = 500,
    )


# ---------------------------------------------------------------------------
# Serializers de respuesta para analytics (DuckDB)
# ---------------------------------------------------------------------------

class CountryBreakdownSerializer(serializers.Serializer):
    country_code       = serializers.CharField()
    total_transactions = serializers.IntegerField()
    total_amount       = serializers.FloatField()


class CategoryBreakdownSerializer(serializers.Serializer):
    category           = serializers.CharField()
    total_transactions = serializers.IntegerField()
    avg_amount         = serializers.FloatField()


class SummaryResponseSerializer(serializers.Serializer):
    """Response de GET /analytics/summary."""
    total_transactions = serializers.IntegerField()
    total_amount       = serializers.FloatField()
    avg_amount         = serializers.FloatField()
    by_country         = CountryBreakdownSerializer(many=True)
    by_category        = CategoryBreakdownSerializer(many=True)


class MerchantSerializer(serializers.Serializer):
    """Un merchant en el ranking."""
    merchant_id        = serializers.IntegerField()
    total_amount       = serializers.FloatField()
    transaction_count  = serializers.IntegerField()


class BatchResponseSerializer(serializers.Serializer):
    """Response de POST /transactions/batch."""
    received           = serializers.IntegerField()
    inserted           = serializers.IntegerField()
    duplicates_skipped = serializers.IntegerField()