# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------
# Stage 1 — build: instala dependencias en un venv aislado
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS build

WORKDIR /build

# Dependencias de compilación solo necesarias para construir wheels
# (duckdb y pyarrow traen wheels precompilados para la mayoría de plataformas,
# pero build-essential evita fallos si pip necesita compilar algo localmente).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Creamos un venv dentro de /build para copiarlo completo al stage final.
# Esto evita arrastrar pip, setuptools y cachés de compilación a la imagen final.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

ENV PYTHONDONTWRITEBYTECODE=1

COPY ejercicio-07-contenedores/requirements.txt .
RUN pip install --no-cache-dir --no-compile -r requirements.txt && \
    # pip y setuptools quedan instalados dentro del venv por defecto
    # (17MB y 11MB respectivamente, medido con `du -sh` en el venv real --
    # ver decisions.md). No se necesitan en runtime: la app no instala
    # paquetes en producción. Se eliminan del venv que se copia al stage
    # final. find -exec rm limpia __pycache__ y *.pyc residuales de la
    # instalación, que tampoco se necesitan en runtime.
    rm -rf /opt/venv/lib/python3.11/site-packages/pip \
           /opt/venv/lib/python3.11/site-packages/pip-* \
           /opt/venv/lib/python3.11/site-packages/setuptools \
           /opt/venv/lib/python3.11/site-packages/setuptools-* \
           /opt/venv/lib/python3.11/site-packages/wheel \
           /opt/venv/lib/python3.11/site-packages/wheel-* \
           /opt/venv/bin/pip* && \
    find /opt/venv -type d -name "__pycache__" -exec rm -rf {} + && \
    find /opt/venv -type f -name "*.pyc" -delete


# ---------------------------------------------------------------------------
# Stage 2 — final: imagen mínima de runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS final

# Imagen final NO instala curl (13.5MB medido con `docker history`).
# El HEALTHCHECK usa el Python del venv (ya presente) con urllib en
# lugar de un binario externo -- ahorra 13.5MB sin perder funcionalidad:
# falla igual con exit != 0 si /health no responde o no retorna 2xx.

# Usuario sin privilegios — la app no necesita root para correr.
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Copiamos el venv ya armado desde el stage de build.
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Solo el código de la aplicación (paquete app/ del E4). NO se copian
# .parquet, .db ni .csv — esos se montan como volumen en runtime
# (ver docker-compose.yml).
#
# El build context de este Dockerfile es la RAIZ del repo (ver
# docker-compose.yml: context: .., dockerfile: ejercicio-07-contenedores/Dockerfile)
# por eso las rutas de origen empiezan en ejercicio-04-sistema/ y
# ejercicio-07-contenedores/.
COPY ejercicio-04-sistema/app/ ./app/
COPY ejercicio-07-contenedores/scripts/entrypoint.sh ./scripts/entrypoint.sh
COPY ejercicio-07-contenedores/scripts/setup.py ./scripts/setup.py
COPY ejercicio-07-contenedores/log_config.yaml ./log_config.yaml
RUN chmod +x ./scripts/entrypoint.sh

USER appuser

EXPOSE 8000

# HEALTHCHECK: cada 30s llama a /health vía Python (urllib), sin depender
# de curl. Si /health no responde en 5s o retorna != 200, urlopen lanza
# excepción -> el proceso Python sale con código != 0 -> "unhealthy".
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)" || exit 1

ENTRYPOINT ["./scripts/entrypoint.sh"]