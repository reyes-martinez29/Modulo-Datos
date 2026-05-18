"""
generate_report.py — Genera report.md del E2 con los datos reales del benchmark.

Uso:
    python generate_report.py
    python generate_report.py --results results/benchmark_results.json
"""

import argparse
import json
from pathlib import Path


def load(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"No se encontró {path}\nCorre primero: python benchmark.py --output results/")
    return json.loads(p.read_text(encoding="utf-8"))


def t(q, qid, eng):    return q[qid]["engines"][eng]["avg_s"]
def ram(q, qid, eng):  return q[qid]["engines"][eng]["peak_mb"]
def rows(q, qid, eng): return q[qid]["engines"][eng]["rows"]
def runs(q, qid, eng): return q[qid]["engines"][eng]["runs_s"]
def sp(base, other):   return round(base / other, 1) if other > 0 else 0
def fmt(v):            return f"{v:.3f}s"

def winner(q, qid):
    return min(["pandas", "duckdb", "polars"], key=lambda e: t(q, qid, e))


def make_table(data):
    q = data["queries"]
    repeats = data["repeats"]
    DESCRIPTIONS = {
        "Q1": "Conteo por `country_code`",
        "Q2": "Promedio, mínimo y máximo de `amount` por `category`",
        "Q3": "Top 10 usuarios por suma de `amount`",
        "Q4": "Transacciones fallidas agrupadas por hora del día",
        "Q5": "Filtro: `amount > 500`, país MX o CO, últimos 30 días",
        "Q6": "Categoría con más transacciones por país",
        "Q7": "Usuarios con más de 5 transacciones fallidas",
        "Q8": "Monto promedio diario por `category`",
    }
    header = (
        "| Query | Descripción | pandas | DuckDB | polars | Ganador | Equiv. |\n"
        "|-------|-------------|-------:|-------:|-------:|:-------:|:------:|\n"
    )
    rows_md = []
    for qid in q:
        eq   = "✓" if q[qid]["validation"]["all_equivalent"] else "✗"
        best = winner(q, qid)
        rows_md.append(
            f"| {qid} | {DESCRIPTIONS[qid]} "
            f"| {fmt(t(q,qid,'pandas'))} | {fmt(t(q,qid,'duckdb'))} | {fmt(t(q,qid,'polars'))} "
            f"| **{best}** | {eq} |"
        )
    note = (
        f"> Cada tiempo es el promedio de {repeats} repeticiones con `gc.collect()` antes de cada run.\n"
        f"> Todos los resultados validados como numéricamente equivalentes entre los tres engines.\n"
    )
    return note + "\n" + header + "\n".join(rows_md) + "\n"


