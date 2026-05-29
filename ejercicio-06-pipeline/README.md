# Ejercicio 6 — El Pipeline de Datos

Pipeline ETL que simula la llegada continua de transacciones con calidad
variable, las valida, las transforma y las carga en la base SQLite del E3.

---

## Comando único para correr el pipeline completo

```bash
cd ejercicio-06-pipeline
python pipeline.py
```

Con parámetros explícitos:

```bash
python pipeline.py --batch-size 1000 --error-rate 0.15
```

Con seed para resultados reproducibles:

```bash
python pipeline.py --seed 42 --batch-size 500 --error-rate 0.10
```

---

## Prerequisitos

La base de datos del E3 debe existir:

```bash
cd ejercicio-03-sqlite
python ingest.py --wal --chunk-size 20000
```

---

## Tests

```bash
pytest tests/ -v
```

La suite incluye 22 tests que verifican cada capa de forma independiente
y el pipeline completo. El test más importante es `test_pipeline_idempotent`:
corre el pipeline dos veces con los mismos datos y verifica que la segunda
corrida inserta 0 filas y el conteo en la base no cambia.

---

## Arquitectura — separación de responsabilidades

El pipeline tiene cuatro capas con responsabilidades estrictamente separadas:

```
data_source.py  →  extract.py  →  transform.py  →  load.py
                                       ↓
                               quarantine/YYYY-MM-DD.jsonl
```

| Archivo | Responsabilidad | Lo que NO hace |
|---------|----------------|----------------|
| `data_source.py` | Genera batches con errores intencionales | No pertenece al pipeline — es la fuente externa |
| `extract.py` | Normaliza tipos y formatos | No valida reglas de negocio |
| `transform.py` | Valida reglas de negocio | No normaliza formatos |
| `load.py` | Inserta en SQLite con idempotencia | No valida datos |
| `pipeline.py` | Orquesta las capas y genera el reporte | No tiene lógica de datos |

La distinción entre `extract.py` y `transform.py` es la más importante:
un `amount=-50.0` pasa por `extract.py` sin cambios (es un float válido)
y es rechazado por `transform.py` (viola la regla de negocio).

---

## Reglas de validación (transform.py)

| Regla | Criterio | Tipo en reporte |
|-------|----------|-----------------|
| amount en rango | Entre 0.01 y 5,000.00 | `amount_out_of_range` |
| category válida | 10 valores del schema | `invalid_category` |
| country_code válido | 15 países del schema | `invalid_country` |
| timestamp no futuro | Máximo 1 hora de adelanto | `future_timestamp` |
| transaction_id UUID4 | Formato UUID4 estándar | `invalid_transaction_id` |
| campos requeridos | Ninguno puede ser None | `null_field` |

---

## Cuarentena

Las filas rechazadas van a `quarantine/YYYY-MM-DD.jsonl` con append.
Múltiples corridas del mismo día acumulan en el mismo archivo.

Cada línea del JSONL contiene la fila completa más:
- `rejection_reason`: motivo exacto y específico del rechazo
- `rejection_type`: categoría del error para el reporte

Ejemplo de línea en cuarentena:

```json
{
  "transaction_id": "550e8400-e29b-41d4-a716-446655440001",
  "amount": -99.5,
  "rejection_reason": "amount=-99.5 fuera del rango [0.01, 5000.0]",
  "rejection_type": "amount_out_of_range"
}
```

---

## Reporte de ejecución

Cada corrida genera `results/run_YYYYMMDD_HHMMSS.json`:

```json
{
  "run_id": "20240315_143022",
  "timestamp": "2024-03-15 14:30:22",
  "params": {
    "batch_size": 500,
    "error_rate": 0.1,
    "seed": 42,
    "db_path": "../../data/transactions.db"
  },
  "extracted": 500,
  "parse_errors": 0,
  "valid": 450,
  "rejected": 50,
  "by_error": {
    "amount_out_of_range": 10,
    "invalid_category": 10,
    "future_timestamp": 10,
    "null_field": 10,
    "invalid_transaction_id": 10
  },
  "inserted": 450,
  "duplicates": 0,
  "quarantine_file": "quarantine/2024-03-15.jsonl",
  "total_time_s": 0.842,
  "invariants": {
    "extracted_eq_valid_plus_rejected": true,
    "inserted_plus_duplicates_eq_valid": true
  }
}
```

### Invariantes matemáticas que siempre se cumplen

```
extracted == valid + rejected
inserted + duplicates == valid
```

El campo `invariants` del reporte confirma explícitamente que los números
cuadran. Si alguna invariante se rompe, el pipeline lanza una excepción
antes de guardar el reporte.

---

## Idempotencia

Correr el pipeline dos veces con los mismos datos produce el mismo
resultado final:

```bash
python pipeline.py --seed 42 --batch-size 500
# inserted: 450, duplicates: 0

python pipeline.py --seed 42 --batch-size 500
# inserted: 0, duplicates: 450  ← misma DB, ningún dato duplicado
```

La idempotencia está garantizada por `INSERT OR IGNORE` en `load.py`
y verificada empíricamente en `test_pipeline_idempotent`.

---

## Estructura de archivos

```
ejercicio-06-pipeline/
├── data_source.py          genera batches con errores intencionales
├── extract.py              normaliza tipos y formatos
├── transform.py            valida reglas de negocio + cuarentena
├── load.py                 INSERT OR IGNORE transaccional en SQLite
├── pipeline.py             orquestador + reporte JSON
├── quarantine/             JSONL de filas rechazadas (en .gitignore)
├── results/                JSON de reportes por corrida (en .gitignore)
├── tests/
│   └── test_pipeline.py    22 tests incluyendo idempotencia
└── README.md
```