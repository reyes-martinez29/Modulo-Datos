# Decisiones técnicas — Ejercicio 5: El Backend con Estructura

El objetivo de este documento extra es ir explicando las decisiones 
que se tomaron durante la implementacion y desarrollo del ejercicio
se que un documento asi se pide hasta el ejercicio 8 pero creo que es importante 
tener estas referencias desde que se hace cada ejercicio, mas que nada para
entender mejor el por que de cada cosa y personalmente me ayuda a no perderme tanto 
con los temas vistos, aclara y justifica lo usado, visto e implementado.

---

## El problema que resuelve este ejercicio

Los ejercicios E1 a E4 construyeron un sistema funcional: un dataset de 1M
transacciones en Parquet, tres engines de consulta comparados, una capa
transaccional en SQLite y una API en FastAPI. Este E5 reimplementa la misma
API usando Django y Django REST Framework.

Al analizar el ejercicio entendi que la pregunta no es "¿cómo hago lo mismo con Django?"
sino "¿qué cambia cuando el framework impone más estructura, y qué decisiones
hay que tomar explícitamente que FastAPI dejaba implícitas?"

---

## Decisión 1 — Base de datos propia de Django, no la del E3

**Lo que se eligió:** Django gestiona su propia base SQLite
(`data/transactions_django.db`) con migraciones propias, independiente de la
base del E3 (`data/transactions.db`).

**Lo que se descartó:** apuntar el ORM de Django a la base del E3 con
`managed = False` en el modelo, evitando duplicar los datos.

La opción descartada parece eficiente a primera vista, los datos ya existen
en la base del E3, no hace falta volver a cargarlos. El problema es que
`managed = False` le dice a Django que no gestione esa tabla: no la crea,
no la migra, no la modifica. Eso elimina el 25% de la nota del ejercicio
(el criterio de "modelos, migraciones e índices") porque no habría migración
que generar ni índices que demostrar. En este ejercicio se espera ver un archivo
`0001_initial.py` que contenga el `CREATE TABLE` y los dos `CREATE INDEX`.

La decisión correcta es aceptar el costo de duplicar los datos para
cubrir es parte de la evaluación. El `management command` `load_transactions` resuelve
la carga de forma eficiente con `bulk_create`.

**Crítica honesta:** duplicar 1M filas tiene un costo real: aprox 138 segundos de
carga y espacio en disco adicional. En producción esto sería un problema
de diseño, dos bases con los mismos datos se desincronizarán. En este contexto
del es indiferente esta decición porque el objetivo es mas "educativo", aprender Django con
migraciones completas, no optimizar el almacenamiento.

---

## Decisión 2 — DuckDB sigue siendo el backend de analytics

**Lo que se eligió:** los endpoints `/analytics/summary` y
`/analytics/top-merchants` usan DuckDB directamente sobre el Parquet del E1,
igual que en el E4. El ORM de Django no participa en esas queries.

**Lo que se descartó:** usar `Transaction.objects.aggregate()` y `annotate()`
del ORM de Django para calcular los totales.

La justificación viene de los números del E2. En ese ejercicio se midió que
DuckDB resuelve una agregación con GROUP BY sobre 1M filas en ~17ms usando
column pruning, es decir, solo lee las columnas que la query necesita del Parquet.
El ORM de Django sobre SQLite haría un full scan de la tabla completa sin
ninguna ventaja columnar, tardando en el orden de los segundos.

El E4 confirmó esto en producción: el endpoint `/analytics/summary` frío
(cache miss) tardó en promedio 39ms en p50 en tres corridas consecutivas,
con p99 máximo de 55ms. Muy por debajo del SLA de 500ms y completamente
atribuible a DuckDB con la conexión ya inicializada.

En el E5 se mantiene la misma arquitectura porque los datos no cambiaron
ni el patrón de acceso tampoco. Cambiar a ORM para analytics sería un paso
atrás sin ningún beneficio funcional.

