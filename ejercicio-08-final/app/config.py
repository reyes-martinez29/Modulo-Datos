"""
app/config.py — Configuración del sistema desde variables de entorno.

En el E7 la validación de variables de entorno vivía en entrypoint.sh, para
no tocar el código del E4 que ya estaba evaluado. En el E8, que es un sistema
nuevo, centralizo la configuración en este módulo Python por dos razones:
es testeable (se puede verificar el comportamiento de validación con pytest
sin levantar un contenedor), y hace que la app falle limpio con un mensaje
claro aunque se corra fuera de Docker.

Las variables se leen y validan una sola vez, al importar el módulo desde
main.py. Si falta una variable requerida o un archivo no existe, se lanza
una excepción con un mensaje accionable antes de que el servidor arranque,
en lugar de fallar de forma confusa en el primer request.
"""

import os
from pathlib import Path


class ConfigError(Exception):
    """Falta una variable de entorno requerida o apunta a un archivo inexistente."""


def _require_env(name: str) -> str:
    """Lee una variable de entorno requerida o falla con un mensaje claro."""
    value = os.getenv(name)
    if not value:
        raise ConfigError(
            f"La variable de entorno '{name}' es requerida y no está definida. "
            "Revisa tu archivo .env comparándolo con .env.example."
        )
    return value


class Config:
    """
    Configuración validada del sistema.

    Se construye una vez en main.py al arrancar. El constructor valida que
    las rutas requeridas existen en disco — así el fallo ocurre al arrancar,
    no en el primer request a /analytics.
    """

    def __init__(self) -> None:
        self.parquet_path: str = _require_env("PARQUET_PATH")
        self.db_path: str = _require_env("DB_PATH")
        self.analytics_ttl: int = int(os.getenv("ANALYTICS_TTL", "300"))

        # Límite de filas para el endpoint de ingesta CSV. Protege la
        # memoria del contenedor frente a un CSV gigante subido por la API.
        self.max_csv_rows: int = int(os.getenv("MAX_CSV_ROWS", "100000"))

        # Umbral por defecto del detector de anomalías cuando el request no
        # especifica uno. Parametrizable por si el negocio ajusta su criterio.
        self.default_anomaly_threshold: int = int(
            os.getenv("DEFAULT_ANOMALY_THRESHOLD", "5")
        )

        # Directorio de cuarentena del pipeline. Por defecto dentro del
        # volumen de datos, para que las filas rechazadas persistan y se
        # puedan auditar fuera del contenedor.
        self.quarantine_dir: str = os.getenv("QUARANTINE_DIR", "/data/quarantine")

        self._validate_paths()

    def _validate_paths(self) -> None:
        """Verifica que las rutas de datos existen, con mensajes accionables."""
        if not Path(self.parquet_path).exists():
            raise ConfigError(
                f"PARQUET_PATH='{self.parquet_path}' no existe. "
                "Verifica que el volumen de datos esté montado y que el "
                "Parquet del E1 esté en su lugar."
            )
        if not Path(self.db_path).exists():
            raise ConfigError(
                f"DB_PATH='{self.db_path}' no existe. "
                "El servicio 'setup' debe generar la base SQLite antes de "
                "que arranque la API."
            )