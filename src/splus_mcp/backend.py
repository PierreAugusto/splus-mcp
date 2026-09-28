"""Operacoes de dados sobre o splus.cloud (via splusdata.Core / ADSS)."""
import contextlib
import logging
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from splus_mcp import catalogs as cat
from splus_mcp.matching import angular_sep_arcsec, group_targets, nearest_per_row, pairs_within
from splus_mcp.releases import POINTING_DR_TO_COLLECTION, resolve_collection

# O adss loga cada requisicao HTTP em INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)

QUERY_TIMEOUT = 600
# O splus.cloud aceita no maximo 4 queries assincronas simultaneas por usuario (HTTP 429).
MAX_CONCURRENT_QUERIES = 4
RATE_LIMIT_RETRIES = 8
MAX_WORKERS = MAX_CONCURRENT_QUERIES
XMATCH_CHUNK = 5000
MAX_CONE_RADIUS_ARCSEC = 1800  # 0.5 grau, ~25 mil fontes no idr6

_core = None
_CORE_LOCK = threading.Lock()
_QUERY_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_QUERIES)


@contextlib.contextmanager
def quiet():
    """splusdata/adss usam print(); em transporte stdio isso corromperia o JSON-RPC.
    Deve envolver tambem os pools de threads (sys.stdout e global)."""
    with contextlib.redirect_stdout(sys.stderr):
        yield


def get_core():
    global _core
    with _CORE_LOCK:
        if _core is not None:
            return _core

        username = os.environ.get("SPLUS_USER")
        password = os.environ.get("SPLUS_PASS")
        if not username or not password:
            raise RuntimeError(
                "SPLUS_USER e SPLUS_PASS precisam estar definidos (via .env ou variaveis de "
                "ambiente) antes de usar qualquer tool do S-PLUS."
            )

        import splusdata

        with quiet():
            _core = splusdata.Core(username, password)
        return _core


def run_query(query: str, upload: pd.DataFrame | None = None, upload_name: str | None = None,
              timeout: int = QUERY_TIMEOUT) -> pd.DataFrame:
    core = get_core()
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            with _QUERY_SLOTS, quiet():
                result = core.query(query, table_upload=upload, table_name=upload_name, timeout=timeout)
            break
        except Exception as e:
            # Outras sessoes (ex: site splus.cloud) tambem contam no limite do servidor.
            if "concurrent async queries" not in str(e) or attempt == RATE_LIMIT_RETRIES:
                raise
            time.sleep(min(2 * (attempt + 1), 10))
    return result if isinstance(result, pd.DataFrame) else pd.DataFrame(result)


UPLOAD_PLACEHOLDER = "__UPLOAD__"


def run_queries(queries: list[str], upload: pd.DataFrame | None = None) -> list[pd.DataFrame]:
    """Executa varias queries em paralelo, preservando a ordem.

    Com `upload`, cada query envia a sua propria copia da tabela com um nome unico, que
    substitui UPLOAD_PLACEHOLDER no texto: o servidor cria uma view com o nome dado e
    queries simultaneas com o mesmo nome colidem (UniqueViolation)."""
    def one(query: str) -> pd.DataFrame:
        if upload is None:
            return run_query(query)
        name = "xm" + uuid.uuid4().hex[:12]
        return run_query(query.replace(UPLOAD_PLACEHOLDER, name), upload=upload, upload_name=name)

    if len(queries) == 1:
        return [one(queries[0])]
    with quiet(), ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(queries))) as pool:
        return list(pool.map(one, queries))


# ---------------------------------------------------------------- catalogos

def _standard(df: pd.DataFrame, catalog: cat.Catalog, bands: list[str], aperture: str,
              extra: tuple[str, ...] = ()) -> pd.DataFrame:
    """standardize_columns + garante as colunas mesmo quando o resultado vem vazio."""
    df = cat.standardize_columns(df, catalog, bands, aperture)
    for col in (*extra, "id", "ra", "dec", "field"):
        if col not in df:
            df[col] = pd.Series(dtype=object if col in ("id", "field") else float)
    return df


