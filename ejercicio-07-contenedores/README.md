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
(`load_transactions`) que tarda alrededor de
138 segundos con 1M filas, y administra su propia base de datos separada.
Empaquetar eso en el servicio `setup` implicaría orquestar
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
ejercicio. (lo voy a corregir eventualmente)


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

