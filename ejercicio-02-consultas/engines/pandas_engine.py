"""
engines/pandas_engine.py — Las 8 queries implementadas en pandas.

Decisiones de diseño:
    - Se lee el Parquet con pd.read_parquet() en cada función.
      Esto hace cada función independiente y el benchmark puede medir
      el tiempo real de cada query incluyendo el I/O de lectura,
      igual que los otros engines.

    - Las columnas de timestamp se parsean con pd.to_datetime() solo
      cuando la query las necesita, no al leer el archivo completo.
      Esto evita overhead innecesario en queries que no usan timestamps.

    - Todas las funciones retornan pd.DataFrame con columnas nombradas
      de forma consistente con los otros engines, para que la validación
      de equivalencia pueda comparar directamente.

    - Los nombres de columnas en el resultado siguen el patrón snake_case
      y son los mismos en los tres engines. Ej: "total", "avg_amount",
      "min_amount", "max_amount". Cualquier diferencia de nombre haría
      fallar la validación aunque los números sean correctos.
"""

import pandas as pd


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _read(path: str) -> pd.DataFrame:
    """
    Lee el Parquet completo. Se llama al inicio de cada función de query.

    Por qué no usar un singleton o cache global:
    El benchmark mide cada query de forma aislada con gc.collect() entre runs.
    Un cache global haría que la primera llamada sea lenta y las siguientes
    instantáneas, lo que contaminaría las mediciones de tiempo.
    Cada función carga el archivo por su cuenta para que el tiempo medido
    sea representativo de un uso real.
    """
    return pd.read_parquet(path)


def _read_cols(path: str, cols: list[str]) -> pd.DataFrame:
    """
    Lee solo las columnas necesarias del Parquet (column pruning).

    Usar esto en queries que no necesitan todas las columnas reduce
    el tiempo de I/O y el consumo de RAM de forma significativa.
    pandas+pyarrow pasa la lista de columnas al lector de Parquet,
    que solo lee los bloques físicos de esas columnas del archivo.
    """
    return pd.read_parquet(path, columns=cols)


# ---------------------------------------------------------------------------
# Q1 — Conteo de transacciones por country_code
# ---------------------------------------------------------------------------

def q1(path: str) -> pd.DataFrame:
    """
    Conteo total de transacciones por country_code, ordenado de mayor a menor.

    Solo necesitamos country_code — column pruning reduce I/O al mínimo.
    """
    df = _read_cols(path, ["country_code"])

    result = (
        df.groupby("country_code", as_index=False)
        .size()
        .rename(columns={"size": "total"})
        .sort_values("total", ascending=False)
        .reset_index(drop=True)
    )
    return result


# ---------------------------------------------------------------------------
# Q2 — Estadísticas de amount por category
# ---------------------------------------------------------------------------

def q2(path: str) -> pd.DataFrame:
    """
    Monto promedio, mínimo y máximo agrupado por category.

    agg() con un dict nombrado produce columnas con nombres explícitos,
    evitando el MultiIndex que genera agg() con lista de funciones.
    """
    df = _read_cols(path, ["category", "amount"])

    result = (
        df.groupby("category", as_index=False)
        .agg(
            avg_amount=("amount", "mean"),
            min_amount=("amount", "min"),
            max_amount=("amount", "max"),
        )
        .sort_values("category")
        .reset_index(drop=True)
    )
    return result


# ---------------------------------------------------------------------------
# Q3 — Top 10 usuarios por suma de amount
# ---------------------------------------------------------------------------

def q3(path: str) -> pd.DataFrame:
    """
    Top 10 user_id por suma de amount, incluyendo su conteo de transacciones.

    Dos métricas en un solo groupby: suma de amount y conteo de filas.
    Se ordena por total_amount descendente y se toman los primeros 10.
    """
    df = _read_cols(path, ["user_id", "amount"])

    result = (
        df.groupby("user_id", as_index=False)
        .agg(
            total_amount=("amount", "sum"),
            tx_count=("amount", "count"),
        )
        .sort_values("total_amount", ascending=False)
        .head(10)
        .reset_index(drop=True)
    )
    return result


# ---------------------------------------------------------------------------
# Q4 — Transacciones fallidas por hora del día
# ---------------------------------------------------------------------------

def q4(path: str) -> pd.DataFrame:
    """
    Conteo de transacciones con status='failed' agrupado por hora del día (0-23).

    Primero filtramos por status para reducir el DataFrame antes de extraer
    la hora — esto evita extraer la hora de 1M filas cuando solo ~100K son 'failed'.

    pd.to_datetime() es necesario aquí porque timestamp viene como string
    en Parquet dependiendo de cómo se guardó. Si ya es datetime64, la
    conversión es un no-op sin costo.
    """
    df = _read_cols(path, ["status", "timestamp"])

    failed = df[df["status"] == "failed"].copy()
    failed["hour"] = pd.to_datetime(failed["timestamp"]).dt.hour

    result = (
        failed.groupby("hour", as_index=False)
        .size()
        .rename(columns={"size": "failed_count"})
        .sort_values("hour")
        .reset_index(drop=True)
    )

    # Garantizar que aparecen las 24 horas aunque alguna tenga 0 transacciones
    all_hours = pd.DataFrame({"hour": range(24)})
    result = all_hours.merge(result, on="hour", how="left").fillna(0)
    result["failed_count"] = result["failed_count"].astype(int)
    return result


