import numpy as np
import pandas as pd
import pytest

from splus_mcp import catalogs as cat


@pytest.mark.parametrize(
    "given, expected",
    [("r", "r"), ("R", "r"), ("rSDSS", "r"), ("uJAVA", "u"), ("F660", "J0660"), ("j0660", "J0660"),
     ("J0378", "J0378"), ("660", "J0660"), ("f861", "J0861"), ("Z", "z")],
)
def test_normalize_band(given, expected):
    assert cat.normalize_band(given) == expected


@pytest.mark.parametrize("bad", ["x", "F999", "", "H"])
def test_normalize_band_invalid(bad):
    with pytest.raises(ValueError):
        cat.normalize_band(bad)


@pytest.mark.parametrize("given, expected", [("r", "R"), ("J0660", "F660"), ("f378", "F378"), ("u", "U")])
def test_image_band(given, expected):
    assert cat.image_band(given) == expected


def test_normalize_aperture():
    assert cat.normalize_aperture("PStotal") == "pstotal"
    assert cat.normalize_aperture("isophotal") == "iso"
    with pytest.raises(ValueError):
        cat.normalize_aperture("kron")


def test_column_names_per_style():
    idr6, dr5 = cat.get_catalog("idr6"), cat.get_catalog("dr5")
    assert idr6.mag_col("J0660", "pstotal") == "mag_pstotal_j0660"
    assert idr6.err_col("r", "iso") == "err_mag_isophotal_r"
    assert dr5.mag_col("J0660", "pstotal") == "J0660_PStotal"
    assert dr5.err_col("r", "auto") == "e_r_auto"


def test_catalog_alias_and_unknown():
    assert cat.get_catalog("DR6").name == "idr6"
    with pytest.raises(ValueError, match="nao suportado"):
        cat.get_catalog("usdr1")


def test_cone_query_single_table():
    [q] = cat.build_cone_queries(cat.get_catalog("idr6"), 16.0, -25.0, 0.01, ["r", "J0660"], "auto")
    assert q.startswith("SELECT id, ra, dec, field, mag_auto_r, err_mag_auto_r, mag_auto_j0660")
    assert "FROM idr6.idr6" in q
    assert "CIRCLE('ICRS', 16.0, -25.0, 0.01)" in q


def test_cone_query_dr4_one_per_band():
    qs = cat.build_cone_queries(cat.get_catalog("dr4"), 0.5, -0.5, 0.01, ["r", "J0660"], "pstotal")
    assert len(qs) == 3
    assert "dr4_dual.dr4_dual_detection" in qs[0]
    assert "FROM dr4_dual.dr4_dual_r " in qs[1] and "r_PStotal" in qs[1]
    assert "FROM dr4_dual.dr4_dual_j0660 " in qs[2] and "e_J0660_PStotal" in qs[2]


def test_xmatch_queries():
    [q] = cat.build_xmatch_queries(cat.get_catalog("dr5"), "xm1", 1 / 3600, ["r"], "auto")
    assert "FROM upload.xm1 AS u JOIN dr5.dr5_dual AS c" in q
    assert "q3c_join(u.xm_ra, u.xm_dec, c.RA, c.DEC," in q
    assert q.startswith("SELECT u.xm_row, c.ID, c.RA, c.DEC, c.Field, c.r_auto, c.e_r_auto")
    assert len(cat.build_xmatch_queries(cat.get_catalog("dr4"), "xm1", 1e-4, ["r", "g"], "auto")) == 3


def test_standardize_renames_case_insensitive_and_masks_bad_mags():
    raw = pd.DataFrame({"ID": ["a", "b"], "RA": [1.0, 2.0], "DEC": [3.0, 4.0], "Field": ["F", "F"],
                        "r_auto": [17.5, 99.0], "e_r_auto": [0.02, 99.0]})
    out = cat.standardize_columns(raw, cat.get_catalog("dr5"), ["r"], "auto")
    assert list(out.columns) == ["id", "ra", "dec", "field", "mag_r", "err_r"]
    assert out["mag_r"].iloc[0] == 17.5
    assert np.isnan(out["mag_r"].iloc[1]) and np.isnan(out["err_r"].iloc[1])


def test_standardize_same_schema_across_styles():
    lower = pd.DataFrame({"id": ["x"], "ra": [1.0], "dec": [2.0], "field": ["F"],
                          "mag_auto_j0660": [18.0], "err_mag_auto_j0660": [0.05]})
    upper = pd.DataFrame({"ID": ["y"], "RA": [1.0], "DEC": [2.0], "Field": ["F"],
                          "J0660_auto": [18.1], "e_J0660_auto": [0.06]})
    a = cat.standardize_columns(lower, cat.get_catalog("idr6"), ["J0660"], "auto")
    b = cat.standardize_columns(upper, cat.get_catalog("dr5"), ["J0660"], "auto")
    assert list(a.columns) == list(b.columns)


def test_bands_and_wavelengths_consistent():
    assert set(cat.PIVOT_WAVELENGTH) == set(cat.BANDS)
    waves = [cat.PIVOT_WAVELENGTH[b] for b in cat.BANDS]
    assert waves == sorted(waves)
