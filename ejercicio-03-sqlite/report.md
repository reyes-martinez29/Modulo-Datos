# Reporte — Ejercicio 3: La Capa Transaccional

## Como reproducir este ejercicio

```bash
cd ejercicio-03-sqlite

# Regenerar la base de datos desde cero
python ingest.py --wal --chunk-size 20000

# Benchmark de los 5 patrones
python benchmark_queries.py

# Generar este reporte
python generate_report.py
```

Entradas requeridas en `data/`:
- `transactions_1m.csv` — generado por el Ejercicio 1
- `transactions_1m_parquet_snappy.parquet` — generado por el Ejercicio 1

La base de datos `data/transactions.db` **no esta en el repositorio** y se regenera con el comando de arriba en menos de 3 minutos.

---

## Entorno de ejecucion

| Componente | Version |
|------------|---------|
| SQLite | 3.45.3 |
| DuckDB | 1.5.2 |
| Repeticiones por medicion | 5 |
| Metodo de agregacion | Promedio de 5 runs |
| gc.collect() antes de cada run | Si |
| ANALYZE antes del benchmark | Si |
| PRAGMAs SQLite activos | `synchronous=NORMAL`, `cache_size=-65536` (64MB), `temp_store=MEMORY` |

**Parametros del benchmark:**

| Parametro | Valor | Justificacion |
|-----------|-------|---------------|
| `transaction_id` | `00000218e2...` | Primera fila real del dataset |
| `user_id` | `2076` | Usuario con mas transacciones (43) — peor caso del SLA |
| `date_from / date_to` | 2025-05-27 a 2025-05-28 | Rango de 2 dias dentro del rango del usuario |
| `month_ago` | 2026-04-18 | 30 dias antes del timestamp maximo del dataset |
| `country_code` | MX | Pais con distribucion representativa |
| `min_tx` | 2 | Umbral realista: promedio es ~1.3 tx/usuario/pais, min_tx=2 filtra la cola superior |

Se usaron parametros reales extraidos del dataset en lugar de valores inventados. Un `user_id` inexistente retornaria 0 filas en microsegundos y eso no mide el SLA real. El usuario con mas transacciones representa el peor caso: mas filas que recorrer.

---

## Ingesta — WAL vs sin WAL

Ambas corridas usan `--chunk-size 20,000`, lo que produce
50 commits para completar el millon de filas.

| Modo | Tiempo total | Filas/segundo | Tiempo commit (avg) | Tiempo total/chunk |
|------|-------------|---------------|--------------------|--------------------|
| WAL | 40.8s | 24,514 | 0.785s | 0.816s |
| DELETE (sin WAL) | 44.3s | 22,593 | 0.853s | 0.885s |

> **Tiempo commit (avg)**: tiempo medido dentro de `ingest_chunk()`, solo el INSERT mas el commit, sin overhead de lectura del CSV ni verificaciones.
> **Tiempo total/chunk**: `total_time_s / chunk_count`, incluye lectura de CSV, progreso en consola y overhead de Python, por lo que es mayor que el tiempo de commit puro.

Ambas corridas terminaron en menos de 3 minutos. La diferencia fue de
**3.5s (8.5% mas lento sin WAL)**, que es menor de lo que cabria esperar
al comparar dos modos de journaling.

Lo que explica ese resultado es el chunk size de 20,000 filas: con solo
50 commits totales el impacto del modo de journaling es relativamente pequeño.
La diferencia entre WAL y DELETE escala con el numero de commits, no con el numero de
filas, por lo que con chunks de 1,000 filas —lo que implicaria 1,000 commits— el resultado
seria notablemente distinto. En DELETE mode cada commit hace un fsync al archivo `.db`
principal, lo que garantiza que los datos llegaron al disco fisico pero tiene un costo por
ser una operacion sincrona. En WAL mode las escrituras van al archivo `.db-wal` de forma
append-only, lo cual es mucho mas rapido, y los checkpoints hacia el archivo principal
ocurren de forma diferida al cerrar la conexion, no en cada commit.

