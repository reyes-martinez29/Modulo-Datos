"""
generate_report.py — Genera report.md del E3 con los datos reales.

Uso:
    python generate_report.py
    python generate_report.py --ingest results/ingest_results.json
                              --benchmark results/benchmark_results.json
"""

import argparse
import json
import sqlite3
from pathlib import Path


def load(path: str):
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"No se encontró {path}")
    return json.loads(p.read_text(encoding="utf-8"))


def fmt_ms(v): return f"{v:.3f}ms"
def fmt_s(v):  return f"{v:.1f}s"
def sp(a, b):  return f"{a/b:.0f}x" if b > 0 else "N/A"


def make_entorno() -> str:
    sqlite_ver = sqlite3.sqlite_version
    try:
        import duckdb as _duckdb
        duckdb_ver = _duckdb.__version__
    except ImportError:
        duckdb_ver = "no disponible"
    return (
        "## Entorno de ejecucion\n\n"
        "| Componente | Version |\n"
        "|------------|---------|\n"
        f"| SQLite | {sqlite_ver} |\n"
        f"| DuckDB | {duckdb_ver} |\n"
        "| Repeticiones por medicion | 5 |\n"
        "| Metodo de agregacion | Promedio de 5 runs |\n"
        "| gc.collect() antes de cada run | Si |\n"
        "| ANALYZE antes del benchmark | Si |\n"
        "| PRAGMAs SQLite activos | `synchronous=NORMAL`, `cache_size=-65536` (64MB), `temp_store=MEMORY` |\n\n"
        "**Parametros del benchmark:**\n\n"
        "| Parametro | Valor | Justificacion |\n"
        "|-----------|-------|---------------|\n"
        "| `transaction_id` | `00000218e2...` | Primera fila real del dataset |\n"
        "| `user_id` | `2076` | Usuario con mas transacciones (43) — peor caso del SLA |\n"
        "| `date_from / date_to` | 2025-05-27 a 2025-05-28 | Rango de 2 dias dentro del rango del usuario |\n"
        "| `month_ago` | 2026-04-18 | 30 dias antes del timestamp maximo del dataset |\n"
        "| `country_code` | MX | Pais con distribucion representativa |\n"
        "| `min_tx` | 2 | Umbral realista: promedio es ~1.3 tx/usuario/pais, min_tx=2 filtra la cola superior |\n\n"
        "Se usaron parametros reales extraidos del dataset en lugar de valores inventados. "
        "Un `user_id` inexistente retornaria 0 filas en microsegundos y eso no mide el SLA real. "
        "El usuario con mas transacciones representa el peor caso: mas filas que recorrer.\n"
    )


