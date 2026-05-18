"""
benchmark_cli.py — CLI principal del benchmark de formatos.

Uso:
    python benchmark_cli.py --size 1m --formats csv jsonl parquet_snappy parquet_gzip
    python benchmark_cli.py --size 100k   # corre todos los formatos por default

Salida:
    results/benchmark_{size}.json   — métricas completas en JSON
    Impresión en consola de cada run en tiempo real
"""

import argparse
import json
import sys
import time
from pathlib import Path

# Aseguramos que el directorio raíz del ejercicio esté en el path
# para importar generate_data sin instalar el paquete
sys.path.insert(0, str(Path(__file__).parent))

from generate_data import generate_dataframe, SIZES
from storage_benchmark import writers, readers, metrics


# ---------------------------------------------------------------------------
# Registro de formatos disponibles
# ---------------------------------------------------------------------------
# Cada entrada: nombre → (write_fn, read_fn, extensión_de_archivo)
# La extensión determina el nombre del archivo temporal en data/.

FORMAT_REGISTRY = {
    "csv": (
        writers.write_csv,
        readers.read_csv,
        "csv",
    ),
    "jsonl": (
        writers.write_jsonl,
        readers.read_jsonl,
        "jsonl",
    ),
    "parquet": (
        writers.write_parquet_plain,
        readers.read_parquet_plain,
        "parquet",
    ),
    "parquet_snappy": (
        writers.write_parquet_snappy,
        readers.read_parquet_snappy,
        "parquet",
    ),
    "parquet_gzip": (
        writers.write_parquet_gzip,
        readers.read_parquet_gzip,
        "parquet",
    ),
}

ALL_FORMATS = list(FORMAT_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Lógica principal del benchmark
# ---------------------------------------------------------------------------

def run_benchmark(size: str, formats: list[str], repeats: int) -> dict:
    """
    Ejecuta el benchmark completo para una escala dada.

    Flujo:
      1. Genera el DataFrame en memoria (no se mide).
      2. Para cada formato, mide escritura + lectura completa + lectura selectiva.
      3. Guarda resultados en results/benchmark_{size}.json.

    Parámetros
    ----------
    size    : "100k", "500k" o "1m".
    formats : lista de nombres de formato del FORMAT_REGISTRY.
    repeats : número de repeticiones por medición.

    Retorna
    -------
    dict con todos los resultados estructurados.
    """
    n = SIZES[size]

    print("=" * 60)
    print(f"Benchmark — escala: {size}  ({n:,} filas)")
    print(f"Formatos: {', '.join(formats)}")
    print(f"Repeticiones por medición: {repeats}")
    print("=" * 60)

    # --- Paso 1: generar el dataset en memoria ---
    # Este tiempo NO cuenta como tiempo de escritura.
    # El evaluador revisa que esta separación esté explícita.
    print("\nGenerando dataset en memoria (esto no se mide)...")
    t_gen_start = time.perf_counter()
    df = generate_dataframe(n)
    t_gen = time.perf_counter() - t_gen_start
    print(f"  Dataset listo en {t_gen:.2f}s — {df.memory_usage(deep=True).sum() / 1e6:.1f} MB en RAM\n")

    # data/ vive en la raíz del repositorio (compartida entre ejercicios).
    # results/ vive dentro de ejercicio-01-formatos/ (resultados de este ejercicio).
    data_dir    = Path(__file__).parent.parent / "data"
    results_dir = Path(__file__).parent / "results"
    data_dir.mkdir(exist_ok=True)
    results_dir.mkdir(exist_ok=True)

    # --- Paso 2: benchmark por formato ---
    format_results = {}

    for fmt in formats:
        if fmt not in FORMAT_REGISTRY:
            print(f"  WARN: formato '{fmt}' no reconocido, se omite.")
            continue

        write_fn, read_fn, ext = FORMAT_REGISTRY[fmt]

        fmt_result = metrics.measure_format(
            fmt_name=fmt,
            write_fn=write_fn,
            read_fn=read_fn,
            df=df,
            data_dir=data_dir,
            ext=ext,
            n_rows=n,
            repeats=repeats,
        )
        format_results[fmt] = fmt_result

        # Resumen compacto después de cada formato
        mb = fmt_result["size_bytes"] / 1e6
        print(f"  => Escritura avg: {fmt_result['write_avg_s']:.3f}s | "
              f"Lectura avg: {fmt_result['read_full_avg_s']:.3f}s | "
              f"Tamaño: {mb:.1f}MB")

    # --- Paso 3: armar el resultado final ---
    output = {
        "size":           size,
        "n_rows":         n,
        "generation_s":   round(t_gen, 3),
        "repeats":        repeats,
        "formats":        format_results,
    }

    # Guardar JSON
    out_path = results_dir / f"benchmark_{size}.json"
    out_path.write_text(json.dumps(output, indent=2, default=str))
    print(f"\nResultados guardados en {out_path}")

    return output


# ---------------------------------------------------------------------------
# Tabla de resumen en consola
# ---------------------------------------------------------------------------

def print_summary(results: dict) -> None:
    """Imprime una tabla comparativa de todos los formatos."""
    size = results["size"]
    print(f"\n{'='*60}")
    print(f"RESUMEN — {size}")
    print(f"{'='*60}")
    header = f"{'Formato':<18} {'Escritura(s)':>12} {'LecturaFull(s)':>15} {'LecturaSel(s)':>14} {'Tamaño(MB)':>11} {'RAM(MB)':>8}"
    print(header)
    print("-" * len(header))

    for fmt, m in results["formats"].items():
        mb = m["size_bytes"] / 1e6
        print(
            f"{fmt:<18} "
            f"{m['write_avg_s']:>12.3f} "
            f"{m['read_full_avg_s']:>15.3f} "
            f"{m['read_selective_avg_s']:>14.3f} "
            f"{mb:>11.1f} "
            f"{m['read_full_peak_mb']:>8.1f}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark de formatos de almacenamiento: CSV, JSONL, Parquet.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python benchmark_cli.py --size 1m
  python benchmark_cli.py --size 100k --formats csv parquet_gzip
  python benchmark_cli.py --size 500k --repeats 5
        """,
    )
    parser.add_argument(
        "--size",
        choices=list(SIZES.keys()),
        required=True,
        help="Escala del dataset.",
    )
    parser.add_argument(
        "--formats",
        nargs="+",
        choices=ALL_FORMATS,
        default=ALL_FORMATS,
        metavar="FORMAT",
        help=f"Formatos a medir. Opciones: {', '.join(ALL_FORMATS)}. Default: todos.",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="Número de repeticiones por medición (default: 3).",
    )
    args = parser.parse_args()

    results = run_benchmark(args.size, args.formats, args.repeats)
    print_summary(results)


if __name__ == "__main__":
    main()