Mas alla de la velocidad de ingesta, la razon de fondo para preferir WAL en produccion es
la concurrencia: en DELETE mode SQLite bloquea a los lectores durante cada commit, lo que
lleva a que cualquier query que llegue en ese momento tenga que esperar. En WAL mode los
lectores ven la version anterior de los datos mientras el writer trabaja en el WAL, sin
bloqueos. Para un sistema donde hay queries corriendo en paralelo a la ingesta WAL es la
opcion mas adecuada; DELETE mode es perfectamente valido en procesos de carga batch donde
no hay lectores concurrentes durante la escritura.

Integridad verificada: 1,000,000 filas en la DB = 1,000,000 filas en el CSV

---

## Tabla comparativa — 5 patrones de acceso

> 5 repeticiones por medicion. gc.collect() antes de cada run. ANALYZE ejecutado antes del benchmark.
> Sin indices secundarios: elimina idx_user_timestamp e idx_country_user, pero no el PRIMARY KEY de transaction_id (no se puede eliminar en SQLite).
> t P1: ambas condiciones usan sqlite_autoindex_transactions_1. Diferencia de tiempos es ruido estadistico, speedup = N/A.

| Patron | Descripcion | SQLite c/indices | SQLite s/indices* | Speedup | DuckDB | SLA | Ganador |
|--------|-------------|----------------:|------------------:|-------:|-------:|:---:|:-------:|
| P1 | Buscar transacción por transaction_id exacto | 0.133ms | 0.063ms t | N/A | 88.807ms | OK | **SQLite** |
| P2 | Últimas 20 transacciones de un usuario | 0.154ms | 108.302ms | 703x | 98.187ms | OK | **SQLite** |
| P3 | Transacciones de un usuario en un rango de fechas | 0.083ms | 112.477ms | 1355x | 56.781ms | OK | **SQLite** |
| P4 | Suma de amount de un usuario en el último mes | 0.158ms | 108.429ms | 686x | 20.110ms | OK | **SQLite** |
| P5 | Usuarios de un país con más de N transacciones | 8.590ms | 143.296ms | 17x | 14.630ms | OK | **SQLite** |

---

## EXPLAIN QUERY PLAN — que hace SQLite en cada patron

`EXPLAIN QUERY PLAN` describe la estrategia de ejecucion que SQLite eligio para
cada query. Las dos palabras que determinan si el indice se esta usando son
`SEARCH` y `SCAN`. Cuando aparece `SEARCH ... USING INDEX` significa que SQLite
navego directamente al dato a traves del B-Tree, con un costo de O(log n) para
lookups exactos y O(log n + k) para rangos, donde k es el numero de filas encontradas.
Cuando aparece `SCAN transactions` significa que SQLite leyo el millon de filas completo,
con costo O(n). Un tercer indicador importante es `USE TEMP B-TREE FOR ORDER BY`
o `FOR GROUP BY`, que senala que SQLite tuvo que construir una estructura temporal
en memoria para ordenar o agrupar porque el indice no cubria esa operacion.

### P1 — Lookup por transaction_id

```
Con indices:    SEARCH transactions USING INDEX sqlite_autoindex_transactions_1 (transaction_id=?)
Sin indices:    SEARCH transactions USING INDEX sqlite_autoindex_transactions_1 (transaction_id=?)
```

Ambos planes son identicos porque el `PRIMARY KEY` crea el indice
`sqlite_autoindex_transactions_1` que SQLite nunca elimina, al ser parte de la
estructura fisica de la tabla y no un indice secundario opcional. La diferencia
de tiempo entre las dos condiciones (0.133ms vs
0.063ms) es ruido estadistico, no el efecto del indice,
por lo que P1 no tiene una version genuinamente sin indice: no existe forma de
eliminar el indice del PRIMARY KEY en SQLite sin recrear la tabla completa.

