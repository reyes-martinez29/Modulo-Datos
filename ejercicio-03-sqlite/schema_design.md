# Schema Design — Ejercicio 3: La Capa Transaccional

Documento para justificar cada decisión de diseño del `schema.sql`. Dentro del mismo
schema hay informacion sobre deciciones tomadas pero aqui no se repite
lo que el schema ya dice, en este documento se explica el razonamiento técnico detrás de cada
elección y las alternativas que se descartaron y por qué.

---

## El problema que resuelve este schema

El Ejercicio 2 usaba DuckDB y polars para consultas analíticas sobre todo el
dataset. Esos engines están diseñados para procesar millones de filas de una
vez — su fortaleza es el throughput en scans completos.

El Ejercicio 3 tiene un problema diferente: el equipo de producto necesita
respuestas en menos de 50ms para consultas sobre un usuario individual. Eso
no es throughput, es latencia. DuckDB puede calcular el promedio de amount de
1M filas en 0.1s, pero para encontrar las últimas 20 transacciones del usuario
42817, al no estar el Parquet ordenado o particionado por `user_id`, DuckDB
probablemente debe inspeccionar muchos o todos los row groups del archivo. No
puede garantizar latencias transaccionales consistentes en <50ms para ese patrón.

SQLite con los índices correctos puede responder esa query en microsegundos
porque el B-Tree lleva directamente al subconjunto de filas del usuario, sin
tocar el resto. Ese es el tradeoff fundamental: SQLite es el engine correcto
para este patrón de acceso, no porque sea "mejor" que DuckDB en general, sino
porque está diseñado para exactamente este tipo de consulta.

---

## Decisiones de tipo de dato

### `transaction_id` como TEXT

Los UUIDs son strings de 36 caracteres (`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`).
SQLite podría guardarlos como BLOB (16 bytes binarios) para ahorrar espacio,
pero eso requeriría conversión en cada INSERT y SELECT, y haría las queries de
desarrollo mucho menos legibles. TEXT es la opción pragmática correcta.

Declararlo como PRIMARY KEY hace que SQLite cree un índice B-Tree único sobre
`transaction_id`. En una tabla con rowid (que es el caso aquí), ese índice
permite localizar el rowid de la fila; la tabla principal sigue almacenándose
por rowid y no queda físicamente ordenada por `transaction_id`. La consecuencia
práctica es que un lookup por `transaction_id` requiere dos pasos internos:
navegar el índice para obtener el rowid, y luego acceder a la fila por rowid.
La alternativa WITHOUT ROWID (donde el PK sí es la clave de clustering) se
analiza más adelante.

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

Con PRIMARY KEY, SQLite crea un índice B-Tree único sobre `transaction_id`.
En una tabla con rowid, un lookup exacto por `transaction_id` requiere navegar
ese índice para obtener el rowid, y luego acceder a la fila por rowid. En la
práctica esos dos pasos son muy rápidos: el índice B-Tree de 1M entradas
requiere aproximadamente log₂(1,000,000) ≈ 20 comparaciones para llegar a la
hoja con el rowid, y el acceso por rowid es directo. A la velocidad de acceso
a SSD eso cabe cómodamente dentro del SLA de <10ms.

### Por qué `(user_id, timestamp DESC)` para P2, P3 y P4

**El principio del prefijo de índice:**
En un índice compuesto (A, B), SQLite puede usarlo para:
- Queries que filtran solo por A: usa el prefijo
- Queries que filtran por A y B: usa el índice completo
- Queries que filtran solo por B: NO puede usar el índice

Los tres patrones tienen `user_id` como primer predicado de filtro, así que
todos pueden usar el mismo índice.

**Por qué DESC en timestamp:**
P2 necesita `ORDER BY timestamp DESC LIMIT 20`. SQLite puede recorrer índices
en ambas direcciones (forward y reverse scan), por lo que un índice
`(user_id, timestamp ASC)` también podría servir este patrón mediante un
reverse scan del rango del usuario. Sin embargo, declarar DESC alinea
físicamente el orden del índice con el patrón más frecuente de la query:
las primeras entradas del sub-árbol de un usuario son ya sus transacciones
más recientes, sin ambigüedad para el optimizador. En la práctica hace
explícito el diseño intencional y evita depender del comportamiento del
planificador en diferentes versiones de SQLite.

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

