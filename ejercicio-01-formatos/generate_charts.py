"""
generate_charts.py — Genera las gráficas del report.md.

Usa escala logarítmica en el eje Y de tiempos porque los formatos difieren
hasta 300x en algunos casos (JSONL vs Parquet). En escala lineal, las barras
de Parquet serían invisibles junto a las de JSONL.
"""

import json
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np

SIZES_ORDER  = ["100k", "500k", "1m"]
FORMAT_ORDER = ["csv", "jsonl", "parquet", "parquet_snappy", "parquet_gzip"]
FORMAT_LABELS = {
    "csv":            "CSV",
    "jsonl":          "JSON Lines",
    "parquet":        "Parquet (sin comp.)",
    "parquet_snappy": "Parquet + Snappy",
    "parquet_gzip":   "Parquet + Gzip",
}
COLORS  = ["#4E79A7", "#F28E2B", "#59A14F", "#76B7B2", "#B07AA1"]
HATCHES = ["", "//", "", "xx", ".."]


def load_results() -> dict:
    results_dir = Path("results")
    data = {}
    for size in SIZES_ORDER:
        path = results_dir / f"benchmark_{size}.json"
        if not path.exists():
            raise FileNotFoundError(f"No se encontró {path}")
        data[size] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _bar_group(ax, size_order, format_order, get_value_fn, colors, hatches, format_labels):
    """Dibuja un grupo de barras agrupadas. Retorna (bars_by_fmt, x_positions)."""
    n_sizes   = len(size_order)
    n_formats = len(format_order)
    x         = np.arange(n_sizes)
    width     = 0.15
    offsets   = np.linspace(-(n_formats - 1) / 2, (n_formats - 1) / 2, n_formats) * width
    bars_by_fmt = {}
    for i, fmt in enumerate(format_order):
        values = [get_value_fn(fmt, size) for size in size_order]
        bars = ax.bar(
            x + offsets[i], values, width=width,
            label=format_labels[fmt],
            color=colors[i], hatch=hatches[i],
            edgecolor="white", linewidth=0.5,
        )
        bars_by_fmt[fmt] = (bars, values)
    return bars_by_fmt, x


def chart_read_time(data: dict, out_path: Path) -> None:
    """
    Tiempo de lectura completa por formato y escala — escala logarítmica.

    Por qué logarítmica: a 1M filas JSONL tarda 24.7s y Parquet+Snappy
    0.08s — diferencia de ~300x. En escala lineal la barra de Parquet
    es un píxel invisible. La escala log hace todas las barras comparables
    y permite leer los valores reales con las anotaciones.
    """
    fig, ax = plt.subplots(figsize=(11, 5))

    def get_val(fmt, size):
        return data[size]["formats"].get(fmt, {}).get("read_full_avg_s", 1e-4)

    bars_by_fmt, x = _bar_group(ax, SIZES_ORDER, FORMAT_ORDER, get_val,
                                 COLORS, HATCHES, FORMAT_LABELS)

    # Anotar valor encima de cada barra
    for fmt, (bars, values) in bars_by_fmt.items():
        for bar, val in zip(bars, values):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                val * 1.15,
                f"{val:.2f}s",
                ha="center", va="bottom", fontsize=6.5, color="#333333",
            )

    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.2f}s"))
    ax.set_xticks(x)
    ax.set_xticklabels([f"{s.upper()}\nfilas" for s in SIZES_ORDER], fontsize=10)
    ax.set_ylabel("Tiempo de lectura completa — escala log (s)", fontsize=10)
    ax.set_title(
        "Tiempo de lectura completa por formato y escala\n"
        "(escala logarítmica — diferencia máxima: ~300x entre JSONL y Parquet+Snappy)",
        fontsize=11, pad=12,
    )
    ax.legend(loc="upper left", fontsize=8, framealpha=0.9)
    ax.grid(axis="y", linestyle="--", alpha=0.35, which="both")
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Guardada: {out_path}")


def chart_file_size(data: dict, out_path: Path) -> None:
    """Tamaño en disco por formato y escala — escala lineal (diferencias son razonables aquí)."""
    fig, ax = plt.subplots(figsize=(11, 5))

    def get_val(fmt, size):
        return data[size]["formats"].get(fmt, {}).get("size_bytes", 0) / 1e6

    bars_by_fmt, x = _bar_group(ax, SIZES_ORDER, FORMAT_ORDER, get_val,
                                 COLORS, HATCHES, FORMAT_LABELS)

    for fmt, (bars, values) in bars_by_fmt.items():
        for bar, val in zip(bars, values):
            if val > 0:
                ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + 1.5,
                    f"{val:.0f}",
                    ha="center", va="bottom", fontsize=6.5, color="#333333",
                )

    ax.set_xticks(x)
    ax.set_xticklabels([f"{s.upper()}\nfilas" for s in SIZES_ORDER], fontsize=10)
    ax.set_ylabel("Tamaño en disco (MB)", fontsize=10)
    ax.set_title("Tamaño en disco por formato y escala", fontsize=12, pad=12)
    ax.legend(loc="upper left", fontsize=8, framealpha=0.9)
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Guardada: {out_path}")


