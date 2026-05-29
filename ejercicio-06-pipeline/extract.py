"""
extract.py — Capa de extracción y normalización del pipeline.

RESPONSABILIDAD ÚNICA: normalizar tipos y formatos.
Esta capa NO valida reglas de negocio.

La distinción es crítica para la evaluación:
    - extract.py:   ¿el dato está en el formato correcto?
    - transform.py: ¿el dato cumple las reglas del negocio?

Un amount=-50.0 pasa por esta capa sin problema — es un float perfectamente
formateado. transform.py lo rechazará porque viola la regla de negocio
(amount debe estar entre 0.01 y 5000.00). Si extract.py rechazara amounts
negativos, estaría mezclando responsabilidades.

Normalizaciones que aplica esta capa:
    1. timestamp  → string ISO8601 'YYYY-MM-DD HH:MM:SS'
                    acepta: datetime objects, strings con T, epoch timestamps
    2. country_code → upper().strip()  (mx → MX)
    3. amount     → float redondeado a 2 decimales
    4. strings    → strip() para eliminar espacios extra
    5. Campos nulos → se mantienen como None (transform los rechazará)

Errores que SÍ maneja esta capa (errores de formato, no de negocio):
    - Si el timestamp no se puede parsear a ningún formato conocido,
      la fila va al log de errores de extracción con motivo técnico.
      Esto es diferente a un timestamp futuro — es un dato irrecuperable.
    - Si amount no se puede convertir a float (ej: "abc"), mismo tratamiento.

Uso como módulo:
    from extract import extract
    rows, errors = extract(raw_batch)

Uso como script:
    python extract.py --input raw_batch.json --output extracted.json
"""

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Formatos de timestamp que esta capa acepta y normaliza
# ---------------------------------------------------------------------------

_TIMESTAMP_FORMATS = [
    "%Y-%m-%d %H:%M:%S",       # formato del módulo: '2024-03-15 14:30:00'
    "%Y-%m-%dT%H:%M:%S",       # ISO 8601 con T: '2024-03-15T14:30:00'
    "%Y-%m-%dT%H:%M:%SZ",      # ISO 8601 UTC: '2024-03-15T14:30:00Z'
    "%Y-%m-%dT%H:%M:%S.%f",    # con microsegundos: '2024-03-15T14:30:00.123456'
    "%Y-%m-%d",                 # solo fecha: '2024-03-15' → '2024-03-15 00:00:00'
]

_TARGET_FORMAT = "%Y-%m-%d %H:%M:%S"


def _normalize_timestamp(value: Any) -> str | None:
    """
    Convierte cualquier representación de timestamp al formato ISO8601
    del módulo ('YYYY-MM-DD HH:MM:SS').

    Retorna None si el valor no puede ser interpretado como fecha válida.
    Un None aquí indica error de formato (no de negocio) — la fila irá
    a los errores de extracción, no a cuarentena.
    """
    if value is None:
        return None

    # Ya es un datetime — formatear directamente
    if isinstance(value, datetime):
        return value.strftime(_TARGET_FORMAT)

    # Es un número — interpretar como epoch Unix
    if isinstance(value, (int, float)):
        try:
            return datetime.utcfromtimestamp(value).strftime(_TARGET_FORMAT)
        except (OSError, OverflowError, ValueError):
            return None

    # Es un string — probar cada formato conocido
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        for fmt in _TIMESTAMP_FORMATS:
            try:
                return datetime.strptime(value, fmt).strftime(_TARGET_FORMAT)
            except ValueError:
                continue
        return None  # ningún formato funcionó

    return None


def _normalize_amount(value: Any) -> float | None:
    """
    Convierte el amount a float redondeado a 2 decimales.
    Retorna None si no se puede convertir (ej: "abc", [], {}).
    """
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _normalize_string(value: Any) -> str | None:
    """
    Elimina espacios al inicio y al final de un string.
    Retorna None si el valor es None o no es un string.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    # Si no es string pero tampoco es None (ej: número donde se espera string),
    # convertir — transform decidirá si el valor es válido para ese campo
    return str(value).strip()


# ---------------------------------------------------------------------------
# Función principal de extracción
# ---------------------------------------------------------------------------

def extract(raw_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Normaliza un batch de transacciones crudas.

    Para cada fila:
        1. Intenta normalizar cada campo
        2. Si hay un error de formato irrecuperable (timestamp imposible de
           parsear, amount no convertible a float), la fila va a parse_errors
        3. Si la normalización es exitosa (aunque el valor sea None o inválido
           para el negocio), la fila va a extracted

    Parámetros
    ----------
    raw_rows : lista de dicts tal como llegan de data_source

    Retorna
    -------
    extracted    : list[dict] — filas normalizadas (pueden tener valores
                   inválidos para el negocio — transform los filtrará)
    parse_errors : list[dict] — filas con errores de formato irrecuperables
                   (no son cuarentena — son errores técnicos de extracción)
    """
    extracted:    list[dict] = []
    parse_errors: list[dict] = []

    for row in raw_rows:
        errors_in_row: list[str] = []

        # Normalizar timestamp
        raw_ts = row.get("timestamp")
        norm_ts = _normalize_timestamp(raw_ts)
        if raw_ts is not None and norm_ts is None:
            errors_in_row.append(
                f"timestamp '{raw_ts}' no pudo ser parseado a ningún formato conocido"
            )

        # Normalizar amount
        raw_amount  = row.get("amount")
        norm_amount = _normalize_amount(raw_amount)
        if raw_amount is not None and norm_amount is None:
            errors_in_row.append(
                f"amount '{raw_amount}' no pudo ser convertido a float"
            )

        # Si hay errores de formato irrecuperables, la fila va a parse_errors
        if errors_in_row:
            parse_errors.append({
                **row,
                "parse_error": "; ".join(errors_in_row),
            })
            continue

        # Construir la fila normalizada
        # Campos None o inválidos para el negocio pasan tal cual — transform decide
        extracted.append({
            "transaction_id": _normalize_string(row.get("transaction_id")),
            "timestamp":      norm_ts,
            "user_id":        row.get("user_id"),        # int o None — transform valida
            "merchant_id":    row.get("merchant_id"),    # int o None — transform valida
            "amount":         norm_amount,               # float o None
            "category":       _normalize_string(row.get("category")),
            "country_code":   _normalize_string(row.get("country_code", "")).upper()
                              if row.get("country_code") else None,
            "status":         _normalize_string(row.get("status")),
        })

    logger.info(
        "Extracción completada: %d filas normalizadas, %d errores de formato",
        len(extracted), len(parse_errors),
    )
    return extracted, parse_errors


# ---------------------------------------------------------------------------
# CLI — para probar la capa de forma independiente
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normaliza un batch JSON de transacciones crudas.",
    )
    parser.add_argument("--input",  required=True,  help="JSON de entrada (raw batch)")
    parser.add_argument("--output", required=False, help="JSON de salida (extracted)")
    args = parser.parse_args()

    raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
    extracted, parse_errors = extract(raw)

    print(f"Extracción:")
    print(f"  Filas normalizadas: {len(extracted)}")
    print(f"  Errores de formato: {len(parse_errors)}")

    if args.output:
        Path(args.output).write_text(
            json.dumps(extracted, indent=2, default=str), encoding="utf-8"
        )
        print(f"  Guardado en {args.output}")
    else:
        print(json.dumps(extracted[:2], indent=2, default=str))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()