La comparacion que si es reveladora es contra DuckDB:
0.133ms de SQLite frente a
88.807ms de DuckDB, lo que resulta en
**668x mas rapido para SQLite**.
DuckDB tiene un overhead de inicializacion fijo de aproximadamente 88ms para cualquier
query sobre Parquet: abrir el archivo, leer el footer de metadatos y localizar el row
group candidato. Ese costo no escala con el resultado, ocurre igual para 1 fila que
para 10,000, de modo que para un lookup puntual ese overhead nunca se amortiza.

### P2 — Ultimas 20 transacciones de un usuario

```
Con indices:  SEARCH transactions USING INDEX idx_user_timestamp (user_id=?)
Sin indices:  SCAN transactions
USE TEMP B-TREE FOR ORDER BY
```

Con el indice, `SEARCH ... (user_id=?)` lleva a SQLite directamente al sub-arbol
del usuario en el B-Tree. Como ese indice almacena `timestamp DESC`, las primeras
20 entradas del sub-arbol son exactamente las 20 transacciones mas recientes, lo
que significa que no hace falta ningun sort adicional: el orden ya esta incorporado
en la estructura.

Sin el indice el resultado es muy distinto. SQLite tiene que hacer `SCAN transactions`
sobre el millon de filas completo, y como los datos no estan ordenados ademas
construye un `USE TEMP B-TREE FOR ORDER BY` para poder ordenar por timestamp antes
de tomar los 20 primeros. Todo eso lleva a un speedup de
**703x** entre ambas condiciones
(0.154ms con indice vs
108.302ms sin indice).

### P3 — Transacciones de usuario en rango de fechas

```
Con indices:  SEARCH transactions USING INDEX idx_user_timestamp (user_id=? AND timestamp>? AND timestamp<?)
Sin indices:  SCAN transactions
```

El plan muestra `(user_id=? AND timestamp>? AND timestamp<?)`, lo que confirma
que SQLite uso ambas columnas del indice compuesto para el range scan: primero
localiza el sub-arbol del usuario y dentro de el navega directamente al rango de
timestamps. El costo real es O(log n + k) donde k=1, ya que el rango de 2 dias
del usuario de prueba contiene una sola transaccion.

Sin indice el trabajo es completamente distinto: `SCAN transactions` puro, comparar
cada una de las 1,000,000 filas contra los dos limites de fecha. Eso lleva al speedup
mas grande del benchmark, **1355x**
(0.083ms vs 112.477ms),
porque el indice elimina de una vez tanto el full scan como la necesidad de cualquier
ordenamiento posterior.

### P4 — Suma de amount del ultimo mes

```
Con indices:  SEARCH transactions USING INDEX idx_user_timestamp (user_id=? AND timestamp>?)
Sin indices:  SCAN transactions
```

El plan `(user_id=? AND timestamp>?)` es un range scan con una sola cota inferior.
SQLite navega al punto correcto del sub-arbol del usuario y suma `amount` recorriendo
hacia adelante hasta el final, con un speedup de **686x**
frente a la version sin indice.

Lo mas interesante de P4 es lo que pasa con DuckDB. DuckDB termina en
20.110ms, que es mas rapido que SQLite sin indice
(108.429ms), lo cual tiene sentido porque una suma sobre
un rango es exactamente el tipo de operacion donde DuckDB aplica su vectorizacion.
Sin embargo no puede acercarse a SQLite con indice (0.158ms).
La causa es que el Parquet no esta clusterizado por `user_id`, por lo que las
transacciones del usuario estan dispersas en multiples row groups, lo que lleva a que
DuckDB tenga que inspeccionar muchos o todos los bloques del archivo buscando un
usuario que representa el 0.004% del dataset. El B-Tree de SQLite lleva directamente
a esas filas sin tocar el resto.

### P5 — Usuarios de un pais con mas de N transacciones