---

## Decisión 3 — La conexión DuckDB como lazy singleton en un módulo separado

**Lo que se eligió:** `transactions/services/duckdb.py` implementa un
singleton con variable de módulo `_connection` y `threading.Lock`. La
conexión se crea la primera vez que cualquier view llama
`get_duckdb_connection()` y se reutiliza en todos los requests siguientes.

**Lo que se descartó:** inicializar la conexión en `AppConfig.ready()`,
que es el lugar "natural" en Django para inicialización de la app.

`AppConfig.ready()` tiene un problema concreto en Django: puede ejecutarse
más de una vez. Con `python manage.py runserver`, Django usa autoreload que
lanza un proceso hijo que importa la app de nuevo, ejecutando `ready()` por
segunda vez. Con management commands como `load_transactions`, `ready()` se
ejecuta antes de que el command corra. En algunos contextos de testing
también puede ejecutarse múltiples veces.

Si la conexión DuckDB se inicializa en `ready()`, puede haber intentos de
reinicializar una conexión ya abierta, o múltiples conexiones apuntando al
mismo archivo Parquet desde el mismo proceso.

El lazy singleton en un módulo separado es más seguro porque Python garantiza
que las variables de módulo se inicializan una sola vez por proceso. El
`threading.Lock` con double-checked locking protege el primer acceso
concurrente dentro del mismo proceso. Si el proceso se reinicia (como hace el
autoreload), la variable vuelve a `None` y la próxima llamada crea una
conexión nueva limpia.

---

## Decisión 4 — Status 422 para errores de validación

**Lo que se eligió:** un `custom_exception_handler` en `exceptions.py`
registrado en `settings.REST_FRAMEWORK['EXCEPTION_HANDLER']` que convierte
automáticamente todos los HTTP 400 de DRF en HTTP 422.

**Lo que se descartó:** devolver 400 (el default de DRF) o cambiar el status
manualmente en cada view con `return Response(errors, status=422)`.

DRF devuelve 400 Bad Request cuando un serializer falla la validación. El
E4 usaba FastAPI que devuelve 422 Unprocessable Entity para el mismo caso —
que es el código semánticamente correcto según RFC 9110 para datos que tienen
formato correcto pero violan las reglas de negocio. Para mantener
consistencia entre ejercicios, se necesita 422.

La solución centralizada en un handler es superior a cambiar el status en
cada view porque: no se puede olvidar en ninguna view, aplica también a
endpoints de terceros como `obtain_auth_token`, y hace que el comportamiento
sea predecible en todo el sistema sin excepciones.

Los tests confirmaron que esto funciona correctamente: `test_batch_invalid_amount`,
`test_batch_invalid_category`, `test_batch_invalid_country`,
`test_batch_missing_field`, `test_batch_empty_list` y `test_batch_over_limit`
retornan 422 en todos los casos. Y un efecto secundario verificado por los
tests: `obtain_auth_token` con credenciales incorrectas también retorna 422
(no 400), porque también lanza `ValidationError`.

---

## Decisión 5 — Índices con nombres exactos del E3

**Lo que se eligió:** `Meta.indexes` con `name='idx_user_timestamp'` y
`name='idx_country_user'` — los mismos nombres del `schema.sql` del E3.

**Lo que se descartó:** omitir el `name=` y dejar que Django genere nombres
automáticos como `transactions_user_id_timestamp_idx`.

Esto debido a que el ejrcicio pide replicar los mismos índices que se diseñaron en el E3.
Creo que por replicar no era solo replicar la estructura sino replicar los nombres para
que el se puedan pueda verificar que corresponden. Sin `name=` explícito,
Django genera un nombre que no tiene ninguna relación con los del E3 y la
verificación se vuelve imposible sin leer el SQL de la migración.

La migración generada (`0001_initial.py`) confirma que los índices aparecen
correctamente:

