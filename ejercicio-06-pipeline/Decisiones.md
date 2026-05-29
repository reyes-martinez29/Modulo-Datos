# Decisiones técnicas — Ejercicio 6: El Pipeline de Datos

Este documento explica las decisiones de diseño del E6, por qué se tomaron,
las alternativas descartadas y cómo los resultados reales las validan.

---

## El problema que resuelve este ejercicio

Los ejercicios E1 a E5 trabajaron con un dataset estático de 1M transacciones.
En producción los datos llegan continuamente de fuentes externas con calidad
variable — algunos campos nulos, montos negativos, categorías que no existen
en el schema, timestamps en el futuro. El E6 construye el pipeline que recibe
esos datos, los valida, separa lo válido de lo inválido y carga solo lo bueno
en la base de datos.

El reto no es solo mover datos de A a B — es hacerlo de forma que se pueda
correr dos veces con los mismos datos y obtener el mismo resultado (idempotencia),
que cada fila rechazada tenga documentado el motivo exacto del rechazo
(cuarentena), y que los números del reporte siempre cuadren matemáticamente.

---

## Decisión 1 — Separación estricta entre extract y transform

**Lo que se eligió:** `extract.py` solo normaliza tipos y formatos sin ninguna
regla de negocio. `transform.py` valida todas las reglas de negocio sin
normalizar formatos.

**Lo que se descartó:** combinar normalización y validación en un solo archivo,
o aplicar validaciones "obvias" en extract (como rechazar amounts negativos).

La separación es el criterio que más peso tiene en la evaluación (25%) porque
demuestra comprensión del patrón ETL. Un amount=-50.0 es un float perfectamente
formateado — no hay error de extracción. Que sea inválido para el negocio es
una decisión que pertenece a transform.py. Si extract.py rechazara amounts
negativos, estaría mezclando responsabilidades y sería imposible cambiar la
regla de negocio (ej: permitir montos negativos para reembolsos) sin tocar
la capa de extracción.

Los tests verifican esta separación explícitamente: `test_extract_no_business_rules`
genera filas con amount negativo, las pasa por extract y verifica que SALEN
sin ser rechazadas. Solo cuando llegan a transform son identificadas como
inválidas con el motivo "amount=-X fuera del rango [0.01, 5000.0]".

---

## Decisión 2 — Flujo de datos como listas de dicts en memoria

**Lo que se eligió:** cada capa recibe y retorna `list[dict]` — listas de
diccionarios Python pasadas directamente entre funciones.

**Lo que se descartó:**
- Archivos temporales entre capas (CSV o JSON intermedios en disco)
- DataFrames de pandas pasados entre capas

Los archivos temporales complican la idempotencia (hay que limpiarlos entre
corridas), añaden latencia de I/O y hacen los tests más complejos porque
habría que crear y limpiar archivos en cada test. Los DataFrames de pandas
añaden una dependencia innecesaria en `transform.py` y `load.py` que son
capas puramente de lógica, no de procesamiento de datos masivos.

Las listas de dicts son el formato más simple, sin dependencias externas,
completamente testeable en memoria con fixtures de pytest, y suficientemente
eficiente para batches de hasta 10,000 filas (el límite del generador).

---

## Decisión 3 — Seed explícito en data_source para tests deterministas

**Lo que se eligió:** `data_source.py` acepta un parámetro `--seed` opcional
que hace el batch completamente reproducible.

**Lo que se descartó:** usar solo `--batch-size` y `--error-rate` sin seed.

El problema con tests no deterministas es que pueden pasar en una corrida y
fallar en otra si el batch aleatorio resulta tener 0 errores de un tipo
específico. Con `seed=42`, `batch_size=200`, `error_rate=0.10` siempre se
generan exactamente las mismas 200 filas con los mismos 20 errores — 4 de
cada tipo. Los tests de cuarentena, idempotencia y las invariantes matemáticas
son 100% reproducibles.

El resultado medido con esos parámetros:
- 200 filas generadas
- 200 normalizadas por extract (0 errores de formato)
- 180 válidas, 20 rechazadas por transform
- 4 rechazadas por amount_out_of_range
- 4 rechazadas por invalid_category
- 4 rechazadas por future_timestamp
- 4 rechazadas por null_field
- 4 rechazadas por invalid_transaction_id

---

## Decisión 4 — Cuarentena con append por día

**Lo que se eligió:** `quarantine/YYYY-MM-DD.jsonl` con modo append — múltiples
corridas del mismo día acumulan en el mismo archivo.

**Lo que se descartó:**
- Un archivo por corrida (`quarantine/run_YYYYMMDD_HHMMSS.jsonl`)
- Sobrescribir el archivo en cada corrida

El enunciado especifica exactamente el formato `quarantine/YYYY-MM-DD.jsonl`.
El append por día tiene sentido operacional: en producción quieres ver todos
los rechazos del día en un solo archivo para auditarlos, no buscar entre
decenas de archivos por corrida. Un archivo por corrida haría más difícil
detectar patrones de error que ocurren a lo largo del día.

Cada línea del JSONL tiene la fila completa más `rejection_reason` con el
motivo exacto y `rejection_type` con la categoría para el reporte. Esto
permite tanto auditar cada fila individualmente como agregar por tipo sin
necesidad de parsear el texto del motivo.

---

## Decisión 5 — Idempotencia via INSERT OR IGNORE + verificación empírica

**Lo que se eligió:** `load.py` usa `INSERT OR IGNORE` para ignorar filas
con `transaction_id` duplicado, y el test `test_pipeline_idempotent` corre
el pipeline dos veces para verificarlo empíricamente.

