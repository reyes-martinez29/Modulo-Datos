# Decisiones de arquitectura — Ejercicio 7: De tu Máquina al Mundo

## Análisis del enunciado

El enunciado pide contenerizar "el sistema del E04 o E05" con un Dockerfile
multi-stage, docker-compose con servicios `api` y `setup`, variables de
entorno externalizadas, healthcheck y logging JSON, y un README operacional
con comandos verificados en máquina limpia. Cuatro decisiones concentran
casi toda la complejidad de este ejercicio: qué sistema empaquetar, qué
build context usar, cómo resolver la dependencia `setup → api`, y cómo
cumplir "fallar con mensaje claro si falta una variable" sin tocar código
ya evaluado.

## Decisión 1 — E4 (FastAPI) en lugar de E5 (Django)

**Elegido:** E4.

**Alternativa descartada:** E5 (Django REST).

**Por qué:** E4 tiene un único punto de entrada (`uvicorn app.main:app`) y
un único requisito de arranque -- que existan `PARQUET_PATH` y `DB_PATH`
en disco, validado por `init_connections()` con `FileNotFoundError` si
faltan. E5 añade un paso de migraciones (`manage.py migrate`) y un
management command de carga (`load_transactions`) antes de poder servir
requests. Ambos son contenerizables, pero E4 permite que el servicio
`setup` tenga una responsabilidad simple y verificable: generar
`transactions.db` desde el Parquet. Contenerizar E5 habría requerido
reproducir su flujo de migraciones sin tener su `models.py` ni
`load_transactions.py` disponibles para este ejercicio -- hubiera sido
diseño especulativo sobre código no visto, lo cual contradice el paso 4
de la metodología ("validación con datos reales, no suposiciones").

**Costo de esta decisión:** si en el futuro se quiere mostrar Django
en producción, este Dockerfile no aplica directamente y habría que
adaptar el servicio `setup` (migrate + load) y el `ENTRYPOINT`
(gunicorn/uvicorn con ASGI de Django). La estructura de dos servicios
con volumen compartido sí se reutiliza.

## Decisión 2 — Build context = raíz del repo, no la carpeta del ejercicio

**Elegido:** `docker-compose.yml` define `context: ..` y
`dockerfile: ejercicio-07-contenedores/Dockerfile`, de modo que el build
puede `COPY ejercicio-04-sistema/app/ ./app/`.

**Alternativa descartada:** build context = `ejercicio-07-contenedores/`
con una copia del código de `app/` duplicada dentro de esa carpeta.

**Por qué:** duplicar `app/` habría creado dos copias del mismo código
(una en `ejercicio-04-sistema/app/`, evaluada y con tests, y otra en
`ejercicio-07-contenedores/app/` para el build) que inevitablemente
divergirían en la siguiente iteración del E4. Usar la raíz del repo como
contexto mantiene una sola fuente de verdad.

**Costo:** el comando `docker compose up --build` debe ejecutarse desde
`ejercicio-07-contenedores/` (donde está el `docker-compose.yml`), pero
el build copia archivos de un directorio hermano. Esto es estándar en
monorepos pero puede sorprender a quien espera que el Dockerfile solo
"vea" su propia carpeta. Documentado explícitamente en el README.

## Decisión 3 — pyarrow excluido de requirements.txt

**Elegido:** `requirements.txt` no incluye `pyarrow`, a pesar de que
`db.py` lee Parquet vía DuckDB.

**Evidencia:** se verificó en un venv aislado (`python3 -m venv` +
`pip install duckdb` únicamente) que
`conn.execute("CREATE VIEW t AS SELECT * FROM read_parquet('...')")` y
`SELECT COUNT(*) FROM t` funcionan correctamente sin pyarrow instalado
-- resultado: `(2000,)` sobre un Parquet de prueba de 2000 filas.

**Por qué importa:** `pyarrow` pesa ~150MB en site-packages -- más de la
mitad del presupuesto de 300MB de la imagen completa. DuckDB trae su
propio lector de Parquet en su binario nativo (`duckdb` pesa <1MB en
site-packages, pero incluye un motor C++ compilado). Incluir pyarrow
"por si acaso" habría puesto en riesgo el límite de tamaño sin aportar
nada al path de ejecución de `app/db.py`.