def _merge_band_tables(frames: list[pd.DataFrame], catalog: cat.Catalog, bands: list[str], aperture: str,
                       keys: list[str]) -> pd.DataFrame:
    """Junta o resultado da tabela de deteccao com os das tabelas por banda (dr4)."""
    extra = tuple(k for k in keys if k != "id")
    base = _standard(frames[0], catalog, bands, aperture, extra)
    for band_df in frames[1:]:
        band_df = cat.standardize_columns(band_df, catalog, bands, aperture)
        if band_df.empty or not set(keys) <= set(band_df.columns):
            continue
        band_df = band_df[[c for c in band_df.columns if c in keys or c.startswith(("mag_", "err_"))]]
        base = base.merge(band_df.drop_duplicates(keys), on=keys, how="left")
    return base


def _ordered_columns(df: pd.DataFrame, bands: list[str], leading: list[str]) -> pd.DataFrame:
    """Ordena colunas e garante mag_/err_ de todas as bandas (NaN se a banda nao veio)."""
    df = df.copy()
    for b in bands:
        for col in (f"mag_{b}", f"err_{b}"):
            if col not in df:
                df[col] = np.nan
    cols = [c for c in leading if c in df]
    for b in bands:
        cols += [f"mag_{b}", f"err_{b}"]
    cols += [c for c in df.columns if c not in cols]
    return df[cols]


def cone_search(catalog_name: str, ra: float, dec: float, radius_arcsec: float, bands: list[str] | None = None,
                aperture: str = "auto", mag_max: float | None = None, mag_band: str = "r",
                max_rows: int | None = None) -> pd.DataFrame:
    """Fontes do catalogo dentro do raio, com esquema padronizado e ordenadas por distancia."""
    catalog = cat.get_catalog(catalog_name)
    bands = [cat.normalize_band(b) for b in (bands or cat.BANDS)]
    aperture = cat.normalize_aperture(aperture)
    if radius_arcsec <= 0 or radius_arcsec > MAX_CONE_RADIUS_ARCSEC:
        raise ValueError(f"radius_arcsec deve estar em (0, {MAX_CONE_RADIUS_ARCSEC}]")
    mag_band = cat.normalize_band(mag_band)
    if mag_max is not None and mag_band not in bands:
        bands.append(mag_band)

    queries = cat.build_cone_queries(catalog, ra, dec, radius_arcsec / 3600, bands, aperture)
    frames = run_queries(queries)
    if catalog.band_table:
        df = _merge_band_tables(frames, catalog, bands, aperture, keys=["id"])
    else:
        df = _standard(frames[0], catalog, bands, aperture)

    df.insert(0, "dist_arcsec", angular_sep_arcsec(ra, dec, df["ra"], df["dec"]) if len(df) else [])
    if mag_max is not None:
        df = df[df[f"mag_{mag_band}"] <= mag_max]
    df = df.sort_values("dist_arcsec").reset_index(drop=True)
    if max_rows:
        df = df.head(max_rows)
    return _ordered_columns(df, bands, ["dist_arcsec", "id", "ra", "dec", "field"])


def catalog_coverage(ra: float, dec: float, radius_arcsec: float = 60) -> list[dict]:
    """Numero de fontes de cada catalogo num cone pequeno: cobertura real, nao so apontamentos."""
    names = list(cat.CATALOGS)
    queries = []
    for name in names:
        c = cat.CATALOGS[name]
        where = cat.cone_condition(c.ra, c.dec, ra, dec, radius_arcsec / 3600)
        queries.append(f"SELECT {c.id} FROM {c.table} WHERE {where}")

    def safe(q):
        try:
            return len(run_query(q, timeout=120)), None
        except Exception as e:  # um catalogo sem permissao nao deve derrubar os outros
            return None, f"{type(e).__name__}: {str(e)[:200]}"

    with quiet(), ThreadPoolExecutor(max_workers=len(queries)) as pool:
        results = list(pool.map(safe, queries))
    return [
        {"catalog": n, "n_sources": count, **({"error": err} if err else {})}
        for n, (count, err) in zip(names, results)
    ]


# ---------------------------------------------------------------- cross-match