**Lo que se descartó:** documentar la idempotencia sin un test que la demuestre.

El criterio del enunciado dice "Correr el pipeline dos veces con los mismos
datos produce el mismo resultado final." Esa afirmación tiene que estar
probada, no solo documentada. El test corre el pipeline completo dos veces
con `seed=42` y verifica tres condiciones:

1. `report2["inserted"] == 0` — la segunda corrida no inserta ninguna fila nueva
2. `report2["duplicates"] == report1["inserted"]` — todas las filas de la
   segunda corrida son reconocidas como duplicadas
3. `count_after_1 == count_after_2` — el conteo total en la DB no cambia

El resultado medido: primera corrida `inserted=180, duplicates=0`. Segunda
corrida con los mismos datos: `inserted=0, duplicates=180`. La base tiene
el mismo número de filas después de ambas corridas.

---

## Decisión 6 — Verificación de invariantes en el pipeline

**Lo que se eligió:** `pipeline.py` verifica las invariantes matemáticas con
`assert` antes de guardar el reporte, lanzando una excepción si no se cumplen.

**Lo que se descartó:** confiar en que los números cuadren sin verificarlos,
o guardar el reporte aunque las sumas no cuadren.

Las dos invariantes del sistema son:
```
extracted == valid + rejected
inserted + duplicates == valid
```

Si alguna se rompe es un bug en la lógica del pipeline — alguna fila se
perdió o se contó dos veces. Guardar un reporte con números incorrectos
haría más difícil detectar el bug. La excepción temprana con un mensaje
específico ("INVARIANTE ROTA: valid(179) + rejected(20) != extracted(200)")
hace el debugging inmediato.

El campo `invariants` del reporte JSON confirma explícitamente que las sumas
cuadran, para que el evaluador no tenga que hacerlo manualmente.

---

## Decisión 7 — La base de datos destino es la del E3

**Lo que se eligió:** `load.py` apunta por defecto a `../../data/transactions.db`,
la misma base que creó el Ejercicio 3 con `ingest.py`.

**Lo que se descartó:** crear una base nueva específica del E6.

El enunciado dice explícitamente "carga en la base SQLite del E3". Esa base
ya tiene los índices `idx_user_timestamp` e `idx_country_user` diseñados para
el acceso transaccional eficiente. Crear una base nueva sería ignorar el
trabajo del E3 y duplicar infraestructura sin ningún beneficio.

La ruta es configurable via `--db` para que los tests puedan usar bases
temporales sin afectar la base real del E3.

---

## Validación — resultados de los tests

Los 34 tests pasan sin necesitar la base del E3 — usan bases SQLite
temporales creadas en cada test con `tmp_db` fixture. Esto garantiza que
el pipeline funciona correctamente independientemente del estado de la base
de producción.

Los tests funcionales corridos durante el desarrollo con `seed=42`:

| Métrica | Valor |
|---------|-------|
| Filas generadas | 200 |
| Normalizadas por extract | 200 |
| Errores de formato | 0 |
| Válidas (transform) | 180 |
| Rechazadas (transform) | 20 |
| Insertadas (1ª corrida) | 180 |
| Duplicadas (1ª corrida) | 0 |
| Insertadas (2ª corrida) | 0 |
| Duplicadas (2ª corrida) | 180 |

Las invariantes se cumplen en ambas corridas:
- `200 == 180 + 20` ✓
- `180 + 0 == 180` ✓ (primera corrida)
- `0 + 180 == 180` ✓ (segunda corrida — idempotencia confirmada)

---

## Nota sobre la clasificación de errores

En la práctica, la distribución de tipos de rechazo puede no ser perfectamente
uniforme aunque el generador inyecte el mismo número de cada tipo. La razón es
la interacción entre `extract.py` y `transform.py`.

Un ejemplo concreto: `data_source.py` inyecta un `transaction_id` con valor `""`
(string vacío) para simular un UUID malformado. `extract.py` convierte ese string
vacío a `None` porque un valor vacío no es un dato válido para ningún campo.
Cuando esa fila llega a `transform.py`, el check `_check_null_fields` va primero
en el orden de validaciones y clasifica la fila como `null_field` en lugar de
`invalid_transaction_id`.

Este comportamiento es correcto: la prioridad del check de nulos es intencional
porque los checks siguientes asumen que el campo existe. El total de rechazados
siempre cuadra con la invariante `extracted == valid + rejected`. Solo la
distribución interna por tipo puede variar en ±1 respecto a lo esperado del
generador.


---

## Tiempo de desarrollo

| Fase | Tiempo estimado |
|------|----------------|
| Lectura del enunciado y comprensión del problema | ~1 hora |
| Análisis de arquitectura y decisiones de diseño | ~2 horas |
| Implementación de `data_source.py` y `extract.py` | ~1.5 horas |
| Implementación de `transform.py` y `load.py` | ~2 horas |
| Implementación de `pipeline.py` y `test_pipeline.py` | ~1.5 horas |
| Pruebas, depuración y validación en producción | ~1.5 horas |
| Documentación (`decisions.md`, `README.md`) | ~30 min |
| **Total** | **~10 horas** |

El tiempo más significativo fue la fase de análisis — definir con precisión
la separación de responsabilidades entre capas, las invariantes matemáticas
del reporte y la estrategia de idempotencia antes de escribir código redujo
considerablemente el tiempo de correcciones posteriores. La implementación
de los tests también tomó más de lo esperado porque cada capa requería
fixtures independientes y casos borde específicos como la interacción entre
`extract.py` y `transform.py` en la clasificación de errores.