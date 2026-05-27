"""
transactions/services/duckdb.py — Singleton lazy para la conexión DuckDB.

Por qué un singleton en lugar de AppConfig.ready():
    AppConfig.ready() puede ejecutarse más de una vez en Django — con runserver
    (que usa autoreload y lanza dos procesos), con management commands, y en
    ciertos contextos de testing. Si la conexión se inicializa en ready(),
    puede haber múltiples instancias o intentos de reinicialización sobre
    una conexión ya abierta.

    El patrón lazy singleton con variable de módulo es más seguro:
    - La conexión se crea la primera vez que get_duckdb_connection() se llama.
    - Las llamadas siguientes devuelven la misma instancia.
    - Si el proceso se reinicia (autoreload), la variable se resetea a None
      y la próxima llamada crea una nueva conexión limpia.

Por qué duckdb.connect(":memory:") con una VIEW en lugar de leer el Parquet:
    DuckDB puede leer el Parquet directamente con FROM 'ruta/archivo.parquet'
    en cada query. Pero registrar una VIEW con el nombre 'transactions' hace
    que las queries sean más limpias y reutilizables — las views son
    equivalentes en rendimiento y no copian datos a memoria.

Uso:
    from transactions.services.duckdb import get_duckdb_connection
    conn = get_duckdb_connection()
    result = conn.execute("SELECT ...").fetchall()
"""

import threading

import duckdb
from django.conf import settings

# Variable de módulo que mantiene la conexión entre llamadas.
# None indica que la conexión todavía no se ha inicializado.
_connection: duckdb.DuckDBPyConnection | None = None

# Lock para evitar condiciones de carrera en el primer acceso concurrente.
# En producción con múltiples workers, cada worker tiene su propio proceso
# y su propia variable _connection — el lock solo protege dentro del proceso.
_lock = threading.Lock()


def get_duckdb_connection() -> duckdb.DuckDBPyConnection:
    """
    Retorna la conexión DuckDB activa, creándola si no existe todavía.

    La conexión se crea en memoria (:memory:) y registra el Parquet del E1
    como una VIEW llamada 'transactions'. Todas las queries analíticas
    usan ese nombre — equivalente a leer el Parquet directamente pero
    con queries más limpias.

    La ruta al Parquet viene de settings.PARQUET_PATH, que a su vez
    viene de la variable de entorno PARQUET_PATH. Si el archivo no existe,
    lanza FileNotFoundError con un mensaje claro.

    Retorna
    -------
    duckdb.DuckDBPyConnection — conexión lista para ejecutar queries.

    Raises
    ------
    FileNotFoundError — si el Parquet no existe en la ruta configurada.
    """
    global _connection

    if _connection is not None:
        return _connection

    with _lock:
        # Double-checked locking: verificar de nuevo dentro del lock
        # por si otro hilo inicializó la conexión mientras esperábamos.
        if _connection is not None:
            return _connection

        parquet_path = settings.PARQUET_PATH

        from pathlib import Path
        if not Path(parquet_path).exists():
            raise FileNotFoundError(
                f"Parquet no encontrado en: {parquet_path}\n"
                "Asegúrate de haber corrido el Ejercicio 1:\n"
                "  python generate_data.py --size 1m\n"
                "  python benchmark_cli.py --size 1m"
            )

        conn = duckdb.connect(database=":memory:")
        # Registrar el Parquet como VIEW — las queries usan 'transactions'
        # igual que en el E4, sin diferencia de rendimiento respecto a
        # leer el archivo directamente en cada query.
        conn.execute(
            f"CREATE VIEW transactions AS "
            f"SELECT * FROM read_parquet('{parquet_path}')"
        )

        _connection = conn

    return _connection


def reset_connection() -> None:
    """
    Cierra y reinicia la conexión DuckDB.

    Útil en tests para garantizar un estado limpio entre suites,
    o para recargar el Parquet si cambió la variable PARQUET_PATH.
    No se usa en el flujo normal de producción.
    """
    global _connection
    with _lock:
        if _connection is not None:
            try:
                _connection.close()
            except Exception:
                pass
            _connection = None