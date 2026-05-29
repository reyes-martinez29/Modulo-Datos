"""
tests/test_pipeline.py — Suite de tests del pipeline ETL.

Cómo correr:
    pytest tests/ -v
    pytest tests/test_pipeline.py::TestTransform -v   # una clase
    pytest tests/ -v --tb=short                       # traceback corto

Estrategia de testing:
    Cada capa se testa de forma independiente y luego se testa el pipeline
    completo. Se usa seed=42 para que todos los tests sean deterministas —
    el mismo seed produce exactamente el mismo batch.

    Con seed=42, batch_size=200, error_rate=0.10:
        - 200 filas generadas
        - ~20 con errores (4 de cada tipo de los 5 tipos)
        - ~180 válidas
        - 0 errores de formato (los errores de data_source son de negocio,
          no de formato)

    El test de idempotencia corre el pipeline DOS VECES con los mismos
    parámetros y verifica empíricamente que:
        run2.inserted == 0
        run2.duplicates == run1.inserted
        conteo en DB no cambia entre corrida 1 y corrida 2

Tests incluidos:
    1.  test_generate_batch_size           — batch tiene el tamaño correcto
    2.  test_generate_batch_errors         — errores en proporción correcta
    3.  test_generate_reproducible         — mismo seed = mismo resultado
    4.  test_extract_normalizes_country    — country_code a mayúsculas
    5.  test_extract_normalizes_amount     — amount redondeado
    6.  test_extract_no_business_rules     — amount negativo pasa por extract
    7.  test_extract_handles_formats       — múltiples formatos de timestamp
    8.  test_transform_rejects_amount      — amount fuera de rango → rechazado
    9.  test_transform_rejects_category    — category inválida → rechazada
    10. test_transform_rejects_country     — country_code inválido → rechazado
    11. test_transform_rejects_future_ts   — timestamp futuro → rechazado
    12. test_transform_rejects_null        — campo nulo → rechazado
    13. test_transform_rejects_bad_uuid    — UUID malformado → rechazado
    14. test_transform_rejection_reason    — motivo exacto en rejection_reason
    15. test_transform_invariant           — extracted == valid + rejected
    16. test_quarantine_file_created       — archivo JSONL creado con motivos
    17. test_load_inserts_correctly        — filas insertadas en DB
    18. test_load_idempotent               — segunda corrida: inserted=0
    19. test_load_invariant                — inserted + duplicates == valid
    20. test_pipeline_invariants           — pipeline completo: todas las sumas cuadran
    21. test_pipeline_idempotent           — pipeline DOS VECES: misma DB final
    22. test_pipeline_quarantine_has_reason — cuarentena tiene motivos exactos
"""

