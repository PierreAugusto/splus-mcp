"""SED de 12 bandas: tabela e grafico, sem acesso a rede."""
from pathlib import Path

import numpy as np
import pandas as pd

from splus_mcp.catalogs import BANDS, PIVOT_WAVELENGTH

AB_ZERO_JY = 3631.0
BROAD_BANDS = {"u", "g", "r", "i", "z"}


def mag_to_mjy(mag, err=None):
    """Magnitude AB -> densidade de fluxo f_nu em mJy (e erro propagado)."""
    mag = np.asarray(mag, dtype=float)
    flux = AB_ZERO_JY * 1e3 * 10 ** (-0.4 * mag)
    if err is None:
        return flux, None
    return flux, 0.4 * np.log(10) * flux * np.asarray(err, dtype=float)


def sed_table(sources: dict[str, pd.Series]) -> pd.DataFrame:
    """Formato longo (uma linha por catalogo x banda) a partir das fontes padronizadas
    (colunas mag_<banda>/err_<banda>), indexadas pelo nome do catalogo."""
    rows = []
    for catalog, src in sources.items():
        for b in BANDS:
            mag = src.get(f"mag_{b}", np.nan)
            err = src.get(f"err_{b}", np.nan)
            flux, flux_err = mag_to_mjy(mag, err)
            rows.append({
                "catalog": catalog,
                "band": b,
                "wavelength_A": PIVOT_WAVELENGTH[b],
                "mag": float(mag),
                "err": float(err),
                "flux_mJy": float(flux),
                "flux_err_mJy": float(flux_err),
            })
    return pd.DataFrame(rows)


def plot_sed(table: pd.DataFrame, title: str, out_path: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
    catalogs = list(dict.fromkeys(table["catalog"]))
    offsets = np.linspace(-25, 25, len(catalogs)) if len(catalogs) > 1 else [0.0]
    for catalog, dx in zip(catalogs, offsets):
        sub = table[(table["catalog"] == catalog) & table["mag"].notna()]
        if sub.empty:
            continue
        line = ax.plot(sub["wavelength_A"] + dx, sub["mag"], "-", alpha=0.5, label=catalog)[0]
        color = line.get_color()
        for broad, marker in ((True, "s"), (False, "o")):
            part = sub[sub["band"].isin(BROAD_BANDS) == broad]
            ax.errorbar(part["wavelength_A"] + dx, part["mag"], yerr=part["err"].fillna(0), fmt=marker,
                        color=color, ms=6, capsize=2)

    for b in BANDS:
        ax.annotate(b, (PIVOT_WAVELENGTH[b], 0), xycoords=("data", "axes fraction"), xytext=(0, 3),
                    textcoords="offset points", ha="center", fontsize=7, color="0.4", rotation=90)
    ax.invert_yaxis()
    ax.set_xlabel("Comprimento de onda pivot (Å)")
    ax.set_ylabel("Magnitude AB")
    ax.set_title(title, fontsize=10)
    ax.grid(alpha=0.3)
    if catalogs:
        ax.legend(title="catálogo (■ larga, ● estreita)", fontsize=8, title_fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return out_path