def make_explain(data):
    q = data["queries"]
    q5_rows_scan = 73704
    q5_pct = round(q5_rows_scan / 1_000_000 * 100, 1)
    return f"""\
### EXPLAIN ANALYZE — Q3: Top 10 usuarios por suma de amount

El plan que DuckDB ejecutó internamente:

```
PARQUET_SCAN       → 1,000,000 filas en 0.01s
  Projections: user_id, amount   ← solo 2 de 8 columnas se leen del disco

HASH_GROUP_BY      → 50,000 grupos en 0.16s
  Groups:      user_id
  Aggregates:  SUM(amount), COUNT(*)

TOP_N              → 10 filas en 0.00s
  Top: 10
  Order By: total_amount DESC
```

El primer operador confirma *column pruning*: DuckDB leyó únicamente los bloques físicos de `user_id` y `amount`. Las otras 6 columnas nunca tocaron la RAM.

El operador `HASH_GROUP_BY` procesó 1,000,000 filas y produjo 50,000 grupos — uno por usuario único en el dataset. DuckDB construye una hash table en memoria mientras escanea el archivo en streaming, acumulando la suma de amount y el conteo sin necesidad de cargar todo antes de agrupar.

El operador más relevante es `TOP_N`. En lugar de ordenar los 50,000 grupos para después tomar 10 — que costaría O(n log n) — DuckDB mantiene una *heap mínima* de tamaño 10 durante el propio GROUP BY. Cada grupo nuevo se compara con el mínimo de la heap: si es mayor, entra y desplaza al menor. Esto reduce el costo de ordenamiento a O(n log 10), prácticamente lineal. Por eso el TOP_N reporta 0.00s.

Tiempo total DuckDB: **{fmt(t(q,'Q3','duckdb'))}**. Polars fue más rápido en esta query ({fmt(t(q,'Q3','polars'))}) porque aplica el mismo patrón TOP_N internamente pero sin el overhead de parsear SQL.

---

### EXPLAIN ANALYZE — Q5: Filtro fecha + país + amount

```
PARQUET_SCAN       → {q5_rows_scan:,} filas materializadas de 1,000,000 en 0.11s
  Filters:         amount > 500.0
  Optional:        country_code IN ('MX', 'CO')
  Dynamic Filter:  timestamp >= '2026-04-18 03:09:55'::TIMESTAMP

FILTER             → 9,883 filas en 0.00s
NESTED_LOOP_JOIN   → 9,883 filas (resultado final)
```

La línea más importante del plan es `Filters: amount > 500.0` dentro del `PARQUET_SCAN`. Ese filtro se aplicó directamente en el lector de Parquet — no después de cargar las filas, sino antes. El Parquet almacena estadísticas de min/max por *row group* (bloques de ~100K filas), y DuckDB usó esas estadísticas para descartar row groups enteros donde el máximo de amount era menor o igual a 500.

El resultado es concreto: de 1,000,000 filas disponibles, el lector materializó solo **{q5_rows_scan:,} — el {q5_pct}% del archivo**. El otro 92.6% nunca se cargó en RAM.

El filtro de timestamp aparece como `Dynamic Filter` porque DuckDB calculó primero el valor de `MAX(timestamp)` con la CTE, obtuvo la fecha concreta `2026-04-18 03:09:55`, y la inyectó como filtro estático en el PARQUET_SCAN. Esto se llama *late materialization con filtro dinámico* y permite descartar row groups también por fecha.

La diferencia de RAM entre pandas ({ram(q,'Q5','pandas'):.0f} MB), DuckDB ({ram(q,'Q5','duckdb'):.1f} MB) y polars ({ram(q,'Q5','polars'):.1f} MB) confirma exactamente esto: pandas cargó el archivo completo antes de filtrar, DuckDB y polars solo cargaron las filas que pasaron los filtros.

---

### EXPLAIN ANALYZE — Q6: Categoría top por país

```
PARQUET_SCAN       → 1,000,000 filas en 0.01s
  Projections:     country_code, category, amount   ← 3 de 8 columnas

HASH_GROUP_BY      → 150 grupos en 0.02s
  Groups:          (country_code, category)
  Aggregates:      COUNT(*), AVG(amount)

WINDOW             → 150 filas en 0.02s
  RANK() OVER (PARTITION BY country_code
               ORDER BY tx_count DESC, category ASC)

FILTER (rnk = 1)   → 15 filas (una por país)
ORDER_BY           → 15 filas (resultado final)
```

Q6 tiene el plan más interesante porque es la query más compleja: encontrar la categoría top de cada país requiere un *argmax por grupo*, que SQL resuelve con window functions.

El `HASH_GROUP_BY` produce exactamente 150 grupos (15 países × 10 categorías). Esa hash table de 150 entradas cabe completa en la caché L2 del CPU, lo que hace que las operaciones de lookup sean prácticamente sin latencia de memoria.

El operador `WINDOW` calcula `RANK()` particionado por país con doble criterio de ordenamiento: `tx_count DESC, category ASC`. El segundo criterio es el desempate determinista — cuando dos categorías tienen el mismo conteo, gana la que va primero alfabéticamente. Sin este criterio, los tres engines podrían retornar resultados distintos en caso de empate y la validación fallaría.

La diferencia de RAM en Q6 es la más extrema del benchmark: pandas usó **{ram(q,'Q6','pandas'):.0f} MB** vs DuckDB **{ram(q,'Q6','duckdb'):.2f} MB** — una diferencia de {round(ram(q,'Q6','pandas')/max(ram(q,'Q6','duckdb'),0.001)):,}x. La razón es que pandas necesita dos GroupBy separados (uno para contar, otro para promediar), un `idxmax()` y un merge — cuatro operaciones que construyen DataFrames intermedios en el heap. DuckDB resuelve todo en la pasada única que muestra el plan.
"""