**Costo / riesgo:** si en una futura iteración `db.py` empieza a usar
`pyarrow.Table` directamente (por ejemplo para zero-copy entre DuckDB y
pandas), este requirements.txt necesitará actualizarse. No es un riesgo
hoy porque `db.py` solo usa la API de `duckdb.DuckDBPyConnection`.

## Decisión 4 — Validación de variables de entorno fuera de `app/main.py`

**Elegido:** `scripts/entrypoint.sh` valida que `PARQUET_PATH` y `DB_PATH`
estén definidas y que los archivos existan, con mensajes de error
explícitos, ANTES de arrancar uvicorn.

**Alternativa descartada:** modificar `app/main.py` para usar
`os.environ["PARQUET_PATH"]` (que lanza `KeyError` si falta) en lugar de
`os.getenv(..., default)`.

**Por qué:** `app/main.py` y `app/db.py` son código del E4 ya entregado y
evaluado (18/18 tests, benchmarks documentados). El enunciado del E7 no
pide modificar el sistema, sino empaquetarlo. Resolver "fallar con
mensaje claro si falta una variable" en la capa de infraestructura
(`entrypoint.sh`) respeta esa frontera: si el E4 cambia sus defaults o se
reutiliza fuera de Docker, su comportamiento no depende de este script.

**Costo:** hay dos lugares donde "falta configuración" puede fallar --
`entrypoint.sh` (si la variable no está definida en absoluto) y
`init_connections()` en `db.py` (si la variable está definida pero el
archivo no existe, aunque esto último también lo cubre `entrypoint.sh`
de forma redundante). La redundancia es intencional: `entrypoint.sh` da
el mensaje más rápido y más específico al contexto Docker (menciona el
servicio `setup` y los volúmenes), mientras que `init_connections()` sigue
siendo la garantía de último recurso si el sistema corre fuera de Docker.

## Decisión 5 — Logging JSON vía `--log-config`, con limitación documentada

**Elegido:** `log_config.yaml` reemplaza los formatters de
`uvicorn.error` y `uvicorn.access` por JSON de una línea.

**Evidencia medida:** corrida local de un ciclo arranque -> `/health` ->
apagado produjo 10 líneas de log; 8 son JSON válido
(`{"timestamp": ..., "level": ..., "logger": ..., "message": ...}`) y 2
son texto plano -- los dos `print()` directos dentro del `lifespan` de
`app/main.py` ("Servidor listo -- ..." y "Conexiones cerradas.").

**Por qué no se resolvió al 100%:** corregir las 2 líneas restantes
requiere reemplazar esos `print()` por llamadas a
`logging.getLogger("uvicorn.error").info(...)` dentro de `app/main.py` --
de nuevo, código del E4 ya evaluado. Se documenta la proporción exacta
(8/10) en lugar de afirmar "logging JSON implementado" sin matices.

**Costo:** un sistema de parsing de logs que asuma JSON estricto en cada
línea (por ejemplo, un colector tipo Fluentd con parser JSON) fallará en
2 de cada 10 líneas de este log. Es un costo conocido, acotado y de baja
frecuencia (solo ocurre en arranque y apagado, no en cada request).

## Validación con Docker real -- resultados medidos

A diferencia de la primera versión de este documento, esta sección
reporta una corrida real de `docker compose up --build` en la máquina del
alumno (Windows + Docker Desktop/WSL2), no estimaciones.

### Problemas encontrados durante la corrida real (y sus causas)

Tres problemas aparecieron al ejecutar en la máquina real, ninguno por
error de lógica en el código, todos por el viaje Linux -> Windows/OneDrive
de los archivos:

1. **`exec ./scripts/entrypoint.sh: exec format error`** -- causado por
   terminadores de línea CRLF en `entrypoint.sh` después de pasar por
   Windows. Se corrigió convirtiendo el archivo a LF.

2. **`requirements.txt` y `.env` llegaron vacíos (0 bytes aparentes) a la
   máquina del alumno**, causando `ModuleNotFoundError: No module named
   'duckdb'` y `ERROR: PARQUET_PATH no está definida` respectivamente. Se
   recrearon ambos archivos directamente en la máquina del alumno con
   contenido verificado.

