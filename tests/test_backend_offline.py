"""Backend com o servidor simulado: valida juncoes, esquema e casos vazios sem rede."""
import threading
import time

import numpy as np
import pandas as pd
import pytest

from splus_mcp import backend


class FakeCore:
    """Imita splusdata.Core.query: conta concorrencia e falha com 429 nas primeiras chamadas."""

    def __init__(self, fail_first=0):
        self.active = 0
        self.peak = 0
        self.calls = 0
        self.fail_first = fail_first
        self.lock = threading.Lock()

    def query(self, query, **kwargs):
        with self.lock:
            self.calls += 1
            if self.calls <= self.fail_first:
                raise RuntimeError('{"code":429,"message":"You have reached the maximum number of concurrent async queries (4)."}')
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.02)
        with self.lock:
            self.active -= 1
        return pd.DataFrame({"q": [query]})


def test_run_queries_respects_server_concurrency_limit(monkeypatch):
    fake = FakeCore()
    monkeypatch.setattr(backend, "get_core", lambda: fake)
    out = backend.run_queries([f"SELECT {i}" for i in range(13)])
    assert [df["q"].iloc[0] for df in out] == [f"SELECT {i}" for i in range(13)]  # ordem preservada
    assert fake.peak <= backend.MAX_CONCURRENT_QUERIES


def test_run_query_retries_on_rate_limit(monkeypatch):
    fake = FakeCore(fail_first=2)
    monkeypatch.setattr(backend, "get_core", lambda: fake)
    monkeypatch.setattr(backend.time, "sleep", lambda s: None)
    assert backend.run_query("SELECT 1")["q"].iloc[0] == "SELECT 1"
    assert fake.calls == 3


def test_parallel_uploads_get_unique_names(monkeypatch):
    seen = []

    class Recorder:
        def query(self, query, table_upload=None, table_name=None, **kwargs):
            seen.append((query, table_name, len(table_upload)))
            return pd.DataFrame()

    monkeypatch.setattr(backend, "get_core", lambda: Recorder())
    up = pd.DataFrame({"xm_row": [0, 1], "xm_ra": [1.0, 2.0], "xm_dec": [3.0, 4.0]})
    q = f"SELECT * FROM upload.{backend.UPLOAD_PLACEHOLDER} AS u"
    backend.run_queries([q + " -- a", q + " -- b", q + " -- c"], upload=up)

    names = [name for _, name, _ in seen]
    assert len(set(names)) == 3  # nomes iguais colidem no servidor (UniqueViolation)
    for query, name, n in seen:
        assert f"upload.{name} " in query and backend.UPLOAD_PLACEHOLDER not in query
        assert n == 2


def test_run_query_does_not_retry_other_errors(monkeypatch):
    class Broken:
        calls = 0

        def query(self, *a, **k):
            Broken.calls += 1
            raise RuntimeError("syntax error")

    monkeypatch.setattr(backend, "get_core", lambda: Broken())
    with pytest.raises(RuntimeError, match="syntax"):
        backend.run_query("SELEC 1")
    assert Broken.calls == 1


@pytest.fixture
def fake_server(monkeypatch):
    """Substitui run_queries; `responses` mapeia um trecho da query -> DataFrame."""
    responses = {}
    seen = []

    def run_queries(queries, **kwargs):
        seen.extend(queries)
        out = []
        for q in queries:
            match = [df for key, df in responses.items() if key in q]
            out.append(match[0].copy() if match else pd.DataFrame())
        return out

    monkeypatch.setattr(backend, "run_queries", run_queries)
    return responses, seen


