"""Utilidades compartidas por los cuadernos de ML (Bloque 2).

Los cuadernos se abren desde esta carpeta (ml/). Leen el almacen en solo lectura y
guardan sus resultados en ml/resultados/ (CSV para Excel y Parquet para Power BI).
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ORO = Path("..") / "oro" / "almacen.duckdb"
RESULTADOS = Path("resultados")
FIGURAS = RESULTADOS / "figuras"
SEMILLA = 20261101  # semilla fija: mismos datos, mismos resultados

AZUL, GRIS, NARANJA, ROJO, VERDE = "#2a78d6", "#8c8c8c", "#e07b28", "#c9342f", "#2f9e6b"

assert ORO.exists(), "No encuentro ../oro/almacen.duckdb: abre el cuaderno desde la carpeta ml/ de la entrega"
RESULTADOS.mkdir(exist_ok=True)
FIGURAS.mkdir(exist_ok=True)

# Solo lectura: desde los cuadernos no se puede modificar el almacen por error
con = duckdb.connect(str(ORO), read_only=True)


def q(sql: str) -> pd.DataFrame:
    """Ejecuta una consulta SQL sobre el almacen y devuelve un DataFrame."""
    return con.sql(sql).df()


# Fecha de cierre de los datos: la ultima venta registrada
CORTE = pd.Timestamp(q("""SELECT MAX(CAST(f.fecha AS DATE)) AS corte FROM fact_ventas v
                          JOIN dim_fecha f ON f.fecha_key = v.fecha_emision_key""").corte[0])


def guardar(df: pd.DataFrame, nombre: str) -> None:
    """Guarda un resultado en CSV (Excel) y en Parquet (Power BI)."""
    df.to_csv(RESULTADOS / f"{nombre}.csv", index=False)
    df.to_parquet(RESULTADOS / f"{nombre}.parquet", index=False)
    print(f"Guardado: resultados/{nombre}.csv y .parquet ({len(df)} filas)")


def guardar_figura(fig, nombre: str) -> None:
    fig.tight_layout()
    fig.savefig(FIGURAS / f"{nombre}.png", dpi=160)
    print(f"Figura guardada: resultados/figuras/{nombre}.png")


def estilo(ax, titulo: str, ylabel: str | None = None) -> None:
    ax.set_title(titulo, loc="left", fontsize=11)
    if ylabel:
        ax.set_ylabel(ylabel)
    ax.grid(axis="y", color="#e5e5e5", linewidth=0.8)
    ax.spines[["top", "right"]].set_visible(False)


def mape(real, prevision) -> float:
    """Error porcentual absoluto medio, en %."""
    real, prevision = np.asarray(real, float), np.asarray(prevision, float)
    return float(np.mean(np.abs(real - prevision) / real) * 100)


print(f"Conectado al almacen. Datos hasta el {CORTE.date()}.")
