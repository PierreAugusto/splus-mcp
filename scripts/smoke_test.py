"""Teste ao vivo contra o splus.cloud (precisa de SPLUS_USER/SPLUS_PASS no .env).

Chama cada tool do servidor e confere o resultado, cobrindo os catalogos dr3, dr4, dr5,
idr5 e idr6. Os arquivos vao para uma pasta temporaria.
Uso: python scripts/smoke_test.py
"""
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

os.environ.setdefault("SPLUS_DOWNLOAD_DIR", tempfile.mkdtemp(prefix="splus_smoke_"))

import pandas as pd  # noqa: E402
from astropy.io import fits  # noqa: E402

from splus_mcp import backend, server  # noqa: E402

# Posicoes conferidas em 2026-09.
S82 = (0.5, -0.5)       # STRIPE82-0001: coberto por dr3, dr4, dr5, idr5 e idr6
SOUTH = (16.0, -25.0)   # SPLUS-s20s12: dr5, idr5 e idr6
NOWHERE = (150.0, 60.0)  # fora do footprint
CATALOGS = ["idr6", "dr5", "idr5", "dr4", "dr3"]
STD_COLS = ["dist_arcsec", "id", "ra", "dec", "field", "mag_u", "err_u"]

results = []


def check(name, fn):
    t = time.time()
    try:
        detail = fn()
        results.append(("OK", name, time.time() - t, detail or ""))
    except AssertionError as e:
        results.append(("FAIL", name, time.time() - t, f"assert: {e}"))
    except Exception as e:
        results.append(("FAIL", name, time.time() - t, f"{type(e).__name__}: {str(e)[:200]}"))
        traceback.print_exc()
    status, _, dt, detail = results[-1]
    print(f"{status:4s} {name:42s} {dt:6.1f}s  {str(detail)[:110]}", flush=True)


def t_releases():
    r = server.list_data_releases()
    assert {"idr6", "dr5"} <= set(r["image_collections"]), r["image_collections"]
    assert set(CATALOGS) == set(r["standard_catalogs"])
    return f"{len(r['catalog_schemas'])} schemas"


def t_describe():
    r = server.describe_table("idr6", "idr6", name_filter="mag_pstotal_r")
    assert [c["name"] for c in r["columns"]] == ["err_mag_pstotal_r", "mag_pstotal_r"] or r["n_columns"] >= 1
    return r["columns"]


def t_check_coords(pos, expect_data):
    def run():
        r = server.check_coords(*pos)
        counts = {c["catalog"]: c.get("n_sources") for c in r["catalogs"]}
        for name in CATALOGS:
            has = bool(counts[name])
            assert has == (name in expect_data), f"{name}: {counts[name]}"
        return counts
    return run


def t_query():
    r = server.query_catalog("SELECT TOP 5 ra, dec, field FROM idr6.idr6", 2)
    assert r["row_count"] == 5 and Path(r["saved_to"]).exists()


def t_cone(catalog):
    def run():
        r = server.cone_search(*S82, radius_arcsec=30, catalog=catalog, max_preview_rows=1)
        df = pd.read_csv(r["saved_to"])
        assert list(df.columns[:7]) == STD_COLS, list(df.columns[:7])
        assert len(df) > 0 and df["dist_arcsec"].is_monotonic_increasing
        assert df["dist_arcsec"].max() <= 30
        return f"{len(df)} fontes, 1a a {df['dist_arcsec'].iloc[0]:.1f}''"
    return run


def t_cone_filter():
    r = server.cone_search(*SOUTH, radius_arcsec=120, catalog="idr6", bands=["r", "F660"], mag_max=18, max_rows=5)
    df = pd.read_csv(r["saved_to"])
    assert len(df) <= 5 and (df["mag_r"] <= 18).all()
    assert list(df.columns) == ["dist_arcsec", "id", "ra", "dec", "field", "mag_r", "err_r", "mag_J0660", "err_J0660"]


_targets_path = None


def targets_path():
    global _targets_path
    if _targets_path is None:
        src = backend.cone_search("idr6", *S82, 900, ["r"], "auto", mag_max=20).sample(100, random_state=0)
        t = pd.DataFrame({"obj": [f"s{i:03d}" for i in range(len(src))],
                          "RAJ2000": src["ra"].values + 0.3 / 3600, "DEJ2000": src["dec"].values,
                          "r_idr6": src["mag_r"].values})
        t.loc[len(t)] = ["nowhere", *NOWHERE, None]
        _targets_path = Path(os.environ["SPLUS_DOWNLOAD_DIR"]) / "smoke_targets.csv"
        t.to_csv(_targets_path, index=False)
    return str(_targets_path)


