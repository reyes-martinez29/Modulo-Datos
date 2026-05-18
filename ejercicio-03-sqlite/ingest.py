"""
ingest.py — Pipeline de ingesta del dataset de transacciones a SQLite.

Uso:
    python ingest.py --wal
    python ingest.py --no-wal
    python ingest.py --wal --chunk-size 20000
    python ingest.py --no-wal --chunk-size 20000

La ingesta usa transacciones explícitas: un commit por chunk, no por fila.
Un commit por fila en SQLite a 1M filas tarda ~15 minutos porque cada commit
implica un fsync al disco. Con chunks de 10k-50k filas, la ingesta completa
tarda 1-2 minutos.

El archivo .db se guarda en data/transactions.db (raíz del módulo, no en
este directorio) para que benchmark_queries.py lo encuentre con la misma
ruta que usa para el Parquet de E1.

Flujo:
    1. Crea la base de datos aplicando schema.sql
    2. Configura WAL mode si se solicita (antes de cualquier escritura)
    3. Lee el CSV en chunks con pandas
    4. Por cada chunk:
       a. Abre una transacción explícita (BEGIN)
       b. Inserta las filas del chunk con executemany
       c. Hace commit (END TRANSACTION)
    5. Verifica la ingesta comparando conteos con el CSV original
    6. Guarda métricas en results/ingest_results.json
"""

import argparse
import json
import sqlite3
import time
from pathlib import Path

import pandas as pd


# ---------------------------------------------------------------------------
# Rutas — relativas a la raíz del módulo
# ---------------------------------------------------------------------------

ROOT_DIR   = Path(__file__).parent.parent
SCHEMA_SQL = Path(__file__).parent / "schema.sql"
CSV_PATH   = ROOT_DIR / "data" / "transactions_1m.csv"
DB_PATH    = ROOT_DIR / "data" / "transactions.db"
RESULTS_DIR = Path(__file__).parent / "results"

# Chunk size por defecto: 20k filas por commit.
# Elegido tras análisis del tradeoff:
# - Muy pequeño (1k): demasiados commits, overhead de transacción domina
# - Muy grande (100k): pocos commits pero mayor presión de memoria
# - 20k: ~50 commits para 1M filas, balance óptimo entre overhead y memoria
DEFAULT_CHUNK_SIZE = 20_000


# ---------------------------------------------------------------------------
# Creación y configuración de la base de datos
# ---------------------------------------------------------------------------

