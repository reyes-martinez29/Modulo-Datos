"""
load.py — Capa de carga del pipeline.

RESPONSABILIDAD ÚNICA: insertar filas válidas en SQLite de forma
transaccional e idempotente.

Dos propiedades garantizadas:
    1. IDEMPOTENCIA: INSERT OR IGNORE por transaction_id — correr la carga
       dos veces con los mismos datos produce el mismo resultado final.
       Las filas ya existentes se ignoran silenciosamente.

    2. TRANSACCIONALIDAD: si la carga falla a mitad (ej: error de disco,
       excepción inesperada), ninguna fila queda insertada parcialmente.
       Se usa una transacción explícita con BEGIN/COMMIT — si hay error,
       SQLite hace rollback automático al salir del contexto 'with conn'.

Por qué INSERT OR IGNORE y no INSERT OR REPLACE:
    INSERT OR REPLACE eliminaría la fila existente y la reinsertaría con
    los mismos datos — tiene el mismo efecto pero con costo adicional y
    podría disparar triggers o cambiar el rowid. INSERT OR IGNORE es la
    opción correcta para deduplicación por PK.

Por qué apunta a la base del E3 (transactions.db):
    El enunciado dice explícitamente "carga en la base SQLite del E3".
    Esa base ya tiene los índices correctos (idx_user_timestamp e
    idx_country_user) y el schema validado. No se crea una base nueva.

Uso como módulo:
    from load import load
    inserted, duplicates = load(valid_rows, db_path="../../data/transactions.db")

Uso como script:
    python load.py --input valid.json --db ../../data/transactions.db
"""

import argparse
import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# Ruta por defecto a la base del E3
DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "transactions.db"


# ---------------------------------------------------------------------------
# Función principal de carga
# ---------------------------------------------------------------------------

def load(
    rows:    list[dict],
    db_path: str | Path = DEFAULT_DB_PATH,
) -> tuple[int, int]:
    """
    Inserta filas válidas en la base SQLite del E3.

    Usa INSERT OR IGNORE para garantizar idempotencia: si una fila con el
    mismo transaction_id ya existe, se ignora sin error. La carga completa
    ocurre en una sola transacción — si cualquier INSERT falla, SQLite
    hace rollback de todas las inserciones del batch.

    Parámetros
    ----------
    rows    : list[dict] — filas validadas por transform(), listas para insertar
    db_path : ruta a la base SQLite del E3

    Retorna
    -------
    inserted   : int — número de filas efectivamente insertadas (nuevas)
    duplicates : int — número de filas ignoradas por transaction_id duplicado

    Raises
    ------
    FileNotFoundError — si db_path no existe
    sqlite3.Error     — si hay un error de base de datos (con rollback automático)
    """
    db_path = Path(db_path)

    if not db_path.exists():
        raise FileNotFoundError(
            f"Base de datos no encontrada: {db_path}\n"
            "Corre primero ingest.py del Ejercicio 3:\n"
            "  python ingest.py --wal --chunk-size 20000"
        )

    if not rows:
        logger.info("Sin filas para insertar.")
        return 0, 0

    conn = sqlite3.connect(str(db_path))

    try:
        # Contar filas antes de la inserción para calcular duplicados
        # No podemos confiar en rowcount con INSERT OR IGNORE porque
        # SQLite no incrementa rowcount para las filas ignoradas en todas
        # las versiones. La forma correcta es comparar conteos antes/después.
        count_before = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE transaction_id IN ({})".format(
                ",".join("?" * len(rows))
            ),
            [r["transaction_id"] for r in rows],
        ).fetchone()[0]

        existing_ids = set(
            row[0]
            for row in conn.execute(
                "SELECT transaction_id FROM transactions WHERE transaction_id IN ({})".format(
                    ",".join("?" * len(rows))
                ),
                [r["transaction_id"] for r in rows],
            ).fetchall()
        )

        duplicates = len(existing_ids)
        new_rows   = [r for r in rows if r["transaction_id"] not in existing_ids]

        if not new_rows:
            logger.info("Todas las filas (%d) son duplicados — sin inserción.", len(rows))
            conn.close()
            return 0, duplicates

        # Inserción transaccional — BEGIN implícito en 'with conn'
        # Si cualquier INSERT falla, SQLite hace rollback automático
        with conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO transactions
                    (transaction_id, timestamp, user_id, merchant_id,
                     amount, category, country_code, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        r["transaction_id"],
                        r["timestamp"],
                        int(r["user_id"]),
                        int(r["merchant_id"]),
                        float(r["amount"]),
                        r["category"],
                        r["country_code"],
                        r["status"],
                    )
                    for r in new_rows
                ],
            )

        inserted = len(new_rows)
        logger.info(
            "Carga completada: %d insertadas, %d duplicadas.",
            inserted, duplicates,
        )
        return inserted, duplicates

    except sqlite3.Error as e:
        # El 'with conn' hace rollback automático si hay excepción
        logger.error("Error en la carga: %s — rollback aplicado.", e)
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI — para probar la capa de forma independiente
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Carga un batch JSON de filas válidas en la base SQLite del E3.",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="JSON de entrada (filas válidas de transform)",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"Ruta a la base SQLite (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args()

    rows = json.loads(Path(args.input).read_text(encoding="utf-8"))
    inserted, duplicates = load(rows, db_path=args.db)

    print(f"Carga:")
    print(f"  Recibidas:  {len(rows)}")
    print(f"  Insertadas: {inserted}")
    print(f"  Duplicadas: {duplicates}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()