def t_xmatch(catalog):
    def run():
        r = server.crossmatch(input_path=targets_path(), catalog=catalog, radius_arcsec=1.5, bands=["r", "J0660"],
                              max_preview_rows=0)
        out = pd.read_csv(r["saved_to"])
        assert len(out) == r["n_input"] == 101
        assert out.loc[out["obj"] == "nowhere", "splus_id"].isna().all()
        assert r["match_fraction"] > 0.9, r["match_fraction"]
        assert r["median_sep_arcsec"] < 1.0
        d = (out["splus_mag_r"] - out["r_idr6"]).dropna()
        return f"{r['n_matched']}/{r['n_input']} sep={r['median_sep_arcsec']}'' dr_auto={d.median():+.3f}"
    return run


def t_xmatch_inline():
    r = server.crossmatch(targets=[{"ra": 0.50109, "dec": -0.50239}, {"ra": NOWHERE[0], "dec": NOWHERE[1]}],
                          catalog="idr6", radius_arcsec=5, all_matches=True, bands=["r"])
    assert r["n_input"] == 2 and r["n_matched"] == 1


def t_sed():
    r = server.get_sed(0.55079, -0.34329, catalogs=["dr3", "dr4", "dr5", "idr6"])
    assert all(s.get("found") for s in r["sources"]), r["sources"]
    assert Path(r["plot"]).exists()
    dr = r["delta_mag_vs_idr6"]
    worst = max(abs(v) for c in dr.values() for b, v in c.items() if v is not None and b != "u")
    assert worst < 0.2, worst
    return f"max |dmag| (sem u) = {worst:.3f}"


def t_compare():
    r = server.compare_releases(*S82, radius_arcsec=600, catalogs=["dr3", "dr4", "dr5"], reference="idr6")
    out = {}
    for c in r["comparisons"]:
        assert c["n_matched"] > 0.8 * c["n_reference"], c
        assert c["median_sep_arcsec"] < 0.5
        dr = c["delta_mag"]["r"]["median"]
        assert abs(dr) < 0.15, (c["catalog"], dr)
        out[c["catalog"]] = round(dr, 3)
    return f"median dr_pstotal vs idr6: {out}"


def t_fits(pos, dr, band="R", weight=False):
    def run():
        r = server.get_cutout(*pos, size=32, band=band, data_release=dr, weight=weight)
        with fits.open(r["saved_to"]) as h:
            data = next(x.data for x in h if x.data is not None)
        assert data.shape == (32, 32)
        return r["data_release"]
    return run


def t_images():
    a = server.get_rgb_image(*SOUTH, size=64, data_release="dr5")
    b = server.get_trilogy_image(*SOUTH, size=64, data_release="idr6")
    assert Path(a["saved_to"]).stat().st_size > 1000 and Path(b["saved_to"]).stat().st_size > 1000


def t_batch():
    t = pd.read_csv(targets_path())
    subset = pd.concat([t.head(3), t.tail(1)])  # 3 validos + nowhere
    r = server.batch_cutouts(targets=subset.to_dict("records"), bands=["r", "F660"], size=32, name_col="obj")
    assert r["n_files_ok"] == 6 and r["n_errors"] == 2, r
    manifest = pd.read_csv(r["manifest"])
    assert set(manifest.loc[manifest["status"] == "error", "name"]) == {"nowhere"}


check("list_data_releases", t_releases)
check("describe_table", t_describe)
check("check_coords S82", t_check_coords(S82, set(CATALOGS)))
check("check_coords fora", t_check_coords(NOWHERE, set()))
check("query_catalog", t_query)
for c in CATALOGS:
    check(f"cone_search {c}", t_cone(c))
check("cone_search filtro+limite", t_cone_filter)
for c in CATALOGS:
    check(f"crossmatch {c}", t_xmatch(c))
check("crossmatch inline all_matches", t_xmatch_inline)
check("get_sed 4 DRs", t_sed)
check("compare_releases S82", t_compare)
check("get_cutout dr4", t_fits(S82, "dr4"))
check("get_cutout dr5 F660", t_fits(SOUTH, "dr5", band="F660"))
check("get_cutout idr6 weight", t_fits(SOUTH, "idr6", weight=True))
check("get_cutout auto", t_fits(S82, None))
check("rgb + trilogy", t_images)
check("batch_cutouts", t_batch)

n_fail = sum(1 for r in results if r[0] != "OK")
print(f"\n{len(results) - n_fail}/{len(results)} ok  (arquivos em {os.environ['SPLUS_DOWNLOAD_DIR']})")
sys.exit(1 if n_fail else 0)