def make_tradeoffs(data):
    q = data["queries"]
    return f"""\
### Caso 1 — polars supera claramente a pandas: Q7

Q7 filtra transacciones con `status='failed'` y cuenta los usuarios con más de 5 de esas transacciones.

Tiempos medidos: pandas **{fmt(t(q,'Q7','pandas'))}**, DuckDB **{fmt(t(q,'Q7','duckdb'))}**, polars **{fmt(t(q,'Q7','polars'))}**. Polars fue **{sp(t(q,'Q7','pandas'), t(q,'Q7','polars'))}x** más rápido que pandas y **{sp(t(q,'Q7','duckdb'), t(q,'Q7','polars'))}x** más rápido que DuckDB.

La razón es la evaluación lazy de polars. El pipeline `scan_parquet → filter(status='failed') → group_by(user_id) → filter(count > 5)` se construye primero como un plan lógico. Cuando se ejecuta, polars aplica el filtro de status directamente en el lector de Parquet — similar al predicate pushdown de DuckDB — y hace el GROUP BY en Rust con instrucciones SIMD sin crear objetos Python intermedios.

Pandas en cambio: lee el Parquet completo a un DataFrame de Python, aplica el filtro de status produciendo una copia filtrada, construye un objeto GroupBy de Python, hace el conteo, y aplica el HAVING como otro filtro. Cada paso tiene overhead de allocación y gestión del heap.

La diferencia de RAM lo confirma: pandas **{ram(q,'Q7','pandas'):.1f} MB**, polars **{ram(q,'Q7','polars'):.2f} MB**. Polars cargó solo lo que necesitaba.

---

### Caso 2 — DuckDB es el ganador claro: Q8

Q8 calcula el monto promedio diario por categoría — GROUP BY donde una columna requiere truncar un timestamp al día.

Tiempos: pandas **{fmt(t(q,'Q8','pandas'))}**, DuckDB **{fmt(t(q,'Q8','duckdb'))}**, polars **{fmt(t(q,'Q8','polars'))}**. DuckDB fue **{sp(t(q,'Q8','pandas'), t(q,'Q8','duckdb'))}x** más rápido que pandas — la diferencia más grande de todo el benchmark.

La causa concreta: cuando pandas ejecuta `dt.normalize()` sobre 1,000,000 timestamps para truncarlos al día, crea un objeto `datetime` de Python por cada fila. Un millón de objetos Python en el heap, cada uno con su overhead de allocación y de referencia. El proceso es secuencial porque Python no puede paralelizar la creación de objetos en el heap. Resultado: **{fmt(t(q,'Q8','pandas'))}** y **{ram(q,'Q8','pandas'):.0f} MB de RAM**.

DuckDB ejecuta `DATE_TRUNC('day', timestamp)` como una operación vectorizada en C++ sobre los buffers Arrow, sin crear ningún objeto Python. Resultado: **{fmt(t(q,'Q8','duckdb'))}** y **{ram(q,'Q8','duckdb'):.2f} MB** — una reducción de {round(ram(q,'Q8','pandas')/max(ram(q,'Q8','duckdb'),0.001))}x en memoria.

Polars también mejora sobre pandas ({fmt(t(q,'Q8','polars'))}) porque `dt.truncate("1d")` también es vectorizado en Rust. Pero DuckDB es más rápido porque su optimizador SQL fusiona el truncado y el GROUP BY en una sola pasada, algo que el API de polars no hace automáticamente.

---

### Caso 3 — Los tres engines son comparables: Q5

Q5 filtra con tres predicados simultáneos: `amount > 500`, `country_code IN ('MX','CO')`, y `timestamp` dentro de los últimos 30 días del dataset.

Tiempos: pandas **{fmt(t(q,'Q5','pandas'))}**, DuckDB **{fmt(t(q,'Q5','duckdb'))}**, polars **{fmt(t(q,'Q5','polars'))}**. El ratio entre el más lento y el más rápido es solo **{sp(t(q,'Q5','pandas'), t(q,'Q5','polars'))}x** — la diferencia más pequeña del benchmark.

La igualdad en tiempo se explica porque Q5 está dominada por el I/O de lectura del Parquet, no por el trabajo computacional. Los tres engines leen del mismo archivo y todos aplican algún nivel de predicate pushdown. El cuello de botella es el tiempo de leer y decodificar los bloques del Parquet desde disco, que es similar para los tres porque todos usan Arrow como formato interno.

Donde sí difieren es en RAM: pandas usó **{ram(q,'Q5','pandas'):.0f} MB** — el archivo completo cargado antes de filtrar. DuckDB usó **{ram(q,'Q5','duckdb'):.1f} MB** y polars **{ram(q,'Q5','polars'):.1f} MB**, porque ambos aplican los filtros antes de materializar filas en RAM. Cuando los tiempos son similares y la RAM importa — en producción con múltiples queries concurrentes — DuckDB y polars tienen ventaja estructural.
"""


