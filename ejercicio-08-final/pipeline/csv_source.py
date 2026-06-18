"""
pipeline/csv_source.py — Lectura de un CSV externo como fuente del pipeline.

En el E6, la fuente del pipeline era data_source.py: un generador sintético
que producía list[dict] con errores inyectados. En el E8, la fuente es un
CSV real que llega de afuera. Esta capa lo lee y produce el mismo formato
list[dict] crudo que el resto del pipeline (extract → transform → load) ya
sabe consumir.

Que esto haya sido un reemplazo de una sola capa, sin tocar extract,
transform ni load, es consecuencia directa de una decisión del E6: el flujo
entre capas son list[dict], no DataFrames ni archivos intermedios. Cambiar
la fuente solo requiere producir el mismo list[dict].

Tres niveles de error, no dos:
    El E6 distinguía errores de formato (extract) de errores de negocio
    (transform). El CSV introduce un tercer nivel que ocurre ANTES de
    cualquier fila: errores de ESTRUCTURA del archivo. Que falte la columna
    'amount', que el archivo no sea un CSV válido, o que esté vacío, no es
    un problema de una fila individual — es que el archivo no tiene la forma
    que el sistema espera. Esta capa valida la estructura y falla limpio con
    un mensaje claro antes de pasar una sola fila a extract.

Protección contra archivos enormes:
    El endpoint de ingesta es público. Un CSV de varios GB podría agotar la
    memoria del contenedor. Esta capa impone un límite de filas configurable
    (MAX_ROWS) y se detiene con un error claro si se supera, en lugar de
    intentar cargar todo en memoria. Se documenta en decisions.md que para
    cargas masivas reales se usaría streaming a una cola de trabajos.
"""

import csv
import io
from typing import Optional


# Las 8 columnas del schema del módulo. El CSV debe tenerlas todas.
EXPECTED_COLUMNS = frozenset({
    "transaction_id", "timestamp", "user_id", "merchant_id",
    "amount", "category", "country_code", "status",
})

# Tope de filas para proteger la memoria del contenedor en el endpoint público.
MAX_ROWS = 100_000


class CSVStructureError(Exception):
    """
    El CSV no tiene la estructura esperada: faltan columnas, está vacío,
    no es parseable, o supera el límite de filas. Es distinto de una fila
    inválida (eso lo maneja transform); aquí el archivo entero es el problema.
    """


def read_csv_text(text: str, max_rows: int = MAX_ROWS) -> list[dict]:
    """
    Lee un CSV desde un string en memoria y lo convierte en list[dict] crudo.

    No normaliza ni valida reglas de negocio — eso es trabajo de extract y
    transform. Aquí solo se valida que el archivo tiene la estructura
    correcta (las 8 columnas) y se respeta el límite de filas.

    Parámetros
    ----------
    text     : contenido completo del CSV como string
    max_rows : tope de filas a leer antes de abortar (default MAX_ROWS)

    Retorna
    -------
    list[dict] — una fila por registro, con las claves del schema y los
    valores como strings (tal como vienen del CSV). extract los normaliza.

    Raises
    ------
    CSVStructureError — si el archivo está vacío, le faltan columnas del
    schema, o supera max_rows.
    """
    if not text or not text.strip():
        raise CSVStructureError("El archivo CSV está vacío.")

    reader = csv.DictReader(io.StringIO(text))

    if reader.fieldnames is None:
        raise CSVStructureError("El archivo CSV no tiene encabezado.")

    header = {col.strip() for col in reader.fieldnames}
    missing = EXPECTED_COLUMNS - header
    if missing:
        raise CSVStructureError(
            f"Al CSV le faltan columnas requeridas: {sorted(missing)}. "
            f"Columnas esperadas: {sorted(EXPECTED_COLUMNS)}. "
            f"Columnas encontradas: {sorted(header)}."
        )

    rows: list[dict] = []
    for i, row in enumerate(reader):
        if i >= max_rows:
            raise CSVStructureError(
                f"El CSV supera el límite de {max_rows:,} filas. "
                "Para cargas masivas, divide el archivo o usa el pipeline "
                "por línea de comandos."
            )
        # Mantener solo las columnas del schema; ignorar columnas extra.
        # Los valores se dejan como vienen (strings) — extract normaliza.
        rows.append({col: row.get(col) for col in EXPECTED_COLUMNS})

    if not rows:
        raise CSVStructureError(
            "El CSV tiene encabezado válido pero no contiene filas de datos."
        )

    return rows


def read_csv_file(path: str, max_rows: int = MAX_ROWS) -> list[dict]:
    """
    Lee un CSV desde una ruta en disco. Envuelve read_csv_text para que la
    CLI del pipeline pueda usar un archivo y el endpoint pueda usar el
    contenido subido directamente.
    """
    with open(path, "r", encoding="utf-8") as f:
        return read_csv_text(f.read(), max_rows=max_rows)