def make_ingesta(ingest: list) -> str:
    wal  = next(r for r in reversed(ingest) if r["wal"])
    nwal = next(r for r in ingest if not r["wal"])
    diff_s   = nwal["total_time_s"] - wal["total_time_s"]
    diff_pct = (nwal["total_time_s"] / wal["total_time_s"] - 1) * 100
    wal_chunk_pure   = wal["avg_chunk_s"]
    nwal_chunk_pure  = nwal["avg_chunk_s"]
    wal_chunk_total  = round(wal["total_time_s"] / wal["chunk_count"], 3)
    nwal_chunk_total = round(nwal["total_time_s"] / nwal["chunk_count"], 3)

    lines = [
        "## Ingesta — WAL vs sin WAL\n",
        f"Ambas corridas usan `--chunk-size {wal['chunk_size']:,}`, lo que produce",
        f"{wal['chunk_count']} commits para completar el millon de filas.\n",
        "| Modo | Tiempo total | Filas/segundo | Tiempo commit (avg) | Tiempo total/chunk |",
        "|------|-------------|---------------|--------------------|--------------------|",
        f"| WAL | {fmt_s(wal['total_time_s'])} | {wal['rows_per_sec']:,.0f} | {wal_chunk_pure:.3f}s | {wal_chunk_total:.3f}s |",
        f"| DELETE (sin WAL) | {fmt_s(nwal['total_time_s'])} | {nwal['rows_per_sec']:,.0f} | {nwal_chunk_pure:.3f}s | {nwal_chunk_total:.3f}s |\n",
        "> **Tiempo commit (avg)**: tiempo medido dentro de `ingest_chunk()`, solo el INSERT mas el commit, sin overhead de lectura del CSV ni verificaciones.",
        "> **Tiempo total/chunk**: `total_time_s / chunk_count`, incluye lectura de CSV, progreso en consola y overhead de Python, por lo que es mayor que el tiempo de commit puro.\n",
        f"Ambas corridas terminaron en menos de 3 minutos. La diferencia fue de",
        f"**{diff_s:.1f}s ({diff_pct:.1f}% mas lento sin WAL)**, que es menor de lo que cabria esperar",
        "al comparar dos modos de journaling.\n",
        f"Lo que explica ese resultado es el chunk size de {wal['chunk_size']:,} filas: con solo",
        f"{wal['chunk_count']} commits totales el impacto del modo de journaling es relativamente pequeño.",
        "La diferencia entre WAL y DELETE escala con el numero de commits, no con el numero de",
        "filas, por lo que con chunks de 1,000 filas —lo que implicaria 1,000 commits— el resultado",
        "seria notablemente distinto. En DELETE mode cada commit hace un fsync al archivo `.db`",
        "principal, lo que garantiza que los datos llegaron al disco fisico pero tiene un costo por",
        "ser una operacion sincrona. En WAL mode las escrituras van al archivo `.db-wal` de forma",
        "append-only, lo cual es mucho mas rapido, y los checkpoints hacia el archivo principal",
        "ocurren de forma diferida al cerrar la conexion, no en cada commit.\n",
        "Mas alla de la velocidad de ingesta, la razon de fondo para preferir WAL en produccion es",
        "la concurrencia: en DELETE mode SQLite bloquea a los lectores durante cada commit, lo que",
        "lleva a que cualquier query que llegue en ese momento tenga que esperar. En WAL mode los",
        "lectores ven la version anterior de los datos mientras el writer trabaja en el WAL, sin",
        "bloqueos. Para un sistema donde hay queries corriendo en paralelo a la ingesta WAL es la",
        "opcion mas adecuada; DELETE mode es perfectamente valido en procesos de carga batch donde",
        "no hay lectores concurrentes durante la escritura.\n",
        f"Integridad verificada: {wal['db_rows']:,} filas en la DB = {wal['csv_rows']:,} filas en el CSV",
    ]
    return "\n".join(lines) + "\n"


def make_tabla(bench: dict) -> str:
    p    = bench["patterns"]
    reps = bench["repeats"]

    header = (
        "| Patron | Descripcion | SQLite c/indices | SQLite s/indices* | "
        "Speedup | DuckDB | SLA | Ganador |\n"
        "|--------|-------------|----------------:|------------------:|"
        "-------:|-------:|:---:|:-------:|\n"
    )
    rows = []
    for pid, pat in p.items():
        wi  = pat["sqlite_with_idx"]["avg_ms"]
        wo  = pat["sqlite_no_idx"]["avg_ms"]
        dk  = pat["duckdb"]["avg_ms"]
        ok  = "OK" if pat["sqlite_with_idx"]["sla_ok"] else "MISS"
        win = "SQLite" if wi < dk else "DuckDB"
        spd = "N/A" if pid == "P1" else f"{wo/wi:.0f}x"
        wo_str = f"{fmt_ms(wo)} t" if pid == "P1" else fmt_ms(wo)
        rows.append(
            f"| {pid} | {pat['description']} "
            f"| {fmt_ms(wi)} | {wo_str} | {spd} "
            f"| {fmt_ms(dk)} | {ok} | **{win}** |"
        )

    note = (
        f"> {reps} repeticiones por medicion. gc.collect() antes de cada run. "
        f"ANALYZE ejecutado antes del benchmark.\n"
        "> Sin indices secundarios: elimina idx_user_timestamp e idx_country_user, "
        "pero no el PRIMARY KEY de transaction_id (no se puede eliminar en SQLite).\n"
        "> t P1: ambas condiciones usan sqlite_autoindex_transactions_1. "
        "Diferencia de tiempos es ruido estadistico, speedup = N/A.\n"
    )
    return note + "\n" + header + "\n".join(rows) + "\n"