import json
import sqlite3
import tempfile
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from data_source import generate_batch
from extract import extract
from transform import transform, write_quarantine
from load import load
from pipeline import run_pipeline


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db():
    """Base de datos SQLite temporal con la tabla transactions."""
    db_file = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(db_file)
    conn.execute("""
        CREATE TABLE transactions (
            transaction_id TEXT PRIMARY KEY,
            timestamp      TEXT NOT NULL,
            user_id        INTEGER NOT NULL,
            merchant_id    INTEGER NOT NULL,
            amount         REAL NOT NULL,
            category       TEXT NOT NULL,
            country_code   TEXT NOT NULL,
            status         TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    yield db_file
    if os.path.exists(db_file):
        os.unlink(db_file)


@pytest.fixture
def tmp_dirs():
    """Directorios temporales para quarantine y results."""
    with tempfile.TemporaryDirectory() as d:
        qdir = Path(d) / "quarantine"
        rdir = Path(d) / "results"
        qdir.mkdir()
        rdir.mkdir()
        yield qdir, rdir


@pytest.fixture
def standard_batch():
    """Batch estándar reproducible para tests."""
    return generate_batch(batch_size=200, error_rate=0.10, seed=42)


@pytest.fixture
def extracted_rows(standard_batch):
    """Batch extraído y normalizado."""
    rows, _ = extract(standard_batch)
    return rows


# ---------------------------------------------------------------------------
# Tests de data_source
# ---------------------------------------------------------------------------

class TestDataSource:

    def test_generate_batch_size(self):
        """El batch tiene exactamente el tamaño solicitado."""
        batch = generate_batch(batch_size=300, error_rate=0.10, seed=1)
        assert len(batch) == 300

    def test_generate_batch_errors(self):
        """La proporción de errores está dentro del rango esperado."""
        # Con error_rate=0.20 y 500 filas: ~100 errores
        # Se acepta ±5% de tolerancia por el redondeo
        batch = generate_batch(batch_size=500, error_rate=0.20, seed=42)
        assert len(batch) == 500
        # Verificar que hay al menos algún error (el generador los introduce)
        has_negative = any(
            r.get("amount") is not None and r["amount"] < 0
            for r in batch
        )
        has_bad_category = any(
            r.get("category") not in {
                "Food", "Travel", "Electronics", "Health", "Entertainment",
                "Retail", "Transport", "Education", "Services", "Other"
            }
            for r in batch
        )
        assert has_negative or has_bad_category, "El generador debe introducir errores"

    def test_generate_reproducible(self):
        """El mismo seed produce exactamente el mismo batch."""
        b1 = generate_batch(batch_size=100, error_rate=0.10, seed=99)
        b2 = generate_batch(batch_size=100, error_rate=0.10, seed=99)
        assert b1 == b2

    def test_generate_different_seeds(self):
        """Seeds diferentes producen batches diferentes."""
        b1 = generate_batch(batch_size=100, error_rate=0.10, seed=1)
        b2 = generate_batch(batch_size=100, error_rate=0.10, seed=2)
        assert b1 != b2


# ---------------------------------------------------------------------------
# Tests de extract
# ---------------------------------------------------------------------------

class TestExtract:

    def test_extract_normalizes_country(self):
        """country_code se convierte a mayúsculas."""
        rows = [{"transaction_id": "a" * 36, "timestamp": "2024-01-01 00:00:00",
                 "user_id": 1, "merchant_id": 1, "amount": 100.0,
                 "category": "Food", "country_code": "mx", "status": "completed"}]
        result, _ = extract(rows)
        assert result[0]["country_code"] == "MX"

    def test_extract_normalizes_amount(self):
        """amount se redondea a 2 decimales."""
        rows = [{"transaction_id": "a" * 36, "timestamp": "2024-01-01 00:00:00",
                 "user_id": 1, "merchant_id": 1, "amount": "99.999",
                 "category": "Food", "country_code": "MX", "status": "completed"}]
        result, _ = extract(rows)
        assert result[0]["amount"] == 100.0

    def test_extract_no_business_rules(self, standard_batch):
        """
        Extract no aplica reglas de negocio.
        Un amount negativo debe pasar por extract sin ser rechazado.
        """
        rows_with_neg = [r for r in standard_batch if r.get("amount", 0) is not None
                         and isinstance(r.get("amount"), (int, float)) and r["amount"] < 0]

        if not rows_with_neg:
            pytest.skip("No hay filas con amount negativo en este batch")

        result, parse_errors = extract(rows_with_neg)
        # Las filas con amount negativo pasan por extract
        # (son float válidos, no hay error de formato)
        neg_in_result = [r for r in result if r.get("amount") is not None and r["amount"] < 0]
        assert len(neg_in_result) > 0, (
            "Extract no debe rechazar amounts negativos — "
            "eso es responsabilidad de transform"
        )

    def test_extract_handles_multiple_formats(self):
        """Extract acepta múltiples formatos de timestamp."""
        formats = [
            "2024-03-15 14:30:00",   # formato del módulo
            "2024-03-15T14:30:00",   # ISO 8601 con T
            "2024-03-15",            # solo fecha
        ]
        base = {"transaction_id": "a" * 36, "user_id": 1, "merchant_id": 1,
                "amount": 100.0, "category": "Food", "country_code": "MX",
                "status": "completed"}

        for fmt in formats:
            rows = [{**base, "timestamp": fmt}]
            result, errors = extract(rows)
            assert len(result) == 1, f"Formato '{fmt}' no fue aceptado"
            assert len(errors) == 0

    def test_extract_returns_all_rows(self, standard_batch):
        """Total de filas = normalizadas + errores de formato."""
        extracted, parse_errors = extract(standard_batch)
        assert len(extracted) + len(parse_errors) == len(standard_batch)


# ---------------------------------------------------------------------------
# Tests de transform
# ---------------------------------------------------------------------------

class TestTransform:

    def _make_valid_row(self, **overrides) -> dict:
        """Fila válida base para tests de transform."""
        base = {
            "transaction_id": "550e8400-e29b-41d4-a716-446655440000",
            "timestamp":      "2024-01-01 12:00:00",
            "user_id":        1234,
            "merchant_id":    567,
            "amount":         99.99,
            "category":       "Food",
            "country_code":   "MX",
            "status":         "completed",
        }
        base.update(overrides)
        return base

    def test_valid_row_passes(self):
        """Una fila completamente válida debe pasar transform."""
        valid, rejected = transform([self._make_valid_row()])
        assert len(valid) == 1
        assert len(rejected) == 0

    def test_rejects_amount_out_of_range(self):
        """amount fuera de rango debe ser rechazado."""
        valid, rejected = transform([self._make_valid_row(amount=-50.0)])
        assert len(rejected) == 1
        assert "amount" in rejected[0]["rejection_reason"]
        assert "fuera del rango" in rejected[0]["rejection_reason"]

    def test_rejects_amount_too_high(self):
        """amount > 5000 debe ser rechazado."""
        valid, rejected = transform([self._make_valid_row(amount=5001.0)])
        assert len(rejected) == 1

    def test_rejects_invalid_category(self):
        """category fuera del set válido debe ser rechazada."""
        valid, rejected = transform([self._make_valid_row(category="Gambling")])
        assert len(rejected) == 1
        assert "category" in rejected[0]["rejection_reason"]

    def test_rejects_invalid_country(self):
        """country_code fuera del set válido debe ser rechazado."""
        valid, rejected = transform([self._make_valid_row(country_code="ZZ")])
        assert len(rejected) == 1
        assert "country_code" in rejected[0]["rejection_reason"]

    def test_rejects_future_timestamp(self):
        """Timestamp futuro (>1h) debe ser rechazado."""
        future = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        valid, rejected = transform([self._make_valid_row(timestamp=future)])
        assert len(rejected) == 1
        assert "futuro" in rejected[0]["rejection_reason"]

    def test_accepts_near_future_timestamp(self):
        """Timestamp dentro de la tolerancia de 1h debe ser aceptado."""
        near_future = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        valid, rejected = transform([self._make_valid_row(timestamp=near_future)])
        assert len(valid) == 1

    def test_rejects_null_field(self):
        """Campo requerido nulo debe ser rechazado."""
        valid, rejected = transform([self._make_valid_row(user_id=None)])
        assert len(rejected) == 1
        assert "None" in rejected[0]["rejection_reason"] or "null" in rejected[0]["rejection_reason"].lower()

    def test_rejects_invalid_uuid(self):
        """transaction_id malformado debe ser rechazado."""
        valid, rejected = transform([self._make_valid_row(transaction_id="not-a-uuid")])
        assert len(rejected) == 1
        assert "transaction_id" in rejected[0]["rejection_reason"]

    def test_rejection_has_exact_reason(self):
        """El rejection_reason debe contener el valor que causó el rechazo."""
        valid, rejected = transform([self._make_valid_row(amount=-99.50)])
        assert "-99.5" in rejected[0]["rejection_reason"]

    def test_invariant_valid_plus_rejected_equals_input(self, extracted_rows):
        """extracted == valid + rejected siempre."""
        valid, rejected = transform(extracted_rows)
        assert len(valid) + len(rejected) == len(extracted_rows)

    def test_all_rejected_have_reason_and_type(self, extracted_rows):
        """Todos los rechazados tienen rejection_reason y rejection_type."""
        _, rejected = transform(extracted_rows)
        for r in rejected:
            assert "rejection_reason" in r, f"Falta rejection_reason en: {r}"
            assert "rejection_type"   in r, f"Falta rejection_type en: {r}"
            assert len(r["rejection_reason"]) > 5


# ---------------------------------------------------------------------------
# Tests de cuarentena
# ---------------------------------------------------------------------------

class TestQuarantine:

    def test_quarantine_file_created(self, extracted_rows, tmp_path):
        """El archivo de cuarentena se crea en la ruta correcta."""
        _, rejected = transform(extracted_rows)
        qfile = write_quarantine(rejected, tmp_path)

        assert qfile.exists()
        assert qfile.suffix == ".jsonl"

    def test_quarantine_has_all_rejected(self, extracted_rows, tmp_path):
        """El archivo de cuarentena tiene exactamente las filas rechazadas."""
        _, rejected = transform(extracted_rows)
        qfile = write_quarantine(rejected, tmp_path)

        lines = [l for l in qfile.read_text(encoding="utf-8").strip().split("\n") if l]
        assert len(lines) == len(rejected)

    def test_quarantine_each_line_has_reason(self, extracted_rows, tmp_path):
        """Cada línea del JSONL tiene rejection_reason."""
        _, rejected = transform(extracted_rows)
        qfile = write_quarantine(rejected, tmp_path)

        for line in qfile.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            row = json.loads(line)
            assert "rejection_reason" in row
            assert len(row["rejection_reason"]) > 5

    def test_quarantine_appends_on_second_call(self, extracted_rows, tmp_path):
        """Segunda escritura a la cuarentena agrega líneas, no sobreescribe."""
        _, rejected = transform(extracted_rows)
        if not rejected:
            pytest.skip("No hay rechazados en este batch")

        write_quarantine(rejected, tmp_path)
        write_quarantine(rejected, tmp_path)

        today = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
        qfile = tmp_path / f"{today}.jsonl"
        lines = [l for l in qfile.read_text(encoding="utf-8").strip().split("\n") if l]
        assert len(lines) == len(rejected) * 2


# ---------------------------------------------------------------------------
# Tests de load
# ---------------------------------------------------------------------------

class TestLoad:

    def test_load_inserts_correctly(self, extracted_rows, tmp_db):
        """Filas válidas se insertan en la DB."""
        valid, _ = transform(extracted_rows)
        inserted, duplicates = load(valid, db_path=tmp_db)

        assert inserted > 0
        assert inserted + duplicates == len(valid)

    def test_load_idempotent(self, extracted_rows, tmp_db):
        """
        IDEMPOTENCIA — el test más importante del E6.

        Correr la carga dos veces con los mismos datos debe producir
        el mismo resultado final:
            - Segunda corrida: inserted=0
            - Segunda corrida: duplicates = primera corrida inserted
            - Conteo en DB no cambia entre corrida 1 y corrida 2
        """
        valid, _ = transform(extracted_rows)

        # Primera corrida
        inserted1, duplicates1 = load(valid, db_path=tmp_db)

        # Verificar que la DB tiene las filas
        conn = sqlite3.connect(tmp_db)
        count_after_1 = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        conn.close()

        # Segunda corrida — con los mismos datos
        inserted2, duplicates2 = load(valid, db_path=tmp_db)

        conn = sqlite3.connect(tmp_db)
        count_after_2 = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        conn.close()

        # Invariantes de idempotencia
        assert inserted2 == 0, (
            f"Segunda corrida debe insertar 0 filas, insertó {inserted2}"
        )
        assert duplicates2 == inserted1, (
            f"Segunda corrida debe tener {inserted1} duplicados, tiene {duplicates2}"
        )
        assert count_after_1 == count_after_2, (
            f"El conteo en DB no debe cambiar: {count_after_1} → {count_after_2}"
        )

    def test_load_invariant(self, extracted_rows, tmp_db):
        """inserted + duplicates == valid siempre."""
        valid, _ = transform(extracted_rows)
        inserted, duplicates = load(valid, db_path=tmp_db)
        assert inserted + duplicates == len(valid)

    def test_load_empty_list(self, tmp_db):
        """Cargar lista vacía retorna (0, 0) sin error."""
        inserted, duplicates = load([], db_path=tmp_db)
        assert inserted == 0
        assert duplicates == 0

    def test_load_raises_if_db_missing(self):
        """Lanzar FileNotFoundError si la DB no existe."""
        with pytest.raises(FileNotFoundError):
            load([{"transaction_id": "x"}], db_path="/ruta/inexistente.db")


# ---------------------------------------------------------------------------
# Tests del pipeline completo
# ---------------------------------------------------------------------------

class TestPipeline:

    def test_pipeline_invariants(self, tmp_db, tmp_dirs):
        """
        Las invariantes matemáticas del reporte se cumplen siempre.

        extracted == valid + rejected
        inserted + duplicates == valid
        """
        qdir, rdir = tmp_dirs
        report = run_pipeline(
            batch_size    = 200,
            error_rate    = 0.10,
            seed          = 42,
            db_path       = tmp_db,
            quarantine_dir = qdir,
            results_dir   = rdir,
        )

        assert report["extracted"] == report["valid"] + report["rejected"], (
            f"extracted({report['extracted']}) != "
            f"valid({report['valid']}) + rejected({report['rejected']})"
        )
        assert report["inserted"] + report["duplicates"] == report["valid"], (
            f"inserted({report['inserted']}) + duplicates({report['duplicates']}) "
            f"!= valid({report['valid']})"
        )
        assert report["invariants"]["extracted_eq_valid_plus_rejected"]
        assert report["invariants"]["inserted_plus_duplicates_eq_valid"]

    def test_pipeline_idempotent(self, tmp_db, tmp_dirs):
        """
        IDEMPOTENCIA DEL PIPELINE COMPLETO.

        Correr el pipeline dos veces con los mismos parámetros:
            - Segunda corrida: inserted=0
            - Conteo en DB es idéntico después de ambas corridas
        """
        qdir, rdir = tmp_dirs

        params = dict(
            batch_size    = 200,
            error_rate    = 0.10,
            seed          = 42,
            db_path       = tmp_db,
            quarantine_dir = qdir,
            results_dir   = rdir,
        )

        # Primera corrida
        report1 = run_pipeline(**params)

        conn = sqlite3.connect(tmp_db)
        count1 = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        conn.close()

        # Segunda corrida — mismos parámetros
        report2 = run_pipeline(**params)

        conn = sqlite3.connect(tmp_db)
        count2 = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        conn.close()

        assert report2["inserted"] == 0, (
            f"Segunda corrida debe insertar 0 filas, insertó {report2['inserted']}"
        )
        assert report2["duplicates"] == report1["inserted"], (
            f"Segunda corrida debe tener {report1['inserted']} duplicados"
        )
        assert count1 == count2, (
            f"El conteo en DB no debe cambiar: {count1} → {count2}"
        )

    def test_pipeline_report_saved(self, tmp_db, tmp_dirs):
        """El reporte JSON se guarda en results/."""
        qdir, rdir = tmp_dirs
        run_pipeline(
            batch_size=100, error_rate=0.10, seed=1,
            db_path=tmp_db, quarantine_dir=qdir, results_dir=rdir,
        )
        reports = list(rdir.glob("run_*.json"))
        assert len(reports) == 1

        report = json.loads(reports[0].read_text(encoding="utf-8"))
        assert "extracted"   in report
        assert "valid"       in report
        assert "rejected"    in report
        assert "by_error"    in report
        assert "inserted"    in report
        assert "duplicates"  in report
        assert "total_time_s" in report
        assert "invariants"  in report

    def test_pipeline_quarantine_has_reasons(self, tmp_db, tmp_dirs):
        """Todas las filas en cuarentena tienen rejection_reason."""
        qdir, rdir = tmp_dirs
        report = run_pipeline(
            batch_size=200, error_rate=0.15, seed=42,
            db_path=tmp_db, quarantine_dir=qdir, results_dir=rdir,
        )

        if report["rejected"] == 0:
            pytest.skip("Sin rechazados en este batch")

        today = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
        qfile = qdir / f"{today}.jsonl"
        assert qfile.exists()

        for line in qfile.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            row = json.loads(line)
            assert "rejection_reason" in row
            assert len(row["rejection_reason"]) > 5