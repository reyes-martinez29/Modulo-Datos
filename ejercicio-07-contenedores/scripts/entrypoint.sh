#!/bin/sh
# scripts/entrypoint.sh
#
# Punto de entrada del contenedor 'api'.
#
# app/main.py usa os.getenv() con defaults relativos ("../../data/...")
# que no tienen sentido dentro del contenedor (el working dir es /app y
# no existe /app/../../data). Por eso este script:
#
#   1. Verifica que PARQUET_PATH y DB_PATH estén definidas explícitamente.
#      Si falta alguna, falla inmediatamente con un mensaje claro en stdout
#      y código de salida distinto de 0 -- en vez de dejar que main.py
#      arranque con un default que apunta a una ruta inexistente y falle
#      con un FileNotFoundError menos descriptivo 30 segundos después.
#   2. Verifica que los archivos referenciados existen en el volumen montado.
#   3. Arranca uvicorn.
#
# ANALYTICS_TTL tiene un default razonable (300) en main.py y no es
# necesario para que el sistema funcione, así que no se valida aquí.

set -e

required_vars="PARQUET_PATH DB_PATH"

for var in $required_vars; do
    eval "value=\$$var"
    if [ -z "$value" ]; then
        echo "ERROR: la variable de entorno '$var' es requerida y no está definida." >&2
        echo "Revisa tu archivo .env -- compara contra .env.example." >&2
        exit 1
    fi
done

if [ ! -f "$PARQUET_PATH" ]; then
    echo "ERROR: PARQUET_PATH='$PARQUET_PATH' no existe dentro del contenedor." >&2
    echo "Verifica que el volumen de datos esté montado y que el servicio 'setup' haya corrido." >&2
    exit 1
fi

if [ ! -f "$DB_PATH" ]; then
    echo "ERROR: DB_PATH='$DB_PATH' no existe dentro del contenedor." >&2
    echo "El servicio 'setup' debe correr antes de 'api' (ver docker-compose.yml)." >&2
    exit 1
fi

echo "Variables de entorno OK. PARQUET_PATH=$PARQUET_PATH DB_PATH=$DB_PATH ANALYTICS_TTL=${ANALYTICS_TTL:-300}"

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-config log_config.yaml