# ---------------------------------------------------------------------------
# Q5 — Transacciones recientes en MX o CO con amount > 500
# ---------------------------------------------------------------------------

def q5(path: str) -> pd.DataFrame:
    """
    Transacciones con amount > 500 en MX o CO, en los últimos 30 días del dataset.

    "Últimos 30 días del dataset" significa: desde (max(timestamp) - 30 días)
    hasta max(timestamp). NO es relativo a hoy — es relativo al rango del dataset.

    Por qué calculamos max_ts del dataset completo antes de filtrar:
    Si filtráramos primero por país y luego calculáramos el max, el max sería
    el máximo de MX/CO solamente, no del dataset completo. El enunciado dice
    "últimos 30 días del dataset", así que el período es fijo para todos los países.
    """
    df = _read_cols(path, ["timestamp", "country_code", "amount",
                            "transaction_id", "user_id", "merchant_id",
                            "category", "status"])

    df["timestamp"] = pd.to_datetime(df["timestamp"])

    # Período de referencia: últimos 30 días del rango completo del dataset
    max_ts   = df["timestamp"].max()
    cutoff   = max_ts - pd.Timedelta(days=30)

    result = df[
        (df["amount"] > 500) &
        (df["country_code"].isin(["MX", "CO"])) &
        (df["timestamp"] >= cutoff)
    ].reset_index(drop=True)

    return result


# ---------------------------------------------------------------------------
# Q6 — Por country_code, la category con más transacciones
# ---------------------------------------------------------------------------

def q6(path: str) -> pd.DataFrame:
    """
    Por cada country_code, la category con más transacciones y su monto promedio.

    Este es el más complejo: requiere un argmax por grupo.
    Estrategia en pandas:
      1. Contar transacciones por (country_code, category).
      2. Para cada country_code, quedarse con la category de mayor conteo.
      3. Unir con el promedio de amount por (country_code, category).

    El desempate cuando dos categorías tienen el mismo conteo se resuelve
    con idxmax(), que en pandas toma la primera ocurrencia en el índice.
    DuckDB y polars deben manejar el desempate de la misma forma para
    que la validación de equivalencia pase. Se documenta en benchmark.py.
    """
    df = _read_cols(path, ["country_code", "category", "amount"])

    # Paso 1: conteo por (country_code, category)
    counts = (
        df.groupby(["country_code", "category"], as_index=False)
        .agg(tx_count=("amount", "count"))
    )

    # Paso 2: para cada country_code, índice de la category con más transacciones
    top_idx = counts.groupby("country_code")["tx_count"].idxmax()
    top_categories = counts.loc[top_idx, ["country_code", "category", "tx_count"]]

    # Paso 3: monto promedio por (country_code, category)
    avg_amounts = (
        df.groupby(["country_code", "category"], as_index=False)
        .agg(avg_amount=("amount", "mean"))
    )

    # Paso 4: unir
    result = (
        top_categories
        .merge(avg_amounts, on=["country_code", "category"])
        .sort_values("country_code")
        .reset_index(drop=True)
    )
    return result


# ---------------------------------------------------------------------------
# Q7 — Usuarios con más de 5 transacciones fallidas
# ---------------------------------------------------------------------------

def q7(path: str) -> pd.DataFrame:
    """
    Usuarios con más de 5 transacciones fallidas. Retornar user_id y conteo.

    Filtro primero, agrupa después — misma razón que Q4.
    """
    df = _read_cols(path, ["user_id", "status"])

    result = (
        df[df["status"] == "failed"]
        .groupby("user_id", as_index=False)
        .size()
        .rename(columns={"size": "failed_count"})
        .query("failed_count > 5")
        .sort_values("user_id")
        .reset_index(drop=True)
    )
    return result


# ---------------------------------------------------------------------------
# Q8 — Monto promedio diario por category
# ---------------------------------------------------------------------------

def q8(path: str) -> pd.DataFrame:
    """
    Monto promedio diario por category — un valor por día por categoría.

    Truncamos el timestamp al día (date) para agrupar todas las transacciones
    del mismo día juntas. dt.normalize() hace floor al día en datetime64,
    equivalente a DATE_TRUNC('day', ...) en SQL.

    Retornamos 'day' como string ('YYYY-MM-DD') para que la comparación
    con DuckDB y polars sea directa sin conversiones de tipo.
    """
    df = _read_cols(path, ["timestamp", "category", "amount"])

    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["day"]       = df["timestamp"].dt.normalize().dt.strftime("%Y-%m-%d")

    result = (
        df.groupby(["day", "category"], as_index=False)
        .agg(avg_amount=("amount", "mean"))
        .sort_values(["day", "category"])
        .reset_index(drop=True)
    )
    return result