def _xmatch_server(inputs: pd.DataFrame, catalog: cat.Catalog, radius_arcsec: float,
                   bands: list[str], aperture: str) -> pd.DataFrame:
    """Pares dentro do raio via q3c_join no servidor, em blocos."""
    pairs = []
    for start in range(0, len(inputs), XMATCH_CHUNK):
        chunk = inputs.iloc[start:start + XMATCH_CHUNK]
        queries = cat.build_xmatch_queries(catalog, UPLOAD_PLACEHOLDER, radius_arcsec / 3600, bands, aperture)
        frames = run_queries(queries, upload=chunk)
        if catalog.band_table:
            pairs.append(_merge_band_tables(frames, catalog, bands, aperture, keys=["xm_row", "id"]))
        else:
            pairs.append(_standard(frames[0], catalog, bands, aperture, extra=("xm_row",)))
    return pd.concat(pairs, ignore_index=True)


def _xmatch_client(inputs: pd.DataFrame, catalog: cat.Catalog, radius_arcsec: float,
                   bands: list[str], aperture: str) -> pd.DataFrame:
    """Pares dentro do raio via buscas conicas agrupadas + casamento local (sem q3c)."""
    groups = group_targets(inputs["xm_ra"], inputs["xm_dec"], max_radius_deg=0.25)
    queries = [
        cat.build_cone_queries(catalog, gra, gdec, grad + radius_arcsec / 3600, bands, aperture)[0]
        for gra, gdec, grad, _ in groups
    ]
    frames = run_queries(queries)

    pairs = []
    for (_, _, _, idx), frame in zip(groups, frames):
        cand = _standard(frame, catalog, bands, aperture)
        sub = inputs.iloc[idx]
        i, j, _ = pairs_within(sub["xm_ra"], sub["xm_dec"], cand["ra"], cand["dec"], radius_arcsec)
        matched = cand.iloc[j].reset_index(drop=True)
        matched.insert(0, "xm_row", sub["xm_row"].values[i])
        pairs.append(matched)
    return pd.concat(pairs, ignore_index=True) if pairs else pd.DataFrame(columns=["xm_row"])


def crossmatch(targets: pd.DataFrame, ra_col: str, dec_col: str, catalog_name: str, radius_arcsec: float = 1.0,
               bands: list[str] | None = None, aperture: str = "auto", all_matches: bool = False,
               method: str = "auto") -> pd.DataFrame:
    """Casa cada linha de `targets` com o catalogo. Mantem todas as linhas de entrada (left join),
    com dist_arcsec/n_candidates vazios quando nao ha contrapartida."""
    catalog = cat.get_catalog(catalog_name)
    bands = [cat.normalize_band(b) for b in (bands or cat.BANDS)]
    aperture = cat.normalize_aperture(aperture)
    if not 0 < radius_arcsec <= 60:
        raise ValueError("radius_arcsec deve estar em (0, 60]")

    coords = targets[[ra_col, dec_col]].apply(pd.to_numeric, errors="coerce")
    valid = coords.notna().all(axis=1).values
    inputs = pd.DataFrame({
        "xm_row": np.arange(len(targets))[valid],
        "xm_ra": coords[ra_col].values[valid],
        "xm_dec": coords[dec_col].values[valid],
    })

    if method == "auto":
        method = "server" if catalog.q3c else "client"
    if inputs.empty:
        pairs = pd.DataFrame(columns=["xm_row"])
    elif method == "server":
        pairs = _xmatch_server(inputs, catalog, radius_arcsec, bands, aperture)
    elif method == "client":
        pairs = _xmatch_client(inputs, catalog, radius_arcsec, bands, aperture)
    else:
        raise ValueError("method deve ser 'auto', 'server' ou 'client'")

    if pairs.empty:
        matches = pd.DataFrame(columns=["xm_row", "dist_arcsec", "n_candidates", "id", "ra", "dec", "field"])
    else:
        pairs["xm_row"] = pairs["xm_row"].astype(int)
        src = inputs.set_index("xm_row").loc[pairs["xm_row"]]
        pairs.insert(1, "dist_arcsec", angular_sep_arcsec(src["xm_ra"].values, src["xm_dec"].values,
                                                          pairs["ra"].astype(float).values,
                                                          pairs["dec"].astype(float).values))
        # q3c_join usa o raio em graus; refiltra para o limite ficar exato
        pairs = pairs[pairs["dist_arcsec"] <= radius_arcsec]
        matches = pairs if all_matches else nearest_per_row(pairs)

    matched = _ordered_columns(matches, bands, ["xm_row", "dist_arcsec", "n_candidates", "id", "ra", "dec", "field"])
    matched["xm_row"] = matched["xm_row"].astype(int)
    matched = matched.rename(columns={c: f"splus_{c}" for c in matched.columns if c != "xm_row"})
    out = targets.reset_index(drop=True).copy()
    out["xm_row"] = np.arange(len(out))
    out = out.merge(matched, on="xm_row", how="left")
    return out.rename(columns={"xm_row": "input_row"})


