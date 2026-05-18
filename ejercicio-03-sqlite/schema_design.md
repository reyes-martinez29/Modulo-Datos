# Schema Design — Ejercicio 3: La Capa Transaccional

Este documento justifica cada decisión de diseño del `schema.sql`. No repite
lo que el schema ya dice — explica el razonamiento técnico detrás de cada
elección y las alternativas que se descartaron y por qué.

---

## El problema que resuelve este schema

El Ejercicio 2 usaba DuckDB y polars para consultas analíticas sobre todo el
dataset. Esos engines están diseñados para procesar millones de filas de una
vez — su fortaleza es el throughput en scans completos.

El Ejercicio 3 tiene un problema diferente: el equipo de producto necesita
respuestas en menos de 50ms para consultas sobre un usuario individual. Eso
no es throughput — es latencia. DuckDB puede calcular el promedio de amount de
1M filas en 0.1s, pero para encontrar las últimas 20 transacciones del usuario
42817 tiene que escanear el archivo Parquet completo. No puede servir ese
patrón en 50ms de forma consistente.

SQLite con los índices correctos puede responder esa query en 2-5ms porque el
B-Tree la lleva directamente al subconjunto de filas que necesita, sin tocar
el resto. Ese es el tradeoff fundamental: SQLite es el engine correcto para
este patrón de acceso, no porque sea "mejor" que DuckDB en general, sino
porque está diseñado para exactamente este tipo de consulta.

---

## Decisiones de tipo de dato

### `transaction_id` como TEXT

Los UUIDs son strings de 36 caracteres (`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`).
SQLite podría guardarlos como BLOB (16 bytes binarios) para ahorrar espacio,
pero eso requeriría conversión en cada INSERT y SELECT, y haría las queries de
desarrollo mucho menos legibles. TEXT es la opción pragmática correcta.

Declararlo como PRIMARY KEY crea automáticamente el índice B-Tree único que
necesita P1. No hay ningún costo adicional — el índice es la consecuencia
natural de la restricción de unicidad.

### `timestamp` como TEXT en formato ISO8601

Esta es la decisión más importante del schema y merece explicación detallada.

SQLite tiene cuatro tipos de almacenamiento nativos: NULL, INTEGER, REAL y
TEXT. No existe DATETIME. Cuando los desarrolladores necesitan almacenar fechas
tienen tres opciones:

**Opción A: TEXT ISO8601** (`'2024-03-15 14:30:00'`)
- Las comparaciones de rango funcionan directamente: `WHERE timestamp > '2024-01-01'`
- El orden lexicográfico coincide con el orden cronológico siempre que el
  formato sea consistente (YYYY-MM-DD HH:MM:SS, con ceros a la izquierda)
- Los índices funcionan igual que con cualquier otro TEXT
- Legible en las queries y en herramientas de inspección como DB Browser

**Opción B: INTEGER epoch Unix** (`1710509400`)
- Mínimo espacio (8 bytes vs ~20 bytes para ISO8601)
- Comparaciones de rango igual de rápidas
- Requiere conversión con `datetime(timestamp, 'unixepoch')` en cada query
  que necesite mostrar la fecha en formato legible
- Problemas de zona horaria si el timestamp no es UTC

**Opción C: REAL (Julian Day Number)**
- Formato nativo de las funciones de fecha de SQLite
- Raramente usado, poca legibilidad

Se eligió **TEXT ISO8601** porque las queries del benchmark necesitan
comparar timestamps con strings calculados en Python
(`datetime.now() - timedelta(days=30)`), y la conversión entre Python datetime
y string ISO8601 es directa. Con INTEGER epoch habría un paso adicional de
conversión que aumenta la posibilidad de bugs de zona horaria.

El costo de espacio es marginal: ~20 bytes por fila × 1M filas = ~20MB extra
comparado con INTEGER. Aceptable.

### `amount` como REAL

Para un sistema financiero de producción real, `amount` debería ser INTEGER
(centavos) para evitar errores de punto flotante en acumulaciones. Pero el
enunciado define `amount` como float entre 0.01 y 5000.00, y el dataset del
Ejercicio 1 lo genera como float. Almacenar como REAL mantiene fidelidad con
el schema del módulo y evita conversiones.

---

## Decisiones de índices

### Por qué PRIMARY KEY sobre `transaction_id` (P1)

Sin PRIMARY KEY explícito, SQLite usa el `rowid` interno como identificador
de fila. Buscar por `transaction_id` requeriría un full scan de 1M filas
(`SCAN transactions` en el EXPLAIN QUERY PLAN) — imposible en <10ms.

Con PRIMARY KEY, SQLite crea un B-Tree donde las hojas son los datos de cada
fila ordenados por `transaction_id`. Un lookup exacto en un B-Tree de 1M
entradas requiere aproximadamente log₂(1,000,000) ≈ 20 comparaciones. A la
velocidad de acceso a SSD (~0.1ms por operación aleatoria), eso es ~2ms en
el peor caso. Dentro del SLA de <10ms con margen amplio.

### Por qué `(user_id, timestamp DESC)` para P2, P3 y P4

**El principio del prefijo de índice:**
En un índice compuesto (A, B), SQLite puede usarlo para:
- Queries que filtran solo por A: usa el prefijo
- Queries que filtran por A y B: usa el índice completo
- Queries que filtran solo por B: NO puede usar el índice

