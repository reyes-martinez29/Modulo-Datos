"""
benchmark.py — Benchmark de query engines: pandas, DuckDB y polars.

Uso:
    python benchmark.py --output results/
    python benchmark.py --output results/ --parquet ../../data/transactions_1m_parquet_snappy.parquet
    python benchmark.py --output results/ --repeats 5

Flujo por cada query y engine:
    1. gc.collect() — elimina basura residual antes de medir.
    2. tracemalloc.start() — comienza a rastrear allocaciones Python.
    3. time.perf_counter() — inicia el cronómetro.
    4. Ejecuta la función del engine.
    5. Registra tiempo y pico de RAM.
    6. Repite REPEATS veces y reporta el promedio.

Validación de equivalencia:
    Después de medir los tres engines, compara los resultados de pandas
    vs DuckDB y pandas vs polars. Si los resultados no son numéricamente
    equivalentes, registra el error en el JSON — no aborta el benchmark,
    para que puedas ver los resultados aunque haya divergencias.

    La equivalencia se valida con tolerancia numérica (atol=1e-6) en
    columnas float. Columnas string y enteras se comparan exactamente.
    Antes de comparar, ambos DataFrames se ordenan por las mismas columnas
    para que el orden no cause falsos negativos.
"""

import argparse
import gc
import json
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

# Importar los tres engines
sys.path.insert(0, str(Path(__file__).parent))
from engines import pandas_engine, duckdb_engine, polars_engine


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

REPEATS = 3  # Repeticiones por medición — promedio de 3 runs

# Ruta default al Parquet de E1. Se puede sobreescribir con --parquet.
DEFAULT_PARQUET = str(
    Path(__file__).parent.parent / "data" / "transactions_1m_parquet_snappy.parquet"
)

# Registro de queries: ID → función en cada engine
# El orden de las columnas de sort determina cómo se ordenan los DataFrames
# antes de la validación de equivalencia.
QUERIES = {
    "Q1": {
        "pandas": pandas_engine.q1,
        "duckdb": duckdb_engine.q1,
        "polars": polars_engine.q1,
        "sort_by": ["country_code"],
        "description": "Conteo por country_code",
    },
    "Q2": {
        "pandas": pandas_engine.q2,
        "duckdb": duckdb_engine.q2,
        "polars": polars_engine.q2,
        "sort_by": ["category"],
        "description": "Stats de amount por category",
    },
    "Q3": {
        "pandas": pandas_engine.q3,
        "duckdb": duckdb_engine.q3,
        "polars": polars_engine.q3,
        "sort_by": ["user_id"],
        "description": "Top 10 usuarios por amount",
        "explain": duckdb_engine.explain_q3,
    },
    "Q4": {
        "pandas": pandas_engine.q4,
        "duckdb": duckdb_engine.q4,
        "polars": polars_engine.q4,
        "sort_by": ["hour"],
        "description": "Transacciones fallidas por hora",
    },
    "Q5": {
        "pandas": pandas_engine.q5,
        "duckdb": duckdb_engine.q5,
        "polars": polars_engine.q5,
        "sort_by": ["transaction_id"],
        "description": "Filtro fecha+país+amount",
        "explain": duckdb_engine.explain_q5,
    },
    "Q6": {
        "pandas": pandas_engine.q6,
        "duckdb": duckdb_engine.q6,
        "polars": polars_engine.q6,
        "sort_by": ["country_code"],
        "description": "Top category por country_code",
        "explain": duckdb_engine.explain_q6,
    },
    "Q7": {
        "pandas": pandas_engine.q7,
        "duckdb": duckdb_engine.q7,
        "polars": polars_engine.q7,
        "sort_by": ["user_id"],
        "description": "Usuarios con >5 transacciones fallidas",
    },
    "Q8": {
        "pandas": pandas_engine.q8,
        "duckdb": duckdb_engine.q8,
        "polars": polars_engine.q8,
        "sort_by": ["day", "category"],
        "description": "Promedio diario por category",
    },
}


# ---------------------------------------------------------------------------
# Medición
# ---------------------------------------------------------------------------