```
Con indices:  SEARCH transactions USING COVERING INDEX idx_country_user (country_code=?)
USE TEMP B-TREE FOR ORDER BY
Sin indices:  SCAN transactions
USE TEMP B-TREE FOR GROUP BY
USE TEMP B-TREE FOR ORDER BY
```

`USING COVERING INDEX idx_country_user` es el plan mas eficiente posible para P5:
significa que el indice contiene todas las columnas que la query necesita,
`country_code` y `user_id`, por lo que SQLite puede responderla leyendo solo el
indice sin acceder en ningun momento a las paginas de datos de la tabla principal.

El indice `(country_code, user_id)` tiene una entrada por transaccion. Para MX con
distribucion uniforme hay aproximadamente 66,666 entradas. Como ya vienen
ordenadas por `(country_code, user_id)`, el GROUP BY se convierte en un simple scan
secuencial contando cambios de `user_id`, sin necesidad de construir ninguna hash table.
El resultado final son **7,584 filas**, que corresponden a los usuarios unicos
de MX con mas de 2 transacciones en ese pais.

Sin el indice el contraste es dramatico: hay tres operaciones temporales encadenadas,
full scan mas `USE TEMP B-TREE FOR GROUP BY` mas `USE TEMP B-TREE FOR ORDER BY`,
lo que resulta en un speedup de **17x** a favor del indice.

DuckDB termina en 14.630ms, mas lento que SQLite
(8.590ms), aunque P5 es exactamente el tipo de
agregacion donde DuckDB suele brillar. La diferencia es que DuckDB tiene que inspeccionar
muchos o todos los row groups del Parquet porque el archivo no esta ordenado por
`country_code`, mientras que SQLite lee unicamente el segmento del indice correspondiente
a MX, que es una fraccion pequeña del total.

---

## Comparacion SQLite vs DuckDB — patron por patron

SQLite con indices gana en los cinco patrones, aunque las razones son diferentes en
cada caso y vamos a entenderlas caso por caso, porque no todas se explican de
la misma manera.

### P1 — Lookup puntual: SQLite gana 668x

0.133ms de SQLite frente a 88.807ms de DuckDB.
Esta es la diferencia mas extrema del benchmark y tiene una explicacion muy concreta.DuckDB tiene un overhead de inicializacion de aproximadamente 88ms para cualquier
query sobre Parquet, independientemente de lo que retorne: abrir el archivo, leer el
footer de metadatos y localizar el row group candidato. Ese costo es fijo. SQLite en
cambio hace unas 20 comparaciones en el B-Tree y retorna el dato. Para un resultado
de 1 fila ese overhead de DuckDB nunca se amortiza, lo que lleva a la brecha de
668x.

### P2 — Ultimas 20 transacciones: SQLite gana 638x

0.154ms de SQLite frente a 98.187ms de DuckDB.
DuckDB tiene que cargar el Parquet, filtrar las filas del usuario, ordenarlas por
timestamp y tomar 20. SQLite navega al sub-arbol del usuario en el indice y toma las
primeras 20 entradas, que ya vienen en orden DESC por diseno, de modo que no hay
ningun sort. La ventaja aqui es estructural: el indice fue construido exactamente
para ayudar este patron.

### P3 — Rango de fechas: SQLite gana 684x

0.083ms de SQLite frente a 56.781ms de DuckDB.
El rango del parametro de prueba es estrecho, 2 dias con 1 resultado, por lo que SQLite
termina casi de inmediato. DuckDB conserva el overhead de bootstrap de P1 y ademas
tiene que filtrar timestamps sobre el Parquet. Aunque con predicate pushdown puede
descartar algunos row groups usando las estadisticas de min/max, no alcanza a los
microsegundos del B-Tree.

### P4 — Suma del ultimo mes: SQLite gana 127x