```sql
CREATE INDEX "idx_user_timestamp" ON "transactions" ("user_id", "timestamp" DESC);
CREATE INDEX "idx_country_user" ON "transactions" ("country_code", "user_id");
```

Estos son exactamente los mismos que el E3 definió en `schema.sql`, con los
mismos nombres y el mismo orden de columnas incluyendo el `DESC` en `timestamp`.

---

## Decisión 6 — CharField para timestamp en lugar de DateTimeField

**Lo que se eligió:** `timestamp = models.CharField(max_length=26)` en el
modelo, almacenando el timestamp como texto ISO8601 (`YYYY-MM-DD HH:MM:SS`).

**Lo que se descartó:** `models.DateTimeField()`, que sería la opción
"correcta" en Django.

La razón es de consistencia con el schema del módulo. Desde el E1,
`generate_data.py` genera los timestamps como strings ISO8601 y los guarda
así en CSV y Parquet. El E3 los almacena como TEXT en SQLite. Si el E5 usara
`DateTimeField` con `USE_TZ=True`, Django aplicaría conversiones de timezone
al leer y escribir, potencialmente desalineando los timestamps con los que
están en la base del E3 y en el Parquet.

Con `CharField`, los timestamps se almacenan y recuperan exactamente como
vienen del Parquet, sin conversiones. El costo es que Django no puede hacer
queries de tipo `timestamp__year=2025` ni `timestamp__gte=datetime.now()` de
forma nativa, hay que usar comparaciones de strings. Para los patrones de
acceso del E5 (filtrar por `user_id` y ordenar por `timestamp`) eso no es
un problema porque el índice `idx_user_timestamp` funciona igual con strings
ISO8601 que con DateTimeField, por la propiedad de que el orden lexicográfico
de ISO8601 coincide con el orden cronológico.

---

## Decisión 7 — bulk_create con ignore_conflicts para load_transactions
 
**Lo que se eligió:** `Transaction.objects.bulk_create(objs, batch_size=10000, ignore_conflicts=True)`.
 
**Lo que se descartó:** `Transaction.objects.create()` en un loop, o
`get_or_create()` por cada fila.
 
Con 1M filas, `create()` en un loop hace 1M INSERT individuales con 1M
round-trips a la base de datos. `get_or_create()` hace dos queries por fila
(SELECT + INSERT si no existe). Ambas opciones tardarían horas.
 
`bulk_create` agrupa los objetos en lotes y hace un INSERT por lote con
múltiples VALUES. Con `batch_size=10000`, son 100 INSERTs para 1M filas.
 
El resultado medido fue **138 segundos para 1M filas** — 7,249 filas/s.
Es más lento que el `executemany` de SQL crudo del E3 (40.8 segundos,
24,514 filas/s), con un overhead de 3.4x. Ese overhead es el costo del ORM:
construir objetos Python, validar tipos, gestionar el estado del queryset.
Para el contexto del ejercicio, 138 segundos es completamente aceptable — el
enunciado no impone un SLA de tiempo para la carga inicial.
 
`ignore_conflicts=True` garantiza idempotencia: si `load_transactions` se
corre dos veces, el segundo run no lanza errores ni duplica datos. Todas las
filas que ya existen se ignoran silenciosamente por la restricción del
PRIMARY KEY.
 
---
 
## Decisión 8 — URLconf manual en lugar de Router de DRF
 
**Lo que se eligió:** `path()` explícito para cada uno de los 6 endpoints
en `transactions/urls.py`.
 
**Lo que se descartó:** `DefaultRouter` de DRF con `ViewSets`.
 
El `DefaultRouter` está diseñado para `ViewSets` con acciones CRUD estándar
(`list`, `create`, `retrieve`, `update`, `destroy`). Los endpoints del E5
no siguen ese patrón — `/analytics/summary` no es un "list" de objetos
Summary, es una agregación. `/users/{id}/stats` no es un "retrieve" de un
objeto User, es un cálculo sobre las transacciones de ese usuario.
 