def measure_query(fn: Callable, path: str, repeats: int) -> dict:
    """
    Ejecuta una función de query `repeats` veces y retorna métricas.

    Cada run:
      1. gc.collect() — fuerza recolección de basura para eliminar ruido.
      2. tracemalloc.start() — comienza a rastrear allocaciones Python.
      3. Ejecuta la función y captura tiempo y pico de RAM.
      4. Descarta el resultado con del + gc.collect() para no contaminar
         la siguiente iteración con memoria residual.

    Retorna:
        avg_s   : promedio de tiempo en segundos
        min_s   : mínimo de tiempo en segundos
        max_s   : máximo de tiempo en segundos
        runs_s  : lista de tiempos individuales
        peak_mb : promedio de pico de RAM en MB
        result  : el DataFrame del último run (para validación)
    """
    times    = []
    peak_mbs = []
    result   = None

    for run in range(repeats):
        gc.collect()
        tracemalloc.start()

        t0     = time.perf_counter()
        result = fn(path)
        elapsed = time.perf_counter() - t0

        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        times.append(elapsed)
        peak_mbs.append(peak_bytes / 1e6)

        # Liberar memoria del resultado excepto en el último run
        if run < repeats - 1:
            del result
            gc.collect()

    return {
        "avg_s":   round(sum(times) / len(times), 4),
        "min_s":   round(min(times), 4),
        "max_s":   round(max(times), 4),
        "runs_s":  [round(t, 4) for t in times],
        "peak_mb": round(sum(peak_mbs) / len(peak_mbs), 2),
        "result":  result,
    }


# ---------------------------------------------------------------------------
# Validación de equivalencia
# ---------------------------------------------------------------------------

def _sort_df(df: pd.DataFrame, sort_cols: list[str]) -> pd.DataFrame:
    """
    Ordena el DataFrame por las columnas indicadas para que la comparación
    entre engines no falle por diferencia de orden de filas.

    Solo ordena por columnas que existen en el DataFrame — algunas queries
    pueden no tener exactamente las mismas columnas en casos de error.
    """
    existing = [c for c in sort_cols if c in df.columns]
    return df.sort_values(existing).reset_index(drop=True)


def validate_equivalence(
    df_a: pd.DataFrame,
    df_b: pd.DataFrame,
    sort_cols: list[str],
    label_a: str,
    label_b: str,
) -> dict:
    """
    Compara dos DataFrames numéricamente.

    Retorna un dict con:
        equivalent : bool — True si son equivalentes
        details    : str  — descripción de la diferencia si no lo son

    Estrategia de comparación:
        - Columnas float: np.allclose con atol=1e-6 (tolerancia absoluta).
          Los engines pueden diferir en el último decimal por el orden
          de operaciones de punto flotante.
        - Columnas int/str: comparación exacta con ==.
        - Filas: deben ser el mismo número después de ordenar.
    """
    try:
        a = _sort_df(df_a, sort_cols)
        b = _sort_df(df_b, sort_cols)

        if a.shape != b.shape:
            return {
                "equivalent": False,
                "details": f"Forma distinta: {label_a}={a.shape} vs {label_b}={b.shape}",
            }

        errors = []
        for col in a.columns:
            if col not in b.columns:
                errors.append(f"Columna '{col}' no existe en {label_b}")
                continue

            col_a = a[col]
            col_b = b[col]

            # Detectar tipo de columna de forma compatible con pandas 2.x.
            # pandas 2.x usa StringDtype para columnas de texto leídas de Parquet,
            # que NO es reconocido por np.issubdtype(). Por eso usamos
            # pd.api.types que sí cubre todos los tipos de pandas.
            is_float = pd.api.types.is_float_dtype(col_a)
            is_int   = pd.api.types.is_integer_dtype(col_a)
            is_num   = is_float or is_int

            if is_float:
                # Floats: tolerancia numérica para diferencias de punto flotante
                try:
                    va = col_a.astype(float).values
                    vb = col_b.astype(float).values
                    if not np.allclose(va, vb, atol=1e-6, equal_nan=True):
                        max_diff = np.nanmax(np.abs(va - vb))
                        errors.append(
                            f"Columna '{col}': diferencia máxima={max_diff:.2e}"
                        )
                except Exception as e:
                    errors.append(f"Columna '{col}': error comparando floats: {e}")
            else:
                # Strings, enteros, fechas: comparación exacta como strings
                # Convertir a str normaliza StringDtype, object, int64, etc.
                va = col_a.astype(str).values
                vb = col_b.astype(str).values
                mismatches = (va != vb).sum()
                if mismatches > 0:
                    errors.append(
                        f"Columna '{col}': {mismatches} valores distintos"
                    )

        if errors:
            return {"equivalent": False, "details": " | ".join(errors)}

        return {"equivalent": True, "details": "OK"}

    except Exception as e:
        return {"equivalent": False, "details": f"Error en validación: {e}"}


# ---------------------------------------------------------------------------
# Benchmark principal
# ---------------------------------------------------------------------------