def test_cone_search_dr4_merges_band_tables(fake_server):
    responses, seen = fake_server
    responses["dr4_dual_detection"] = pd.DataFrame(
        {"ID": ["a", "b"], "RA": [0.5, 0.5 + 10 / 3600], "DEC": [-0.5, -0.5], "Field": ["S82", "S82"]})
    responses["dr4_dual_r "] = pd.DataFrame({"ID": ["b", "a"], "r_auto": [19.0, 17.0], "e_r_auto": [0.1, 0.01]})
    responses["dr4_dual_j0660 "] = pd.DataFrame({"ID": ["a"], "J0660_auto": [99.0], "e_J0660_auto": [99.0]})

    df = backend.cone_search("dr4", 0.5, -0.5, 30, ["r", "J0660"], "auto")

    assert len(seen) == 3
    assert list(df.columns) == ["dist_arcsec", "id", "ra", "dec", "field", "mag_r", "err_r", "mag_J0660", "err_J0660"]
    assert df["id"].tolist() == ["a", "b"]  # ordenado por distancia
    assert df["mag_r"].tolist() == [17.0, 19.0]  # juncao por ID, nao por posicao
    assert df["mag_J0660"].isna().all()  # 99 = nao-deteccao; 'b' ausente na tabela
    assert df["dist_arcsec"].iloc[1] == pytest.approx(10, rel=1e-3)


def test_cone_search_empty_result_keeps_schema(fake_server):
    df = backend.cone_search("idr6", 150.0, 60.0, 30, ["r"], "auto")
    assert df.empty
    assert list(df.columns) == ["dist_arcsec", "id", "ra", "dec", "field", "mag_r", "err_r"]


def test_cone_search_mag_filter_and_limit(fake_server):
    responses, _ = fake_server
    responses["idr6.idr6"] = pd.DataFrame({
        "id": list("abc"), "ra": [16.0, 16.001, 16.002], "dec": [-25.0] * 3, "field": ["F"] * 3,
        "mag_auto_r": [15.0, 22.0, 18.0], "err_mag_auto_r": [0.01, 0.2, 0.03]})
    df = backend.cone_search("idr6", 16.0, -25.0, 30, ["r"], "auto", mag_max=20, max_rows=1)
    assert df["id"].tolist() == ["a"]
    df = backend.cone_search("idr6", 16.0, -25.0, 30, ["r"], "auto", mag_max=20)
    assert df["id"].tolist() == ["a", "c"]


def test_cone_search_rejects_huge_radius(fake_server):
    with pytest.raises(ValueError):
        backend.cone_search("idr6", 16.0, -25.0, 7200)


def test_compare_frames_recovers_injected_offsets():
    rng = np.random.default_rng(3)
    n = 400
    ref = pd.DataFrame({
        "id": [f"r{i}" for i in range(n)],
        "ra": 10 + rng.uniform(0, 0.1, n), "dec": -30 + rng.uniform(0, 0.1, n),
        "mag_r": rng.uniform(13, 22, n), "mag_g": rng.uniform(13, 22, n),
    })
    other = ref.copy()
    other["id"] = [f"o{i}" for i in range(n)]
    other["dec"] += 0.1 / 3600  # 0.1'' para o norte
    other["mag_r"] += 0.05 + rng.normal(0, 0.01, n)
    other["mag_g"] -= 0.02
    other = other.iloc[: n - 50]  # 50 fontes da referencia sem contrapartida

    summary, matched = backend.compare_frames({"idr6": ref, "dr5": other}, "idr6", ["r", "g"], 1.0, (14, 19))
    [row] = summary
    in_range = ref["mag_r"].between(14, 19)
    assert row["n_reference"] == in_range.sum()
    assert row["n_matched"] == in_range.iloc[: n - 50].sum()
    assert row["median_ddec_arcsec"] == pytest.approx(0.1, abs=1e-3)
    assert row["median_dra_arcsec"] == pytest.approx(0.0, abs=1e-3)
    assert row["delta_mag"]["r"]["median"] == pytest.approx(0.05, abs=0.005)
    assert row["delta_mag"]["r"]["sigma_mad"] == pytest.approx(0.01, abs=0.004)
    assert row["delta_mag"]["g"]["median"] == pytest.approx(-0.02, abs=1e-6)
    assert len(matched) == row["n_matched"]
    assert {"idr6_mag_r", "dr5_mag_r", "dist_arcsec"} <= set(matched.columns)


def _targets():
    return pd.DataFrame({"name": ["t0", "t1", "t2"], "RA": [16.0, 16.1, 150.0], "DEC": [-25.0, -25.0, 60.0],
                         "extra": [1, 2, 3]})


