"""
data_source.py — Fuente de datos simulada para el pipeline del E6.

Simula la llegada de transacciones nuevas desde una fuente externa con
calidad variable. Genera batches con el schema del módulo pero introduce
errores deliberados en una fracción configurable de las filas.

Uso:
    python data_source.py                          # defaults
    python data_source.py --batch-size 500 --error-rate 0.15
    python data_source.py --seed 42                # reproducible
    python data_source.py --output raw_batch.json  # guardar en archivo

Decisión de diseño — seed explícito:
    El enunciado pide --batch-size y --error-rate. Se agrega --seed
    opcionalmente para que los tests sean deterministas: con el mismo seed
    el mismo comando produce exactamente el mismo batch, lo que permite
    a test_pipeline.py saber de antemano cuántas filas válidas y rechazadas
    esperar sin hardcodear valores frágiles.

Tipos de errores introducidos (en proporciones iguales del error_rate):
    1. amount negativo           → amount = -abs(amount)
    2. category inválida         → category = "Gambling"
    3. timestamp futuro          → timestamp = ahora + 2 días
    4. campo nulo               → user_id = None
    5. transaction_id malformado → transaction_id = "not-a-uuid"

Esta capa NO pertenece al pipeline — es la fuente externa que el pipeline
consume. Por eso no importa nada de extract/transform/load.
"""

import argparse
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Constantes del schema del módulo (mismas que E1-E5)
# ---------------------------------------------------------------------------

CATEGORIES = [
    "Food", "Travel", "Electronics", "Health", "Entertainment",
    "Retail", "Transport", "Education", "Services", "Other",
]

COUNTRY_CODES = [
    "MX", "CO", "BR", "AR", "CL", "PE", "EC",
    "VE", "BO", "PY", "UY", "CR", "GT", "PA", "DO",
]

STATUSES = ["completed", "failed", "pending"]

# Pesos de status: 85% completed, 10% failed, 5% pending — igual que E1
STATUS_WEIGHTS = [0.85, 0.10, 0.05]

# Tipos de error y sus nombres — usados también por transform.py para
# clasificar los rechazos en el reporte
ERROR_TYPES = [
    "negative_amount",
    "invalid_category",
    "future_timestamp",
    "null_field",
    "invalid_transaction_id",
]


# ---------------------------------------------------------------------------
# Generador de una transacción válida
# ---------------------------------------------------------------------------

def _make_valid_transaction(rng: random.Random, now: datetime) -> dict:
    """
    Genera una transacción con todos los campos dentro del schema.

    Parámetros
    ----------
    rng : instancia de Random con seed controlado
    now : datetime de referencia para el timestamp (UTC)
    """
    # Timestamp en el último año, antes del momento actual
    days_ago = rng.randint(0, 365)
    ts       = now - timedelta(days=days_ago, seconds=rng.randint(0, 86400))

    return {
        "transaction_id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
        "timestamp":      ts.strftime("%Y-%m-%d %H:%M:%S"),
        "user_id":        rng.randint(1, 50_000),
        "merchant_id":    rng.randint(1, 10_000),
        "amount":         round(rng.uniform(0.01, 5_000.0), 2),
        "category":       rng.choice(CATEGORIES),
        "country_code":   rng.choice(COUNTRY_CODES),
        "status":         rng.choices(STATUSES, weights=STATUS_WEIGHTS)[0],
    }


# ---------------------------------------------------------------------------
# Inyección de errores
# ---------------------------------------------------------------------------