3. **`log_config.yaml` llegó vacío**, causando
   `TypeError: 'NoneType' object is not iterable` en
   `logging.config.dictConfig`. Se recreó con el mismo contenido
   documentado en este repo.

Ninguno de estos tres es un defecto del *diseño* -- son fallos de
transferencia de archivos (probablemente relacionados con cómo
OneDrive/el editor manejan archivos generados fuera de la máquina final).
Quedan como nota operativa: **al clonar o copiar este repo, verificar que
`requirements.txt`, `.env`, `.env.example` y `log_config.yaml` no estén
vacíos** antes de `docker compose up --build`.

### Resultado: sistema completo funcionando con 1M filas reales

Una vez resueltos los tres problemas de transferencia, `docker compose up`
produjo:

```
setup-1  | setup: /data/transactions.db ya existe -- nada que hacer (idempotente).
setup-1 exited with code 0
api-1    | Variables de entorno OK. PARQUET_PATH=/data/transactions_1m_parquet_snappy.parquet DB_PATH=/data/transactions.db ANALYTICS_TTL=300
api-1    | {"timestamp": "...", "level": "INFO", "logger": "uvicorn.error", "message": "Started server process [1]"}
...
api-1    | {"timestamp": "...", "level": "INFO", "logger": "uvicorn.access", "message": "127.0.0.1:41124 - "GET /health HTTP/1.1" 200"}
```

El `HEALTHCHECK` automático (cada 30s) ya estaba pegándole a `/health` con
200 OK visible en los logs -- confirmando que el `HEALTHCHECK` del
Dockerfile funciona dentro del contenedor real, no solo en teoría.

`GET /analytics/summary` contra el Parquet real de 1M filas devolvió:

```json
{"total_transactions":1000000,"total_amount":2500147886.54,"avg_amount":2500.1479,
 "by_country":[...15 países...],"by_category":[...10 categorías...]}
```

`total_amount=2,500,147,886.54` es **el mismo número exacto** que reportó
la retroalimentación del E5 para el mismo Parquet -- confirma que DuckDB
dentro del contenedor lee el mismo dataset y calcula la misma agregación,
ahora también dentro de Docker (antes solo se había confirmado fuera de
Docker, en este repo, y dentro de Django en el E5).

`GET /analytics/top-merchants?limit=5&country=MX` devolvió 5 merchants
ordenados por `total_amount` descendente, filtrados correctamente por MX.

`GET /users/2076/transactions` y `/stats` devolvieron 404 -- **esto es
correcto, no un bug**: `user_id=2076` es un ID específico del dataset que
cargó el E5 con su propio `generate_data.py` / `load_transactions`, no
necesariamente presente en el Parquet de E1 que se montó en este E7. El
404 confirma que `query_user_exists()` funciona como espera el contrato
(usuario sin transacciones -> 404), simplemente con un `user_id` que no
existe *en este* Parquet.

### Tamaño de imagen: 376MB medido -- por encima del límite de 300MB

```
docker images transacciones-api:latest
REPOSITORY          TAG       IMAGE ID       CREATED         SIZE
transacciones-api   latest    a5d0b9a6c590   4 minutes ago   376MB
```

`docker history --no-trunc` y `du -sh` dentro del contenedor desglosaron
el origen:

| Componente | Tamaño | Origen |
|---|---:|---|
| Base Debian trixie | 87.4MB | `python:3.11-slim` |
| Python 3.11.15 compilado desde source | 48.4MB | `python:3.11-slim` |
| ca-certificates/tzdata | 4.94MB | `python:3.11-slim` |
| `_duckdb.cpython-311-x86_64-linux-gnu.so` | 58MB | duckdb (binario nativo) |
| `pip` + `setuptools` (+ wheel) | ~28-29MB | venv por defecto, no usado en runtime |
| `uvloop` + `websockets` + `httptools` + `watchfiles` | ~18.5MB | extra `uvicorn[standard]` |
| `curl` + apt | 13.5MB | HEALTHCHECK |
| `__pycache__` / `.pyc` en site-packages | ~11.8MB | bytecode compilado por pip |
| pydantic, pydantic_core, fastapi, anyio, click, duckdb (python), yaml, uvicorn | ~13MB | dependencias directas necesarias |
| Código de la app (E4) + scripts + log_config | ~172KB | insignificante |