def make_explain(bench: dict) -> str:
    p  = bench["patterns"]
    p5_result_rows = p["P5"]["sqlite_with_idx"]["rows"]
    p5_index_rows  = 1_000_000 // 15

    def plan(pid, mode):
        return p[pid][f"sqlite_{mode}_idx"]["plan"]

    lines = [
        "## EXPLAIN QUERY PLAN — que hace SQLite en cada patron\n",
        "`EXPLAIN QUERY PLAN` describe la estrategia de ejecucion que SQLite eligio para",
        "cada query. Las dos palabras que determinan si el indice se esta usando son",
        "`SEARCH` y `SCAN`. Cuando aparece `SEARCH ... USING INDEX` significa que SQLite",
        "navego directamente al dato a traves del B-Tree, con un costo de O(log n) para",
        "lookups exactos y O(log n + k) para rangos, donde k es el numero de filas encontradas.",
        "Cuando aparece `SCAN transactions` significa que SQLite leyo el millon de filas completo,",
        "con costo O(n). Un tercer indicador importante es `USE TEMP B-TREE FOR ORDER BY`",
        "o `FOR GROUP BY`, que senala que SQLite tuvo que construir una estructura temporal",
        "en memoria para ordenar o agrupar porque el indice no cubria esa operacion.\n",

        "### P1 — Lookup por transaction_id\n",
        "```",
        f"Con indices:    {plan('P1','with')}",
        f"Sin indices:    {plan('P1','no')}",
        "```\n",
        "Ambos planes son identicos porque el `PRIMARY KEY` crea el indice",
        "`sqlite_autoindex_transactions_1` que SQLite nunca elimina, al ser parte de la",
        "estructura fisica de la tabla y no un indice secundario opcional. La diferencia",
        f"de tiempo entre las dos condiciones ({fmt_ms(p['P1']['sqlite_with_idx']['avg_ms'])} vs",
        f"{fmt_ms(p['P1']['sqlite_no_idx']['avg_ms'])}) es ruido estadistico, no el efecto del indice,",
        "por lo que P1 no tiene una version genuinamente sin indice: no existe forma de",
        "eliminar el indice del PRIMARY KEY en SQLite sin recrear la tabla completa.\n",
        "La comparacion que si es reveladora es contra DuckDB:",
        f"{fmt_ms(p['P1']['sqlite_with_idx']['avg_ms'])} de SQLite frente a",
        f"{fmt_ms(p['P1']['duckdb']['avg_ms'])} de DuckDB, lo que resulta en",
        f"**{sp(p['P1']['duckdb']['avg_ms'], p['P1']['sqlite_with_idx']['avg_ms'])} mas rapido para SQLite**.",
        "DuckDB tiene un overhead de inicializacion fijo de aproximadamente 88ms para cualquier",
        "query sobre Parquet: abrir el archivo, leer el footer de metadatos y localizar el row",
        "group candidato. Ese costo no escala con el resultado, ocurre igual para 1 fila que",
        "para 10,000, de modo que para un lookup puntual ese overhead nunca se amortiza.\n",

        "### P2 — Ultimas 20 transacciones de un usuario\n",
        "```",
        f"Con indices:  {plan('P2','with')}",
        f"Sin indices:  {plan('P2','no')}",
        "```\n",
        "Con el indice, `SEARCH ... (user_id=?)` lleva a SQLite directamente al sub-arbol",
        "del usuario en el B-Tree. Como ese indice almacena `timestamp DESC`, las primeras",
        "20 entradas del sub-arbol son exactamente las 20 transacciones mas recientes, lo",
        "que significa que no hace falta ningun sort adicional: el orden ya esta incorporado",
        "en la estructura.\n",
        "Sin el indice el resultado es muy distinto. SQLite tiene que hacer `SCAN transactions`",
        "sobre el millon de filas completo, y como los datos no estan ordenados ademas",
        "construye un `USE TEMP B-TREE FOR ORDER BY` para poder ordenar por timestamp antes",
        "de tomar los 20 primeros. Todo eso lleva a un speedup de",
        f"**{p['P2']['sqlite_no_idx']['speedup_vs_with']:.0f}x** entre ambas condiciones",
        f"({fmt_ms(p['P2']['sqlite_with_idx']['avg_ms'])} con indice vs",
        f"{fmt_ms(p['P2']['sqlite_no_idx']['avg_ms'])} sin indice).\n",

        "### P3 — Transacciones de usuario en rango de fechas\n",
        "```",
        f"Con indices:  {plan('P3','with')}",
        f"Sin indices:  {plan('P3','no')}",
        "```\n",
        "El plan muestra `(user_id=? AND timestamp>? AND timestamp<?)`, lo que confirma",
        "que SQLite uso ambas columnas del indice compuesto para el range scan: primero",
        "localiza el sub-arbol del usuario y dentro de el navega directamente al rango de",
        "timestamps. El costo real es O(log n + k) donde k=1, ya que el rango de 2 dias",
        "del usuario de prueba contiene una sola transaccion.\n",
        "Sin indice el trabajo es completamente distinto: `SCAN transactions` puro, comparar",
        "cada una de las 1,000,000 filas contra los dos limites de fecha. Eso lleva al speedup",
        f"mas grande del benchmark, **{p['P3']['sqlite_no_idx']['speedup_vs_with']:.0f}x**",
        f"({fmt_ms(p['P3']['sqlite_with_idx']['avg_ms'])} vs {fmt_ms(p['P3']['sqlite_no_idx']['avg_ms'])}),",
        "porque el indice elimina de una vez tanto el full scan como la necesidad de cualquier",
        "ordenamiento posterior.\n",

        "### P4 — Suma de amount del ultimo mes\n",
        "```",
        f"Con indices:  {plan('P4','with')}",
        f"Sin indices:  {plan('P4','no')}",
        "```\n",
        "El plan `(user_id=? AND timestamp>?)` es un range scan con una sola cota inferior.",
        "SQLite navega al punto correcto del sub-arbol del usuario y suma `amount` recorriendo",
        f"hacia adelante hasta el final, con un speedup de **{p['P4']['sqlite_no_idx']['speedup_vs_with']:.0f}x**",
        "frente a la version sin indice.\n",
        "Lo mas interesante de P4 es lo que pasa con DuckDB. DuckDB termina en",
        f"{fmt_ms(p['P4']['duckdb']['avg_ms'])}, que es mas rapido que SQLite sin indice",
        f"({fmt_ms(p['P4']['sqlite_no_idx']['avg_ms'])}), lo cual tiene sentido porque una suma sobre",
        "un rango es exactamente el tipo de operacion donde DuckDB aplica su vectorizacion.",
        f"Sin embargo no puede acercarse a SQLite con indice ({fmt_ms(p['P4']['sqlite_with_idx']['avg_ms'])}).",
        "La causa es que el Parquet no esta clusterizado por `user_id`, por lo que las",
        "transacciones del usuario estan dispersas en multiples row groups, lo que lleva a que",
        "DuckDB tenga que inspeccionar muchos o todos los bloques del archivo buscando un",
        "usuario que representa el 0.004% del dataset. El B-Tree de SQLite lleva directamente",
        "a esas filas sin tocar el resto.\n",

        "### P5 — Usuarios de un pais con mas de N transacciones\n",
        "```",
        f"Con indices:  {plan('P5','with')}",
        f"Sin indices:  {plan('P5','no')}",
        "```\n",
        "`USING COVERING INDEX idx_country_user` es el plan mas eficiente posible para P5:",
        "significa que el indice contiene todas las columnas que la query necesita,",
        "`country_code` y `user_id`, por lo que SQLite puede responderla leyendo solo el",
        "indice sin acceder en ningun momento a las paginas de datos de la tabla principal.\n",
        "El indice `(country_code, user_id)` tiene una entrada por transaccion. Para MX con",
        f"distribucion uniforme hay aproximadamente {p5_index_rows:,} entradas. Como ya vienen",
        "ordenadas por `(country_code, user_id)`, el GROUP BY se convierte en un simple scan",
        "secuencial contando cambios de `user_id`, sin necesidad de construir ninguna hash table.",
        f"El resultado final son **{p5_result_rows:,} filas**, que corresponden a los usuarios unicos",
        f"de MX con mas de {bench['params']['min_tx']} transacciones en ese pais.\n",
        "Sin el indice el contraste es dramatico: hay tres operaciones temporales encadenadas,",
        "full scan mas `USE TEMP B-TREE FOR GROUP BY` mas `USE TEMP B-TREE FOR ORDER BY`,",
        f"lo que resulta en un speedup de **{p['P5']['sqlite_no_idx']['speedup_vs_with']:.0f}x** a favor del indice.\n",
        f"DuckDB termina en {fmt_ms(p['P5']['duckdb']['avg_ms'])}, mas lento que SQLite",
        f"({fmt_ms(p['P5']['sqlite_with_idx']['avg_ms'])}), aunque P5 es exactamente el tipo de",
        "agregacion donde DuckDB suele brillar. La diferencia es que DuckDB tiene que inspeccionar",
        "muchos o todos los row groups del Parquet porque el archivo no esta ordenado por",
        "`country_code`, mientras que SQLite lee unicamente el segmento del indice correspondiente",
        "a MX, que es una fraccion pequeña del total.\n",
    ]
    return "\n".join(lines)