P5 hace: `WHERE country_code = ? GROUP BY user_id HAVING COUNT(*) > N ORDER BY COUNT(*) DESC`

Sin índice: SQLite hace full scan de 1M filas, filtra en memoria las de ese
país (~67k filas con distribución uniforme entre 15 países), y luego hace
GROUP BY con una hash table y un sort para el ORDER BY.

Con `(country_code)` solo: SQLite encuentra el rango del país en el índice
(mejor que el full scan), pero aún necesita un hash/sort para el GROUP BY por
`user_id` sobre ~67k filas.

Con `(country_code, user_id)`: las filas del índice ya están agrupadas por
usuario dentro de cada país, por lo que SQLite puede calcular el COUNT con un
scan secuencial sobre las entradas del país, contando cambios de `user_id`,
sin necesidad de hash table para el GROUP BY. Sin embargo, si la query ordena
por una expresión agregada como `COUNT(*) DESC`, SQLite todavía puede necesitar
una estructura temporal para el ORDER BY — y así lo confirma el EXPLAIN QUERY
PLAN real, que muestra `USE TEMP B-TREE FOR ORDER BY` incluso con el índice.
El índice elimina el costo del agrupamiento pero no el del ordenamiento
posterior.

---

## Decisión: WITH ROWID vs WITHOUT ROWID

SQLite soporta tablas `WITHOUT ROWID` donde el PRIMARY KEY es la clave de
clustering — los datos se almacenan directamente ordenados por PK, sin la
indirección PK→rowid de las tablas normales.

**Ventaja de WITHOUT ROWID para esta tabla:**
- Elimina la indirección PK→rowid en lookups por `transaction_id` (P1 sería
  marginalmente más rápido porque el dato está en la hoja del B-Tree del PK,
  no en una página aparte de la tabla)
- Ahorra ~8 bytes por fila (el rowid interno)

**Por qué se descartó:**
- Las transacciones del dataset se generan con UUIDs aleatorios como
  `transaction_id`. Con WITHOUT ROWID el PK define la estructura principal de
  almacenamiento, por lo que SQLite necesitaría insertar en posiciones
  arbitrarias del B-Tree durante la ingesta, generando fragmentación de páginas
  y ralentizando significativamente las escrituras.
- Con una tabla rowid normal, las inserciones secuenciales son más eficientes
  porque el rowid crece de forma ordenada y la tabla principal no se reordena
  por UUID aleatorio. SQLite gestiona páginas libres internamente, pero el
  patrón de inserción secuencial por rowid tiende a generar menos splits de
  páginas que la inserción aleatoria por UUID.
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

Para ingesta masiva de 1M filas en chunks de 20,000 filas (50 commits):
- Sin WAL: cada commit hace fsync y actualiza el archivo principal — 50 fsyncs
  al archivo principal en esta corrida concreta.
- Con WAL: las escrituras van al archivo WAL de forma append-only, mucho más
  rápido. SQLite hace checkpoints (flush WAL → archivo principal)
  periódicamente o al cerrar la conexión.

El benchmark mide ambos modos con el mismo `--chunk-size` para que la
comparación refleje únicamente el impacto del WAL, no diferencias de volumen
de datos por commit.

---

## Resumen de índices y patrones cubiertos

| Índice | Columnas | Patrones cubiertos | Justificación |
|--------|----------|-------------------|---------------|
| PRIMARY KEY | `transaction_id` | P1 (<10ms) | Índice B-Tree único; lookup por rowid en tabla normal |
| `idx_user_timestamp` | `(user_id, timestamp DESC)` | P2, P3, P4 (<50ms) | Prefijo de índice para filtro por usuario; DESC alinea con ORDER BY más frecuente |
| `idx_country_user` | `(country_code, user_id)` | P5 (<200ms) | Covering index; GROUP BY implícito por orden del índice; ORDER BY sigue usando estructura temporal |