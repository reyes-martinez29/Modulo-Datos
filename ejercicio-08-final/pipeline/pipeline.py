"""
pipeline/pipeline.py — Orquestador del pipeline ETL desde CSV.

Adapta el orquestador del E6 a la fuente del E8 (un CSV externo en lugar del
generador sintético). El cambio es de una sola capa: donde el E6 llamaba a
generate_batch(), aquí se llama a read_csv_text/read_csv_file. Las capas
extract, transform y load se reutilizan intactas del E6.

Se conservan las dos invariantes matemáticas que el E6 verificaba con assert
antes de devolver el reporte, adaptando la primera al hecho de que ahora la
fuente es un CSV:

    filas_csv == extracted + parse_errors
    extracted == valid + rejected
    inserted + duplicates == valid

run_pipeline_csv() es invocable como función (lo usa el endpoint
POST /pipeline/ingest de la API) y también desde la CLI al final del archivo.
Devolver un dict con el reporte, en lugar de imprimirlo, es lo que permite
que la API lo serialice como JSON sin lógica adicional.
"""

import time
from pathlib import Path
from typing import Optional

from pipeline.csv_source import read_csv_text, read_csv_file
from pipeline.extract import extract
from pipeline.transform import transform, write_quarantine
from pipeline.load import load


def run_pipeline_csv(
    csv_text: Optional[str] = None,
    csv_path: Optional[str] = None,
    db_path: str = "",
    quarantine_dir: str | Path = "quarantine",
    max_rows: Optional[int] = None,
) -> dict:
    """
    Ejecuta el pipeline completo sobre un CSV: read → extract → transform → load.

    Se debe pasar exactamente uno de csv_text o csv_path. csv_text es lo que
    usa el endpoint de la API (el contenido subido); csv_path es lo que usa
    la CLI.

    Parámetros
    ----------
    csv_text       : contenido del CSV como string (mutuamente excluyente con csv_path)
    csv_path       : ruta a un CSV en disco (mutuamente excluyente con csv_text)
    db_path        : ruta a la base SQLite destino
    quarantine_dir : directorio para las filas rechazadas
    max_rows       : tope de filas del CSV (None usa el default de csv_source)

    Retorna
    -------
    dict con el reporte completo: filas leídas, normalizadas, válidas,
    rechazadas (con desglose por tipo), insertadas, duplicadas, tiempo y la
    verificación explícita de invariantes.

    Raises
    ------
    ValueError        — si no se pasa csv_text ni csv_path, o se pasan ambos
    CSVStructureError — si el CSV no tiene la estructura esperada
    """
    if (csv_text is None) == (csv_path is None):
        raise ValueError("Debes pasar exactamente uno de csv_text o csv_path.")

    t_start = time.perf_counter()

    # Paso 1 — leer el CSV (valida estructura del archivo)
    read_kwargs = {} if max_rows is None else {"max_rows": max_rows}
    if csv_text is not None:
        raw_rows = read_csv_text(csv_text, **read_kwargs)
    else:
        raw_rows = read_csv_file(csv_path, **read_kwargs)

    n_csv = len(raw_rows)

    # Paso 2 — extracción y normalización (capa del E6, intacta)
    extracted, parse_errors = extract(raw_rows)

    # Paso 3 — transformación y validación (capa del E6, intacta)
    valid, rejected = transform(extracted)

    by_error: dict[str, int] = {}
    for r in rejected:
        key = r.get("rejection_type", "other")
        by_error[key] = by_error.get(key, 0) + 1

    qfile = write_quarantine(rejected, quarantine_dir)

    # Paso 4 — carga en SQLite (capa del E6, intacta)
    inserted, duplicates = load(valid, db_path=db_path)

    total_time = round(time.perf_counter() - t_start, 3)

    # Invariantes — mismas del E6, con la primera adaptada a la fuente CSV
    assert n_csv == len(extracted) + len(parse_errors), (
        f"INVARIANTE ROTA: filas_csv({n_csv}) != "
        f"extracted({len(extracted)}) + parse_errors({len(parse_errors)})"
    )
    assert len(extracted) == len(valid) + len(rejected), (
        f"INVARIANTE ROTA: extracted({len(extracted)}) != "
        f"valid({len(valid)}) + rejected({len(rejected)})"
    )
    assert inserted + duplicates == len(valid), (
        f"INVARIANTE ROTA: inserted({inserted}) + "
        f"duplicates({duplicates}) != valid({len(valid)})"
    )

    return {
        "rows_in_csv": n_csv,
        "extracted": len(extracted),
        "parse_errors": len(parse_errors),
        "valid": len(valid),
        "rejected": len(rejected),
        "by_error": by_error,
        "inserted": inserted,
        "duplicates": duplicates,
        "quarantine_file": str(qfile),
        "total_time_s": total_time,
        "invariants": {
            "csv_eq_extracted_plus_parse_errors": n_csv == len(extracted) + len(parse_errors),
            "extracted_eq_valid_plus_rejected": len(extracted) == len(valid) + len(rejected),
            "inserted_plus_duplicates_eq_valid": inserted + duplicates == len(valid),
        },
    }


# ---------------------------------------------------------------------------
# CLI — para correr el pipeline sobre un CSV desde la terminal
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Pipeline ETL desde CSV: read → extract → transform → load",
    )
    parser.add_argument("--csv", required=True, help="Ruta al CSV de entrada")
    parser.add_argument("--db", required=True, help="Ruta a la base SQLite destino")
    parser.add_argument("--quarantine", default="quarantine",
                        help="Directorio de cuarentena (default: quarantine/)")
    args = parser.parse_args()

    report = run_pipeline_csv(
        csv_path=args.csv,
        db_path=args.db,
        quarantine_dir=args.quarantine,
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()