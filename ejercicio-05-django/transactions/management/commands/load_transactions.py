"""
management/commands/load_transactions.py — Carga masiva de transacciones desde Parquet.

Uso:
    python manage.py load_transactions
    python manage.py load_transactions --parquet /ruta/al/archivo.parquet
    python manage.py load_transactions --batch-size 5000
    python manage.py load_transactions --clear

Por qué bulk_create en lugar de create() en loop:
    Con 1M filas, Transaction.objects.create() en un loop hace 1M INSERT
    individuales — cada uno con su propio round-trip a la base de datos.
    A ~1,000 inserts/segundo eso sería ~17 minutos.

    Transaction.objects.bulk_create(objs, batch_size=N) agrupa los objetos
    en lotes y hace un solo INSERT por lote con múltiples VALUES.
    A 10,000 filas por INSERT la carga completa tarda 1-3 minutos.

Por qué ignore_conflicts=True:
    Si el comando se corre dos veces (idempotencia), los UUIDs que ya existen
    son ignorados silenciosamente por la restricción PRIMARY KEY de SQLite.
    Sin ignore_conflicts, el segundo run lanzaría IntegrityError.

Por qué pandas.read_parquet en chunks y no todo de una vez:
    1M filas × 8 columnas ≈ 200MB en RAM como DataFrame. Con batch_size=10000,
    el pico de RAM es ~20MB por lote — significativamente menos.
    pandas.read_parquet no soporta chunked reading nativo, así que se lee
    el archivo completo una vez y se itera sobre slices del DataFrame.
    Para archivos >1GB se usaría pyarrow directamente con batch reading.
"""

import time
from pathlib import Path

import pandas as pd
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from transactions.models import Transaction


class Command(BaseCommand):
    help = "Carga transacciones desde el Parquet del E1 usando el ORM de Django."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--parquet",
            type=str,
            default=settings.PARQUET_PATH,
            help="Ruta al archivo Parquet (default: settings.PARQUET_PATH)",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=10_000,
            help="Filas por bulk_create (default: 10000)",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            default=False,
            help="Eliminar todas las transacciones existentes antes de cargar.",
        )

    def handle(self, *args, **options) -> None:
        parquet_path = Path(options["parquet"])
        batch_size   = options["batch_size"]
        clear        = options["clear"]

        # Verificar que el Parquet existe antes de continuar
        if not parquet_path.exists():
            raise CommandError(
                f"No se encontró el Parquet en: {parquet_path}\n"
                "Genera el dataset primero con:\n"
                "  python generate_data.py --size 1m  (en ejercicio-01-formatos/)"
            )

        self.stdout.write(f"Parquet: {parquet_path}")
        self.stdout.write(f"Batch size: {batch_size:,} filas por commit")

        # Limpiar si se solicitó
        if clear:
            count = Transaction.objects.count()
            Transaction.objects.all().delete()
            self.stdout.write(
                self.style.WARNING(f"Eliminadas {count:,} transacciones existentes.")
            )

        # Leer el Parquet completo en un DataFrame
        self.stdout.write("Leyendo Parquet...")
        t0 = time.perf_counter()
        df = pd.read_parquet(
            str(parquet_path),
            columns=[
                "transaction_id", "timestamp", "user_id", "merchant_id",
                "amount", "category", "country_code", "status",
            ],
        )
        read_time = time.perf_counter() - t0
        self.stdout.write(f"  {len(df):,} filas leídas en {read_time:.2f}s")

        # Normalizar el timestamp a formato ISO8601 string
        # El modelo usa CharField para timestamp — consistente con el E3
        df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.strftime("%Y-%m-%d %H:%M:%S")
        df["amount"]    = df["amount"].round(2)

        # Ingestar en lotes usando bulk_create
        total      = len(df)
        inserted   = 0
        skipped    = 0
        t_start    = time.perf_counter()

        self.stdout.write(f"Ingestando {total:,} filas en lotes de {batch_size:,}...")

        for start in range(0, total, batch_size):
            chunk = df.iloc[start : start + batch_size]

            objs = [
                Transaction(
                    transaction_id = row["transaction_id"],
                    timestamp      = row["timestamp"],
                    user_id        = int(row["user_id"]),
                    merchant_id    = int(row["merchant_id"]),
                    amount         = float(row["amount"]),
                    category       = row["category"],
                    country_code   = row["country_code"],
                    status         = row["status"],
                )
                for _, row in chunk.iterrows()
            ]

            # ignore_conflicts=True: filas con transaction_id duplicado
            # son ignoradas silenciosamente — garantiza idempotencia
            result = Transaction.objects.bulk_create(
                objs,
                batch_size     = batch_size,
                ignore_conflicts = True,
            )

            batch_inserted = len(result)
            batch_skipped  = len(objs) - batch_inserted
            inserted += batch_inserted
            skipped  += batch_skipped

            # Progreso cada 10 lotes
            if (start // batch_size + 1) % 10 == 0:
                elapsed = time.perf_counter() - t_start
                rate    = inserted / elapsed if elapsed > 0 else 0
                pct     = (start + len(chunk)) / total * 100
                self.stdout.write(
                    f"  {pct:5.1f}% — {inserted:>9,} insertadas | "
                    f"{skipped:>7,} duplicadas | {rate:,.0f} filas/s"
                )

        total_time = time.perf_counter() - t_start

        # Verificar integridad
        db_count = Transaction.objects.count()

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Carga completada en {total_time:.1f}s"
        ))
        self.stdout.write(f"  Filas en Parquet:  {total:,}")
        self.stdout.write(f"  Insertadas:        {inserted:,}")
        self.stdout.write(f"  Duplicadas:        {skipped:,}")
        self.stdout.write(f"  Total en DB:       {db_count:,}")
        self.stdout.write(f"  Velocidad:         {total / total_time:,.0f} filas/s")