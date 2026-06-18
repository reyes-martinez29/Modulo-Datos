"""
transform.py — Capa de transformación y validación del pipeline.

RESPONSABILIDAD ÚNICA: validar reglas de negocio y separar filas válidas
de filas rechazadas. Las filas rechazadas van a cuarentena con el motivo
exacto del rechazo.

La distinción con extract.py es fundamental:
    - extract.py pregunta: ¿el dato está en el formato técnico correcto?
    - transform.py pregunta: ¿el dato cumple las reglas del negocio?

Reglas de negocio validadas (exactamente las del enunciado):
    1. amount entre 0.01 y 5,000.00 — rechazar fuera de rango
    2. category en el set de 10 valores del schema
    3. country_code en el set de 15 países del schema
    4. timestamp no puede ser futuro (más de 1 hora de adelanto)
    5. transaction_id debe ser UUID4 válido
    6. campos requeridos no pueden ser None (null_field)

Cuarentena:
    Las filas rechazadas se escriben en quarantine/YYYY-MM-DD.jsonl
    con append — múltiples corridas del mismo día van al mismo archivo.
    Cada línea del JSONL contiene la fila completa más el campo
    'rejection_reason' con el motivo exacto y específico del rechazo.

    Ejemplos de rejection_reason:
        "amount=-50.0 fuera del rango [0.01, 5000.00]"
        "category='Gambling' no está en el set válido"
        "timestamp '2030-01-01 00:00:00' es futuro (límite: 2024-03-15 15:30:00)"
        "transaction_id 'not-a-uuid' no es un UUID4 válido"
        "campo requerido 'user_id' es None"

Uso como módulo:
    from transform import transform
    valid, rejected = transform(extracted_rows)

Uso como script:
    python transform.py --input extracted.json --quarantine quarantine/
"""

import argparse
import json
import logging
import re
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constantes de validación — mismas que el resto del módulo
# ---------------------------------------------------------------------------

VALID_CATEGORIES: frozenset[str] = frozenset({
    "Food", "Travel", "Electronics", "Health", "Entertainment",
    "Retail", "Transport", "Education", "Services", "Other",
})

VALID_COUNTRIES: frozenset[str] = frozenset({
    "MX", "CO", "BR", "AR", "CL", "PE", "EC",
    "VE", "BO", "PY", "UY", "CR", "GT", "PA", "DO",
})

VALID_STATUSES: frozenset[str] = frozenset({
    "completed", "failed", "pending",
})

REQUIRED_FIELDS: tuple[str, ...] = (
    "transaction_id", "timestamp", "user_id",
    "merchant_id", "amount", "category",
    "country_code", "status",
)

AMOUNT_MIN = 0.01
AMOUNT_MAX = 5_000.00

# Tolerancia para timestamps futuros — el enunciado dice "más de 1 hora de adelanto"
FUTURE_TOLERANCE_HOURS = 1

# Regex para UUID4 — formato xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx
_UUID4_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Validadores individuales — cada uno retorna None si es válido,
# o un string con el motivo del rechazo si es inválido
# ---------------------------------------------------------------------------

def _check_null_fields(row: dict) -> str | None:
    """Verifica que todos los campos requeridos tienen valor."""
    for field in REQUIRED_FIELDS:
        if row.get(field) is None:
            return f"campo requerido '{field}' es None"
    return None


def _check_amount(amount: Any) -> str | None:
    """Verifica que amount está en el rango [0.01, 5000.00]."""
    if amount is None:
        return f"campo requerido 'amount' es None"
    try:
        val = float(amount)
    except (TypeError, ValueError):
        return f"amount='{amount}' no es un número válido"
    if not AMOUNT_MIN <= val <= AMOUNT_MAX:
        return f"amount={val} fuera del rango [{AMOUNT_MIN}, {AMOUNT_MAX}]"
    return None


def _check_category(category: Any) -> str | None:
    """Verifica que category pertenece al set del schema."""
    if category not in VALID_CATEGORIES:
        return f"category='{category}' no está en el set válido {sorted(VALID_CATEGORIES)}"
    return None


def _check_country(country_code: Any) -> str | None:
    """Verifica que country_code pertenece al set del schema."""
    if country_code not in VALID_COUNTRIES:
        return f"country_code='{country_code}' no está en el set válido {sorted(VALID_COUNTRIES)}"
    return None


def _check_timestamp(timestamp: Any, now: datetime) -> str | None:
    """
    Verifica que el timestamp no está en el futuro más allá de la tolerancia.

    El timestamp llega como string ISO8601 normalizado por extract.py.
    Si no puede parsearse es un error de extract — aquí solo evaluamos
    si el valor parseado está dentro del rango temporal permitido.
    """
    if timestamp is None:
        return "campo requerido 'timestamp' es None"
    try:
        ts = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return f"timestamp='{timestamp}' no pudo ser parseado"

    limite = now + timedelta(hours=FUTURE_TOLERANCE_HOURS)
    if ts > limite:
        return (
            f"timestamp '{timestamp}' es futuro "
            f"(límite: {limite.strftime('%Y-%m-%d %H:%M:%S')})"
        )
    return None


def _check_transaction_id(tid: Any) -> str | None:
    """Verifica que transaction_id es un UUID4 válido."""
    if tid is None:
        return "campo requerido 'transaction_id' es None"
    if not isinstance(tid, str) or not _UUID4_RE.match(str(tid)):
        return f"transaction_id '{tid}' no es un UUID4 válido"
    return None