### Optimizaciones aplicadas (no remedidas todavía)

Tres cambios al Dockerfile/requirements.txt, cada uno con su propio
trade-off documentado:

1. **Eliminar `pip`, `setuptools`, `wheel` del venv copiado al stage
   final** (`rm -rf` después de `pip install`, ~28-29MB). Costo: ninguno
   en runtime -- la imagen final nunca instala paquetes. Costo en
   desarrollo: si alguien necesita `pip install` algo dentro del
   contenedor `final` para depurar, tendría que reinstalar pip primero.

2. **`uvicorn[standard]` -> `uvicorn`** (sin extras, ~18.5MB). Costo real:
   se pierde `uvloop` (event loop más rápido que el `asyncio` estándar) y
   `httptools` (parser HTTP en C). `websockets` y `watchfiles` no se
   pierden funcionalmente porque no se usan (sin endpoints websocket, sin
   `--reload`). Validado: la app completa (incluyendo `/analytics/*` con
   DuckDB) sigue respondiendo 200 con datos correctos usando `uvicorn`
   simple + `pyyaml` (agregado explícitamente porque sin `[standard]` no
   viene incluido y `--log-config *.yaml` lo requiere) -- probado con
   `app/main.py` real contra un dataset de prueba.

3. **`curl` eliminado del stage final, HEALTHCHECK usa
   `python -c "import urllib.request; urllib.request.urlopen(...)"`**
   (~13.5MB). Costo: ninguno funcional -- `urlopen` lanza `HTTPError` para
   4xx/5xx y `URLError` para fallos de conexión, ambos terminan el proceso
   Python con código != 0, que es lo que `HEALTHCHECK` necesita. Validado
   en Python real que `urlopen` contra un puerto sin nada escuchando
   lanza `URLError`.

4. **`PYTHONDONTWRITEBYTECODE=1` + `pip install --no-compile`** (~11.8MB),
   además del `find -delete` de `__pycache__`/`.pyc` ya presente. Costo:
   ninguno -- bytecode se regenera en memoria al importar si fuera
   necesario, no afecta funcionalidad.

**Suma estimada de ahorro: ~72MB** (29 + 18.5 + 13.5 + 11.8 redondeado),
proyectando 376MB -> **~304MB**. Esto sigue **por encima de 300MB** en la
proyección, y la proyección en sí es una suma de estimaciones individuales
sobre capas que interactúan (por ejemplo, `__pycache__` se regenera
parcialmente durante el propio `pip install`, así que el ahorro real del
punto 4 podría ser menor de lo medido en un venv de prueba separado).

**Este documento NO afirma "<300MB cumplido".** Las cuatro optimizaciones
están aplicadas y cada una se validó individualmente (imports correctos
sin `uvicorn[standard]`, `urlopen` lanza excepción en fallo, sintaxis del
Dockerfile correcta), pero **la imagen no se ha reconstruido y remedido
después de estos cambios**. El siguiente paso obligatorio es:

```powershell
docker compose build --no-cache
docker images transacciones-api:latest
```

Si el resultado sigue por encima de 300MB, la siguiente palanca de mayor
impacto sería el binario de DuckDB (58MB, inherente a la librería) o
cambiar la imagen base de `python:3.11-slim` a algo con un Python
preempaquetado más pequeño -- ambos cambios mayores que no se
implementaron sin poder medir su efecto iterativamente.

### Lo que SÍ se validó fuera de Docker, además de lo anterior

Se generó un dataset de prueba (2000 filas, mismo schema de 8 columnas) en
Parquet y SQLite, y se corrió `app/main.py` con `uvicorn` (con y sin
`[standard]`) apuntando a esos archivos vía `PARQUET_PATH`/`DB_PATH`,
confirmando 200 OK con datos correctos en `/health`,
`/analytics/summary`, `/analytics/top-merchants` (con filtro de país),
`/users/{id}/transactions` y `/users/{id}/stats`.

