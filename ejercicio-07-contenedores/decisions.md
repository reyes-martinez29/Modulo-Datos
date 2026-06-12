# Decisiones de arquitectura — Ejercicio 7: De tu máquina al mundo

## Análisis del enunciado

El enunciado pide contenerizar el sistema del E04 o el E05 con un Dockerfile multi-stage, un docker-compose con servicios `api` y `setup`, variables de entorno externalizadas, healthcheck, logging en JSON y un README operacional con comandos verificados en una máquina limpia. Cuatro decisiones concentran casi toda la complejidad de este ejercicio: qué sistema empaquetar, qué build context usar, cómo resolver la dependencia entre `setup` y `api`, y cómo cumplir con "fallar con un mensaje claro si falta una variable" sin tocar código que ya había sido creado.

## Decisión 1 — elegí E4 (FastAPI) en lugar de E5 (Django)

Elegí contenerizar el E4 porque tiene un único punto de entrada (`uvicorn app.main:app`) y un único requisito de arranque: que existan `PARQUET_PATH` y `DB_PATH` en disco, lo cual ya está validado por `init_connections()` con un `FileNotFoundError` si faltan.

El E5, en cambio, tiene un flujo de arranque más largo y con más pasos con efectos secundarios: migraciones (`manage.py migrate`), un comando de carga (`load_transactions`) que en la pruebas tarda alrededor de 138 segundos con 1M filas, y administra su propia base de datos separada de la del E3. Empaquetar eso en el servicio `setup` implica orquestar varios pasos secuenciales en lugar de uno solo, y cada paso adicional es una oportunidad más de que algo falle. Con E4, en cambio, el servicio `setup` tiene una responsabilidad simple y verificable: generar `transactions.db` a partir del Parquet.

El costo de esta decisión es que, si más adelante se quiere mostrar Django en producción, este Dockerfile no aplica directamente: habría que adaptar el servicio `setup` para que corra `migrate` y `load`, y cambiar el `ENTRYPOINT` a gunicorn o uvicorn con el ASGI de Django. La estructura general de dos servicios con un volumen compartido sí se podría reutilizar.

## Decisión 2 — el build context es la raíz del repo, no la carpeta del ejercicio

El `docker-compose.yml` define `context: ..` y `dockerfile: ejercicio-07-contenedores/Dockerfile`, de modo que el build puede hacer `COPY ejercicio-04-sistema/app/ ./app/` directamente desde la carpeta del E4.

Consideré la alternativa de usar como build context solo `ejercicio-07-contenedores/`, duplicando ahí una copia del código de `app/`. Descarté esto porque habría creado dos copias del mismo código, una en `ejercicio-04-sistema/app/`, ya evaluada y con tests, y otra dentro de `ejercicio-07-contenedores/app/` solo para el build, que inevitablemente divergirían en la siguiente iteración del E4. Usar la raíz del repo como contexto mantiene una sola fuente de verdad.

El costo de esto es que `docker compose up --build` debe ejecutarse desde `ejercicio-07-contenedores/` (donde está el `docker-compose.yml`), pero el build termina copiando archivos de una carpeta hermana. Esto es normal en monorepos, aunque puede sorprender a alguien que espera que el Dockerfile solo "vea" su propia carpeta, así que lo documenté explícitamente en el README.

## Decisión 3 — excluí pyarrow de requirements.txt

`requirements.txt` no incluye `pyarrow`, a pesar de que `db.py` lee Parquet a través de DuckDB. Antes de descartarlo verifiqué, en un venv aislado con solo `duckdb` instalado, que `conn.execute("CREATE VIEW t AS SELECT * FROM read_parquet('...')")` y el `SELECT COUNT(*) FROM t` posterior funcionan correctamente sin pyarrow. Sobre un Parquet de prueba de 2000 filas, el resultado fue `(2000,)`.

