"""
engines/ — Implementaciones de las 8 queries en tres engines distintos.

Cada módulo expone exactamente las mismas 8 funciones (q1 a q8) con la
misma firma: reciben la ruta al archivo Parquet y retornan un pd.DataFrame.

Retornar siempre pd.DataFrame — incluso desde polars y DuckDB — permite
que benchmark.py compare resultados entre engines sin conversiones ad-hoc.

Engines disponibles:
    pandas_engine  — pandas sobre Parquet via pyarrow
    duckdb_engine  — DuckDB leyendo Parquet directamente (sin pasar por pandas)
    polars_engine  — polars con LazyFrame, collect() al final
"""

from . import pandas_engine, duckdb_engine, polars_engine

__all__ = ["pandas_engine", "duckdb_engine", "polars_engine"]