# ---------------------------------------------------------------- consistencia entre DRs

def compare_frames(frames: dict[str, pd.DataFrame], reference: str, bands: list[str], match_radius_arcsec: float,
                   mag_range: tuple[float, float], select_band: str = "r") -> tuple[list[dict], pd.DataFrame]:
    """Casa cada catalogo com o de referencia e resume offsets (outro - referencia).

    Usa so fontes da referencia com select_band em mag_range, para evitar ruido de fontes
    fracas e saturacao. Estatisticas robustas: mediana e MAD escalado (1.4826 * MAD)."""
    ref = frames[reference]
    sel = ref[f"mag_{select_band}"].between(*mag_range)
    ref = ref[sel].reset_index(drop=True)

    summary, matched_tables = [], []
    for name, other in frames.items():
        if name == reference:
            continue
        i, j, sep = pairs_within(ref["ra"], ref["dec"], other["ra"], other["dec"], match_radius_arcsec)
        pairs = nearest_per_row(pd.DataFrame({"xm_row": i, "j": j, "dist_arcsec": sep}))
        a = ref.iloc[pairs["xm_row"].to_numpy()].reset_index(drop=True)
        b = other.iloc[pairs["j"].to_numpy()].reset_index(drop=True)

        row = {"catalog": name, "reference": reference, "n_reference": len(ref), "n_matched": len(pairs),
               "median_sep_arcsec": round(float(np.median(pairs["dist_arcsec"])), 4) if len(pairs) else None}
        if len(pairs):
            dra = ((b["ra"] - a["ra"]) * np.cos(np.radians(a["dec"]))).to_numpy(float) * 3600
            ddec = (b["dec"] - a["dec"]).to_numpy(float) * 3600
            row["median_dra_arcsec"] = round(float(np.median(dra)), 4)
            row["median_ddec_arcsec"] = round(float(np.median(ddec)), 4)
        delta_mag = {}
        for band in bands:
            d = (b[f"mag_{band}"] - a[f"mag_{band}"]).dropna().to_numpy(float)
            if len(d) >= 3:
                med = float(np.median(d))
                delta_mag[band] = {"median": round(med, 4), "sigma_mad": round(1.4826 * float(np.median(np.abs(d - med))), 4),
                                   "n": int(len(d))}
            else:
                delta_mag[band] = {"median": None, "sigma_mad": None, "n": int(len(d))}
        row["delta_mag"] = delta_mag
        summary.append(row)

        table = a[["id", "ra", "dec"] + [f"mag_{bd}" for bd in bands]].add_prefix(f"{reference}_")
        table = pd.concat([table, b[["id"] + [f"mag_{bd}" for bd in bands]].add_prefix(f"{name}_")], axis=1)
        table.insert(0, "catalog", name)
        table.insert(1, "dist_arcsec", pairs["dist_arcsec"].to_numpy())
        matched_tables.append(table)

    matched = pd.concat(matched_tables, ignore_index=True) if matched_tables else pd.DataFrame()
    return summary, matched


