#!/bin/sh
# scripts/entrypoint.sh — valida variables de entorno y arranca uvicorn.
#
# PARQUET_PATH y DB_PATH son requeridas. Si falta alguna o el archivo no
# existe, falla con un mensaje claro antes de arrancar el servidor, en vez
# de fallar de forma confusa en el primer request. (app/config.py tambien
# valida esto al importar; esta es la primera linea de defensa, mas rapida
# y especifica al contexto Docker.)

set -e

for var in PARQUET_PATH DB_PATH; do
    eval "value=\$$var"
    if [ -z "$value" ]; then
        echo "ERROR: la variable de entorno '$var' es requerida y no esta definida." >&2
        echo "Revisa tu archivo .env -- comparalo con .env.example." >&2
        exit 1
    fi
done

if [ ! -f "$PARQUET_PATH" ]; then
    echo "ERROR: PARQUET_PATH='$PARQUET_PATH' no existe dentro del contenedor." >&2
    echo "Verifica que el volumen de datos este montado." >&2
    exit 1
fi

if [ ! -f "$DB_PATH" ]; then
    echo "ERROR: DB_PATH='$DB_PATH' no existe dentro del contenedor." >&2
    echo "El servicio 'setup' debe correr antes de 'api' (ver docker-compose.yml)." >&2
    exit 1
fi

echo "Variables de entorno OK. PARQUET_PATH=$PARQUET_PATH DB_PATH=$DB_PATH ANALYTICS_TTL=${ANALYTICS_TTL:-300}"

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-config log_config.yaml