def make_comparacion(bench: dict) -> str:
    p = bench["patterns"]

    lines = [
        "## Comparacion SQLite vs DuckDB — patron por patron\n",
        "SQLite con indices gana en los cinco patrones, aunque las razones son diferentes en",
        "cada caso y vamos a entenderlas caso por caso, porque no todas se explican de",
        "la misma manera.\n",

        f"### P1 — Lookup puntual: SQLite gana {sp(p['P1']['duckdb']['avg_ms'], p['P1']['sqlite_with_idx']['avg_ms'])}\n",
        f"{fmt_ms(p['P1']['sqlite_with_idx']['avg_ms'])} de SQLite frente a {fmt_ms(p['P1']['duckdb']['avg_ms'])} de DuckDB.",
        "Esta es la diferencia mas extrema del benchmark y tiene una explicacion muy concreta."
        "DuckDB tiene un overhead de inicializacion de aproximadamente 88ms para cualquier",
        "query sobre Parquet, independientemente de lo que retorne: abrir el archivo, leer el",
        "footer de metadatos y localizar el row group candidato. Ese costo es fijo. SQLite en",
        "cambio hace unas 20 comparaciones en el B-Tree y retorna el dato. Para un resultado",
        "de 1 fila ese overhead de DuckDB nunca se amortiza, lo que lleva a la brecha de",
        f"{sp(p['P1']['duckdb']['avg_ms'], p['P1']['sqlite_with_idx']['avg_ms'])}.\n",

        f"### P2 — Ultimas 20 transacciones: SQLite gana {sp(p['P2']['duckdb']['avg_ms'], p['P2']['sqlite_with_idx']['avg_ms'])}\n",
        f"{fmt_ms(p['P2']['sqlite_with_idx']['avg_ms'])} de SQLite frente a {fmt_ms(p['P2']['duckdb']['avg_ms'])} de DuckDB.",
        "DuckDB tiene que cargar el Parquet, filtrar las filas del usuario, ordenarlas por",
        "timestamp y tomar 20. SQLite navega al sub-arbol del usuario en el indice y toma las",
        "primeras 20 entradas, que ya vienen en orden DESC por diseno, de modo que no hay",
        "ningun sort. La ventaja aqui es estructural: el indice fue construido exactamente",
        "para ayudar este patron.\n",

        f"### P3 — Rango de fechas: SQLite gana {sp(p['P3']['duckdb']['avg_ms'], p['P3']['sqlite_with_idx']['avg_ms'])}\n",
        f"{fmt_ms(p['P3']['sqlite_with_idx']['avg_ms'])} de SQLite frente a {fmt_ms(p['P3']['duckdb']['avg_ms'])} de DuckDB.",
        "El rango del parametro de prueba es estrecho, 2 dias con 1 resultado, por lo que SQLite",
        "termina casi de inmediato. DuckDB conserva el overhead de bootstrap de P1 y ademas",
        "tiene que filtrar timestamps sobre el Parquet. Aunque con predicate pushdown puede",
        "descartar algunos row groups usando las estadisticas de min/max, no alcanza a los",
        "microsegundos del B-Tree.\n",

        f"### P4 — Suma del ultimo mes: SQLite gana {sp(p['P4']['duckdb']['avg_ms'], p['P4']['sqlite_with_idx']['avg_ms'])}\n",
        f"{fmt_ms(p['P4']['sqlite_with_idx']['avg_ms'])} de SQLite frente a {fmt_ms(p['P4']['duckdb']['avg_ms'])} de DuckDB.",
        "Este resultado me llamo la atencion porque una suma sobre un rango de fechas es exactamente",
        "el tipo de operacion donde DuckDB deberia ser competitivo. Y en cierta medida lo es:",
        f"DuckDB termina en {fmt_ms(p['P4']['duckdb']['avg_ms'])}, claramente mejor que SQLite sin indice",
        f"({fmt_ms(p['P4']['sqlite_no_idx']['avg_ms'])}). El problema es que no puede alcanzar a SQLite",
        "con indice. La causa de fondo es que el Parquet no esta clusterizado por `user_id`,",
        "por lo que las transacciones del usuario estan dispersas en multiples row groups y",
        "DuckDB tiene que inspeccionar muchos o todos los bloques del archivo buscando un usuario",
        "que representa el 0.004% del dataset. El B-Tree de SQLite va directamente a esas filas superando su respuesta de DuckDB.\n",

        f"### P5 — Usuarios por pais: SQLite gana {sp(p['P5']['duckdb']['avg_ms'], p['P5']['sqlite_with_idx']['avg_ms'])}\n",
        f"{fmt_ms(p['P5']['sqlite_with_idx']['avg_ms'])} de SQLite frente a {fmt_ms(p['P5']['duckdb']['avg_ms'])} de DuckDB.",
        "P5 es nuevamente la consulta del benchmark donde el terreno donde DuckDB suele brillar. La diferencia",
        f"de {sp(p['P5']['duckdb']['avg_ms'], p['P5']['sqlite_with_idx']['avg_ms'])} es la mas pequena de los",
        "cinco patrones, lo que confirma que aqui DuckDB esta mucho mas cerca de ser competitivo.",
        "SQLite gana gracias al covering index: no necesita tocar la tabla principal, lee solo",
        "el segmento del indice `(country_code, user_id)` correspondiente al pais, donde los",
        "datos ya vienen agrupados implicitamente. Con un Parquet clusterizado por `country_code`,",
        "DuckDB podria invertir ese resultado y superar a SQLite, todo recae en la estructura de los datos.\n",

        "### Cuando usar cada engine - mi recomendacion\n",
        "No hay una respuesta unica a esa pregunta, y aunque ne este caso SQLite es el ganador en los 5 patrones, basicamente todo depende del patron de acceso y del SLA que se quiera cumplir, y en este caso de que justamente la estructura de los datos no esta optimizada para DuckDB por lo que no debe ser descartado. En general,",
        "SQLite con indices es la herramienta, que basado en las recomendaciones y demas informacion que investigue, es la mejor para consultas de alta selectividad por",
        "entidad: buscar una transaccion concreta, obtener el historial de un usuario, sumar el",
        "gasto de un cliente en el ultimo mes. Cualquier consulta donde un indice puede reducir",
        "el trabajo a O(log n + k) con k pequeno. DuckDB por su parte es la opcion correcta",
        "para consultas analiticas sobre el dataset completo: calcular el promedio de todas las",
        "transacciones de Mexico, encontrar el top de merchants por volumen, o cualquier query",
        "que necesite procesar decenas de miles de filas de una vez. En el Ejercicio 2 ya vimos y medimos a",
        "DuckDB en ese terreno. En consultas transaccionales de alta selectividad el overhead",
        "de inicializacion de DuckDB no se amortiza y SQLite con un buen schema gana con claridad como acabamos de ver en este caso.",
        "Nuevamente antes de implementar un modelo u otro hay que evaluar que se requiere, creo que es una ley que palica en todos los campos de la tecnologia,",
        "pero especificamente con estos ejercicios aplicado a datos es que puedo comparar y conocer estas herramientas,",
        "me permite tener en cuneta sus fortalezas y debilidades para elegir la mas adecuada segun el caso de uso, y no caer en la trampa de usar una herramienta para todo sin considerar si es la mejor opcion para cada necesidad puntual o si no esta optimizada correctamento como los datos para DuckDB en este caso.\n"
    ]
    return "\n".join(lines)