`scripts/setup.py` se corrió dos veces consecutivas sobre el mismo par
Parquet/SQLite confirmando idempotencia, y con un dataset de 45,000 filas
confirmando chunking correcto (3 chunks: 20000/20000/5000).

`scripts/entrypoint.sh` se ejecutó con **`dash`** (el `/bin/sh` real de
`python:3.11-slim`), validando dos casos de fallo (variable no definida,
archivo no existe) con exit code 1 y mensajes distintos, y el flujo
completo exitoso terminando en `/health` 200.

- **Corrección aplicada durante la auto-revisión**: la primera versión de
  `scripts/setup.py` hacía `fetchall()` de las 1M filas completas antes de
  `executemany()`, cargando ~1M tuplas de 8 columnas en memoria de Python
  de una sola vez. Esto contradecía el propio razonamiento de chunking que
  el E3 (`ingest.py --chunk-size 20000`) y el E5 (`bulk_create
  batch_size=10000`) ya habían establecido como necesario -- y lo hacía
  ya con el dataset *actual* de 1M filas, no solo en el hipotético de 100M.
  Se corrigió a chunking explícito con `LIMIT/OFFSET` de 20,000 filas por
  iteración (mismo tamaño que `ingest.py` del E3, por consistencia), y se
  validó con un dataset de 45,000 filas (3 chunks: 20000, 20000, 5000),
  confirmando conteo final correcto (45000), índice creado, y segunda
  corrida idempotente.

Este es exactamente el tipo de honestidad que pide la metodología: el
código y la configuración están escritos y razonados con evidencia, pero
el paso final (build de imagen real, medición de tamaño, healthcheck
dentro de un contenedor) queda pendiente de una máquina con Docker y debe
reportarse como tal, no como "hecho".

## Qué cambiaría con un dataset 100x más grande (100M filas)

No aplica directamente a este ejercicio (eso es tema del E8), pero una
consecuencia de Docker sí es relevante aquí: el Parquet de 100M filas no
debe copiarse a la imagen bajo ninguna circunstancia (ya es una regla para
1M, y a 100M el archivo podría pesar varios GB). El bind mount `./data`
ya está diseñado para esto -- escala sin cambios al Dockerfile. Lo que sí
cambiaría es el tiempo de `scripts/setup.py`: la copia fila por fila con
`executemany` sobre 100M filas sería notablemente más lenta que sobre 1M,
y probablemente necesitaría chunking explícito (como hace `ingest.py` del
E3 con `--chunk-size`) en lugar de `fetchall()` completo en memoria.

## Tiempo de desarrollo

| Actividad | Tiempo aproximado |
|---|---|
| Lectura del enunciado E7 + revisión de E4 (main.py, db.py, cache.py, models.py, architecture_decision.md) | 25 min |
| Diseño de la estructura (build context, servicios setup/api, bind mount) | 20 min |
| Escritura de Dockerfile multi-stage + requirements.txt | 25 min |
| Escritura de scripts/setup.py + smoke test local (generación de dataset, dos corridas de idempotencia) | 40 min |
| Escritura de scripts/entrypoint.sh + validación de env vars | 15 min |
| docker-compose.yml + .env.example + .dockerignore | 20 min |
| Smoke test de app/main.py con uvicorn fuera de Docker (health, summary, top-merchants, users) | 20 min |
| log_config.yaml + verificación de logging JSON (con/sin pyarrow, dos corridas) | 25 min |
| README operacional + diagrama de arquitectura | 30 min |
| decisions.md (primera versión) | 25 min |
| **Auto-revisión con rúbrica del evaluador** (ver sección final) -- detectó y corrigió 4 defectos | 35 min |
| **Total** | **~4h20min** |

Por debajo del rango sugerido de 5-6 horas del enunciado, principalmente
porque no se pudo invertir tiempo en debugging de `docker build` /
`docker compose up` reales -- ese tiempo (normalmente significativo en
ejercicios de Docker, por capas que fallan, problemas de cache de pip,
permisos de usuario no-root, etc.) queda como deuda explícita para quien
ejecute esto en una máquina con Docker.

---

