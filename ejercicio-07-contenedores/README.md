# Ejercicio 7 — De tu Máquina al Mundo

Configuración Docker completa para el sistema del Ejercicio 4 (API FastAPI
con DuckDB + SQLite). Un solo `docker compose up --build` levanta la base
de datos y la API, sin dependencias adicionales más que Docker.

## Qué sistema se contenerizó: E4, no E5

El enunciado deja elegir entre E4 (FastAPI) y E5 (Django REST). Se eligió
**E4** porque su único requisito de arranque es que existan
`PARQUET_PATH` y `DB_PATH` en disco — exactamente el tipo de dependencia
que un servicio `setup` de un solo uso puede resolver de forma simple.

El E5, en cambio, tiene un flujo de arranque con varios pasos y efectos
secundarios: migraciones (`manage.py migrate`), un comando de carga
(`load_transactions`) que según su propio decisions.md tarda alrededor de
138 segundos con 1M filas, y administra su propia base de datos separada
de la del E3. Empaquetar eso en el servicio `setup` implicaría orquestar
varios pasos secuenciales, y cada paso adicional es una oportunidad más de
que algo falle en el "un solo comando desde cero" que pide el enunciado.

Si en una iteración futura se quiere contenerizar E5 en lugar de (o además
de) E4, el servicio `setup` cambiaría de "generar SQLite desde Parquet" a
"correr `manage.py migrate` + `manage.py load_transactions`", y el
`Dockerfile` cambiaría su `ENTRYPOINT` de `uvicorn app.main:app` a
`gunicorn config.wsgi` o `uvicorn` con el ASGI de Django. La estructura de
dos servicios (`setup` + `api`) compartiendo un volumen se mantiene igual.

## Prerequisitos

- Docker y Docker Compose v2 (`docker compose version` debe funcionar).
- El Parquet de E1 (`transactions_1m_parquet_snappy.parquet`), generado
  con `ejercicio-01-formatos/benchmark_cli.py --size 1m`.

## Paso 0 — Preparar `.env`, `.dockerignore` y los datos

```powershell
# Desde ejercicio-07-contenedores/
Copy-Item .env.example .env

# .dockerignore debe vivir en la RAIZ del repo (ver nota en el archivo) --
# docker-compose.yml usa context: .. (la raiz del repo como build context),
# y Docker solo respeta .dockerignore en la raiz del contexto.
Copy-Item .dockerignore ..\.dockerignore -Force

New-Item -ItemType Directory -Force -Path .\data
Copy-Item ..\data\transactions_1m_parquet_snappy.parquet .\data\
```

```bash
# bash equivalente
cp .env.example .env
cp .dockerignore ../.dockerignore
mkdir -p ./data
cp ../data/transactions_1m_parquet_snappy.parquet ./data/
```

`./data` queda como bind mount compartido entre `setup` y `api`. El
servicio `setup` generará `transactions.db` ahí mismo a partir del
Parquet — no es necesario traer una SQLite preexistente.

## Cómo levantar el sistema desde cero

```bash
docker compose up --build
```

Esto construye la imagen (`Dockerfile` multi-stage, build context = raíz
del repo), corre `setup` hasta que termine con éxito (genera
`./data/transactions.db` con el índice `idx_user_timestamp` si no existe
ya — es idempotente), y luego arranca `api` en el puerto 8000.

## Cómo verificar que está corriendo

```bash
docker compose ps
curl http://localhost:8000/health
```

Respuesta esperada de `/health`:

```json
{"status":"ok","uptime_seconds":1.23,"cache_hit_rate":0.0,"cache_hits":0,"cache_misses":0,"duckdb_connected":true,"sqlite_connected":true}
```

Otros endpoints para probar (ver `ejercicio-04-sistema/README.md` para el
contrato completo):

```bash
curl http://localhost:8000/analytics/summary
curl "http://localhost:8000/analytics/top-merchants?limit=5&country=MX"
curl http://localhost:8000/users/1/transactions
curl http://localhost:8000/users/1/stats
```

## Cómo ver los logs en tiempo real

```bash
docker compose logs -f api
```

Cada línea de log de uvicorn aparece en stdout del contenedor; `docker
compose logs` las captura tal cual.

## Cómo parar y limpiar todo

```bash
docker compose down -v
```

`-v` elimina los volúmenes anónimos creados por compose (no afecta
`./data`, que es un bind mount a tu disco — bórralo manualmente con
`rm -rf ./data` o `Remove-Item -Recurse .\data` si quieres limpiar
también la SQLite generada).

## Variables de entorno (`.env.example`)

| Variable | Default | Requerida | Descripción |
|---|---|---|---|
| `PARQUET_PATH` | `/data/transactions_1m_parquet_snappy.parquet` | Sí | Ruta dentro del contenedor al Parquet de E1 |
| `DB_PATH` | `/data/transactions.db` | Sí | Ruta dentro del contenedor a la SQLite de E3/setup |
| `ANALYTICS_TTL` | `300` | No | TTL del cache de `/analytics/*` en segundos |

