import numpy as np
import pandas as pd
import pytest

from splus_mcp.matching import angular_sep_arcsec, find_coord_columns, group_targets, nearest_per_row, pairs_within


@pytest.mark.parametrize(
    "cols, expected",
    [(["name", "ra", "dec"], ("ra", "dec")), (["RAJ2000", "DEJ2000"], ("RAJ2000", "DEJ2000")),
     (["RA", "DEC", "z"], ("RA", "DEC")), (["ra_deg", "dec_deg"], ("ra_deg", "dec_deg"))],
)
def test_find_coord_columns(cols, expected):
    assert find_coord_columns(pd.DataFrame(columns=cols)) == expected


def test_find_coord_columns_explicit_and_missing():
    df = pd.DataFrame(columns=["alpha", "delta"])
    assert find_coord_columns(df, "ALPHA", "delta") == ("alpha", "delta")
    with pytest.raises(ValueError, match="RA"):
        find_coord_columns(df)


def test_angular_sep():
    assert angular_sep_arcsec(10, 0, 10, 1) == pytest.approx(3600)
    # 1 grau em RA a dec=60 vale cos(60) = 0.5 grau
    assert angular_sep_arcsec(10, 60, 11, 60) == pytest.approx(1800, rel=1e-3)
    # atravessando RA=0
    assert angular_sep_arcsec(359.9999, 0, 0.0001, 0) == pytest.approx(0.72, rel=1e-3)


def test_group_targets_covers_all_within_radius():
    rng = np.random.default_rng(1)
    ra = np.concatenate([rng.uniform(10, 10.3, 50), rng.uniform(200, 200.1, 20), [45.0]])
    dec = np.concatenate([rng.uniform(-30, -29.7, 50), rng.uniform(5, 5.1, 20), [-60.0]])
    groups = group_targets(ra, dec, max_radius_deg=0.25)

    seen = np.sort(np.concatenate([g[3] for g in groups]))
    assert np.array_equal(seen, np.arange(len(ra)))
    for gra, gdec, rad, idx in groups:
        assert rad <= 0.25
        assert np.all(angular_sep_arcsec(gra, gdec, ra[idx], dec[idx]) / 3600 <= rad + 1e-9)
    assert len(groups) < len(ra)


def test_pairs_within_and_nearest():
    in_ra, in_dec = [10.0, 20.0], [0.0, 0.0]
    cand_ra = [10.0, 10.0 + 0.5 / 3600, 30.0]
    cand_dec = [0.2 / 3600, 0.0, 0.0]
    i, j, s = pairs_within(in_ra, in_dec, cand_ra, cand_dec, 1.0)
    assert sorted(zip(i.tolist(), j.tolist())) == [(0, 0), (0, 1)]

    pairs = pd.DataFrame({"xm_row": i, "cand": j, "dist_arcsec": s})
    best = nearest_per_row(pairs)
    assert best["cand"].tolist() == [0]
    assert best["n_candidates"].tolist() == [2]


def test_pairs_within_empty():
    i, j, s = pairs_within([], [], [1.0], [1.0], 1.0)
    assert len(i) == len(j) == len(s) == 0
    i, j, s = pairs_within([1.0], [1.0], [], [], 1.0)
    assert len(i) == 0