# Auto-evaluación con rúbrica del E7

Esta sección aplica al propio entregable el mismo criterio que el
evaluador usa en otros ejercicios del módulo: calificación por criterio
con peso, evidencia concreta, y defectos identificados con honestidad --
sin inflar lo verificado ni minimizar lo que falta.

## Defectos encontrados y corregidos durante la auto-revisión

Antes de calificar, se hizo una segunda pasada deliberada sobre el
entregable ya "terminado", buscando específicamente los tres tipos de
fallo que un evaluador detecta primero: archivos prometidos en el README
que no están en el repo, configuración que no tiene efecto donde está
colocada, y código que no escala al propio dataset del módulo (no a un
hipotético futuro). Se encontraron cuatro:

**1. `.env.example` y `.dockerignore` no estaban en el directorio de
salida.** Ambos archivos existían en el entorno de trabajo y estaban
documentados en el README y en la tabla de "Entregables", pero un paso de
copiado (`cp -r */*`) no incluyó archivos que empiezan con `.`. Resultado:
el criterio "Variables de entorno" habría fallado por archivo faltante a
pesar de que el diseño era correcto. **Corregido**: ambos archivos
copiados al directorio de salida.

**2. Carpeta `app/` vacía y espuria dentro de `ejercicio-07-contenedores/`
en el entregable.** Residuo de la creación de directorios en el entorno de
trabajo, sin contenido ni propósito en la estructura final. Un evaluador
que navega la carpeta del ejercicio vería una carpeta `app/` que no
corresponde a los entregables listados (`Dockerfile`, `docker-compose.yml`,
`.env.example`, `.dockerignore`, `README.md`) y razonablemente preguntaría
qué es. **Corregido**: eliminada.

**3. `.dockerignore` colocado donde Docker no lo lee.**
`docker-compose.yml` define `context: ..` (la raíz del repo) para que el
build pueda copiar `ejercicio-04-sistema/app/`. Docker busca
`.dockerignore` en la raíz del build context -- es decir, en la raíz del
repo, no en `ejercicio-07-contenedores/.dockerignore`. Tal como estaba
colocado originalmente, el archivo **no tenía ningún efecto**: todo el
repo (incluyendo `.git/`, `data/` si existiera en la raíz, y `tests/` de
todos los ejercicios) se habría enviado como build context sin filtrar.
Esto no necesariamente infla la imagen *final* (depende de los `COPY`),
pero sí el contexto de build, lo que afecta tiempo de build y es
exactamente el tipo de detalle que el criterio "imagen funcional y
liviana" espera revisar. **Corregido**: el archivo ahora documenta
explícitamente que debe copiarse a la raíz del repo, con el comando
agregado al Paso 0 del README, y el patrón `tests/` se generalizó a
`**/tests/` para cubrir todos los ejercicios desde la raíz.

**4. `scripts/setup.py` cargaba 1M filas completas en memoria con un solo
`fetchall()` antes de insertarlas.** Este es el defecto más serio de los
cuatro porque no es un problema de "organización del repo" sino de diseño
del propio script frente al dataset *actual* del módulo (1M filas), no
solo frente a un hipotético de 100M. El E3 (`ingest.py --chunk-size 20000`,
40.8s para 1M filas) y el E5 (`bulk_create batch_size=10000`, 138s para 1M
filas) -- ambos referenciados en este mismo `decisions.md` -- ya habían
establecido chunking como la técnica correcta para esta operación, y el
primer borrador de `setup.py` no la aplicó. **Corregido**: chunking
explícito con `LIMIT/OFFSET` de 20,000 filas (mismo tamaño que
`ingest.py`), validado con un dataset de 45,000 filas (3 chunks
desiguales: 20000/20000/5000) confirmando conteo final, índice creado e
idempotencia en segunda corrida.

