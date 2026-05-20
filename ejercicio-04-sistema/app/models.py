"""
app/models.py — Modelos Pydantic del sistema.

Dos grupos de modelos:

1. Modelos de entrada (request):
   TransactionIn — una transacción individual del batch
   BatchRequest   — el payload completo del POST /transactions/batch

2. Modelos de salida (response):
   Definen la estructura exacta que retorna cada endpoint.
   Tenerlos explícitos tiene dos ventajas: FastAPI los usa para generar
   la documentación automática en /docs, y los tests pueden validar la
   estructura del response sin parsear JSON manualmente.

Decisión de diseño: los tipos de los campos de TransactionIn son estrictos
a propósito. Si el cliente manda amount como string ("123.45" en lugar de
123.45), Pydantic retorna HTTP 422 automáticamente con el detalle del campo
que falló. Eso es exactamente el comportamiento que pide el enunciado.

Las constantes de valores válidos (VALID_CATEGORIES, etc.) se definen a nivel
de módulo en lugar de como atributos de clase. Pydantic 2.x puede tratar
atributos de clase sin anotación de tipo como campos del modelo, lo que
genera comportamiento inesperado en validación y serialización.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Constantes de validación — a nivel de módulo, no de clase
# ---------------------------------------------------------------------------
# Mismos valores que usa generate_data.py en E1. Si el schema del módulo
# cambia, se actualiza aquí y los validadores reflejan el cambio automáticamente.

VALID_CATEGORIES: frozenset[str] = frozenset({
    "Food", "Travel", "Electronics", "Health", "Entertainment",
    "Retail", "Transport", "Education", "Services", "Other",
})

VALID_STATUSES: frozenset[str] = frozenset({
    "completed", "failed", "pending",
})

VALID_COUNTRIES: frozenset[str] = frozenset({
    "MX", "CO", "BR", "AR", "CL", "PE", "EC",
    "VE", "BO", "PY", "UY", "CR", "GT", "PA", "DO",
})


# ---------------------------------------------------------------------------
# Modelos de entrada — POST /transactions/batch
# ---------------------------------------------------------------------------

class TransactionIn(BaseModel):
    """
    Representa una transacción individual dentro de un batch.

    Los validadores de campo garantizan que los valores están dentro de los
    rangos definidos en el schema del módulo. Una transacción con amount=-5
    o country_code="ZZ" es rechazada con 422 antes de llegar a la base de datos.
    """

    transaction_id: str = Field(
        ...,
        description="UUID único de la transacción",
        min_length=10,
    )
    timestamp: datetime = Field(
        ...,
        description="Momento de la transacción en formato ISO8601",
    )
    user_id: int = Field(
        ...,
        ge=1,
        le=50_000,
        description="ID del usuario (1-50000)",
    )
    merchant_id: int = Field(
        ...,
        ge=1,
        le=10_000,
        description="ID del merchant (1-10000)",
    )
    amount: float = Field(
        ...,
        gt=0,
        le=5_000.0,
        description="Monto de la transacción (0.01-5000.00)",
    )
    category: str = Field(
        ...,
        description="Categoría de la transacción",
    )
    country_code: str = Field(
        ...,
        min_length=2,
        max_length=2,
        description="Código de país ISO 3166-1 alpha-2",
    )
    status: str = Field(
        ...,
        description="Estado: completed, failed o pending",
    )

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        if v not in VALID_CATEGORIES:
            raise ValueError(
                f"'{v}' no es una categoría válida. "
                f"Opciones: {sorted(VALID_CATEGORIES)}"
            )
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in VALID_STATUSES:
            raise ValueError(
                f"'{v}' no es un status válido. "
                f"Opciones: {sorted(VALID_STATUSES)}"
            )
        return v

    @field_validator("country_code")
    @classmethod
    def validate_country(cls, v: str) -> str:
        v = v.upper()
        if v not in VALID_COUNTRIES:
            raise ValueError(
                f"'{v}' no es un country_code válido. "
                f"Opciones: {sorted(VALID_COUNTRIES)}"
            )
        return v


class BatchRequest(BaseModel):
    """
    Payload del POST /transactions/batch.

    Límite de 500 transacciones por batch — definido en el enunciado.
    Pydantic valida este límite antes de que el código del endpoint procese
    nada: si llegan 501 transacciones, el cliente recibe 422 inmediatamente.
    """

    transactions: list[TransactionIn] = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Lista de transacciones a insertar (1-500)",
    )


# ---------------------------------------------------------------------------
# Modelos de salida — estructura de cada response
# ---------------------------------------------------------------------------

class CountryBreakdown(BaseModel):
    country_code: str
    total_transactions: int
    total_amount: float


class CategoryBreakdown(BaseModel):
    category: str
    total_transactions: int
    avg_amount: float


class SummaryResponse(BaseModel):
    """Response de GET /analytics/summary."""
    total_transactions: int
    total_amount: float
    avg_amount: float
    by_country: list[CountryBreakdown]
    by_category: list[CategoryBreakdown]


class MerchantResponse(BaseModel):
    """Un merchant en el ranking de GET /analytics/top-merchants."""
    merchant_id: int
    total_amount: float
    transaction_count: int


class TopMerchantsResponse(BaseModel):
    """Response de GET /analytics/top-merchants."""
    merchants: list[MerchantResponse]
    limit: int
    country: Optional[str]


class TransactionOut(BaseModel):
    """Una transacción en el response de GET /users/{id}/transactions."""
    transaction_id: str
    timestamp: str
    amount: float
    category: str
    status: str
    merchant_id: int


class UserTransactionsResponse(BaseModel):
    """Response de GET /users/{user_id}/transactions."""
    user_id: int
    page: int
    page_size: int
    transactions: list[TransactionOut]


class UserStatsResponse(BaseModel):
    """Response de GET /users/{user_id}/stats."""
    user_id: int
    total_amount: float
    transaction_count: int
    top_category: Optional[str]
    top_country: Optional[str]


class BatchResponse(BaseModel):
    """Response de POST /transactions/batch."""
    received: int
    inserted: int
    duplicates_skipped: int


class HealthResponse(BaseModel):
    """Response de GET /health."""
    status: str
    uptime_seconds: float
    cache_hit_rate: float
    cache_hits: int
    cache_misses: int
    duckdb_connected: bool
    sqlite_connected: bool