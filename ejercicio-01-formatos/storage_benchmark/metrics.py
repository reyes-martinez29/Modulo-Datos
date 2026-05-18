"""
storage_benchmark/metrics.py — Motor de medición del benchmark.

Principios de diseño
---------------------
1. Aislamiento de mediciones
   Antes de cada medición se llama a gc.collect() para forzar la
   recolección de basura. Sin esto, un GC que ocurra durante la
   medición infla el tiempo medido de forma no determinista.

2. Repeticiones y promedio
   Cada operación se repite REPEATS veces y se reporta promedio,
   mínimo y máximo. Esto reduce el ruido del SO (scheduling, cache
   de disco, interrupciones) y da una señal más confiable.

3. Separación escritura / lectura
   La escritura elimina el archivo antes de cada repetición para
   evitar que el SO sirva el write desde caché (write-behind cache).
   La lectura no hace flush del page cache porque en Python no hay
   forma portable de hacerlo — simplemente el primer run puede ser
   más rápido que los siguientes por eso.

4. tracemalloc para RAM
   tracemalloc mide el pico de allocaciones Python durante la
   operación. No mide la RAM del proceso completo (que incluiría
   el intérprete, los módulos, etc.), sino solo lo que se allocó
   dentro del bloque medido. Para el objetivo del ejercicio —
   comparar formatos — esta métrica es correcta y suficiente.

5. Sincronización de escritura
   Después de cada write llamamos a file.flush() + os.fsync() para
   garantizar que los bytes llegaron al disco y no están en el
   buffer del SO. Sin esto, el tiempo medido sería el de llenar
   el buffer, no el de escritura real.
"""

import gc
import os
import time
import tracemalloc
from pathlib import Path
from typing import Callable

import pandas as pd


REPEATS = 3  # Número de repeticiones por medición


# ---------------------------------------------------------------------------
# Medición de escritura
# ---------------------------------------------------------------------------

def measure_write(
    write_fn: Callable[[pd.DataFrame, Path], None],
    df: pd.DataFrame,
    path: Path,
    repeats: int = REPEATS,
) -> dict:
    """
    Mide el tiempo promedio de escritura de `df` al archivo `path`.

    El proceso por cada repetición:
      1. Elimina el archivo si existe (evita caché de SO).
      2. gc.collect() — fuerza recolección de basura antes de medir.
      3. Registra tiempo de inicio con perf_counter (nanosegundos).
      4. Llama a write_fn(df, path).
      5. fsync() — garantiza escritura real a disco.
      6. Registra tiempo de fin.

    Parámetros
    ----------
    write_fn : función que escribe df a path.
    df       : DataFrame en memoria (ya generado, no se mide su creación).
    path     : destino del archivo.
    repeats  : número de repeticiones.

    Retorna
    -------
    dict con write_avg_s, write_min_s, write_max_s, size_bytes.
    """
    times = []

    for i in range(repeats):
        # Eliminar archivo previo para no beneficiarse del caché del SO
        if path.exists():
            path.unlink()

        # Limpiar basura de Python antes de medir para evitar que un GC
        # se dispare en mitad de la medición e infle el tiempo
        gc.collect()

        t0 = time.perf_counter()
        write_fn(df, path)

        # fsync garantiza que los bytes llegaron al disco.
        # En Windows, fsync sobre un archivo recién cerrado puede lanzar
        # OSError dependiendo del modo de apertura. Se captura y se continúa:
        # pandas ya hizo flush() interno al cerrar el archivo, así que la
        # medición sigue siendo válida aunque fsync no ejecute.
        try:
            with open(path, "ab") as f:
                os.fsync(f.fileno())
        except OSError:
            pass

        elapsed = time.perf_counter() - t0
        times.append(elapsed)

        print(f"      run {i+1}/{repeats}: {elapsed:.3f}s")

    return {
        "write_avg_s":  sum(times) / len(times),
        "write_min_s":  min(times),
        "write_max_s":  max(times),
        "write_runs_s": times,
        "size_bytes":   path.stat().st_size,
    }


# ---------------------------------------------------------------------------
# Medición de lectura (completa y selectiva)
# ---------------------------------------------------------------------------