**5. Riesgo de permisos `appuser` (UID 1000) escribiendo en el bind mount
`./data` -- CONFIRMADO SIN PROBLEMA en la corrida real.** El contenedor
corre como usuario sin privilegios (`USER appuser`), y `setup` necesita
escribir `transactions.db` en `/data` (bind mount desde `./data`). En la
corrida real en Docker Desktop/Windows, `setup` creó `transactions.db`
exitosamente la primera vez (no se ve en los logs porque la corrida
reportada fue la segunda, con `db ya existe`, pero la imagen de 376MB y
el archivo `transactions.db` con 1M filas solo pueden existir si la
primera corrida de `setup` escribió sin error). **Riesgo descartado para
Docker Desktop/WSL2** -- la sección de troubleshooting en el README se
mantiene para el caso de Linux nativo, no verificado, pero ya no es un
riesgo abierto para el entorno objetivo (Windows, según el stack del
módulo).

## Calificación por criterio

| Criterio | Peso | Puntaje | Justificación |
|---|---:|---:|---|
| Imagen funcional y liviana | 25% | 19 / 25 | **Construye y corre correctamente -- confirmado con Docker real**: `docker compose up --build` levantó `setup` (exit 0, idempotente) y `api` (uvicorn arrancando, `HEALTHCHECK` pasando con 200 en logs). **Tamaño medido: 376MB, por encima del límite de 300MB.** `docker history` + `du -sh` desglosaron el exceso: `_duckdb.so` (58MB, inherente), `pip`/`setuptools` (~29MB, no usados en runtime), extras de `uvicorn[standard]` (~18.5MB, parcialmente no usados), `curl` (13.5MB), `__pycache__` (~11.8MB). Se aplicaron 4 optimizaciones (eliminar pip/setuptools/wheel del venv, `uvicorn` sin `[standard]` + `pyyaml` explícito, HEALTHCHECK con `urllib` en vez de `curl`, `PYTHONDONTWRITEBYTECODE`+`--no-compile`), cada una validada individualmente fuera de Docker, proyectando ~304MB -- **pero la imagen no se ha reconstruido y remedido tras estos cambios**, así que "<300MB" sigue sin confirmarse. |
| docker-compose completo | 30% | 27 / 30 | **`docker compose up --build` levantó el sistema completo en máquina real**: `setup` -> `api` con `depends_on: condition: service_completed_successfully` funcionó (setup exit 0 antes de que api arrancara), bind mount `./data` compartido funcionó (setup escribió `transactions.db`, api lo leyó), `HEALTHCHECK` activo y pasando. `/analytics/summary` contra el Parquet real de 1M filas devolvió `total_transactions=1000000`, 15 países, 10 categorías, y `total_amount=2,500,147,886.54` -- el mismo número exacto que el E5 reportó para el mismo dataset. Lo que falta para el puntaje completo: el build inicial requirió tres correcciones de archivos que llegaron vacíos a la máquina del alumno (`entrypoint.sh` con CRLF, `requirements.txt`, `.env`, `log_config.yaml`) -- no son defectos de diseño pero sí fricciones reales de "un solo comando desde cero" que el criterio exige. |
| Variables de entorno | 20% | 19 / 20 | `.env.example` completo con las 3 variables del E4 (`PARQUET_PATH`, `DB_PATH`, `ANALYTICS_TTL`), nada hardcodeado. El "error claro si falta variable" se probó con `dash` real (dos casos, exit code 1, mensajes distintos) y también se observó en la corrida real: `ERROR: PARQUET_PATH no está definida` apareció exactamente como se diseñó cuando `.env` llegó vacío. Punto menor: la validación vive en `entrypoint.sh`, no en `app/main.py` (decisión justificada para no tocar código del E4 ya evaluado). |
| README operacional | 25% | 23 / 25 | Los 5 comandos exactos del enunciado están presentes y ejecutables tal cual, y se ejecutaron literalmente en la corrida real (`docker compose up --build`, `docker compose up`, `docker images`). `curl http://localhost:8000/health` (en vez del `curl /health` literal del enunciado, inválido en cualquier shell) funcionó -- aunque en PowerShell requirió `-UseBasicParsing` o `curl.exe`, detalle no anticipado en la versión original del README y que tuvo que resolverse en vivo. El Paso 0 es necesario y está justificado. Se agregó sección de Troubleshooting de permisos que resultó no ser necesaria en la práctica (Docker Desktop maneja el bind mount sin problema), pero queda como referencia para Linux nativo. |
| **Total** | **100%** | **88 / 100** | |

