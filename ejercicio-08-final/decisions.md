# Decisiones de arquitectura — Ejercicio 8: El sistema completo

## El problema y cómo lo encaré

El E8 pide integrar lo construido en los ejercicios anteriores en un sistema de monitoreo de transacciones para una fintech de LATAM. El enunciado no da pasos predefinidos: define cinco capacidades de negocio y cinco componentes obligatorios, y deja que uno decida la arquitectura. Por eso la mayor parte del valor de este ejercicio está en las decisiones, no en el código en sí.

Decidí construir sobre FastAPI (el E4) en lugar de Django (el E5). La razón no es que fuera más fácil, sino que el problema lo pedía: la ingesta de un CSV es una operación larga que no debe bloquear los endpoints de monitoreo que el equipo de producto está observando, y el modelo async de FastAPI junto con el `asyncio.Lock` que ya venía del E4 modela esa coordinación de forma limpia. Django síncrono lo habría hecho más torpe. Además, el sistema dual DuckDB para analytics y SQLite para lo transaccional ya estaba probado y medido en el E4, así que partir de ahí me dejó concentrarme en lo nuevo del E8.

## La decisión central: quién es dueño del histórico

La decisión más importante del ejercicio apareció a mitad del desarrollo, cuando un test falló y me obligó a pensar con cuidado dónde vive el histórico de transacciones. El sistema tiene dos fuentes: el Parquet del E1, que es el histórico de 1M transacciones, y la base SQLite, que recibe las transacciones nuevas. Y tiene tres tipos de consulta: analytics globales, lookups por usuario y detección de anomalías. La pregunta es cuál de las dos fuentes contiene qué.

Al principio diseñé un "analytics unificado" en el que el Parquet guardaba el histórico, SQLite guardaba solo lo nuevo, y las consultas de analytics hacían un `UNION ALL` entre ambos. Sonaba elegante: cada motor en lo suyo, DuckDB columnar sobre el Parquet, SQLite para la escritura. Pero al escribir el test del summary noté que el conteo salía mal, y ahí entendí el problema real: si los lookups por usuario y las anomalías necesitan el histórico del usuario, y ese histórico solo está en el Parquet, entonces esas consultas tendrían que escanear el Parquet de 1M filas para encontrar las transacciones de un solo usuario. Eso destruye el índice `idx_user_timestamp` que el E3 diseñó y que resuelve esos lookups en 0.154 ms. Un modelo que inutiliza el índice más valioso del sistema para ganar en analytics es un mal intercambio, sobre todo en un sistema de monitoreo donde los lookups por usuario son frecuentes.

Por eso elegí el modelo opuesto, al que llamo "SQLite es la fuente de verdad viva". El servicio `setup` copia el histórico del Parquet a SQLite una sola vez al levantar el sistema. A partir de ahí, SQLite contiene el histórico completo más todo lo que llega por el pipeline y el batch. El Parquet queda como el snapshot histórico inmutable cuyo rol es alimentar ese setup inicial; en runtime no se consulta. Como SQLite ya tiene todo, analytics no necesita unir nada: DuckDB consulta la tabla SQLite directamente mediante la extensión `sqlite_scanner`, aprovechando su motor columnar y vectorizado sobre esa tabla, mientras que los lookups por usuario usan SQLite directo con su índice. Una sola fuente en runtime, consultada con la herramienta adecuada según el caso, sin doble conteo y sin escaneos de Parquet en el camino de un request. Que el test me forzara a tomar esta decisión es justamente lo que la hace sólida: no es una preferencia, es la consecuencia de un requisito que no se podía ignorar.

## Reutilizar el pipeline del E6 sin reescribirlo

El pipeline de ingesta del E8 reutiliza las capas `extract`, `transform` y `load` del E6 sin tocarlas. Lo único nuevo es `csv_source.py`, que reemplaza al generador sintético del E6 por un lector de CSV externo real. Que este reemplazo fuera de una sola capa es consecuencia directa de una buena decisión del E6: el flujo entre capas son listas de diccionarios, no DataFrames ni archivos intermedios, así que cambiar la fuente solo requiere producir el mismo formato.