Si `PARQUET_PATH` o `DB_PATH` faltan en `.env`, `scripts/entrypoint.sh`
falla antes de arrancar uvicorn con un mensaje explícito
(`ERROR: la variable de entorno '...' es requerida...`). Si están
definidas pero el archivo no existe en el volumen montado, también falla
con un mensaje que indica revisar el montaje o el servicio `setup`.

## Health check

El `Dockerfile` define:

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1
```

`docker compose ps` muestra el estado (`healthy` / `unhealthy` /
`starting`) de `api` una vez transcurrido el `start-period`.

## Logging en JSON

`scripts/entrypoint.sh` arranca uvicorn con `--log-config log_config.yaml`,
que reemplaza los formatters por defecto de `uvicorn.error` y
`uvicorn.access` por un formato JSON de una línea por evento:
`{"timestamp": "...", "level": "...", "logger": "...", "message": "..."}`.
Esto cubre todos los logs de arranque, apagado y acceso HTTP sin modificar
`app/main.py`.

**Limitación conocida:** `app/main.py` (E4) usa dos `print()` directos en
el `lifespan` (`"Servidor listo — ..."` y `"Conexiones cerradas."`). Esas
dos líneas salen como texto plano, no como JSON, porque `print()` no pasa
por el sistema de `logging` que configura `log_config.yaml`. Verificado
localmente: de 10 líneas de log en un ciclo de arranque/health/apagado, 8
son JSON válido y 2 (los `print()` de `main.py`) son texto plano. Corregir
esto requeriría cambiar esos `print()` por `logging.getLogger("uvicorn.error").info(...)`
en `app/main.py` — código ya evaluado del E4, fuera del alcance de este
ejercicio.

## Troubleshooting — permisos de escritura en `./data`

El contenedor corre como usuario sin privilegios `appuser` (UID 1000,
creado en el Dockerfile con `useradd`). El servicio `setup` necesita
**escribir** `transactions.db` en `/data` (el bind mount de `./data`).

En Docker Desktop para Windows (WSL2 o Hyper-V) esto normalmente funciona
sin configuración adicional -- el backend de Docker Desktop no aplica
permisos POSIX estrictos a los bind mounts de la misma forma que Linux
nativo. **No se pudo verificar esto en el entorno donde se preparó esta
entrega** (sin Docker disponible), así que si `setup` falla con un error
de permisos al crear `transactions.db` (algo como
`sqlite3.OperationalError: unable to open database file` o
`PermissionError: [Errno 13]`), las soluciones en orden de preferencia son:

1. **Verificar que `./data` tiene permisos de escritura para todos**:
   ```powershell
   # PowerShell -- dar permisos de escritura a la carpeta data
   icacls .\data /grant Everyone:F /T
   ```

2. **Si corres en Linux/WSL2 directamente** (no Docker Desktop GUI), igualar
   el UID del contenedor con el tuyo al construir:
   ```bash
   docker compose build --build-arg UID=$(id -u) --build-arg GID=$(id -g)
   ```
   (Esto requeriría agregar `ARG UID=1000` / `ARG GID=1000` y usarlos en
   `useradd -u $UID -g $GID` en el Dockerfile -- no incluido en esta
   versión porque no se pudo probar; ver `decisions.md`.)

3. **Como último recurso para diagnosticar** (no para producción): correr
   temporalmente como root para confirmar que el problema es de permisos
   y no de otra cosa:
   ```powershell
   docker compose run --user root setup
   ```
   Si esto funciona y la versión normal no, el problema es de UID/permisos
   del bind mount, no del código.

```
ejercicio-07-contenedores/
├── Dockerfile              multi-stage, build context = raíz del repo
├── docker-compose.yml      servicios setup + api, bind mount ./data
├── .env.example
├── .dockerignore
├── requirements.txt        deps de runtime (sin pyarrow, sin pandas/django)
├── log_config.yaml         formato JSON para logs de uvicorn
├── scripts/
│   ├── entrypoint.sh        valida env vars y arranca uvicorn
│   └── setup.py             genera SQLite desde Parquet, idempotente
├── decisions.md
└── README.md               este archivo
```

## Tamaño de la imagen

No fue posible correr `docker build` en el entorno donde se preparó esta
entrega (sin daemon Docker disponible). La estimación basada en
`python:3.11-slim` (~125-130MB) + dependencias de runtime instaladas
(`fastapi`, `uvicorn[standard]`, `duckdb`, `pydantic` ≈ 14MB sin pyarrow,
medido con `pip show` + suma de tamaños de `site-packages`) sitúa la
imagen final en un rango aproximado de 180-220MB, por debajo del límite
de 300MB. Esta cifra debe confirmarse con `docker images
transacciones-api:latest` en una máquina con Docker — ver `decisions.md`
para el detalle de esta limitación.