def chart_selective_vs_full(data: dict, out_path: Path) -> None:
    """Lectura completa vs selectiva a 1M — escala logarítmica."""
    size   = "1m"
    labels = [FORMAT_LABELS[f] for f in FORMAT_ORDER]
    full_times      = [data[size]["formats"].get(f, {}).get("read_full_avg_s", 1e-4) for f in FORMAT_ORDER]
    selective_times = [data[size]["formats"].get(f, {}).get("read_selective_avg_s", 1e-4) for f in FORMAT_ORDER]

    x     = np.arange(len(FORMAT_ORDER))
    width = 0.35

    fig, ax = plt.subplots(figsize=(11, 5))
    bars_f = ax.bar(x - width/2, full_times,      width, label="Lectura completa",  color="#4E79A7", edgecolor="white")
    bars_s = ax.bar(x + width/2, selective_times,  width, label="Lectura selectiva", color="#59A14F", edgecolor="white", hatch="//")

    for bar, val in list(zip(bars_f, full_times)) + list(zip(bars_s, selective_times)):
        ax.text(bar.get_x() + bar.get_width() / 2, val * 1.15,
                f"{val:.2f}s", ha="center", va="bottom", fontsize=8)

    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.2f}s"))
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=9, rotation=12, ha="right")
    ax.set_ylabel("Tiempo promedio — escala log (s)", fontsize=10)
    ax.set_title(
        "Lectura completa vs selectiva — 1M filas (columnas: amount + category)\n"
        "(escala logarítmica)",
        fontsize=11, pad=12,
    )
    ax.legend(fontsize=9)
    ax.grid(axis="y", linestyle="--", alpha=0.35, which="both")
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Guardada: {out_path}")


def chart_ram_usage(data: dict, out_path: Path) -> None:
    """
    Pico de RAM por formato y escala — con nota de advertencia sobre Parquet.

    ADVERTENCIA VISIBLE EN LA GRÁFICA: los valores de RAM de Parquet
    (plain, snappy, gzip) son prácticamente iguales a su tamaño en disco.
    Esto ocurre porque tracemalloc solo rastrea el heap de Python.
    pyarrow hace sus allocaciones en C (fuera del GIL y del heap Python),
    invisibles para tracemalloc. Los valores de CSV y JSONL sí son
    capturados porque pandas los construye en el heap Python.
    """
    fig, ax = plt.subplots(figsize=(11, 5))

    def get_val(fmt, size):
        return data[size]["formats"].get(fmt, {}).get("read_full_peak_mb", 0)

    bars_by_fmt, x = _bar_group(ax, SIZES_ORDER, FORMAT_ORDER, get_val,
                                 COLORS, HATCHES, FORMAT_LABELS)

    for fmt, (bars, values) in bars_by_fmt.items():
        for bar, val in zip(bars, values):
            if val > 0:
                ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + 5,
                    f"{val:.0f}",
                    ha="center", va="bottom", fontsize=6.5, color="#333333",
                )

    # Advertencia visible en la gráfica
    ax.text(
        0.01, 0.97,
        "* RAM de Parquet ≈ tamaño en disco: tracemalloc no captura\n"
        "  allocaciones de pyarrow en C. Ver análisis en el reporte.",
        transform=ax.transAxes,
        fontsize=8, va="top", color="#993C1D",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="#FAECE7", edgecolor="#993C1D", linewidth=0.5),
    )

    ax.set_xticks(x)
    ax.set_xticklabels([f"{s.upper()}\nfilas" for s in SIZES_ORDER], fontsize=10)
    ax.set_ylabel("Pico de RAM durante lectura (MB)", fontsize=10)
    ax.set_title("Pico de RAM por formato y escala\n(* valores de Parquet limitados por tracemalloc — ver nota)", fontsize=11, pad=12)
    ax.legend(loc="upper left", fontsize=8, framealpha=0.9)
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Guardada: {out_path}")


def main() -> None:
    results_dir = Path("results")
    results_dir.mkdir(exist_ok=True)

    print("Cargando resultados...")
    data = load_results()

    print("Generando gráficas...")
    chart_read_time(data,          results_dir / "chart_read_time.png")
    chart_file_size(data,          results_dir / "chart_file_size.png")
    chart_selective_vs_full(data,  results_dir / "chart_selective_vs_full.png")
    chart_ram_usage(data,          results_dir / "chart_ram_usage.png")

    print("\nDone. Gráficas guardadas en results/")


if __name__ == "__main__":
    main()