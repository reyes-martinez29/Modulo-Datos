"""
app/models.py — Modelos Pydantic del sistema de monitoreo.

Reutiliza los modelos del E4 (request de batch, responses de analytics y
usuarios) y agrega los nuevos del E8:
    - AnomalyResponse  → salida de GET /analytics/anomalies
    - IngestReport     → salida de POST /pipeline/ingest
    - El response de transacciones de usuario gana campos de fecha opcionales

Las constantes de validación están a nivel de módulo, no como atributos de
clase, porque Pydantic 2.x trata los atributos de clase sin anotación como
campos del modelo, lo que genera comportamiento inesperado.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Constantes de validación — mismas del schema del módulo (E1-E6)
# ---------------------------------------------------------------------------

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
# Modelos de entrada — POST /transactions/batch (del E4)
# ---------------------------------------------------------------------------

class TransactionIn(BaseModel):
    transaction_id: str = Field(..., min_length=10)
    timestamp: datetime = Field(...)
    user_id: int = Field(..., ge=1, le=50_000)
    merchant_id: int = Field(..., ge=1, le=10_000)
    amount: float = Field(..., gt=0, le=5_000.0)
    category: str = Field(...)
    country_code: str = Field(..., min_length=2, max_length=2)
    status: str = Field(...)

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        if v not in VALID_CATEGORIES:
            raise ValueError(
                f"'{v}' no es una categoría válida. Opciones: {sorted(VALID_CATEGORIES)}"
            )
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in VALID_STATUSES:
            raise ValueError(
                f"'{v}' no es un status válido. Opciones: {sorted(VALID_STATUSES)}"
            )
        return v

    @field_validator("country_code")
    @classmethod
    def validate_country(cls, v: str) -> str:
        v = v.upper()
        if v not in VALID_COUNTRIES:
            raise ValueError(
                f"'{v}' no es un country_code válido. Opciones: {sorted(VALID_COUNTRIES)}"
            )
        return v


class BatchRequest(BaseModel):
    transactions: list[TransactionIn] = Field(..., min_length=1, max_length=500)


# ---------------------------------------------------------------------------
# Modelos de salida — analytics (del E4)
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
    total_transactions: int
    total_amount: float
    avg_amount: float
    by_country: list[CountryBreakdown]
    by_category: list[CategoryBreakdown]


class MerchantResponse(BaseModel):
    merchant_id: int
    total_amount: float
    transaction_count: int


class TopMerchantsResponse(BaseModel):
    merchants: list[MerchantResponse]
    limit: int
    country: Optional[str]


# ---------------------------------------------------------------------------
# Modelos de salida — usuarios (del E4, con fecha opcional en el E8)
# ---------------------------------------------------------------------------

class TransactionOut(BaseModel):
    transaction_id: str
    timestamp: str
    amount: float
    category: str
    status: str
    merchant_id: int


class UserTransactionsResponse(BaseModel):
    user_id: int
    page: int
    page_size: int
    # Campos nuevos del E8: reflejan el rango de fechas aplicado (o None).
    # Que el response indique el filtro usado hace la respuesta auto-explicativa.
    date_from: Optional[str]
    date_to: Optional[str]
    transactions: list[TransactionOut]


class UserStatsResponse(BaseModel):
    user_id: int
    total_amount: float
    transaction_count: int
    top_category: Optional[str]
    top_country: Optional[str]


class BatchResponse(BaseModel):
    received: int
    inserted: int
    duplicates_skipped: int


# ---------------------------------------------------------------------------
# Modelos de salida — nuevos del E8
# ---------------------------------------------------------------------------

class AnomalousUser(BaseModel):
    """Un usuario detectado como anómalo en GET /analytics/anomalies."""
    user_id: int
    failed_count: int
    window_days: int
    threshold: int


class AnomalyResponse(BaseModel):
    """Response de GET /analytics/anomalies."""
    threshold: int
    window_days: int
    anomalous_users: list[AnomalousUser]
    total_flagged: int


class IngestReport(BaseModel):
    """
    Response de POST /pipeline/ingest. Es el reporte del pipeline del E6
    adaptado a la fuente CSV, con las invariantes verificadas incluidas para
    que quien llama al endpoint pueda confirmar que los números cuadran.
    """
    rows_in_csv: int
    extracted: int
    parse_errors: int
    valid: int
    rejected: int
    by_error: dict[str, int]
    inserted: int
    duplicates: int
    total_time_s: float
    invariants: dict[str, bool]


# ---------------------------------------------------------------------------
# Health — enriquecido en el E8 con métricas de rendimiento
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str
    uptime_seconds: float
    cache_hit_rate: float
    cache_hits: int
    cache_misses: int
    duckdb_connected: bool
    sqlite_connected: bool
    # Métrica nueva del E8: número de filas en la tabla transaccional.
    # En un monitoreo real, el crecimiento de esta cifra vs lo esperado es
    # una señal de salud del pipeline de ingesta.
    transactions_in_db: int