Usar `APIView` con URLconf manual hace que cada endpoint sea explícito y
legible: el evaluador puede leer las 6 rutas de un vistazo sin necesitar
entender la convención del Router. También evita exponer acciones no
intencionadas — el Router genera automáticamente rutas para `PUT`, `PATCH`
y `DELETE` que en este sistema no tienen sentido.
 
---
 
## Validación global — tests automatizados y servidor real
 
### Tests automatizados
 
Los 22 tests pasando en 11.576s son la primera capa de validación:
 
El test `test_batch_deduplication` confirma que `ignore_conflicts=True`
funciona: insertar el mismo `transaction_id` dos veces retorna
`inserted=0, duplicates_skipped=1` sin errores.
 
El test `test_user_transactions_ok` confirma que el índice
`idx_user_timestamp` está activo: la query de transacciones del usuario
responde en milisegundos sobre una base de test en memoria.
 
Los tests de analytics se ejecutaron con el Parquet real disponible y
confirmaron que DuckDB dentro de Django responde correctamente — la misma
infraestructura del E4 funciona sin cambios en la lógica de queries.
 
### Validación en servidor real — 8/8 endpoints verificados
 
Se ejecutó una validación manual con el servidor corriendo contra la base
de 1M filas cargada con `load_transactions`. Resultados:
 
| Request | Resultado | Observación |
|---------|-----------|-------------|
| GET /health | `{"status":"ok","uptime_seconds":75.59}` | `time.monotonic()` funciona correctamente |
| GET /analytics/summary | 1,000,000 tx, 15 países, 10 categorías | `total_amount=2,500,147,886.54` — idéntico al E4 con los mismos datos |
| GET /analytics/top-merchants?limit=5&country=MX | 5 merchants ordenados DESC | Filtro de país y límite funcionan |
| GET /users/2076/transactions (sin token) | 401 | Mensaje de DRF en español confirma auth activo |
| GET /users/2076/transactions (con token) | 43 transacciones paginadas | `count=43` coincide con E3. `next=` confirma paginación |
| GET /users/2076/stats (con token) | `transaction_count=43, top_category=Education` | Agregaciones ORM correctas |
| POST /batch con ID duplicado | `inserted=0, duplicates_skipped=1` | `ignore_conflicts` funciona en producción real |
| POST /batch con amount=-50 | 422 | `custom_exception_handler` convierte 400→422 en producción |
 
Hay un detalle notable en el resultado 2: `total_amount=2,500,147,886.54`
es exactamente el mismo número que retornaba el E4 con los mismos datos —
confirmando que DuckDB lee el mismo Parquet y calcula las mismas agregaciones
independientemente del framework que lo envuelve (FastAPI vs Django).
 
El resultado 7 tiene contexto: `inserted=0` porque `test-e5-validation-001`
ya había sido insertado en una corrida anterior de pruebas. Esto confirma que
la deduplicación funciona correctamente en producción y que correr el endpoint
de batch dos veces con los mismos datos no genera inconsistencias.
 
### El costo real del ORM — qué dice el número
 
La velocidad de ingesta (7,249 filas/s con ORM vs 24,514 filas/s con
`executemany` SQL crudo en el E3) cuantifica el overhead del ORM para
escritura masiva: 3.4x más lento. En un sistema de producción real, la carga
inicial se haría con SQL crudo o herramientas especializadas de ETL.
 
El ORM es la herramienta correcta para el acceso a datos en tiempo de
ejecución — los endpoints responden en milisegundos porque las queries van
a través de los índices. Para la carga masiva de 1M filas que ocurre una
sola vez, 138 segundos es aceptable y `bulk_create` es el punto de equilibrio
correcto entre la comodidad del ORM y la eficiencia del SQL crudo.