0.158ms de SQLite frente a 20.110ms de DuckDB.
Este resultado me llamo la atencion porque una suma sobre un rango de fechas es exactamente
el tipo de operacion donde DuckDB deberia ser competitivo. Y en cierta medida lo es:
DuckDB termina en 20.110ms, claramente mejor que SQLite sin indice
(108.429ms). El problema es que no puede alcanzar a SQLite
con indice. La causa de fondo es que el Parquet no esta clusterizado por `user_id`,
por lo que las transacciones del usuario estan dispersas en multiples row groups y
DuckDB tiene que inspeccionar muchos o todos los bloques del archivo buscando un usuario
que representa el 0.004% del dataset. El B-Tree de SQLite va directamente a esas filas superando su respuesta de DuckDB.

### P5 — Usuarios por pais: SQLite gana 2x

8.590ms de SQLite frente a 14.630ms de DuckDB.
P5 es nuevamente la consulta del benchmark donde el terreno donde DuckDB suele brillar. La diferencia
de 2x es la mas pequena de los
cinco patrones, lo que confirma que aqui DuckDB esta mucho mas cerca de ser competitivo.
SQLite gana gracias al covering index: no necesita tocar la tabla principal, lee solo
el segmento del indice `(country_code, user_id)` correspondiente al pais, donde los
datos ya vienen agrupados implicitamente. Con un Parquet clusterizado por `country_code`,
DuckDB podria invertir ese resultado y superar a SQLite, todo recae en la estructura de los datos.

### Cuando usar cada engine - mi recomendacion

No hay una respuesta unica a esa pregunta, y aunque ne este caso SQLite es el ganador en los 5 patrones, basicamente todo depende del patron de acceso y del SLA que se quiera cumplir, y en este caso de que justamente la estructura de los datos no esta optimizada para DuckDB por lo que no debe ser descartado. En general,
SQLite con indices es la herramienta, que basado en las recomendaciones y demas informacion que investigue, es la mejor para consultas de alta selectividad por
entidad: buscar una transaccion concreta, obtener el historial de un usuario, sumar el
gasto de un cliente en el ultimo mes. Cualquier consulta donde un indice puede reducir
el trabajo a O(log n + k) con k pequeno. DuckDB por su parte es la opcion correcta
para consultas analiticas sobre el dataset completo: calcular el promedio de todas las
transacciones de Mexico, encontrar el top de merchants por volumen, o cualquier query
que necesite procesar decenas de miles de filas de una vez. En el Ejercicio 2 ya vimos y medimos a
DuckDB en ese terreno. En consultas transaccionales de alta selectividad el overhead
de inicializacion de DuckDB no se amortiza y SQLite con un buen schema gana con claridad como acabamos de ver en este caso.
Nuevamente antes de implementar un modelo u otro hay que evaluar que se requiere, creo que es una ley que palica en todos los campos de la tecnologia,
pero especificamente con estos ejercicios aplicado a datos es que puedo comparar y conocer estas herramientas,
me permite tener en cuneta sus fortalezas y debilidades para elegir la mas adecuada segun el caso de uso, y no caer en la trampa de usar una herramienta para todo sin considerar si es la mejor opcion para cada necesidad puntual o si no esta optimizada correctamento como los datos para DuckDB en este caso.

---

## Resumen del diseno de indices

| Indice | Columnas | Patrones | Resultado medido |
|--------|----------|----------|------------------|
| `PRIMARY KEY` | `transaction_id` | P1 | 0.133ms — SLA <10ms OK |
| `idx_user_timestamp` | `(user_id, timestamp DESC)` | P2, P3, P4 | 0.154ms / 0.083ms / 0.158ms — SLA <50ms OK |
| `idx_country_user` | `(country_code, user_id)` | P5 | 8.590ms — SLA <200ms OK |

Los speedups entre con y sin indices — P2=703x, P3=1355x, P4=686x, P5=17x — son la evidencia directa de que cada indice esta haciendo el trabajo para el que fue disenado. La justificacion tecnica completa de cada decision esta en `schema_design.md`.