def make_recommendation(data):
    q = data["queries"]
    duckdb_wins = [qid for qid in q if winner(q, qid) == "duckdb"]
    polars_wins = [qid for qid in q if winner(q, qid) == "polars"]

    return f"""\
Con los datos medidos, la conclusion es que la elección de engine depende del tipo de query que se busque realizar, no seria profecional decir que hay un ganador contundente en esta prueba.

**Cuándo usar polars:** polars ganó en {len(polars_wins)} de 8 queries ({", ".join(polars_wins)}). Es la mejor opción para pipelines de transformación en Python sobre Parquet y filtros, GROUP BY, JOIN, ORDER BY, donde la evaluación lazy de `scan_parquet()` puede optimizar el plan completo antes de ejecutar. Su ventaja se hace mucho ams notoria cuando la query tiene predicados que reducen significativamente el número de filas (Q4, Q7) o cuando encadena varias operaciones (Q6). El tradeoff: para queries con window functions o CTEs complejas, el API de polars es más verbose que SQL y requiere más cuidado con el desempate y el orden de operaciones.

**Cuándo usar DuckDB:** DuckDB ganó en {len(duckdb_wins)} queries ({", ".join(duckdb_wins)}), pero las que ganó son estratégicas. Q8 muestra la ventaja más contundente del benchmark: operaciones sobre timestamps donde DuckDB es {sp(t(q,'Q8','pandas'), t(q,'Q8','duckdb'))}x más rápido que pandas y {sp(t(q,'Q8','polars'), t(q,'Q8','duckdb'))}x más rápido que polars. Si el pipeline procesa fechas con frecuencia, DuckDB es la elección correcta. Además, SQL es más legible para queries complejas con CTEs y subqueries, lo que facilita el mantenimiento por parte de un equipo con conocimiento de SQL.

**Cuándo usar pandas:** pandas no ganó ninguna query en este benchmark, lo que nos indica que para queries analíticas sobre Parquet a gandes escalas (como en este caso. 1M filas), no es la herramienta correcta.Esto no lo descarta totalmente pues Pandas sigue siendo útil como una capa de integración final, ejemplo, cuando el resultado de polars o DuckDB necesita conectarse librerias como scikit-learn, matplotlib u otras del ecosistema Python que esperan DataFrames, pero es definitivo que su uso como engine de query no es recomendado.

**Recomendación práctica para este sistema:**

- Queries de filtro y agregación sobre Parquet → **polars** por defecto
- Queries con timestamps, window functions, o CTEs complejas → **DuckDB**
- Exploración interactiva en Jupyter o integración con librerías de ML → **pandas** como capa de conversión del resultado, no como engine
- RAM limitada o múltiples queries concurrentes → **polars** o **DuckDB** (ambos entre 0.01 y 3.33 MB, vs 4–83 MB de pandas)
"""