Los tres patrones tienen `user_id` como primer predicado de filtro, así que
todos pueden usar el mismo índice.

**Por qué DESC en timestamp:**
P2 necesita `ORDER BY timestamp DESC LIMIT 20`. Sin DESC en el índice, SQLite
tendría que:
1. Encontrar todas las filas del usuario en el índice (forward scan)
2. Cargarlas en memoria
3. Ordenarlas inversamente
4. Tomar las 20 primeras

Con DESC en el índice, las filas del usuario ya están almacenadas de más
reciente a más antigua. SQLite puede tomar las primeras 20 entradas del
sub-árbol del usuario sin ningún sort. Esto es crucial para cumplir el SLA
de <50ms, especialmente para usuarios con muchas transacciones.

**¿Afecta DESC a P3 y P4?**
No. Para range scans (`BETWEEN`, `>=`), SQLite puede recorrer el índice en
cualquier dirección. El optimizador elige automáticamente la dirección más
eficiente según los predicados de la query.

**¿Por qué no tres índices separados?**
Cada índice adicional tiene un costo en escritura: cada INSERT actualiza todos
los índices de la tabla. Con 1M filas en la ingesta, tres índices en lugar de
uno aumentarían el tiempo de ingesta sin ningún beneficio adicional. Un índice
compuesto bien diseñado sirve a múltiples patrones.

### Por qué `(country_code, user_id)` para P5

P5 hace: `WHERE country_code = ? GROUP BY user_id HAVING COUNT(*) > N`

Sin índice: SQLite hace full scan de 1M filas, filtra en memoria las de ese
país (~67k filas con distribución uniforme entre 15 países), y luego hace
GROUP BY con una hash table.

Con `(country_code)` solo: SQLite encuentra el rango del país en el índice
(mejor que el full scan), pero aún necesita un hash/sort para el GROUP BY por
user_id sobre ~67k filas.

Con `(country_code, user_id)`: dentro del índice, las filas de cada país
ya están agrupadas por usuario. SQLite puede calcular el COUNT con un simple
scan secuencial sobre las entradas del país, contando cambios de `user_id`.
No necesita hash table ni sort adicional — el índice hace el trabajo de
agrupación implícitamente. Esto es lo que permite cumplir el SLA de <200ms.

---

## Decisión: WITH ROWID vs WITHOUT ROWID

SQLite soporta tablas `WITHOUT ROWID` donde el PRIMARY KEY es la clave de
clustering — los datos se almacenan directamente ordenados por PK, sin
indirección PK→rowid.

**Ventaja de WITHOUT ROWID para esta tabla:**
- Elimina la indirección en lookups por `transaction_id` (P1 sería
  marginalmente más rápido)
- Ahorra ~8 bytes por fila (el rowid interno)

**Por qué se descartó:**
- Las transacciones del dataset están ordenadas aproximadamente por timestamp
  (se generaron en orden temporal). Con WITHOUT ROWID y PK en `transaction_id`
  (UUID aleatorio), SQLite necesitaría insertar en posiciones arbitrarias del
  B-Tree durante la ingesta — generando mucha fragmentación de páginas y
  ralentizando significativamente la ingesta.
- Con rowid normal, SQLite siempre inserta al final del heap (append-only),
  que es la estrategia más eficiente para ingesta masiva. El orden del B-Tree
  del PK se mantiene por separado sin afectar la velocidad de escritura.
- La diferencia de rendimiento en P1 es de microsegundos — irrelevante frente
  a los millisegundos del SLA.

---

## Decisión: WAL mode

SQLite soporta dos modos de journaling para garantizar la atomicidad de las
transacciones:

**Delete journal (modo por defecto):**
Antes de modificar una página, SQLite copia la versión original al archivo
`.db-journal`. Si el proceso falla, SQLite usa el journal para revertir.
Al hacer commit, borra el journal (de ahí el nombre "delete"). Cada commit
implica múltiples operaciones de I/O y un fsync.

**WAL (Write-Ahead Log):**
En lugar de modificar el archivo `.db` directamente, SQLite escribe los cambios
en un archivo `.db-wal` separado. El archivo original no se toca hasta el
checkpoint. Los readers pueden leer del archivo original mientras los writers
escriben al WAL — eliminando el bloqueo lector/escritor.

Para ingesta masiva de 1M filas en chunks:
- Sin WAL: cada commit hace fsync y actualiza el archivo principal. Con chunks
  de 10k filas y 100 commits, eso son 100 fsyncs al archivo principal.
- Con WAL: las escrituras van al archivo WAL de forma append-only, mucho más
  rápido. SQLite hace checkpoints (flush WAL → archivo principal)
  periódicamente o al cerrar la conexión.

El benchmark debe medir ambos modos con el mismo `--chunk-size` para que la
comparación sea justa y los resultados reflejen únicamente el impacto del WAL.

---

## Resumen de índices y patrones cubiertos

| Índice | Columnas | Patrones cubiertos | Justificación |
|--------|----------|-------------------|---------------|
| PRIMARY KEY | `transaction_id` | P1 (<10ms) | Lookup exacto en B-Tree |
| `idx_user_timestamp` | `(user_id, timestamp DESC)` | P2, P3, P4 (<50ms) | Range scan por usuario + tiempo, ORDER BY sin sort |
| `idx_country_user` | `(country_code, user_id)` | P5 (<200ms) | Agrupación implícita por país+usuario |