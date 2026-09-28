import numpy as np
import pandas as pd
import pytest

from splus_mcp.catalogs import BANDS
from splus_mcp.sed import mag_to_mjy, plot_sed, sed_table


def test_mag_to_mjy():
    flux, err = mag_to_mjy(16.4, 0.1)
    assert flux == pytest.approx(1.0, rel=0.01)  # AB 16.4 ~ 1 mJy
    assert err == pytest.approx(0.4 * np.log(10) * flux * 0.1)


def test_sed_table_and_plot(tmp_path):
    src = pd.Series({f"mag_{b}": 18.0 + 0.1 * k for k, b in enumerate(BANDS)} | {f"err_{b}": 0.05 for b in BANDS})
    src["mag_u"] = np.nan  # nao-deteccao
    table = sed_table({"idr6": src, "dr5": src + 0.02})
    assert len(table) == 2 * len(BANDS)
    assert table.groupby("catalog")["wavelength_A"].apply(lambda w: w.is_monotonic_increasing).all()
    assert table.loc[(table.catalog == "idr6") & (table.band == "u"), "flux_mJy"].isna().all()

    png = plot_sed(table, "teste", tmp_path / "sed.png")
    assert png.exists() and png.stat().st_size > 1000
