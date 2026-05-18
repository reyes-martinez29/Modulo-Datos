"""
storage_benchmark/readers.py — Funciones de lectura por formato.

Cada función expone la misma firma:
    read_*(path, selective=False) -> pd.DataFrame

Cuando selective=True se leen únicamente las columnas ["amount", "category"].
Esto permite medir la ventaja real del almacenamiento columnar (Parquet)
frente a los formatos de fila (CSV, JSONL).

Por qué importa la lectura selectiva
-------------------------------------
- CSV y JSONL deben parsear CADA byte de CADA fila, incluso de las columnas
  que no necesitas. Solo después de parsear pueden descartar lo que sobra.
- Parquet conoce el offset exacto en el archivo donde empieza cada columna.
  Con columns=[...] solo lee esos bytes físicos del disco — el resto nunca
  toca RAM. Esto es column pruning y es la razón por la que Parquet domina
  en analytics.
"""

from pathlib import Path
from typing import Optional

import pandas as pd


SELECTIVE_COLS = ["amount", "category"]


def read_csv(path: Path, selective: bool = False) -> pd.DataFrame:
    """
    Lee un CSV completo o solo las columnas selectivas.
    usecols evita parsear columnas innecesarias a nivel de pandas,
    pero el archivo se lee completo desde disco de todas formas.
    """
    cols = SELECTIVE_COLS if selective else None
    return pd.read_csv(path, usecols=cols)


def read_jsonl(path: Path, selective: bool = False) -> pd.DataFrame:
    """
    Lee un JSON Lines completo o solo las columnas selectivas.
    JSONL no soporta column pruning real: se parsea todo el archivo
    y luego se filtra en memoria. Por eso la lectura selectiva de JSONL
    no es significativamente más rápida que la lectura completa.
    """
    df = pd.read_json(path, orient="records", lines=True)
    if selective:
        return df[SELECTIVE_COLS]
    return df


def read_parquet_plain(path: Path, selective: bool = False) -> pd.DataFrame:
    """Parquet sin compresión, completo o selectivo."""
    cols = SELECTIVE_COLS if selective else None
    return pd.read_parquet(path, columns=cols)


def read_parquet_snappy(path: Path, selective: bool = False) -> pd.DataFrame:
    """Parquet con Snappy, completo o selectivo."""
    cols = SELECTIVE_COLS if selective else None
    return pd.read_parquet(path, columns=cols)


def read_parquet_gzip(path: Path, selective: bool = False) -> pd.DataFrame:
    """Parquet con Gzip, completo o selectivo."""
    cols = SELECTIVE_COLS if selective else None
    return pd.read_parquet(path, columns=cols)