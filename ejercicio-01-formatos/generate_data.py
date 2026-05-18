"""
generate_data.py — Generador de dataset de transacciones financieras.

Uso:
    python generate_data.py --size 100k
    python generate_data.py --size 500k
    python generate_data.py --size 1m

El schema es fijo para todo el módulo. No modificar.
"""

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Constantes del schema (fijas para los 4 ejercicios del módulo)
# ---------------------------------------------------------------------------

SIZES = {
    "100k": 100_000,
    "500k": 500_000,
    "1m":   1_000_000,
}

CATEGORIES = [
    "Food", "Travel", "Electronics", "Health", "Entertainment",
    "Retail", "Transport", "Education", "Services", "Other",
]

COUNTRIES = [
    "MX", "CO", "BR", "AR", "CL", "PE", "EC",
    "VE", "BO", "PY", "UY", "CR", "GT", "PA", "DO",
]

# Las probabilidades deben sumar exactamente 1.0
STATUSES      = ["completed", "failed", "pending"]
STATUS_PROBS  = [0.85,        0.10,     0.05]

# Semilla fija → el dataset es reproducible entre ejecuciones y máquinas.
# Cualquiera que clone el repo obtiene exactamente las mismas filas.
RANDOM_SEED = 42


# ---------------------------------------------------------------------------
# Generación del DataFrame
# ---------------------------------------------------------------------------

def generate_dataframe(n: int) -> pd.DataFrame:
    """
    Genera n filas de transacciones financieras sintéticas.

    Toda la generación se hace con NumPy vectorizado para que sea
    rápida incluso a 1M de filas. Los loops de Python se evitan
    deliberadamente: a 1M filas un loop es ~100x más lento que NumPy.

    Parámetros
    ----------
    n : int
        Número de filas a generar.

    Retorna
    -------
    pd.DataFrame con el schema exacto del módulo.
    """
    rng = np.random.default_rng(RANDOM_SEED)

    # --- timestamps ---
    # Rango de un año hacia atrás desde hoy, distribución uniforme en segundos.
    # Se genera como array de enteros (segundos desde epoch) y luego se
    # convierte a datetime. Mucho más rápido que generar objetos datetime uno
    # a uno.
    now_ts     = int(time.time())
    year_secs  = 365 * 24 * 3600
    timestamps = pd.to_datetime(
        rng.integers(now_ts - year_secs, now_ts, size=n),
        unit="s",
        utc=True,
    ).tz_localize(None)  # Guardamos sin timezone para compatibilidad con Parquet

    # --- UUIDs ---
    # uuid.uuid4() es puro Python → lento en volumen.
    # Generamos los 128 bits directamente con NumPy y formateamos.
    # A 1M filas esto es ~3x más rápido que [str(uuid.uuid4()) for _ in range(n)].
    uuid_high = rng.integers(0, 2**64, size=n, dtype=np.uint64)
    uuid_low  = rng.integers(0, 2**64, size=n, dtype=np.uint64)
    transaction_ids = [
        f"{h:016x}-{l:04x}-{(l>>16)&0xffff:04x}-{(l>>32)&0xffff:04x}-{l>>48:012x}"
        for h, l in zip(uuid_high, uuid_low)
    ]

    return pd.DataFrame({
        "transaction_id": transaction_ids,
        "timestamp":      timestamps,
        "user_id":        rng.integers(1, 50_001,  size=n).astype(np.int32),
        "merchant_id":    rng.integers(1, 10_001,  size=n).astype(np.int32),
        "amount":         np.round(rng.uniform(0.01, 5_000.00, size=n), 2),
        "category":       rng.choice(CATEGORIES, size=n),
        "country_code":   rng.choice(COUNTRIES,  size=n),
        "status":         rng.choice(STATUSES, size=n, p=STATUS_PROBS),
    })


# ---------------------------------------------------------------------------
# Validaciones post-generación
# ---------------------------------------------------------------------------

def validate_dataframe(df: pd.DataFrame, n: int) -> None:
    """
    Verifica que el DataFrame generado cumple exactamente con el schema.
    Lanza AssertionError si algo no cuadra.
    """
    assert len(df) == n, f"Se esperaban {n} filas, se obtuvieron {len(df)}"
    assert df["transaction_id"].nunique() == n, "transaction_id tiene duplicados"
    assert df["amount"].between(0.01, 5_000.00).all(), "amount fuera de rango"
    assert df["user_id"].between(1, 50_000).all(),     "user_id fuera de rango"
    assert df["merchant_id"].between(1, 10_000).all(), "merchant_id fuera de rango"
    assert set(df["category"].unique()).issubset(set(CATEGORIES)), "category inválida"
    assert set(df["country_code"].unique()).issubset(set(COUNTRIES)), "country_code inválida"
    assert set(df["status"].unique()).issubset(set(STATUSES)), "status inválido"

    # Verificar distribución aproximada de status (±3% de tolerancia)
    dist = df["status"].value_counts(normalize=True)
    assert abs(dist.get("completed", 0) - 0.85) < 0.03, "distribución 'completed' fuera de rango"
    assert abs(dist.get("failed",    0) - 0.10) < 0.03, "distribución 'failed' fuera de rango"
    assert abs(dist.get("pending",   0) - 0.05) < 0.03, "distribución 'pending' fuera de rango"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Genera dataset de transacciones financieras sintéticas.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python generate_data.py --size 100k
  python generate_data.py --size 1m --validate
        """,
    )
    parser.add_argument(
        "--size",
        choices=list(SIZES.keys()),
        required=True,
        help="Tamaño del dataset a generar.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        default=False,
        help="Valida el schema del DataFrame después de generarlo.",
    )
    args = parser.parse_args()

    n = SIZES[args.size]

    # data/ vive en la raíz del repositorio, compartida por todos los ejercicios.
    # generate_data.py está en ejercicio-01-formatos/, así que subimos dos niveles.
    # Estructura esperada:
    #   mi-modulo-datos/
    #   ├── data/                  ← aquí
    #   ├── ejercicio-01-formatos/
    #   │   └── generate_data.py   ← aquí estamos
    out_dir  = Path(__file__).parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"transactions_{args.size}.csv"

    print(f"Generando {n:,} registros (seed={RANDOM_SEED})...")
    t0 = time.perf_counter()
    df = generate_dataframe(n)
    gen_time = time.perf_counter() - t0
    print(f"  Generación: {gen_time:.2f}s")

    if args.validate:
        print("  Validando schema...")
        validate_dataframe(df, n)
        print("  Schema OK")

    print(f"  Guardando en {out_path}...")
    t0 = time.perf_counter()
    df.to_csv(out_path, index=False)
    write_time = time.perf_counter() - t0

    size_mb = out_path.stat().st_size / 1e6
    print(f"  Escritura: {write_time:.2f}s  |  Tamaño: {size_mb:.1f} MB")
    print(f"Done. {out_path}")


if __name__ == "__main__":
    main()