def run_benchmark(parquet_path: str, output_dir: Path, repeats: int) -> dict:
    """
    Ejecuta las 8 queries en los 3 engines y valida equivalencia.

    Orden de ejecución:
        Para cada query: primero mide los 3 engines, luego valida.
        Esto minimiza la interferencia entre mediciones de engines distintos.
    """
    if not Path(parquet_path).exists():
        print(f"ERROR: No se encontró el Parquet en {parquet_path}")
        print("Corre primero: python benchmark_cli.py --size 1m (en ejercicio-01-formatos/)")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)
    results = {
        "parquet_path": parquet_path,
        "repeats": repeats,
        "queries": {},
    }

    engines_order = ["pandas", "duckdb", "polars"]

    for query_id, config in QUERIES.items():
        print(f"\n{'='*55}")
        print(f"{query_id} — {config['description']}")
        print(f"{'='*55}")

        query_result = {"engines": {}, "validation": {}, "explain": None}
        engine_results = {}  # guardamos resultados para validar después

        # --- Medir cada engine ---
        for engine_name in engines_order:
            fn = config[engine_name]
            print(f"  [{engine_name}]")

            metrics = measure_query(fn, parquet_path, repeats)
            engine_results[engine_name] = metrics["result"]

            # Guardar métricas sin el DataFrame (no es serializable a JSON)
            query_result["engines"][engine_name] = {
                "avg_s":   metrics["avg_s"],
                "min_s":   metrics["min_s"],
                "max_s":   metrics["max_s"],
                "runs_s":  metrics["runs_s"],
                "peak_mb": metrics["peak_mb"],
                "rows":    len(metrics["result"]) if metrics["result"] is not None else 0,
            }
            print(f"    avg: {metrics['avg_s']:.3f}s | "
                  f"RAM: {metrics['peak_mb']:.1f}MB | "
                  f"filas: {query_result['engines'][engine_name]['rows']}")

        # --- Validar equivalencia ---
        sort_cols = config["sort_by"]
        val_pd_duck = validate_equivalence(
            engine_results["pandas"], engine_results["duckdb"],
            sort_cols, "pandas", "duckdb",
        )
        val_pd_pol = validate_equivalence(
            engine_results["pandas"], engine_results["polars"],
            sort_cols, "pandas", "polars",
        )

        query_result["validation"] = {
            "pandas_vs_duckdb": val_pd_duck,
            "pandas_vs_polars": val_pd_pol,
            "all_equivalent": (val_pd_duck["equivalent"] and val_pd_pol["equivalent"]),
        }

        status = "✓" if query_result["validation"]["all_equivalent"] else "✗"
        print(f"  Validación: {status} pandas↔duckdb={val_pd_duck['details']} | "
              f"pandas↔polars={val_pd_pol['details']}")

        # --- EXPLAIN ANALYZE (solo Q3, Q5, Q6) ---
        if "explain" in config:
            print(f"  Capturando EXPLAIN ANALYZE...")
            try:
                plan = config["explain"](parquet_path)
                query_result["explain"] = plan
            except Exception as e:
                query_result["explain"] = f"Error: {e}"

        results["queries"][query_id] = query_result

        # Liberar resultados de esta query
        del engine_results
        gc.collect()

    # --- Guardar JSON ---
    out_path = output_dir / "benchmark_results.json"
    out_path.write_text(
        json.dumps(results, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\n{'='*55}")
    print(f"Resultados guardados en {out_path}")

    return results


# ---------------------------------------------------------------------------
# Resumen en consola
# ---------------------------------------------------------------------------

def print_summary(results: dict) -> None:
    """Imprime tabla resumen de tiempos por query y engine."""
    print(f"\n{'='*55}")
    print("RESUMEN — Tiempos promedio (segundos)")
    print(f"{'='*55}")
    header = f"{'Query':<6} {'pandas':>10} {'duckdb':>10} {'polars':>10} {'equiv':>6}"
    print(header)
    print("-" * len(header))

    for qid, qdata in results["queries"].items():
        eng = qdata["engines"]
        eq  = "✓" if qdata["validation"]["all_equivalent"] else "✗"
        print(
            f"{qid:<6} "
            f"{eng['pandas']['avg_s']:>10.3f} "
            f"{eng['duckdb']['avg_s']:>10.3f} "
            f"{eng['polars']['avg_s']:>10.3f} "
            f"{eq:>6}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark de query engines: pandas, DuckDB y polars.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python benchmark.py --output results/
  python benchmark.py --output results/ --repeats 5
  python benchmark.py --output results/ --parquet ../../data/transactions_1m_parquet_snappy.parquet
        """,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results"),
        help="Directorio donde guardar results/benchmark_results.json",
    )
    parser.add_argument(
        "--parquet",
        type=str,
        default=DEFAULT_PARQUET,
        help="Ruta al archivo Parquet de E1. Default: ../../data/transactions_1m_parquet_snappy.parquet",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=REPEATS,
        help=f"Repeticiones por medición (default: {REPEATS})",
    )
    args = parser.parse_args()

    print(f"Parquet: {args.parquet}")
    print(f"Output:  {args.output}")
    print(f"Repeats: {args.repeats}")

    results = run_benchmark(args.parquet, args.output, args.repeats)
    print_summary(results)


if __name__ == "__main__":
    main()