Esto importa porque pyarrow pesa alrededor de 150MB en site-packages, más de la mitad del presupuesto de 300MB de la imagen completa. DuckDB ya trae su propio lector de Parquet dentro de su binario nativo (el paquete `duckdb` en sí pesa menos de 1MB en site-packages, aunque incluye un motor en C++ compilado). Agregar pyarrow "por si acaso" habría puesto en riesgo el límite de tamaño sin aportar nada al camino de ejecución de `app/db.py`.

El riesgo de esta decisión es que, si en una futura iteración `db.py` empezara a usar `pyarrow.Table` directamente, por ejemplo para hacer zero-copy entre DuckDB y pandas, habría que actualizar este requirements.txt. Hoy no es un riesgo porque `db.py` solo usa la API de `duckdb.DuckDBPyConnection`.

## Decisión 4 — la validación de variables de entorno vive fuera de app/main.py

`scripts/entrypoint.sh` valida que `PARQUET_PATH` y `DB_PATH` estén definidas y que los archivos correspondientes existan, con mensajes de error explícitos, antes de arrancar uvicorn.

La alternativa habría sido modificar `app/main.py` para usar `os.environ["PARQUET_PATH"]` (que lanza `KeyError` si falta la variable) en lugar de `os.getenv(..., default)`. No hice esto porque `app/main.py` y `app/db.py` son código del E4 ya entregado y evaluado, con 18 de 18 tests pasando y benchmarks documentados.

El costo de esto es que hay dos lugares donde "falta configuración" puede fallar: `entrypoint.sh`, si la variable no está definida en absoluto, y `init_connections()` dentro de `db.py`, si la variable está definida pero el archivo no existe, aunque esto último también lo cubre `entrypoint.sh` de forma redundante. Esa redundancia es intencional: `entrypoint.sh` da el mensaje más rápido y más específico al contexto de Docker, ya que menciona el servicio `setup` y los volúmenes, mientras que `init_connections()` sigue siendo la garantía de último recurso si el sistema corre fuera de Docker.

## Decisión 5 — logging en JSON vía --log-config, con una limitación que documento explícitamente

`log_config.yaml` reemplaza los formatters de `uvicorn.error` y `uvicorn.access` por un formato JSON de una línea por evento.

Para verificar esto corrí localmente un ciclo de arranque, una petición a `/health` y el apagado, lo cual produjo 10 líneas de log. De esas, 8 son JSON válido con la forma `{"timestamp": ..., "level": ..., "logger": ..., "message": ...}`, y 2 son texto plano: los dos `print()` directos dentro del `lifespan` de `app/main.py` ("Servidor listo — ..." y "Conexiones cerradas.").

No corregí esas dos líneas porque hacerlo implicaría reemplazar esos `print()` por llamadas a `logging.getLogger("uvicorn.error").info(...)` dentro de `app/main.py`, que de nuevo es código del E4. Prefiero documentar la proporción exacta, 8 de 10, en lugar de afirmar "logging JSON implementado" sin matices. El costo concreto es que un sistema de recolección de logs que asuma JSON estricto en cada línea fallaría en esas 2 de cada 10, algo conocido, acotado, y que solo ocurre en arranque y apagado, no en cada request.


### El sistema completo funcionando con 1M filas

Una vez corrido `docker compose up --build` produjo lo siguiente:

```
setup-1  | setup: /data/transactions.db ya existe -- nada que hacer (idempotente).
setup-1 exited with code 0
api-1    | Variables de entorno OK. PARQUET_PATH=/data/transactions_1m_parquet_snappy.parquet DB_PATH=/data/transactions.db ANALYTICS_TTL=300
api-1    | {"timestamp": "...", "level": "INFO", "logger": "uvicorn.error", "message": "Started server process [1]"}
...
api-1    | {"timestamp": "...", "level": "INFO", "logger": "uvicorn.access", "message": "127.0.0.1:41124 - "GET /health HTTP/1.1" 200"}
```

El healthcheck automático, configurado cada 30 segundos, ya estaba devolviendo 200 en los logs, lo cual confirma que funciona dentro del contenedor real y no solo en la definición del Dockerfile.