# ---------------------------------------------------------------------------
# Clasificador de errores para el reporte
# ---------------------------------------------------------------------------

# Mapa de tipo de error → clave en el reporte JSON
# El orden importa: null_field se chequea primero para dar el error más específico
_ERROR_KEY_MAP = {
    "campo requerido":  "null_field",
    "amount=":          "amount_out_of_range",
    "amount no es":     "amount_out_of_range",
    "category=":        "invalid_category",
    "country_code=":    "invalid_country",
    "timestamp":        "future_timestamp",
    "transaction_id":   "invalid_transaction_id",
}


def classify_rejection(reason: str) -> str:
    """
    Clasifica el motivo de rechazo en una de las categorías del reporte.

    Retorna la clave para el campo 'by_error' del reporte JSON.
    """
    for keyword, key in _ERROR_KEY_MAP.items():
        if keyword in reason:
            return key
    return "other"


# ---------------------------------------------------------------------------
# Función principal de transformación
# ---------------------------------------------------------------------------

def transform(
    rows: list[dict],
    now:  datetime | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Valida cada fila contra las reglas de negocio del schema.

    Las validaciones se aplican en este orden:
        1. null_field — primero porque los siguientes checks asumen
           que el campo existe
        2. transaction_id — identifica la fila
        3. amount — la regla más crítica del negocio financiero
        4. category — valor del dominio
        5. country_code — valor del dominio
        6. timestamp — regla temporal
        7. status — valor del dominio (no rechaza, solo normaliza si es inválido)

    Una fila se rechaza si FALLA ALGUNA validación. El rejection_reason
    describe el PRIMER fallo encontrado — esto es intencional para que el
    motivo sea específico y accionable.

    Parámetros
    ----------
    rows : list[dict] — filas normalizadas por extract.py
    now  : datetime de referencia para validar timestamps futuros.
           Si es None, usa datetime.now(timezone.utc).replace(tzinfo=None). Se acepta como parámetro
           para facilitar los tests deterministas.

    Retorna
    -------
    valid    : list[dict] — filas que pasan todas las validaciones
    rejected : list[dict] — filas rechazadas, cada una con campos extra:
                            'rejection_reason' (str) y 'rejection_type' (str)
    """
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)

    valid:    list[dict] = []
    rejected: list[dict] = []

    for row in rows:
        reason = None

        # Las validaciones se aplican en orden — la primera que falla
        # determina el rejection_reason
        reason = (
            _check_null_fields(row)
            or _check_transaction_id(row.get("transaction_id"))
            or _check_amount(row.get("amount"))
            or _check_category(row.get("category"))
            or _check_country(row.get("country_code"))
            or _check_timestamp(row.get("timestamp"), now)
        )

        if reason:
            rejected.append({
                **row,
                "rejection_reason": reason,
                "rejection_type":   classify_rejection(reason),
            })
        else:
            valid.append(row)

    logger.info(
        "Transformación: %d válidas, %d rechazadas",
        len(valid), len(rejected),
    )
    return valid, rejected


# ---------------------------------------------------------------------------
# Cuarentena — escritura en archivo JSONL por día
# ---------------------------------------------------------------------------

def write_quarantine(
    rejected:         list[dict],
    quarantine_dir:   str | Path = "quarantine",
) -> Path:
    """
    Escribe las filas rechazadas en quarantine/YYYY-MM-DD.jsonl con append.

    Cada línea del archivo es un JSON con la fila completa más los campos
    'rejection_reason' y 'rejection_type'. El archivo se abre en modo append
    para que múltiples corridas del mismo día acumulen en el mismo archivo.

    Parámetros
    ----------
    rejected       : filas rechazadas por transform()
    quarantine_dir : directorio donde crear los archivos de cuarentena

    Retorna
    -------
    Path del archivo de cuarentena donde se escribieron las filas.
    """
    if not rejected:
        # Sin rechazados no hay nada que escribir — retornar path igualmente
        qdir = Path(quarantine_dir)
        qdir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
        return qdir / f"{today}.jsonl"

    qdir = Path(quarantine_dir)
    qdir.mkdir(parents=True, exist_ok=True)

    today     = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
    qfile     = qdir / f"{today}.jsonl"

    # Append — múltiples corridas del mismo día van al mismo archivo
    with qfile.open("a", encoding="utf-8") as f:
        for row in rejected:
            f.write(json.dumps(row, default=str) + "\n")

    logger.info(
        "%d filas rechazadas escritas en %s",
        len(rejected), qfile,
    )
    return qfile


# ---------------------------------------------------------------------------
# CLI — para probar la capa de forma independiente
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Valida un batch JSON de transacciones normalizadas.",
    )
    parser.add_argument("--input",      required=True,
                        help="JSON de entrada (extracted batch)")
    parser.add_argument("--quarantine", default="quarantine",
                        help="Directorio de cuarentena (default: quarantine/)")
    args = parser.parse_args()

    rows = json.loads(Path(args.input).read_text(encoding="utf-8"))
    valid, rejected = transform(rows)
    qfile = write_quarantine(rejected, args.quarantine)

    print(f"Transformación:")
    print(f"  Válidas:    {len(valid)}")
    print(f"  Rechazadas: {len(rejected)}")

    if rejected:
        by_type: dict[str, int] = {}
        for r in rejected:
            key = r.get("rejection_type", "other")
            by_type[key] = by_type.get(key, 0) + 1
        print(f"  Por tipo:")
        for k, v in sorted(by_type.items()):
            print(f"    {k}: {v}")
        print(f"  Cuarentena: {qfile}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()