def _inject_error(row: dict, error_type: str, rng: random.Random, now: datetime) -> dict:
    """
    Introduce un error específico en una transacción válida.

    El row se modifica in-place y se retorna. La fila sigue teniendo todos
    los campos — solo uno de ellos es inválido. Esto simula datos reales
    donde el error no siempre es obvio (ej: un amount negativo llega como
    float perfectamente formateado).

    Parámetros
    ----------
    row        : transacción válida generada por _make_valid_transaction
    error_type : uno de ERROR_TYPES
    rng        : generador aleatorio con seed controlado
    now        : datetime de referencia
    """
    if error_type == "negative_amount":
        # Amount negativo — pasa extracción (es un float), falla transform
        row["amount"] = -round(rng.uniform(0.01, 500.0), 2)

    elif error_type == "invalid_category":
        # Categoría que no existe en el schema
        row["category"] = rng.choice(["Gambling", "Crypto", "NFT", "Adult", "Unknown"])

    elif error_type == "future_timestamp":
        # Más de 1 hora en el futuro — el límite del enunciado es 1h
        future = now + timedelta(hours=rng.randint(2, 72))
        row["timestamp"] = future.strftime("%Y-%m-%d %H:%M:%S")

    elif error_type == "null_field":
        # Campo nulo en un campo requerido
        null_field = rng.choice(["user_id", "merchant_id", "amount", "category"])
        row[null_field] = None

    elif error_type == "invalid_transaction_id":
        # UUID malformado — no pasa la validación de UUID4
        row["transaction_id"] = rng.choice([
            "not-a-uuid",
            "12345",
            "",
            "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            str(uuid.uuid4()).replace("-", ""),   # sin guiones
        ])

    return row


# ---------------------------------------------------------------------------
# Generador principal del batch
# ---------------------------------------------------------------------------

def generate_batch(
    batch_size:  int   = 500,
    error_rate:  float = 0.10,
    seed:        int | None = None,
) -> list[dict]:
    """
    Genera un batch de transacciones con errores intencionales.

    Parámetros
    ----------
    batch_size : número total de filas a generar (100-1000)
    error_rate : fracción de filas con error (0.0-1.0)
    seed       : semilla para reproducibilidad (None = aleatorio)

    Retorna
    -------
    list[dict] — batch mezclado (válidos y erróneos en orden aleatorio)

    Invariante matemática:
        len(result) == batch_size
        filas_con_error ≈ batch_size * error_rate
    """
    if not 1 <= batch_size <= 10_000:
        raise ValueError(f"batch_size debe estar entre 1 y 10,000, recibido: {batch_size}")
    if not 0.0 <= error_rate <= 1.0:
        raise ValueError(f"error_rate debe estar entre 0.0 y 1.0, recibido: {error_rate}")

    rng = random.Random(seed)
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)  # naive UTC

    n_errors = round(batch_size * error_rate)
    n_valid  = batch_size - n_errors

    rows: list[dict] = []

    # Generar filas válidas
    for _ in range(n_valid):
        rows.append(_make_valid_transaction(rng, now))

    # Generar filas con error — distribución uniforme entre tipos de error
    for i in range(n_errors):
        base       = _make_valid_transaction(rng, now)
        error_type = ERROR_TYPES[i % len(ERROR_TYPES)]
        rows.append(_inject_error(base, error_type, rng, now))

    # Mezclar para que los errores no estén al final
    rng.shuffle(rows)
    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Genera un batch de transacciones con errores intencionales.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python data_source.py
  python data_source.py --batch-size 1000 --error-rate 0.20
  python data_source.py --seed 42 --batch-size 200 --error-rate 0.10
  python data_source.py --seed 42 --output data/raw_batch.json
        """,
    )
    parser.add_argument("--batch-size",  type=int,   default=500,
                        help="Número de transacciones a generar (default: 500)")
    parser.add_argument("--error-rate",  type=float, default=0.10,
                        help="Fracción de filas con errores (default: 0.10)")
    parser.add_argument("--seed",        type=int,   default=None,
                        help="Semilla aleatoria para reproducibilidad")
    parser.add_argument("--output",      type=str,   default=None,
                        help="Archivo de salida JSON (default: imprime en stdout)")
    args = parser.parse_args()

    batch = generate_batch(
        batch_size = args.batch_size,
        error_rate = args.error_rate,
        seed       = args.seed,
    )

    n_errors = round(args.batch_size * args.error_rate)
    print(f"Batch generado: {args.batch_size} filas "
          f"({args.batch_size - n_errors} válidas, ~{n_errors} con error)")

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(batch, indent=2, default=str), encoding="utf-8")
        print(f"Guardado en {args.output}")
    else:
        print(json.dumps(batch[:3], indent=2, default=str))
        if len(batch) > 3:
            print(f"... ({len(batch) - 3} filas más)")


if __name__ == "__main__":
    main()