def create_database(db_path: Path, wal_mode: bool) -> sqlite3.Connection:
    """
    Crea la base de datos SQLite y aplica el schema.

    El orden importa:
    1. Abrir conexión (crea el archivo si no existe)
    2. Configurar WAL ANTES de crear tablas — el pragma JOURNAL_MODE
       aplica a todas las operaciones subsiguientes de la conexión
    3. Aplicar el schema.sql completo

    Parámetros
    ----------
    db_path  : ruta donde crear el archivo .db
    wal_mode : True = activar WAL, False = usar journal por defecto

    Retorna
    -------
    Conexión SQLite configurada y lista para recibir datos.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Eliminar la base si ya existe para empezar desde cero
    if db_path.exists():
        db_path.unlink()
        # WAL crea archivos auxiliares .db-wal y .db-shm
        for suffix in ["-wal", "-shm"]:
            aux = db_path.with_suffix(db_path.suffix + suffix)
            if aux.exists():
                aux.unlink()

    conn = sqlite3.connect(str(db_path))

    # Optimizaciones de rendimiento para ingesta masiva
    # Estas configuraciones son seguras para ingesta porque:
    # - synchronous=NORMAL: reduce fsyncs sin riesgo de corrupción en caídas
    #   de aplicación (solo arriesga datos si hay falla de hardware durante
    #   la escritura, aceptable para un pipeline de ingesta)
    # - cache_size: 64MB de caché de páginas reduce I/O repetido
    # - temp_store=MEMORY: las tablas temporales van a RAM, no a disco
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -65536")   # 64MB (negativo = KB)
    conn.execute("PRAGMA temp_store = MEMORY")

    if wal_mode:
        result = conn.execute("PRAGMA journal_mode = WAL").fetchone()
        print(f"  Journal mode: {result[0]}")
    else:
        result = conn.execute("PRAGMA journal_mode").fetchone()
        print(f"  Journal mode: {result[0]} (delete, modo por defecto)")

    # Aplicar el schema desde schema.sql.
    #
    # Por qué no usamos executescript() directamente con el contenido crudo:
    # executescript() hace un COMMIT implícito antes de ejecutar y no maneja
    # bien los comentarios multilínea con caracteres especiales en algunas
    # versiones de sqlite3 de Python en Windows. La solución robusta es
    # extraer solo los statements DDL (líneas que no son comentarios ni
    # líneas vacías) y ejecutar cada uno por separado con conn.execute().
    #
    # Esto garantiza que schema.sql es la única fuente de verdad del DDL —
    # el mismo archivo que se puede aplicar con `sqlite3 db.db < schema.sql`.
    if not SCHEMA_SQL.exists():
        raise FileNotFoundError(
            f"No se encontró {SCHEMA_SQL}\n"
            "Asegúrate de que schema.sql está en el mismo directorio que ingest.py."
        )

    schema_text = SCHEMA_SQL.read_text(encoding="utf-8")

    # Extraer solo las líneas que son parte de DDL (no comentarios, no vacías),
    # luego dividir por ';' para obtener statements individuales.
    # Este enfoque es más robusto que executescript() porque evita el COMMIT
    # implícito de executescript() y maneja correctamente comentarios largos.
    ddl_lines = [
        line for line in schema_text.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    ddl_clean = "\n".join(ddl_lines)

    for stmt in ddl_clean.split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)

    conn.commit()

    # Verificar que la tabla fue creada correctamente antes de retornar.
    # Si algo salió mal en el schema, este check da un error claro en lugar
    # de fallar misteriosamente 200 líneas después durante la ingesta.
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='transactions'"
    ).fetchone()
    if not tables:
        raise RuntimeError(
            "La tabla 'transactions' no se creó. "
            "Verifica que schema.sql no tiene errores de sintaxis."
        )

    return conn


# ---------------------------------------------------------------------------
# Ingesta chunked
# ---------------------------------------------------------------------------

def ingest_chunk(
    conn: sqlite3.Connection,
    chunk: pd.DataFrame,
) -> int:
    """
    Inserta un chunk de filas en una sola transacción.

    Por qué executemany en lugar de to_sql():
    pandas.to_sql() con method='multi' es conveniente pero tiene overhead
    de construcción de queries Python. executemany() pasa la lista de tuplas
    directamente al driver C de sqlite3, que las procesa sin overhead
    de objetos Python intermedios por fila.

    El INSERT OR IGNORE descarta filas con transaction_id duplicado.
    En una ingesta limpia desde el CSV generado en E1 no debería haber
    duplicados, pero la robustez es mejor que un crash silencioso.

    Parámetros
    ----------
    conn  : conexión SQLite activa
    chunk : DataFrame con las columnas del schema

    Retorna
    -------
    Número de filas efectivamente insertadas (puede ser < len(chunk) si
    hay duplicados).
    """
    # Preparar los datos como lista de tuplas en el orden exacto del schema
    rows = [
        (
            str(row.transaction_id),
            str(row.timestamp),
            int(row.user_id),
            int(row.merchant_id),
            float(row.amount),
            str(row.category),
            str(row.country_code),
            str(row.status),
        )
        for row in chunk.itertuples(index=False)
    ]

    sql = """
        INSERT OR IGNORE INTO transactions
            (transaction_id, timestamp, user_id, merchant_id,
             amount, category, country_code, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    # Una sola transacción para todo el chunk — el commit ocurre al salir
    # del bloque with. Si hay un error, se hace rollback automático.
    with conn:
        conn.executemany(sql, rows)

    return len(rows)


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

def run_ingestion(
    csv_path: Path,
    db_path: Path,
    chunk_size: int,
    wal_mode: bool,
) -> dict:
    """
    Ejecuta la ingesta completa del CSV a SQLite.

    Flujo:
        1. Crear y configurar la base de datos
        2. Leer el CSV en chunks con pandas (bajo consumo de RAM)
        3. Por cada chunk: ingestar con transacción explícita
        4. Verificar integridad comparando conteos
        5. Retornar métricas

    La lectura chunked del CSV con pandas evita cargar 1M filas en RAM
    de una vez. Con chunk_size=20k, el pico de RAM es ~20k filas × ~200 bytes
    ≈ 4MB, en lugar de los ~120MB del CSV completo.

    Parámetros
    ----------
    csv_path   : ruta al CSV generado por E1
    db_path    : ruta donde crear la base SQLite
    chunk_size : filas por commit
    wal_mode   : True = WAL, False = journal por defecto

    Retorna
    -------
    dict con métricas de ingesta listas para guardar en JSON.
    """
    mode_label = "WAL" if wal_mode else "DELETE (sin WAL)"
    print(f"\n{'='*55}")
    print(f"Ingesta — modo: {mode_label}")
    print(f"  CSV:        {csv_path}")
    print(f"  DB:         {db_path}")
    print(f"  Chunk size: {chunk_size:,} filas por commit")
    print(f"{'='*55}")

    if not csv_path.exists():
        raise FileNotFoundError(
            f"No se encontró {csv_path}\n"
            "Corre primero: python generate_data.py --size 1m "
            "(en ejercicio-01-formatos/)"
        )

    # --- Paso 1: crear base de datos ---
    print("\n[1/3] Creando base de datos y aplicando schema...")
    conn = create_database(db_path, wal_mode)

    # --- Paso 2: ingestar en chunks ---
    print(f"\n[2/3] Ingestando {csv_path.name} en chunks de {chunk_size:,}...")
    t_start      = time.perf_counter()
    total_rows   = 0
    chunk_count  = 0
    chunk_times  = []

    # pandas lee el CSV en chunks sin cargar todo en memoria
    reader = pd.read_csv(str(csv_path), chunksize=chunk_size)

    for chunk in reader:
        t_chunk_start = time.perf_counter()
        inserted      = ingest_chunk(conn, chunk)
        chunk_time    = time.perf_counter() - t_chunk_start

        total_rows  += inserted
        chunk_count += 1
        chunk_times.append(round(chunk_time, 3))

        # Progreso cada 10 chunks para no saturar la consola
        if chunk_count % 10 == 0:
            elapsed = time.perf_counter() - t_start
            rate    = total_rows / elapsed if elapsed > 0 else 0
            print(f"  chunk {chunk_count:3d} | {total_rows:>9,} filas | "
                  f"{elapsed:.1f}s | {rate:,.0f} filas/s")

    total_time = time.perf_counter() - t_start
    rate_final = total_rows / total_time if total_time > 0 else 0

    print(f"\n  Total: {total_rows:,} filas en {total_time:.2f}s "
          f"({rate_final:,.0f} filas/s)")

    # --- Paso 3: verificar integridad ---
    print("\n[3/3] Verificando integridad...")
    db_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    # Comparar con el CSV original
    csv_count = sum(1 for _ in open(str(csv_path), encoding="utf-8")) - 1  # -1 header

    integrity_ok = (db_count == csv_count == total_rows)
    print(f"  Filas en DB:  {db_count:,}")
    print(f"  Filas en CSV: {csv_count:,}")
    print(f"  Integridad:   {'✓ OK' if integrity_ok else '✗ DIVERGENCIA'}")

    # Verificar vista de resumen
    summary = conn.execute("SELECT * FROM v_ingestion_summary").fetchone()
    print(f"  Usuarios únicos: {summary[1]:,}")
    print(f"  Países únicos:   {summary[3]}")
    print(f"  Rango fechas:    {summary[4]} → {summary[5]}")

    conn.close()

    return {
        "mode":          mode_label,
        "wal":           wal_mode,
        "chunk_size":    chunk_size,
        "total_rows":    total_rows,
        "db_rows":       db_count,
        "csv_rows":      csv_count,
        "integrity_ok":  integrity_ok,
        "total_time_s":  round(total_time, 3),
        "rows_per_sec":  round(rate_final, 0),
        "chunk_count":   chunk_count,
        "avg_chunk_s":   round(sum(chunk_times) / len(chunk_times), 3)
                         if chunk_times else 0,
        "under_3min":    total_time < 180,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingesta del dataset de transacciones a SQLite.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python ingest.py --wal
  python ingest.py --no-wal
  python ingest.py --wal --chunk-size 50000
  python ingest.py --no-wal --chunk-size 5000

Para comparar WAL vs no-WAL usa el mismo --chunk-size en ambas corridas.
        """,
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--wal",
        dest="wal_mode",
        action="store_true",
        help="Activar WAL (Write-Ahead Log) para mayor velocidad de escritura.",
    )
    mode_group.add_argument(
        "--no-wal",
        dest="wal_mode",
        action="store_false",
        help="Usar journal mode DELETE (modo por defecto de SQLite).",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"Filas por commit (default: {DEFAULT_CHUNK_SIZE:,}).",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=CSV_PATH,
        help=f"Ruta al CSV de entrada (default: {CSV_PATH}).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help=f"Ruta de la base de datos de salida (default: {DB_PATH}).",
    )
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    result = run_ingestion(
        csv_path   = args.csv,
        db_path    = args.db,
        chunk_size = args.chunk_size,
        wal_mode   = args.wal_mode,
    )

    # Guardar resultado en JSON (acumula resultados de múltiples corridas)
    results_path = RESULTS_DIR / "ingest_results.json"
    all_results  = []
    if results_path.exists():
        all_results = json.loads(results_path.read_text(encoding="utf-8"))

    all_results.append(result)
    results_path.write_text(
        json.dumps(all_results, indent=2),
        encoding="utf-8",
    )

    print(f"\nResultados guardados en {results_path}")

    if not result["under_3min"]:
        print(f"\n!  La ingesta tardó {result['total_time_s']:.1f}s "
              f"(límite: 180s). Considera aumentar --chunk-size.")
    else:
        print(f"\n✓ Ingesta completada en {result['total_time_s']:.1f}s "
              f"(límite: 180s).")


if __name__ == "__main__":
    main()