El CSV introdujo un tercer nivel de error que el E6 no tenía. El E6 distinguía errores de formato (que maneja extract) de errores de negocio (que maneja transform). El CSV agrega errores de estructura del archivo: que falte una columna entera, que el archivo esté vacío o que no sea parseable. Eso no es un problema de una fila, es que el archivo no tiene la forma esperada, así que `csv_source.py` valida la estructura y falla limpio antes de pasar una sola fila a extract. También impuse un límite de filas, porque el endpoint de ingesta es público y un CSV de varios gigabytes podría agotar la memoria del contenedor.

Mantuve la validación de `transaction_id` como UUID4 estricto que ya tenía el transform del E6, porque coincide con lo que el batch del E4 ya esperaba. Una sola definición de qué es un identificador válido en todo el sistema.

## La detección de anomalías como módulo, no como query

El enunciado pide detectar usuarios con más de N transacciones fallidas en los últimos 30 días. Es una consulta simple, pero en lugar de incrustarla en el endpoint la puse en un módulo aparte. La razón es de dominio: en una fintech, un usuario con muchas fallidas es una señal de fraude, de una tarjeta comprometida o de un problema con un merchant. Hoy el negocio quiere el conteo absoluto, pero mañana querrá señales más finas, como la tasa de fallo respecto al comportamiento normal del usuario. Tener la detección en un módulo separado permite agregar esos detectores sin tocar el endpoint. La consulta usa SQLite y no la vista de analytics porque mira solo los últimos 30 días, y el histórico del Parquet queda fuera de esa ventana; además el índice `idx_user_timestamp` cubre exactamente este patrón.

## La tensión entre "tiempo real" y el cache

El enunciado pide datos en tiempo real, pero el cache del E4 con TTL de 300 segundos significa "fresco cada cinco minutos", que no es lo mismo. Resolví esta tensión con invalidación dirigida: tanto el batch como la ingesta de CSV invalidan el prefijo `analytics:` del cache después de escribir. Así los datos recién ingeridos se reflejan de inmediato en analytics, y el TTL queda solo como red de seguridad. El cache es lo que hace que analytics cumpla su SLA; la invalidación es lo que evita que mienta sobre datos frescos.

## Qué cambiaría con 100 millones de filas

A esa escala, SQLite deja de ser la herramienta adecuada para el lado transaccional. SQLite funciona muy bien hasta millones de filas, pero a 100 millones, con escritura concurrente del pipeline y del batch, el modelo de "una sola base con un lock de escritura" se vuelve un cuello de botella. Migraría el lado transaccional a PostgreSQL, que maneja concurrencia de escritura real con MVCC. El analytics seguiría con DuckDB, que escala bien a esa escala por su naturaleza columnar, pero probablemente leyendo directamente del Parquet particionado por fecha en lugar de copiarlo a la base transaccional. El `setup` de copiar el histórico completo, que con 1M toma segundos, con 100M tomaría demasiado y habría que repensarlo como un proceso de carga incremental. El límite de filas del endpoint de ingesta también tendría que cambiar a un modelo de cola de trabajos, donde el CSV se sube, se encola y se procesa en segundo plano, en lugar de procesarse dentro del request.

## Qué monitorearía en producción

El endpoint `/health` ya reporta uptime, hit rate del cache, estado de las conexiones y el número de transacciones en la base. Esa última métrica es la más útil para el monitoreo real: si el conteo de transacciones deja de crecer al ritmo esperado, el pipeline de ingesta probablemente está fallando aguas arriba. Más allá de eso, monitorearía la latencia p99 de cada endpoint para detectar degradación antes de que viole los SLA, y la tasa de rechazo del pipeline: si de repente sube el porcentaje de filas que van a cuarentena, hay un problema de calidad en los datos de la fuente que conviene atender antes de que afecte las decisiones de negocio. El hit rate del cache también es una señal: si cae mucho, significa que las escrituras están invalidando el cache con demasiada frecuencia y quizá convenga revisar el TTL o la estrategia de invalidación.

## Validación con Docker  — resultados medidos

Corrí `docker compose up` contra el Parquet real de 1M transacciones del módulo.

`setup` copió las 1M filas del Parquet a SQLite en chunks de 20,000 (50 iteraciones visibles en los logs) y creó los dos índices del E3. `api` arrancó con logs JSON y el HEALTHCHECK pasando con 200.

Los resultados de cada endpoint con datos reales:

| Endpoint | Resultado |
|---|---|
| `/health` | `transactions_in_db: 1000000`, ambas conexiones activas |
| `/analytics/summary` | `total_transactions: 1000000`, `total_amount: 2,500,147,886.54` — el mismo número que el E4 y el E5 con el mismo Parquet, confirmando que DuckDB sobre SQLite calcula la misma agregación |
| `/analytics/top-merchants?limit=5&country=MX` | 5 merchants ordenados por monto descendente, idénticos al E4 |
| `/analytics/anomalies?threshold=5` | 0 flagged — correcto para el dataset histórico, cuyas transacciones son de 2024-2025 y no tienen usuarios con más de 5 fallidas en los últimos 30 días |
| `/users/2076/stats` | `transaction_count: 43, top_category: Education` — el mismo usuario verificado en el E3 y referenciado en la retroalimentación del E4 |
| `POST /pipeline/ingest` | CSV de 3 filas → 2 válidas + 1 rechazada (`amount_out_of_range`), 2 insertadas, 3 invariantes en `true` |

Después de la ingesta CSV, `/analytics/summary` devolvió `total_transactions: 1,000,002` — las 1M del histórico más las 2 filas válidas del CSV. Esto confirma que el sistema integra correctamente el histórico con lo recién ingerido y que la invalidación del cache tras la ingesta funciona.

**Tamaño de imagen medido: 335MB.** El desglose con `docker history`:

| Componente | Tamaño |
|---|---|
| Base Debian + Python 3.11 (en `python:3.11-slim`) | ~155MB |
| venv — duckdb (58MB binario nativo) + resto de dependencias | 75.3MB |
| Extensión sqlite_scanner preinstalada | 34.7MB |
| Código app/ + pipeline/ + scripts | ~185KB |

335MB supera el límite de 300MB del enunciado. El componente que impide llegar a 300MB es la extensión sqlite_scanner (34.7MB), que preinstalé durante el build para que el `ATTACH` de analytics funcione sin necesitar salida a internet en runtime. La alternativa sería no preinstalarla y dejar que DuckDB la descargue en el primer request — eso llevaría la imagen a ~300MB pero requeriría acceso a `extensions.duckdb.org` en cada arranque desde cero. Decidí preinstalarla porque prefiero una imagen que funcione offline a cambio de 35MB extra, y lo documento con el número real en lugar de afirmar "<300MB" sin matices.

Durante el build noté y corregí un problema de capa duplicada: la primera versión copiaba `/root/.duckdb` y luego hacía `chown` en un `RUN` separado, lo que generaba dos capas de 34.7MB cada una (total 69.4MB extra). Lo corregí usando `COPY --from=build --chown=appuser:appuser` en una sola instrucción, eliminando la capa redundante y bajando de 383MB a 335MB.

Cada pieza se validó con datos reales conforme la construí, no al final. El analytics, las anomalías, el filtro de fecha y el pipeline CSV los probé con un dataset que incluye anomalías conocidas (un usuario con 8 fallidas recientes, otro con 6, un tercero con 2 que no debe ser marcado con umbral 5). La suite tiene 26 tests que cubren los nueve endpoints, la detección de anomalías con tres umbrales distintos, el filtro de fecha, el pipeline CSV con sus invariantes matemáticas y su idempotencia, la validación de estructura del CSV y los códigos de error. Los 26 pasan en 0.41s.


## Tiempo de desarrollo

| Actividad | Tiempo aproximado |
|---|---|
| Lectura del enunciado, análisis de los ejercicios anteriores y diseño de la arquitectura | 1h 30min |
| db.py — analytics, usuarios, filtro de fecha, primera versión con UNION, descubrimiento del doble conteo y decisión del Modelo A | 2h |
| anomaly.py — detector como módulo extensible, pruebas con tres umbrales | 45 min |
| pipeline: csv_source.py nuevo + reutilización de extract/transform/load del E6, pruebas de estructura del CSV | 1h 20min |
| models.py y config.py | 45 min |
| main.py — los 9 endpoints, lifespan, invalidación de cache, coordinación del lock de escritura | 1h 30min |
| tests — 26 casos, dataset determinista con anomalías conocidas, depuración del fallo que reveló el doble conteo | 1h 40min |
| capa Docker — Dockerfile, compose, setup.py, entrypoint, preinstalación de la extensión sqlite_scanner, corrección de la capa duplicada de .duckdb | 2h |
| Corrida con Docker, verificación de los 9 endpoints con 1M filas, ajustes de configuración | 1h 30min |
| README y decisions.md | 1h |
| **Total** | **~15h30min** |