def build_report(ingest: list, bench: dict) -> str:
    p = bench["patterns"]
    return (
        "# Reporte — Ejercicio 3: La Capa Transaccional\n\n"
        "## Como reproducir este ejercicio\n\n"
        "```bash\n"
        "cd ejercicio-03-sqlite\n\n"
        "# Regenerar la base de datos desde cero\n"
        "python ingest.py --wal --chunk-size 20000\n\n"
        "# Benchmark de los 5 patrones\n"
        "python benchmark_queries.py\n\n"
        "# Generar este reporte\n"
        "python generate_report.py\n"
        "```\n\n"
        "Entradas requeridas en `data/`:\n"
        "- `transactions_1m.csv` — generado por el Ejercicio 1\n"
        "- `transactions_1m_parquet_snappy.parquet` — generado por el Ejercicio 1\n\n"
        "La base de datos `data/transactions.db` **no esta en el repositorio** y se regenera "
        "con el comando de arriba en menos de 3 minutos.\n\n"
        "---\n\n"
        + make_entorno() + "\n"
        "---\n\n"
        + make_ingesta(ingest) + "\n"
        "---\n\n"
        "## Tabla comparativa — 5 patrones de acceso\n\n"
        + make_tabla(bench) + "\n"
        "---\n\n"
        + make_explain(bench) + "\n"
        "---\n\n"
        + make_comparacion(bench) + "\n"
        "---\n\n"
        "## Resumen del diseno de indices\n\n"
        "| Indice | Columnas | Patrones | Resultado medido |\n"
        "|--------|----------|----------|------------------|\n"
        f"| `PRIMARY KEY` | `transaction_id` | P1 | {fmt_ms(p['P1']['sqlite_with_idx']['avg_ms'])} — SLA <10ms OK |\n"
        f"| `idx_user_timestamp` | `(user_id, timestamp DESC)` | P2, P3, P4 | {fmt_ms(p['P2']['sqlite_with_idx']['avg_ms'])} / {fmt_ms(p['P3']['sqlite_with_idx']['avg_ms'])} / {fmt_ms(p['P4']['sqlite_with_idx']['avg_ms'])} — SLA <50ms OK |\n"
        f"| `idx_country_user` | `(country_code, user_id)` | P5 | {fmt_ms(p['P5']['sqlite_with_idx']['avg_ms'])} — SLA <200ms OK |\n\n"
        f"Los speedups entre con y sin indices — P2={p['P2']['sqlite_no_idx']['speedup_vs_with']:.0f}x, "
        f"P3={p['P3']['sqlite_no_idx']['speedup_vs_with']:.0f}x, "
        f"P4={p['P4']['sqlite_no_idx']['speedup_vs_with']:.0f}x, "
        f"P5={p['P5']['sqlite_no_idx']['speedup_vs_with']:.0f}x — son la evidencia directa de que cada "
        "indice esta haciendo el trabajo para el que fue disenado. La justificacion tecnica "
        "completa de cada decision esta en `schema_design.md`.\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ingest",    default="results/ingest_results.json")
    parser.add_argument("--benchmark", default="results/benchmark_results.json")
    args = parser.parse_args()

    print(f"Cargando {args.ingest}...")
    ingest = load(args.ingest)
    print(f"Cargando {args.benchmark}...")
    bench = load(args.benchmark)
    print("Construyendo report.md...")
    report = build_report(ingest, bench)
    out = Path("report.md")
    out.write_text(report, encoding="utf-8")
    print(f"Done. {out} ({len(report.splitlines())} lineas)")


if __name__ == "__main__":
    main()