`GET /analytics/summary` contra el Parquet real de 1M filas devolvió `total_transactions: 1000000`, con el desglose esperado de 15 países y 10 categorías, y un `total_amount` de 2,500,147,886.54. Note que este es el mismo número exacto que reportó la retroalimentación del E5 para el mismo Parquet, lo cual confirma que DuckDB, ahora corriendo dentro del contenedor, lee el mismo dataset y calcula la misma agregación que ya se había validado antes fuera de Docker y también dentro de Django en el E5.

`GET /analytics/top-merchants?limit=5&country=MX` devolvió 5 merchants ordenados por monto total de forma descendente, filtrados correctamente por México.

`GET /users/2076/transactions` y `/stats` devolvieron 404. Esto es correcto y no un error: el `user_id=2076` corresponde al dataset que cargó el E5 con su propio `generate_data.py` y `load_transactions`, y no necesariamente está presente en el Parquet de E1 que se montó en este E7. El 404 confirma que `query_user_exists()` funciona como espera el contrato, un usuario sin transacciones devuelve 404, simplemente con un `user_id` que no existe en este Parquet en particular.

### El primer tamaño medido fue 376MB, por encima del límite de 300MB

```
docker images transacciones-api:latest
REPOSITORY          TAG       IMAGE ID       CREATED         SIZE
transacciones-api   latest    a5d0b9a6c590   4 minutes ago   376MB
```

Para entender de dónde venía el exceso usé `docker history --no-trunc` junto con `du -sh` dentro del contenedor, y encontré lo siguiente.

La base de Debian trixie aporta 87.4MB, y Python 3.11.15 compilado desde código fuente agrega otros 48.4MB, ambos vienen incluidos en `python:3.11-slim` y no son algo que se pueda reducir sin cambiar de imagen base. El binario nativo de DuckDB, `_duckdb.cpython-311-x86_64-linux-gnu.so`, pesa 58MB por sí solo. Las certificaciones y la configuración de zona horaria agregan casi 5MB.

Lo que sí podía recortarse sin perder funcionalidad eran cuatro cosas. `pip` y `setuptools`, junto con `wheel`, suman entre 28 y 29MB y quedan instalados dentro del venv por defecto, sin que la imagen final los necesite para nada. Los extras de `uvicorn[standard]`, que son `uvloop`, `websockets`, `httptools` y `watchfiles`, agregan alrededor de 18.5MB. `curl`, instalado solo para el healthcheck, agrega 13.5MB. Y los archivos `__pycache__` y `.pyc` generados durante la instalación suman cerca de 11.8MB.

### Las cuatro optimizaciones que aplique

La primera fue eliminar `pip`, `setuptools` y `wheel` del venv que se copia al stage final, con un `rm -rf` después de `pip install`. Esto no tiene ningún costo en runtime porque la imagen final nunca instala paquetes; el único costo es que, si alguien quisiera hacer `pip install` algo dentro del contenedor final para depurar, tendría que reinstalar pip primero.

La segunda fue cambiar `uvicorn[standard]` por `uvicorn` sin extras. Aquí el costo es real: se pierde `uvloop`, que da un event loop más rápido que el `asyncio` estándar, y `httptools`, un parser HTTP escrito en C. En cambio, `websockets` y `watchfiles` no se pierden funcionalmente porque esta API no tiene endpoints de websocket ni usa `--reload`. Para validar que esto no rompía nada, agregué `pyyaml` explícitamente, porque sin `[standard]` no viene incluido y `--log-config` con un archivo YAML lo requiere, y corrí `app/main.py` completo contra un dataset de prueba, incluyendo `/analytics/*` con DuckDB, y todo respondió 200 con los datos correctos.

La tercera fue quitar `curl` del stage final y hacer que el healthcheck use Python directamente, con `python -c "import urllib.request; urllib.request.urlopen(...)"`. Esto no tiene costo funcional: `urlopen` lanza `HTTPError` para respuestas 4xx o 5xx y `URLError` si la conexión falla, y en ambos casos el proceso de Python termina con un código distinto de cero, que es justo lo que `HEALTHCHECK` necesita para marcar el contenedor como no saludable. Verifiqué en Python real que `urlopen` contra un puerto donde no hay nada escuchando efectivamente lanza `URLError`.

