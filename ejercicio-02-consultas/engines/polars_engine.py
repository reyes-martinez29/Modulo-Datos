"""
engines/polars_engine.py — Las 8 queries implementadas en polars.

Decisión de diseño fundamental:
    Todas las queries usan pl.scan_parquet() en lugar de pl.read_parquet().

    scan_parquet() retorna un LazyFrame — polars no lee nada del disco todavía.
    Solo cuando llamamos .collect() polars ejecuta el plan optimizado:
      1. Column pruning: lee solo las columnas que la query necesita.
      2. Predicate pushdown: empuja filtros al lector de Parquet.
      3. Optimización del plan lógico antes de ejecutar.

    read_parquet() carga todo el archivo en memoria inmediatamente.
    scan_parquet() + .collect() es siempre igual o más eficiente.

    Por qué .to_pandas() al final:
    benchmark.py valida equivalencia comparando pd.DataFrames.
    polars internamente usa su propio tipo DataFrame (pl.DataFrame),
    así que convertimos al retornar. El costo de conversión es mínimo
    porque Arrow es el formato interno de ambas librerías.

    Desempate en Q6:
    Usamos sort() con múltiples columnas para que el desempate sea
    determinista y consistente con DuckDB (ORDER BY tx_count DESC, category ASC).
"""

import polars as pl
import pandas as pd


# ---------------------------------------------------------------------------
# Q1 — Conteo de transacciones por country_code
# ---------------------------------------------------------------------------

def q1(path: str) -> pd.DataFrame:
    """
    scan_parquet hace column pruning: solo lee el bloque de country_code.
    """
    return (
        pl.scan_parquet(path)
        .select("country_code")
        .group_by("country_code")
        .agg(pl.len().alias("total"))
        .sort("total", descending=True)
        .collect()
        .to_pandas()
    )


# ---------------------------------------------------------------------------
# Q2 — Estadísticas de amount por category
# ---------------------------------------------------------------------------

def q2(path: str) -> pd.DataFrame:
    return (
        pl.scan_parquet(path)
        .select(["category", "amount"])
        .group_by("category")
        .agg([
            pl.col("amount").mean().alias("avg_amount"),
            pl.col("amount").min().alias("min_amount"),
            pl.col("amount").max().alias("max_amount"),
        ])
        .sort("category")
        .collect()
        .to_pandas()
    )


# ---------------------------------------------------------------------------
# Q3 — Top 10 usuarios por suma de amount
# ---------------------------------------------------------------------------

def q3(path: str) -> pd.DataFrame:
    """
    polars puede empujar LIMIT al lector cuando la query tiene un ORDER BY
    sobre una columna de agregación — evita materializar todos los grupos
    para quedarse con 10.
    """
    return (
        pl.scan_parquet(path)
        .select(["user_id", "amount"])
        .group_by("user_id")
        .agg([
            pl.col("amount").sum().alias("total_amount"),
            pl.col("amount").count().alias("tx_count"),
        ])
        .sort("total_amount", descending=True)
        .head(10)
        .collect()
        .to_pandas()
    )


# ---------------------------------------------------------------------------
# Q4 — Transacciones fallidas por hora del día
# ---------------------------------------------------------------------------

def q4(path: str) -> pd.DataFrame:
    """
    dt.hour() extrae la hora de un tipo Datetime en polars.
    cast(pl.Datetime) convierte el timestamp si viene como String.

    Para garantizar las 24 horas, hacemos un join con un rango completo
    de horas — equivalente al LEFT JOIN de DuckDB y el merge de pandas.
    """
    failed = (
        pl.scan_parquet(path)
        .select(["status", "timestamp"])
        .filter(pl.col("status") == "failed")
        .with_columns(
            pl.col("timestamp").cast(pl.Datetime).dt.hour().alias("hour")
        )
        .group_by("hour")
        .agg(pl.len().alias("failed_count"))
        .collect()
    )

    # Rango completo de horas para garantizar 0-23 sin gaps
    all_hours = pl.DataFrame({"hour": list(range(24))})
    result = (
        all_hours
        .join(failed, on="hour", how="left")
        .with_columns(pl.col("failed_count").fill_null(0))
        .sort("hour")
        .with_columns([
            pl.col("hour").cast(pl.Int64),
            pl.col("failed_count").cast(pl.Int64),
        ])
    )
    return result.to_pandas()


