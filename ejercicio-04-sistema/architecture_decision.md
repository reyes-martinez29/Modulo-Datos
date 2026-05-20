# Architecture Decision — Ejercicio 4: El Sistema Completo

Este documento justifica qué backend usa cada endpoint y por qué. La decisión
no es arbitraria — cada elección está respaldada por los números medidos en
los ejercicios anteriores.

---

## Contexto: dos backends con propósitos distintos

El sistema tiene acceso a dos fuentes de datos:

- **Parquet de E1** — 1M transacciones en formato columnar, optimizado para
  agregaciones sobre todo el dataset. Lo consulta DuckDB.
- **SQLite de E3** — las mismas 1M transacciones en una base relacional con
  índices B-Tree diseñados para lookups por usuario. Lo consulta SQLite.

La elección de backend por endpoint no es una preferencia de estilo — es una
consecuencia directa de lo que cada engine demostró en los ejercicios anteriores.

---

## Decisiones por endpoint

### `GET /analytics/summary` → DuckDB

Este endpoint necesita agregar sobre todo el dataset: conteo total, monto total,
promedio global, y breakdown por 15 países y 10 categorías. Son tres GROUP BY
sobre 1M filas.

En E2 este tipo de query tardó entre 13ms y 130ms en DuckDB dependiendo de la
complejidad. DuckDB con Parquet aplica column pruning — para calcular el breakdown
por país solo lee las columnas `country_code` y `amount` del archivo, no las 8
columnas completas. SQLite podría hacer lo mismo pero requeriría un full scan de
la tabla de 1M filas sin ninguna ventaja columnar.

El resultado se cachea con TTL=300s porque los datos solo cambian cuando llega
un batch de escritura. Cold: <500ms. Warm: <20ms.

### `GET /analytics/top-merchants` → DuckDB

Top N merchants por volumen total con filtro opcional por país. Otro GROUP BY
analítico sobre hasta 10,000 merchants. Mismo argumento que `summary` — DuckDB
con Parquet es el engine correcto para esta operación.

La cache key incluye `limit` y `country` para evitar que una query filtrada por
MX devuelva el resultado cacheado de la query sin filtro. Cold: <500ms. Warm: <20ms.

### `GET /users/{user_id}/transactions` → SQLite

Las últimas transacciones de un usuario individual con paginación. En E3 este
patrón (P2) tardó **0.154ms** con el índice `idx_user_timestamp (user_id, timestamp DESC)`.
DuckDB tardó **98ms** por el overhead de inicialización sobre Parquet.

El SLA es <80ms. Con DuckDB ese SLA se cumple apenas en condiciones ideales.
Con SQLite hay un margen de 500x. No hay discusión posible sobre qué backend
usar aquí — los números del E3 lo determinan.

No se cachea porque cada usuario es diferente y los datos pueden cambiar
con cada batch de escritura.

### `GET /users/{user_id}/stats` → SQLite

Monto total, conteo, categoría más frecuente y país más frecuente del usuario.
Todas las queries están filtradas por `user_id`, por lo que el índice
`idx_user_timestamp` cubre el filtrado y reduce el trabajo a las filas del
usuario únicamente.

En E3 el patrón equivalente (P4: suma de amount por usuario) tardó **0.158ms**.
Mismo argumento que `/transactions`: SQLite con índice vs DuckDB sin clustering
por `user_id` no tiene competencia a esta escala. SLA: <80ms.

### `POST /transactions/batch` → SQLite

Escritura de hasta 500 transacciones nuevas. SQLite es la base transaccional
de escritura del sistema. DuckDB sobre Parquet es de solo lectura en esta
arquitectura — no existe un mecanismo simple para insertar filas en un Parquet
y que esas filas sean visibles en consultas posteriores sin reescribir el archivo.

SQLite con WAL mode y `executemany` en una transacción explícita puede insertar
500 filas en bien por debajo del SLA de 2s. La deduplicación por `transaction_id`
se hace antes de insertar para no depender de excepciones de constraint.

Después de un insert exitoso se invalida el cache analítico para que los próximos
requests a `/analytics/*` reflejen los datos actualizados.

### `GET /health` → ningún backend