def measure_read(
    read_fn: Callable[[Path, bool], pd.DataFrame],
    path: Path,
    selective: bool,
    repeats: int = REPEATS,
) -> dict:
    """
    Mide el tiempo promedio de lectura y el pico de RAM.

    El proceso por cada repetición:
      1. gc.collect() — fuerza recolección de basura antes de medir.
      2. tracemalloc.start() — comienza a rastrear allocaciones Python.
      3. Registra tiempo de inicio.
      4. Llama a read_fn(path, selective).
      5. Registra tiempo de fin y pico de RAM.
      6. tracemalloc.stop() — libera el rastreador.
      7. El DataFrame leído se descarta (del = df) para liberar RAM
         antes de la siguiente repetición.

    Parámetros
    ----------
    read_fn   : función que lee el archivo y retorna un DataFrame.
    path      : archivo a leer.
    selective : True = solo columnas ["amount", "category"].
    repeats   : número de repeticiones.

    Retorna
    -------
    dict con claves prefijadas por "full_" o "selective_" según el modo.
    """
    times    = []
    peak_mbs = []
    prefix   = "selective" if selective else "full"

    for i in range(repeats):
        # Limpiar RAM antes de medir para que el pico medido
        # corresponda solo a la lectura, no a datos residuales
        gc.collect()

        tracemalloc.start()
        t0 = time.perf_counter()

        result_df = read_fn(path, selective)

        elapsed = time.perf_counter() - t0
        _current, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        times.append(elapsed)
        peak_mbs.append(peak_bytes / 1e6)

        # Liberar el DataFrame para no contaminar la siguiente iteración
        del result_df
        gc.collect()

        print(f"      run {i+1}/{repeats}: {elapsed:.3f}s  RAM pico: {peak_mbs[-1]:.1f}MB")

    return {
        f"read_{prefix}_avg_s":   sum(times) / len(times),
        f"read_{prefix}_min_s":   min(times),
        f"read_{prefix}_max_s":   max(times),
        f"read_{prefix}_runs_s":  times,
        f"read_{prefix}_peak_mb": sum(peak_mbs) / len(peak_mbs),
    }


# ---------------------------------------------------------------------------
# Medición completa de un formato
# ---------------------------------------------------------------------------

def measure_format(
    fmt_name:  str,
    write_fn:  Callable[[pd.DataFrame, Path], None],
    read_fn:   Callable[[Path, bool], pd.DataFrame],
    df:        pd.DataFrame,
    data_dir:  Path,
    ext:       str,
    n_rows:    int = 0,
    repeats:   int = REPEATS,
) -> dict:
    """
    Ejecuta todas las mediciones para un formato dado:
      - Escritura (repeats veces)
      - Lectura completa (repeats veces)
      - Lectura selectiva (repeats veces)

    Parámetros
    ----------
    fmt_name : nombre del formato (ej. "parquet_snappy").
    write_fn : función de escritura.
    read_fn  : función de lectura.
    df       : DataFrame en memoria.
    data_dir : directorio donde se guardan los archivos temporales.
    ext      : extensión del archivo (ej. "parquet", "csv").
    repeats  : número de repeticiones por medición.

    Retorna
    -------
    dict con todas las métricas del formato.
    """
    # Archivos permanentes vs temporales
    # ─────────────────────────────────────────────────────────────────────
    # Parquet de 1M filas → PERMANENTE. E2 los lee con DuckDB directamente,
    # E3 compara contra ellos, E4 los sirve en el endpoint de analytics.
    # Nombre limpio sin prefijo _bench_ para que los otros ejercicios puedan
    # localizarlos fácilmente: data/transactions_1m_parquet_snappy.parquet
    #
    # Todo lo demás (100k, 500k, JSONL, CSV de benchmark) → TEMPORAL.
    # Se borra al terminar para no llenar el disco con datos de prueba.
    is_permanent = (n_rows >= 1_000_000 and ext == "parquet")

    if is_permanent:
        path = data_dir / f"transactions_1m_{fmt_name}.parquet"
    else:
        path = data_dir / f"_bench_{fmt_name}.{ext}"

    print(f"\n  [{fmt_name}]")
    if is_permanent:
        print(f"    → archivo permanente: {path.name} (usado por E2/E3/E4)")

    print(f"    Escritura ({repeats} runs):")
    result = measure_write(write_fn, df, path, repeats)

    print(f"    Lectura completa ({repeats} runs):")
    result.update(measure_read(read_fn, path, selective=False, repeats=repeats))

    print(f"    Lectura selectiva ({repeats} runs):")
    result.update(measure_read(read_fn, path, selective=True, repeats=repeats))

    # Borrar solo temporales. Los Parquet de 1M se conservan para E2/E3/E4.
    if not is_permanent and path.exists():
        path.unlink()

    result["output_path"] = str(path) if is_permanent else None
    return result