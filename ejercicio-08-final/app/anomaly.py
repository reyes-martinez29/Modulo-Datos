"""
app/anomaly.py — Detección de usuarios con patrones anómalos.

El enunciado pide un detector concreto: usuarios con más de N transacciones
fallidas en los últimos 30 días, con N parametrizable. Esa es la señal que
el equipo de producto pidió, y es la que expone el endpoint.

Decisión de diseño: en lugar de incrustar la query directamente en el
endpoint, este módulo modela la detección como una operación con nombre
propio. La razón es de dominio, no de estética: en una fintech, "usuario
con muchas transacciones fallidas" es una señal de fraude, de una tarjeta
comprometida, o de un problema con un merchant. Hoy el negocio quiere el
conteo absoluto, pero mañana querrá señales más finas (tasa de fallo
respecto al comportamiento normal del usuario, concentración en un solo
merchant, velocidad de los intentos). Tener la detección en un módulo
separado permite agregar esos detectores sin tocar el endpoint ni la query
existente.

Por qué SQLite y no la vista unificada Parquet+SQLite:
    La detección de anomalías mira los últimos 30 días. El histórico del
    Parquet es de meses o años atrás — no aporta a una ventana de 30 días.
    Las transacciones recientes (las que importan aquí) están en SQLite,
    que además tiene el índice idx_user_timestamp que cubre exactamente
    este patrón: filtrar por status y rango de timestamp, agrupar por
    usuario. Usar la vista unificada obligaría a leer el Parquet completo
    para luego descartarlo por la condición de fecha — desperdicio puro.
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Optional


# Ventana de detección por defecto. El enunciado dice "últimos 30 días".
DEFAULT_WINDOW_DAYS = 30


def detect_failed_transaction_anomalies(
    db_path: str,
    threshold: int,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: Optional[datetime] = None,
) -> list[dict]:
    """
    Detecta usuarios con MÁS de `threshold` transacciones fallidas en la
    ventana de `window_days` días.

    Esta es la señal que pide el enunciado. La query se apoya en
    idx_user_timestamp: filtra por status='failed' y timestamp dentro de la
    ventana, agrupa por user_id, y se queda con los que superan el umbral.

    Parámetros
    ----------
    db_path     : ruta a la base SQLite transaccional
    threshold   : N — un usuario es anómalo si tiene MÁS de N fallidas
                  (estrictamente mayor, no mayor-o-igual)
    window_days : tamaño de la ventana en días (default 30)
    now         : momento de referencia. Parametrizable para tests
                  deterministas; si es None usa datetime.now()

    Retorna
    -------
    list[dict] ordenada por failed_count descendente, cada elemento con
    user_id, failed_count, y la ventana evaluada para que el resultado sea
    auto-explicativo (el equipo de producto ve sobre qué periodo se calculó).
    """
    if now is None:
        now = datetime.now()

    cutoff = (now - timedelta(days=window_days)).strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("""
            SELECT user_id, COUNT(*) AS failed_count
            FROM transactions
            WHERE status = 'failed'
              AND timestamp >= ?
            GROUP BY user_id
            HAVING COUNT(*) > ?
            ORDER BY failed_count DESC
        """, [cutoff, threshold]).fetchall()

        return [
            {
                "user_id": r[0],
                "failed_count": r[1],
                "window_days": window_days,
                "threshold": threshold,
            }
            for r in rows
        ]
    finally:
        conn.close()