Estado del sistema en memoria: uptime, hit rate del cache, estado de conexiones.
Este endpoint nunca consulta ninguna base de datos. Si lo hiciera, podría tardar
más de 50ms bajo carga, violando su SLA. El uptime se calcula con `time.monotonic()`
y el hit rate del cache se lee directamente del objeto `TTLCache`. SLA: <50ms siempre.

---

## La regla que determina el rendimiento del sistema

Las conexiones a DuckDB y SQLite se inicializan **una sola vez** en el lifespan
de FastAPI, no dentro de los endpoints.

Abrir una conexión a DuckDB sobre Parquet cuesta ~88ms (medido en E3). Si cada
request abriera su propia conexión, el endpoint `/analytics/summary` nunca podría
cumplir su SLA cold de 500ms porque solo la apertura ya consume el 18% del presupuesto
de tiempo. Con conexión global ese costo se paga una vez al arrancar el servidor.

El benchmark de latencia (`benchmarks/latency_benchmark.py`) detecta esta violación
directamente: si los tiempos cold de `/analytics/*` son consistentemente >500ms,
hay una conexión abriéndose en el path de request.

---

## Resumen

| Endpoint | Backend | Justificación |
|----------|---------|---------------|
| `GET /analytics/summary` | DuckDB | GROUP BY sobre 1M filas — column pruning + vectorización |
| `GET /analytics/top-merchants` | DuckDB | Aggregación analítica con filtro opcional de país |
| `GET /users/{id}/transactions` | SQLite | idx_user_timestamp: 0.154ms medido en E3 (P2) |
| `GET /users/{id}/stats` | SQLite | Mismo índice, filtro por user_id: 0.158ms medido en E3 (P4) |
| `POST /transactions/batch` | SQLite | Base transaccional de escritura del sistema |
| `GET /health` | ninguno | Solo estado en memoria — nunca toca la DB |

---

## Validación de las decisiones con evidencia medida

Las decisiones de arquitectura no fueron solo teóricas — cada una está respaldada
por mediciones reales de los ejercicios anteriores y confirmada por el benchmark
de latencia de este ejercicio.

### DuckDB para analytics — confirmado

La justificación inicial decía que DuckDB tarda entre 13ms y 130ms en queries
analíticas sobre 1M filas (medido en E2). El benchmark de latencia del E4 midió
`/analytics/summary` cold en **~40ms p50** y `/analytics/top-merchants` cold en
**~17ms p50** — dentro del rango esperado y muy por debajo del SLA de 500ms.

Si se hubiera usado SQLite para estos endpoints, el full scan de 1M filas sin
column pruning habría tardado ~2s (medido en E3 sin índices), violando el SLA.

### SQLite para usuarios — confirmado

La justificación inicial decía que SQLite con `idx_user_timestamp` resuelve
lookups por usuario en <1ms (medido en E3, P2=0.154ms, P4=0.158ms). En la API
esto se traduce en que `/users/{id}/transactions` y `/users/{id}/stats` tienen
un margen de ~400x respecto al SLA de 80ms — el índice absorbe completamente
el costo de las queries transaccionales.

Si se hubiera usado DuckDB para estos endpoints, cada request habría pagado
~88ms de overhead de apertura del Parquet (medido en E3, P1 vs DuckDB),
haciendo el SLA de 80ms imposible de cumplir de forma consistente.

### Cache con TTL — confirmado

El benchmark de latencia midió un speedup de **~60x** entre cold (~40ms) y
warm (~0.7ms) en `/analytics/summary` en 3 corridas consecutivas, con p99
warm consistentemente por debajo de 2ms. Eso confirma que el path caliente
no tiene ninguna operación de I/O y que el TTL de 300s es adecuado para este
patrón de acceso.

### Conexiones en el lifespan — confirmado por ausencia de outliers

El benchmark midió 100 requests cold a cada endpoint analítico. Si las
conexiones se abrieran dentro de los endpoints, cada request pagaría ~88ms
de overhead adicional y el p50 cold sería ~128ms en lugar de ~40ms. El hecho
de que el p50 cold sea consistente en ~17-40ms confirma que las conexiones
están correctamente inicializadas en el lifespan y no en cada request.