La cuarta fue agregar `PYTHONDONTWRITEBYTECODE=1` junto con `pip install --no-compile`, además del `find -delete` de `__pycache__` y `.pyc` que ya tenía. Esto tampoco tiene costo: el bytecode se regenera en memoria al importar si hiciera falta, y no afecta la funcionalidad.

### El resultado final fue 288MB, confirmado bajo el límite

```
docker images transacciones-api:latest
REPOSITORY          TAG       IMAGE ID       CREATED          SIZE
transacciones-api   latest    7e03050f54a7   51 seconds ago   288MB
```

El ahorro real fue de 88MB, de 376MB a 288MB. Esperaba una imagen mucho menos pesada en la planeacion, pero probablemente esto se debe a que las optimizaciones interactúan entre sí: al usar `--no-compile`, `pip install` directamente no genera los archivos `.pyc` que luego habría que borrar con `find -delete`, así que el ahorro de la cuarta optimización no se resta de las anteriores, sino que se evita desde el origen.

Con esto, "menos de 300MB" queda confirmado. El margen es de 12MB, alrededor del 4% por debajo del límite, ajustado pero cumplido.

### Otras validaciones hechas fuera de Docker

Antes de probar con el dataset real, generé un dataset de prueba de 2000 filas con el mismo schema de 8 columnas, en Parquet y en SQLite, y corrí `app/main.py` con `uvicorn`, con y sin `[standard]`, apuntando a esos archivos vía `PARQUET_PATH` y `DB_PATH`. Confirmé respuestas 200 con datos correctos en `/health`, `/analytics/summary`, `/analytics/top-merchants` con filtro de país, `/users/{id}/transactions` y `/users/{id}/stats`.

También corrí `scripts/setup.py` dos veces consecutivas sobre el mismo par de archivos Parquet y SQLite para confirmar la idempotencia, y con un dataset de 45,000 filas para confirmar que el chunking funciona correctamente, procesándose en 3 chunks de 20000, 20000 y 5000.

Y corrí `scripts/entrypoint.sh` con `dash`, que es el `/bin/sh` real de `python:3.11-slim`, validando dos casos de falla, variable no definida y archivo que no existe, ambos con código de salida 1 y mensajes distintos, además del flujo completo exitoso terminando en `/health` con 200.


## Tiempo de desarrollo

| Actividad | Tiempo aproximado |
|---|---|
| Lectura del enunciado E7 y revisión de E4 (main.py, db.py, cache.py, models.py, architecture_decision.md) | 25 min |
| Diseño de la estructura (build context, servicios setup y api, bind mount) | 20 min |
| Escritura del Dockerfile multi-stage y requirements.txt | 25 min |
| Escritura de scripts/setup.py y pruebas locales (generación de dataset, dos corridas de idempotencia) | 40 min |
| Escritura de scripts/entrypoint.sh y validación de variables de entorno | 15 min |
| docker-compose.yml, .env.example y .dockerignore | 20 min |
| Pruebas de app/main.py con uvicorn fuera de Docker (health, summary, top-merchants, users) | 20 min |
| log_config.yaml y verificación de logging JSON | 25 min |
| README y diagrama de arquitectura | 30 min |
| Primera versión de decisions.md | 25 min |
| Revisión posterior: detección y corrección del problema de chunking en setup.py | 35 min |
| Validación: resolución de CRLF, archivos vacíos, conflicto de tags, y la ronda de optimización de tamaño de imagen de 376MB a 288MB | 90 min |
| **Total** | **~6h10min** |

Buena parte del tiempo se fue en la validación con Docker, que trajo varias sorpresas, archivos que llegaron vacíos, terminadores de línea, y un conflicto de tags al construir dos servicios en paralelo, que no aparecen al revisar solo el código.