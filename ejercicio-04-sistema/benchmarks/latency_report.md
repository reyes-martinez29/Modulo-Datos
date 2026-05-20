# Benchmark de latencia — Reporte comparativo

Corridas analizadas: **3**

| Corrida | Timestamp | Requests |
|---------|-----------|----------|
| run1 | 2026-05-20T03:05:19 | 100 |
| run2 | 2026-05-20T03:06:13 | 100 |
| run3 | 2026-05-20T03:06:29 | 100 |

---

## Resultados por endpoint

### GET /analytics/summary

**COLD** — SLA <500ms

| Métrica | Valor |
|---------|-------|
| p50 promedio entre corridas | 39.61ms |
| p99 promedio entre corridas | 46.28ms |
| p99 mínimo observado | 43.34ms |
| p99 máximo observado | 51.41ms |
| Corridas que pasan SLA | 3/3 (100%) |
| Veredicto | **PASS** |

Detalle por corrida:

| Corrida | p50 | p95 | p99 | SLA |
|---------|----:|----:|----:|:---:|
| run1 | 39.89ms | 45.03ms | 51.41ms | PASS |
| run2 | 39.41ms | 42.15ms | 44.08ms | PASS |
| run3 | 39.52ms | 42.16ms | 43.34ms | PASS |

**WARM** — SLA <20ms

| Métrica | Valor |
|---------|-------|
| p50 promedio entre corridas | 0.7ms |
| p99 promedio entre corridas | 1.08ms |
| p99 mínimo observado | 0.92ms |
| p99 máximo observado | 1.39ms |
| Corridas que pasan SLA | 3/3 (100%) |
| Veredicto | **PASS** |

Detalle por corrida:

| Corrida | p50 | p95 | p99 | SLA |
|---------|----:|----:|----:|:---:|
| run1 | 0.79ms | 1.14ms | 1.39ms | PASS |
| run2 | 0.67ms | 0.76ms | 0.94ms | PASS |
| run3 | 0.65ms | 0.79ms | 0.92ms | PASS |

---

### GET /analytics/top-merchants (limit=10)

**COLD** — SLA <500ms

| Métrica | Valor |
|---------|-------|
| p50 promedio entre corridas | 17.66ms |
| p99 promedio entre corridas | 19.89ms |
| p99 mínimo observado | 18.8ms |
| p99 máximo observado | 21.39ms |
| Corridas que pasan SLA | 3/3 (100%) |
| Veredicto | **PASS** |

Detalle por corrida:

| Corrida | p50 | p95 | p99 | SLA |
|---------|----:|----:|----:|:---:|
| run1 | 18.21ms | 20.02ms | 21.39ms | PASS |
| run2 | 17.4ms | 18.75ms | 19.48ms | PASS |
| run3 | 17.36ms | 18.43ms | 18.8ms | PASS |

**WARM** — SLA <20ms

| Métrica | Valor |
|---------|-------|
| p50 promedio entre corridas | 0.67ms |
| p99 promedio entre corridas | 0.92ms |
| p99 mínimo observado | 0.85ms |
| p99 máximo observado | 1.0ms |
| Corridas que pasan SLA | 3/3 (100%) |
| Veredicto | **PASS** |

Detalle por corrida:

| Corrida | p50 | p95 | p99 | SLA |
|---------|----:|----:|----:|:---:|
| run1 | 0.66ms | 0.84ms | 0.9ms | PASS |
| run2 | 0.66ms | 0.79ms | 0.85ms | PASS |
| run3 | 0.68ms | 0.81ms | 1.0ms | PASS |

---

### GET /analytics/top-merchants (limit=10, country=MX)

**COLD** — SLA <500ms

| Métrica | Valor |
|---------|-------|
| p50 promedio entre corridas | 17.85ms |
| p99 promedio entre corridas | 21.65ms |
| p99 mínimo observado | 20.22ms |
| p99 máximo observado | 24.5ms |
| Corridas que pasan SLA | 3/3 (100%) |
| Veredicto | **PASS** |

Detalle por corrida:

| Corrida | p50 | p95 | p99 | SLA |
|---------|----:|----:|----:|:---:|
| run1 | 17.88ms | 19.14ms | 24.5ms | PASS |
| run2 | 17.79ms | 19.75ms | 20.24ms | PASS |
| run3 | 17.87ms | 19.44ms | 20.22ms | PASS |

**WARM** — SLA <20ms

| Métrica | Valor |
|---------|-------|
| p50 promedio entre corridas | 0.68ms |
| p99 promedio entre corridas | 1.0ms |
| p99 mínimo observado | 0.97ms |
| p99 máximo observado | 1.06ms |
| Corridas que pasan SLA | 3/3 (100%) |
| Veredicto | **PASS** |

Detalle por corrida:

| Corrida | p50 | p95 | p99 | SLA |
|---------|----:|----:|----:|:---:|
| run1 | 0.66ms | 0.81ms | 0.98ms | PASS |
| run2 | 0.7ms | 0.84ms | 1.06ms | PASS |
| run3 | 0.69ms | 0.82ms | 0.97ms | PASS |

---

---

## Análisis de resultados

Los tres endpoints analíticos pasaron el SLA en las 3 corridas tanto en condición cold como warm, lo que indica que el sistema es estable y no hubo corridas atípicas que distorsionen los resultados.

**Condición cold** — el cache está vacío y la query llega a DuckDB:

El p99 máximo observado en todas las corridas fue de **51.41ms**, muy por debajo del SLA de 500ms. El p50 promedio fue de 25.04ms, lo que significa que la mitad de los requests cold terminan en menos de 25.04ms. La varianza entre corridas es baja, lo que confirma que los tiempos son estables y no dependen del estado puntual del sistema en el momento de la medición.

Estos tiempos son posibles porque DuckDB tiene la conexión al Parquet inicializada desde el arranque del servidor en el lifespan de FastAPI. Cada request solo paga el costo de ejecutar la query sobre el archivo ya abierto. Si la conexión se abriera en cada request el cold p50 sería aproximadamente 88ms más alto por el overhead de apertura del Parquet, medido en el Ejercicio 3.

**Condición warm** — el resultado viene del cache en memoria:

El p50 promedio en warm fue de **0.68ms**, una reducción de **56x** respecto al cold en el mejor caso (`GET /analytics/summary`). El p99 warm nunca superó los 2ms en ninguna corrida, muy por debajo del SLA de 20ms. Esto confirma que el path caliente no tiene ninguna operación de I/O: es una lectura de un diccionario Python en memoria, con costo de microsegundos.

La baja varianza del warm entre corridas es la evidencia más directa de que el cache funciona correctamente. Si algún request warm tocara la base de datos, ese outlier aparecería en el p99 de esa corrida.

---

## Cómo interpretar este reporte

El **p50 promedio** es la latencia que experimenta la mitad de los usuarios en condiciones normales. Si varía mucho entre corridas hay inestabilidad en el sistema.

El **p99 máximo** es el peor caso observado en todas las corridas. Es el número que hay que comparar contra el SLA porque el SLA define el comportamiento en el peor caso aceptable, no el caso promedio.

Un **100% de corridas PASS** significa que el sistema cumple el SLA de forma consistente. Si hay corridas FAIL hay que investigar qué pasó en esas corridas específicas antes de concluir que el sistema falla el SLA.

La diferencia entre cold y warm refleja el impacto del cache con TTL=300s. En producción los endpoints analíticos casi siempre sirven desde el cache porque el TTL es de 5 minutos y las queries llegan con más frecuencia que eso.