# ---------------------------------------------------------------------------
# Q5 — Transacciones recientes en MX o CO con amount > 500
# ---------------------------------------------------------------------------

def q5(path: str) -> pd.DataFrame:
    """
    Polars con scan_parquet hace predicate pushdown igual que DuckDB:
    los filtros se empujan al lector y los row groups que no los cumplen
    no se leen del disco.

    La diferencia con DuckDB es que polars calcula el max_ts en un pase
    separado antes de filtrar (no puede hacer el cálculo en un solo pase
    como DuckDB con la subconsulta CTE). Esto hace Q5 ligeramente más
    lento en polars que en DuckDB para datasets grandes.
    """
    # Paso 1: calcular el período de referencia del dataset completo
    max_ts = (
        pl.scan_parquet(path)
        .select(pl.col("timestamp").cast(pl.Datetime).max())
        .collect()
        .item()
    )
    cutoff = max_ts - pl.duration(days=30)

    # Paso 2: filtrar con los predicados — polars hace pushdown de estos
    return (
        pl.scan_parquet(path)
        .with_columns(pl.col("timestamp").cast(pl.Datetime))
        .filter(
            (pl.col("amount") > 500) &
            (pl.col("country_code").is_in(["MX", "CO"])) &
            (pl.col("timestamp") >= cutoff)
        )
        .sort(["timestamp", "transaction_id"])
        .collect()
        .to_pandas()
        .assign(timestamp=lambda df: df["timestamp"].astype(str))
    )


# ---------------------------------------------------------------------------
# Q6 — Por country_code, la category con más transacciones
# ---------------------------------------------------------------------------

def q6(path: str) -> pd.DataFrame:
    """
    Estrategia: sort + group_by(maintain_order=True) + first().

    Ordenamos por (country_code, tx_count DESC, category ASC) antes de
    agrupar. Cuando llamamos .first() sobre el grupo, tomamos la primera
    fila de cada país — que después del sort es la de mayor tx_count.

    El sort secundario por category garantiza el mismo desempate que
    DuckDB (ORDER BY tx_count DESC, category ASC → en empate, gana la
    categoría que viene primero alfabéticamente).

    Por qué no usar over() (window function):
    over() materializaría el DataFrame completo antes de filtrar.
    La estrategia de sort + first() es más eficiente en polars porque
    puede ser parcialmente empujada al optimizador de plan.
    """
    counts = (
        pl.scan_parquet(path)
        .select(["country_code", "category", "amount"])
        .group_by(["country_code", "category"])
        .agg([
            pl.len().alias("tx_count"),
            pl.col("amount").mean().alias("avg_amount"),
        ])
        .collect()
    )

    result = (
        counts
        .sort(["country_code", "tx_count", "category"],
              descending=[False, True, False])
        .group_by("country_code", maintain_order=True)
        .first()
        .select(["country_code", "category", "tx_count", "avg_amount"])
        .sort("country_code")
    )
    return result.to_pandas()


# ---------------------------------------------------------------------------
# Q7 — Usuarios con más de 5 transacciones fallidas
# ---------------------------------------------------------------------------

def q7(path: str) -> pd.DataFrame:
    return (
        pl.scan_parquet(path)
        .select(["user_id", "status"])
        .filter(pl.col("status") == "failed")
        .group_by("user_id")
        .agg(pl.len().alias("failed_count"))
        .filter(pl.col("failed_count") > 5)
        .sort("user_id")
        .collect()
        .to_pandas()
    )


# ---------------------------------------------------------------------------
# Q8 — Monto promedio diario por category
# ---------------------------------------------------------------------------

def q8(path: str) -> pd.DataFrame:
    """
    dt.truncate("1d") hace floor al inicio del día en polars.
    strftime("%Y-%m-%d") convierte a string para comparación directa
    con pandas y DuckDB sin conversiones de tipo.
    """
    return (
        pl.scan_parquet(path)
        .select(["timestamp", "category", "amount"])
        .with_columns(
            pl.col("timestamp")
            .cast(pl.Datetime)
            .dt.truncate("1d")
            .dt.strftime("%Y-%m-%d")
            .alias("day")
        )
        .group_by(["day", "category"])
        .agg(pl.col("amount").mean().alias("avg_amount"))
        .sort(["day", "category"])
        .collect()
        .to_pandas()
    )