# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------
# Stage 1 — build: instala dependencias en un venv aislado
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS build

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1

COPY ejercicio-08-final/requirements.txt .
RUN pip install --no-cache-dir --no-compile -r requirements.txt && \
    # Pre-instalar sqlite_scanner mientras pip esta disponible.
    # Se usa la ruta absoluta al Python del venv para garantizar el modulo correcto.
    /opt/venv/bin/python -c "import duckdb; c=duckdb.connect(); c.execute('INSTALL sqlite;'); print('sqlite_scanner preinstalada')" && \
    rm -rf /opt/venv/lib/python3.11/site-packages/pip \
           /opt/venv/lib/python3.11/site-packages/pip-* \
           /opt/venv/lib/python3.11/site-packages/setuptools \
           /opt/venv/lib/python3.11/site-packages/setuptools-* \
           /opt/venv/lib/python3.11/site-packages/wheel \
           /opt/venv/lib/python3.11/site-packages/wheel-* \
           /opt/venv/bin/pip* \
           /opt/venv/lib/python3.11/site-packages/pkg_resources && \
    find /opt/venv -type d -name "__pycache__" -exec rm -rf {} + && \
    find /opt/venv -type f -name "*.pyc" -delete


# ---------------------------------------------------------------------------
# Stage 2 — final: imagen minima de runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS final

# Usuario sin privilegios.
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1

# Codigo de la aplicacion.
COPY ejercicio-08-final/app/ ./app/
COPY ejercicio-08-final/pipeline/ ./pipeline/
COPY ejercicio-08-final/scripts/entrypoint.sh ./scripts/entrypoint.sh
COPY ejercicio-08-final/scripts/setup.py ./scripts/setup.py
COPY ejercicio-08-final/log_config.yaml ./log_config.yaml
RUN chmod +x ./scripts/entrypoint.sh

# Copiar la extension sqlite_scanner preinstalada desde el stage de build.
# Se copia directamente al home de appuser en una sola capa, sin chown posterior,
# para evitar la capa duplicada que sumaba ~34MB extra (COPY 34MB + chown 34MB).
COPY --from=build --chown=appuser:appuser /root/.duckdb /home/appuser/.duckdb

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)" || exit 1

ENTRYPOINT ["./scripts/entrypoint.sh"]