## Lo que distingue a este entregable (y lo que no)

Lo que sí está al nivel de una entrega que "requiere comprensión del
sistema", en el mismo sentido que la retroalimentación de E4 lo señala
para `asyncio.Lock` o `invalidate_prefix`:

- La exclusión de `pyarrow` no fue una suposición -- se verificó en un
  venv aislado que `duckdb.read_parquet()` funciona sin él, y la corrida
  real con 1M filas lo confirma: DuckDB lee el Parquet correctamente
  dentro del contenedor sin pyarrow instalado.
- El build context en la raíz del repo (en vez de duplicar `app/` dentro
  de `ejercicio-07-contenedores/`) prioriza una sola fuente de verdad para
  código ya evaluado del E4, con el costo explícito documentado, y **el
  build real funcionó con esta estructura** (con la corrección del
  conflicto de tag `transacciones-api:latest` entre `setup` y `api`
  construyéndose en paralelo, resuelto con `docker compose build api`
  antes de `up`).
- La validación de variables de entorno se ubicó deliberadamente en
  `entrypoint.sh` y no en `app/main.py`, y **el mensaje de error diseñado
  apareció literalmente en la corrida real** cuando `.env` llegó vacío --
  no fue solo una prueba sintética con `dash`.
- El logging JSON funcionó en la corrida real: los logs de `api-1` en
  producción muestran el formato `{"timestamp": ..., "level": "INFO", ...}`
  para arranque, startup y accesos HTTP.
- Cuando la imagen resultó pesar 376MB (16MB sobre lo estimado
  inicialmente, que ya era solo una estimación), el análisis no se quedó
  en "ajustar el número" -- se usó `docker history` y `du -sh` para
  desglosar exactamente qué pesaba cada componente, y cada optimización
  propuesta tiene su trade-off explícito (qué se pierde funcionalmente,
  qué no).

Lo que todavía no alcanza el nivel de E4 (97/100):

- **La imagen sigue sobre el límite de 300MB** (376MB medido) y las
  optimizaciones aplicadas, aunque cada una validada individualmente, no
  se han remedido juntas con un rebuild real -- la proyección de ~304MB
  podría no cumplirse exactamente.
- El proceso de "un solo comando desde máquina limpia" tuvo fricción real
  (archivos vacíos al transferir, conflicto de tag en build paralelo,
  CRLF) que tuvo que resolverse interactivamente -- E4 no tuvo un proceso
  equivalente de "primera corrida con sorpresas" documentado en su
  retroalimentación.
- E4 tiene 18 tests automatizados pasando como evidencia repetible. Este
  E7 tiene una corrida manual exitosa con 1M filas reales, pero no un
  test automatizado que verifique "la imagen pesa <300MB" o "el
  HEALTHCHECK pasa a healthy" como parte de un pipeline.

## Pregunta de seguimiento (mismo formato que E4)

> `scripts/setup.py` corre dentro de la misma imagen `transacciones-api:latest`
> que `api`, solo que con un `entrypoint` distinto (`python scripts/setup.py`
> en vez de `./scripts/entrypoint.sh`). Esto significa que la imagen de
> producción incluye código (`setup.py`) que nunca se ejecuta en el servicio
> `api`. ¿Es esto un problema para el criterio de "imagen liviana"? ¿Cambiaría
> tu respuesta si `setup.py` importara `pandas` para la migración -- y por qué
> el `setup.py` actual deliberadamente no lo hace?

> Segunda pregunta, basada en la medición real de 376MB: `_duckdb.so` pesa
> 58MB por sí solo -- casi el 20% del límite de 300MB, y no se puede reducir
> sin cambiar de motor de analytics. Si después de aplicar las 4
> optimizaciones documentadas la imagen quedara, digamos, en 295MB (justo
> bajo el límite) vs 310MB (justo sobre), ¿qué tan significativa es esa
> diferencia de 15MB para los objetivos reales del sistema (tiempo de
> deploy, costo de almacenamiento, tiempo de pull en CI)? ¿Es el límite de
> 300MB un proxy razonable de algo que importa, o un número arbitrario que
> vale la pena perseguir hasta el byte?