def compare_releases(ra: float, dec: float, radius_arcsec: float, catalogs: list[str], reference: str,
                     bands: list[str] | None, aperture: str, match_radius_arcsec: float,
                     mag_range: tuple[float, float]) -> tuple[list[dict], pd.DataFrame, dict[str, int]]:
    names = [cat.get_catalog(c).name for c in catalogs]
    reference = cat.get_catalog(reference).name
    if reference not in names:
        names.append(reference)
    bands = [cat.normalize_band(b) for b in (bands or cat.BANDS)]
    if "r" not in bands:  # usada para selecionar a faixa de magnitude
        bands.append("r")

    with quiet(), ThreadPoolExecutor(max_workers=len(names)) as pool:
        results = list(pool.map(lambda n: cone_search(n, ra, dec, radius_arcsec, bands, aperture), names))
    frames = dict(zip(names, results))
    summary, matched = compare_frames(frames, reference, bands, match_radius_arcsec, mag_range)
    return summary, matched, {n: len(f) for n, f in frames.items()}


# ---------------------------------------------------------------- imagens

_collections: list[dict] | None = None


def image_collection(data_release: str) -> dict:
    global _collections
    if _collections is None:
        with quiet():
            _collections = get_core().client.get_collections()
    return resolve_collection(data_release, _collections)


def pointing_coverage(ra: float, dec: float, radius_deg: float = 1.0) -> list[dict]:
    """DRs com centro de campo a ate radius_deg (listas de apontamento do splusdata)."""
    import astropy.units as u
    from astropy.coordinates import SkyCoord
    from splusdata.features.find_pointings import _load_dr

    target = SkyCoord(ra=ra * u.deg, dec=dec * u.deg)
    hits = []
    for dr, collection in POINTING_DR_TO_COLLECTION.items():
        with quiet():
            df = _load_dr(dr)
        sep = target.separation(SkyCoord(df["ra"].values * u.deg, df["dec"].values * u.deg))
        idx = int(sep.argmin())
        if sep[idx].deg <= radius_deg:
            hits.append({
                "data_release": dr,
                "image_collection": collection,
                "field": str(df["field"].iloc[idx]),
                "distance_deg": round(float(sep[idx].deg), 4),
            })
    return hits


def pick_image_collection(ra: float, dec: float, data_release: str | None) -> dict:
    if data_release:
        return image_collection(data_release)
    hits = pointing_coverage(ra, dec)
    if not hits:
        raise RuntimeError(f"Nenhum DR do S-PLUS cobre (ra={ra}, dec={dec}); informe data_release explicitamente.")
    return image_collection(hits[-1]["image_collection"])


def download_stamp(collection: dict, ra: float, dec: float, size: float, band: str, size_unit: str,
                   weight: bool, out_path: Path) -> Path:
    with quiet():
        get_core().client.create_stamp_by_coordinates(
            collection_id=collection["id"], ra=ra, dec=dec, size=size, filter=band, size_unit=size_unit,
            pattern="weight" if weight else "", output_path=str(out_path), timeout=120,
        )
    return out_path


def download_rgb(collection: dict, ra: float, dec: float, size: float, size_unit: str, r_band: str, g_band: str,
                 b_band: str, stretch: float, q: float, out_path: Path) -> Path:
    with quiet():
        get_core().client.create_rgb_image_by_coordinates(
            collection_id=collection["id"], ra=ra, dec=dec, size=size, r_filter=r_band, g_filter=g_band,
            b_filter=b_band, stretch=stretch, Q=q, size_unit=size_unit, output_path=str(out_path),
        )
    return out_path


def download_trilogy(collection: dict, ra: float, dec: float, size: float, size_unit: str, r_bands: list[str],
                     g_bands: list[str], b_bands: list[str], noiselum: float, satpercent: float,
                     colorsatfac: float, out_path: Path) -> Path:
    with quiet():
        get_core().client.trilogy_images.create_trilogy_rgb_by_coordinates(
            collection_id=collection["id"], ra=ra, dec=dec, size=size, r_filters=r_bands, g_filters=g_bands,
            b_filters=b_bands, size_unit=size_unit, noiselum=noiselum, satpercent=satpercent,
            colorsatfac=colorsatfac, output_path=str(out_path),
        )
    return out_path