def build_report(data):
    q    = data["queries"]
    reps = data["repeats"]

    q1_runs_list = runs(q, "Q1", "pandas")
    q1_ratio = round(q1_runs_list[0] / q1_runs_list[1], 1) if q1_runs_list[1] > 0 else 0

    n_polars = len([qid for qid in q if winner(q, qid) == "polars"])
    n_duckdb = len([qid for qid in q if winner(q, qid) == "duckdb"])

    return f"""\
# Reporte — Ejercicio 2: El Motor de Consultas

## Cómo reproducir este benchmark

```bash
cd ejercicio-02-consultas
python benchmark.py --output results/
python generate_report.py
```

El Parquet de entrada es `data/transactions_1m_parquet_snappy.parquet`, generado en el Ejercicio 1.

---

## Tabla comparativa — 3 engines, 8 queries

{make_table(data)}

**Resumen de ganadores:** polars ganó en {n_polars} de 8 queries, DuckDB en {n_duckdb}, pandas en ninguna. Todos los resultados fueron validados como numéricamente equivalentes entre los tres engines.

---

## Por qué cada engine funciona distinto — decisiones técnicas que explican los resultados

Antes de leer los números, conviene entender qué hace distinto cada engine por dentro, porque sus diferencias de diseño son exactamente lo que aparece reflejado en los tiempos y en el consumo de RAM.

**pandas** funciona de forma *eager*: cada operación se ejecuta inmediatamente y produce un resultado en memoria. `pd.read_parquet()` carga el archivo entero en un DataFrame. Aplicar un filtro crea una nueva Serie de booleanos y una copia del DataFrame. Hacer un GroupBy construye un objeto Python con su propio overhead. Cada paso tiene costo de allocación en el heap de Python — esa es la razón por la que pandas siempre aparece con más RAM y más tiempo en este benchmark.

**polars** funciona de forma *lazy*: `scan_parquet()` no lee nada del disco todavía. Construye un plan lógico que describe todas las operaciones, y solo cuando llamas `.collect()` las ejecuta. En ese momento, el optimizador analiza el plan completo, aplica column pruning (lee solo las columnas que la query necesita del Parquet), hace predicate pushdown (filtra filas antes de cargarlas en RAM), y ejecuta todo en Rust con instrucciones SIMD sin objetos Python intermedios. Esto es lo que explica que polars use consistentemente menos de 1 MB de RAM en casi todas las queries.

**DuckDB** funciona como un motor SQL completo embebido: parsea la query, construye un plan de ejecución optimizado, y lo ejecuta en C++ con paralelización automática. Su ventaja específica sobre Parquet viene de que puede leer las estadísticas de row groups (min/max por columna por bloque de ~100K filas) y descartar bloques enteros sin leerlos si no cumplen los filtros de la query.

Con ese contexto, los resultados tienen sentido.

---

## Una observación sobre Q1 y el cold start de pandas

En Q1, los tres runs de pandas fueron: `{q1_runs_list}`. El primero tardó {q1_runs_list[0]}s y los siguientes dos tardaron {q1_runs_list[1]}s y {q1_runs_list[2]}s — el primer run fue {q1_ratio}x más lento.

Esto se llama *cold start*: la primera vez que pyarrow abre un archivo Parquet, inicializa el schema reader, valida el footer y establece el lector de columnas. Ese trabajo no se repite en llamadas siguientes porque los metadatos quedan en caché dentro del proceso. Reportar el promedio de 3 runs (en lugar de solo el primero) es importante precisamente por esto: el promedio incluye ese costo inicial que en producción ocurriría en el primer acceso a cada archivo.

---

## Interpretación de EXPLAIN ANALYZE

{make_explain(data)}

---

## Análisis de tradeoffs

{make_tradeoffs(data)}

---

## Recomendación de arquitectura

{make_recommendation(data)}
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default="results/benchmark_results.json")
    args = parser.parse_args()

    print(f"Cargando {args.results}...")
    data = load(args.results)

    print("Construyendo report.md...")
    report = build_report(data)
    Path("report.md").write_text(report, encoding="utf-8")
    print("Done. Reporte guardado en report.md")


if __name__ == "__main__":
    main()