def test_crossmatch_server_keeps_all_inputs(fake_server):
    responses, seen = fake_server
    responses["q3c_join"] = pd.DataFrame({
        "xm_row": [0, 0, 1],
        "id": ["near", "far", "other"],
        "ra": [16.0 + 0.2 / 3600, 16.0 + 0.9 / 3600, 16.1],
        "dec": [-25.0, -25.0, -25.0],
        "field": ["F"] * 3,
        "mag_auto_r": [17.0, 18.0, 19.0],
        "err_mag_auto_r": [0.01, 0.02, 0.03],
    })
    out = backend.crossmatch(_targets(), "RA", "DEC", "idr6", 1.0, ["r"], "auto")

    assert "upload." in seen[0]
    assert len(out) == 3
    assert out["name"].tolist() == ["t0", "t1", "t2"]
    assert out["splus_id"].tolist()[:2] == ["near", "other"]
    assert out["splus_n_candidates"].tolist()[:2] == [2, 1]
    assert np.isnan(out["splus_dist_arcsec"].iloc[2])  # sem contrapartida continua na saida
    assert out["extra"].tolist() == [1, 2, 3]


def test_crossmatch_all_matches(fake_server):
    responses, _ = fake_server
    responses["q3c_join"] = pd.DataFrame({
        "xm_row": [0, 0], "id": ["near", "far"], "ra": [16.0, 16.0 + 0.9 / 3600], "dec": [-25.0, -25.0],
        "field": ["F"] * 2, "mag_auto_r": [17.0, 18.0], "err_mag_auto_r": [0.01, 0.02]})
    out = backend.crossmatch(_targets(), "RA", "DEC", "idr6", 1.0, ["r"], "auto", all_matches=True)
    assert len(out) == 4  # t0 duas vezes + t1 e t2 sem par
    assert out.loc[out["input_row"] == 0, "splus_id"].tolist() == ["near", "far"]


def test_crossmatch_refilters_radius(fake_server):
    responses, _ = fake_server
    responses["q3c_join"] = pd.DataFrame({
        "xm_row": [0], "id": ["edge"], "ra": [16.0 + 1.5 / 3600], "dec": [-25.0], "field": ["F"],
        "mag_auto_r": [17.0], "err_mag_auto_r": [0.01]})
    out = backend.crossmatch(_targets(), "RA", "DEC", "idr6", 1.0, ["r"], "auto")
    assert out["splus_id"].isna().all()


def test_crossmatch_client_path_for_dr3(fake_server):
    responses, seen = fake_server
    responses["dr3.all_dr3"] = pd.DataFrame({
        "ID": ["x", "y"], "RA": [16.0 + 0.3 / 3600, 16.1], "DEC": [-25.0, -25.0 + 0.1 / 3600], "Field": ["F", "F"],
        "r_auto": [17.0, 99.0], "e_r_auto": [0.01, 99.0]})
    out = backend.crossmatch(_targets(), "RA", "DEC", "dr3", 1.0, ["r"], "auto")

    assert not any("q3c_join" in q for q in seen)
    assert len(seen) == 2  # (16.0,-25) e (16.1,-25) no mesmo grupo; (150, 60) em outro
    assert out["splus_id"].tolist()[:2] == ["x", "y"]
    assert np.isnan(out["splus_mag_r"].iloc[1])  # 99 -> NaN


def test_crossmatch_dr4_server_merges_bands(fake_server):
    responses, seen = fake_server
    responses["dr4_dual_detection"] = pd.DataFrame(
        {"xm_row": [0], "ID": ["d1"], "RA": [16.0], "DEC": [-25.0], "Field": ["F"]})
    responses["dr4_dual_r "] = pd.DataFrame({"xm_row": [0], "ID": ["d1"], "r_auto": [17.2], "e_r_auto": [0.01]})
    out = backend.crossmatch(_targets(), "RA", "DEC", "dr4", 1.0, ["r"], "auto")
    assert len(seen) == 2
    assert out["splus_mag_r"].iloc[0] == 17.2


def test_crossmatch_ignores_invalid_coords(fake_server):
    t = pd.DataFrame({"ra": [16.0, None, "abc"], "dec": [-25.0, -25.0, -25.0]})
    out = backend.crossmatch(t, "ra", "dec", "idr6", 1.0, ["r"], "auto")
    assert len(out) == 3
    assert out["splus_id"].isna().all()
