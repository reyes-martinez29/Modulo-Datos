"""
storage_benchmark/writers.py — Funciones de escritura por formato.

Cada función recibe un DataFrame ya en memoria y lo escribe al disco.
El tiempo de generación del DataFrame NO se mide aquí — eso ocurre
antes de llamar a estas funciones.

Ninguna función tiene efectos secundarios más allá de escribir el archivo.
"""

from pathlib import Path

import pandas as pd


def write_csv(df: pd.DataFrame, path: Path) -> None:
    """Escribe el DataFrame como CSV con separador coma, sin índice."""
    df.to_csv(path, index=False)


def write_jsonl(df: pd.DataFrame, path: Path) -> None:
    """
    Escribe el DataFrame como JSON Lines (una línea JSON por fila).
    Es el formato "JSON" del enunciado — más eficiente que un JSON array
    porque permite lectura línea a línea sin cargar todo el archivo.
    """
    df.to_json(path, orient="records", lines=True, date_format="iso")


def write_parquet_plain(df: pd.DataFrame, path: Path) -> None:
    """Parquet sin compresión. Útil como línea base para aislar el costo
    de la serialización columnar pura, sin el overhead del compresor."""
    df.to_parquet(path, index=False, compression=None)


def write_parquet_snappy(df: pd.DataFrame, path: Path) -> None:
    """
    Parquet con Snappy. Snappy prioriza velocidad sobre ratio de compresión:
    comprime y descomprime muy rápido a costa de archivos algo más grandes
    que Gzip. Es el default de la mayoría de sistemas en producción
    (Spark, BigQuery, Athena) por ese motivo.
    """
    df.to_parquet(path, index=False, compression="snappy")


def write_parquet_gzip(df: pd.DataFrame, path: Path) -> None:
    """
    Parquet con Gzip. Gzip prioriza ratio de compresión sobre velocidad:
    los archivos son más pequeños que Snappy pero la escritura y lectura
    son más lentas porque el compresor hace más trabajo por byte.
    """
    df.to_parquet(path, index=False, compression="gzip")