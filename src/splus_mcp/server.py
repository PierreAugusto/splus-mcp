import functools
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from splus_mcp import backend  # noqa: E402  (depois do .env)
from splus_mcp import catalogs as cat  # noqa: E402
from splus_mcp.matching import find_coord_columns  # noqa: E402
from splus_mcp.sed import plot_sed, sed_table  # noqa: E402

DOWNLOAD_DIR = Path(os.environ.get("SPLUS_DOWNLOAD_DIR", "./splus_downloads")).expanduser().resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_BATCH_FILES = 2000

mcp = MCPServer(
    "splus",
    instructions=(
        "Acesso ao S-PLUS (splus.cloud). Catalogos com esquema padronizado (cone_search, crossmatch, "
        "get_sed): " + ", ".join(cat.CATALOGS) + ". Colunas de saida: id, ra, dec, field, mag_<banda>, "
        "err_<banda>, com bandas u J0378 J0395 J0410 J0430 g J0515 r J0660 i J0861 z; nao-deteccoes "
        "viram vazio. Para queries livres use list_tables/describe_table antes de query_catalog. "
        "Tools de imagem aceitam data_release (dr4, dr5, idr6...); se omitido, usa o DR mais novo "
        "que cobre a coordenada. Arquivos sao salvos em disco e o caminho vem na resposta."
    ),
)


logger = logging.getLogger("splus_mcp")


def tool(fn):
    """@mcp.tool() que repassa a mensagem de erro ao cliente.

    O SDK so mostra ao modelo o texto de ToolError; qualquer outra excecao vira um
    "Error executing tool X" generico, e o modelo nao consegue corrigir a chamada
    (catalogo invalido, erro de sintaxe na query, coluna inexistente...)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as e:
            logger.warning("tool %s falhou", fn.__name__, exc_info=True)
            message = f"{type(e).__name__}: {e}"
            password = os.environ.get("SPLUS_PASS")
            if password:
                message = message.replace(password, "***")
            raise ToolError(message[:2000]) from e

    return mcp.tool()(wrapper)


def _unique_path(stem: str, suffix: str, directory: Path = DOWNLOAD_DIR) -> Path:
    stem = "".join(ch if ch.isalnum() or ch in "._+-" else "_" for ch in stem)
    path = directory / f"{stem}{suffix}"
    i = 1
    while path.exists():
        path = directory / f"{stem}_{i}{suffix}"
        i += 1
    return path


def _coords(ra: float, dec: float) -> str:
    return f"{ra:.5f}_{dec:+.5f}"


def _records(df: pd.DataFrame, n: int) -> list[dict]:
    """Primeiras n linhas em JSON puro (NaN -> null, tipos numpy -> nativos)."""
    return json.loads(df.head(n).to_json(orient="records", double_precision=6))


def _read_table(path: str) -> pd.DataFrame:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"arquivo nao encontrado: {p}")
    suffix = p.suffix.lower()
    if suffix in (".csv", ".txt", ".tsv", ".dat"):
        return pd.read_csv(p, sep=None, engine="python")
    if suffix == ".parquet":
        return pd.read_parquet(p)
    from astropy.table import Table

    return Table.read(p).to_pandas()


def _targets_frame(input_path: str | None, targets: list[dict] | None) -> pd.DataFrame:
    if bool(input_path) == bool(targets):
        raise ValueError("informe input_path (arquivo CSV/FITS/VOTable/parquet) ou targets (lista de {ra, dec, ...})")
    return _read_table(input_path) if input_path else pd.DataFrame(targets)


# ---------------------------------------------------------------- descoberta

@tool
def list_data_releases() -> dict:
    """Lista o que a sua conta acessa: colecoes de imagem, schemas de catalogo e os catalogos
    com esquema padronizado usados por cone_search/crossmatch/get_sed."""
    core = backend.get_core()
    with backend.quiet():
        collections = core.client.get_collections()
        schemas = core.client.get_schemas()
    return {
        "image_collections": [c["name"] for c in collections],
        "catalog_schemas": sorted(schemas),
        "standard_catalogs": {name: f"{c.table} - {c.description}" for name, c in cat.CATALOGS.items()},
    }


@tool
def list_tables(schema: str) -> list[str]:
    """Lista as tabelas de um schema de catalogo (ex: 'dr5', 'idr6', 'dr4_dual', 'dr5_vacs')."""
    with backend.quiet():
        return backend.get_core().client.get_tables(schema)


@tool
def describe_table(schema: str, table: str, name_filter: str | None = None) -> dict:
    """Lista as colunas (nome e tipo) de uma tabela de catalogo. Tabelas grandes tem centenas
    de colunas; use name_filter (substring, case-insensitive) para restringir, ex: 'r_' ou 'photoz'."""
    with backend.quiet():
        columns = backend.get_core().client.get_columns(schema, table)
    cols = [{"name": c.name, "type": c.data_type} for c in columns]
    if name_filter:
        cols = [c for c in cols if name_filter.lower() in c["name"].lower()]
    return {"table": f"{schema}.{table}", "n_columns": len(cols), "columns": cols}


@tool
def check_coords(ra: float, dec: float, radius_arcsec: float = 60, verify_catalogs: bool = True) -> dict:
    """Verifica a cobertura do S-PLUS numa posicao.

    - pointings: DRs (dr4, dr5, dr6) com centro de campo a ate 1 grau, pelas listas de
      apontamentos; rapido, mas campo proximo nao garante que a posicao tenha dados.
    - catalogs: quantas fontes cada catalogo padronizado tem a ate radius_arcsec, consultando
      os dados reais (inclui dr3 e idr5, que nao tem lista de apontamentos).
    """
    result = {"ra": ra, "dec": dec, "pointings": backend.pointing_coverage(ra, dec)}
    if verify_catalogs:
        result["catalogs"] = backend.catalog_coverage(ra, dec, radius_arcsec)
    return result


# ---------------------------------------------------------------- catalogos

@tool
def query_catalog(query: str, max_preview_rows: int = 20) -> dict:
    """Executa uma query (ADQL/SQL) no banco do S-PLUS e salva o resultado completo em CSV.
    Retorna preview, contagem de linhas e caminho do arquivo.

    Tabelas sao referenciadas como schema.tabela, ex:
      SELECT TOP 10 ra, dec, field FROM idr6.idr6
      SELECT TOP 10 * FROM dr5_vacs.dr5_photoz_v3
    Busca conica: WHERE 1=CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', ra0, dec0, raio_graus)).
    Use list_tables/describe_table para descobrir nomes de tabelas e colunas.
    """
    df = backend.run_query(query)
    out_path = _unique_path("query_result", ".csv")
    df.to_csv(out_path, index=False)
    return {
        "row_count": len(df),
        "columns": list(df.columns),
        "preview": _records(df, max_preview_rows),
        "saved_to": str(out_path),
    }


@tool
def cone_search(
    ra: float,
    dec: float,
    radius_arcsec: float = 60,
    catalog: str = "idr6",
    bands: list[str] | None = None,
    aperture: str = "auto",
    mag_max: float | None = None,
    mag_band: str = "r",
    max_rows: int | None = None,
    max_preview_rows: int = 10,
) -> dict:
    """Busca conica num catalogo do S-PLUS, sem escrever SQL. Salva CSV ordenado por distancia.

    Args:
        ra, dec: centro em graus.
        radius_arcsec: raio (max 1800).
        catalog: idr6 (padrao), dr5, idr5, dr4 ou dr3.
        bands: bandas a incluir (padrao: as 12). Aceita 'r', 'R', 'F660', 'J0660'...
        aperture: auto, petro, pstotal, aper_3, aper_6 ou iso.
        mag_max: se informado, so fontes com mag_<mag_band> <= mag_max.
        max_rows: limita as N fontes mais proximas.
    Colunas de saida: dist_arcsec, id, ra, dec, field, mag_<banda>, err_<banda>.
    """
    df = backend.cone_search(catalog, ra, dec, radius_arcsec, bands, aperture, mag_max, mag_band, max_rows)
    out_path = _unique_path(f"cone_{catalog}_{_coords(ra, dec)}_{radius_arcsec}as", ".csv")
    df.to_csv(out_path, index=False)
    return {
        "catalog": catalog,
        "aperture": aperture,
        "row_count": len(df),
        "saved_to": str(out_path),
        "preview": _records(df, max_preview_rows),
    }


@tool
def crossmatch(
    input_path: str | None = None,
    targets: list[dict] | None = None,
    catalog: str = "idr6",
    radius_arcsec: float = 1.0,
    ra_col: str | None = None,
    dec_col: str | None = None,
    bands: list[str] | None = None,
    aperture: str = "auto",
    all_matches: bool = False,
    max_preview_rows: int = 5,
) -> dict:
    """Cross-match de uma lista de posicoes com um catalogo do S-PLUS.

    Entrada: input_path (CSV/TSV, FITS, VOTable ou parquet) ou targets (lista de dicts com ra/dec).
    As colunas de RA/Dec (graus) sao detectadas pelo nome (ra, RAJ2000, ra_deg...) ou informadas.
    Saida: todas as colunas/linhas de entrada + splus_dist_arcsec, splus_n_candidates, splus_id,
    splus_ra, splus_dec, splus_field, splus_mag_<banda>, splus_err_<banda> (vazio sem contrapartida).
    all_matches=True devolve todos os candidatos dentro do raio em vez do mais proximo.
    idr6/dr5/idr5/dr4 casam no servidor (q3c); dr3 usa buscas conicas agrupadas.
    """
    df = _targets_frame(input_path, targets)
    ra_c, dec_c = find_coord_columns(df, ra_col, dec_col)
    out = backend.crossmatch(df, ra_c, dec_c, catalog, radius_arcsec, bands, aperture, all_matches)

    stem = f"xmatch_{Path(input_path).stem if input_path else 'targets'}_{catalog}"
    out_path = _unique_path(stem, ".csv")
    out.to_csv(out_path, index=False)

    has_match = out["splus_dist_arcsec"].notna()
    n_matched = int(out.loc[has_match, "input_row"].nunique())
    seps = out["splus_dist_arcsec"].dropna()
    return {
        "catalog": catalog,
        "ra_col": ra_c,
        "dec_col": dec_c,
        "n_input": len(df),
        "n_matched": n_matched,
        **({"n_pairs": int(has_match.sum())} if all_matches else {}),
        "match_fraction": round(n_matched / len(df), 4) if len(df) else 0.0,
        "median_sep_arcsec": round(float(seps.median()), 4) if len(seps) else None,
        "saved_to": str(out_path),
        "preview": _records(out, max_preview_rows),
    }


@tool
def get_sed(
    ra: float,
    dec: float,
    catalogs: list[str] = ["idr6"],
    aperture: str = "pstotal",
    match_radius_arcsec: float = 2.0,
    plot: bool = True,
) -> dict:
    """SED de 12 bandas da fonte mais proxima de (ra, dec), em um ou mais catalogos
    (passar varios, ex ['dr4', 'dr5', 'idr6'], permite comparar DRs no mesmo grafico).

    Retorna magnitudes AB e fluxos (mJy) por banda, salva CSV e, com plot=True, um PNG.
    Com varios catalogos, delta_mag compara cada um com o ultimo da lista que achou a fonte.
    aperture: pstotal (padrao, recomendada para cores), auto, petro, aper_3, aper_6, iso.
    """
    def nearest(name):
        try:
            return backend.cone_search(name, ra, dec, match_radius_arcsec, None, aperture, max_rows=1), None
        except Exception as e:
            return None, f"{type(e).__name__}: {str(e)[:200]}"

    with backend.quiet(), ThreadPoolExecutor(max_workers=len(catalogs) or 1) as pool:
        found = list(pool.map(nearest, catalogs))

    sources, info = {}, []
    for name, (df, err) in zip(catalogs, found):
        if err:
            info.append({"catalog": name, "error": err})
        elif df.empty:
            info.append({"catalog": name, "found": False})
        else:
            src = df.iloc[0]
            sources[name] = src
            info.append({
                "catalog": name, "found": True, "id": str(src["id"]), "field": src.get("field"),
                "dist_arcsec": round(float(src["dist_arcsec"]), 3),
            })

    if not sources:
        return {"ra": ra, "dec": dec, "sources": info, "message": "nenhuma fonte dentro do raio"}

    table = sed_table(sources)
    stem = f"sed_{_coords(ra, dec)}_{aperture}"
    csv_path = _unique_path(stem, ".csv")
    table.to_csv(csv_path, index=False)
    result = {"ra": ra, "dec": dec, "aperture": aperture, "sources": info, "saved_to": str(csv_path)}

    if len(sources) > 1:
        wide = table.pivot(index="band", columns="catalog", values="mag").reindex(cat.BANDS)
        ref = list(sources)[-1]
        result["delta_mag_vs_" + ref] = {
            c: {b: (None if pd.isna(v) else round(float(v), 3)) for b, v in (wide[c] - wide[ref]).items()}
            for c in sources if c != ref
        }
    if plot:
        png = _unique_path(stem, ".png")
        plot_sed(table, f"S-PLUS SED  ra={ra:.5f} dec={dec:+.5f}  ({aperture})", png)
        result["plot"] = str(png)
    result["sed"] = _records(table[["catalog", "band", "wavelength_A", "mag", "err", "flux_mJy"]], len(table))
    return result


@tool
def compare_releases(
    ra: float,
    dec: float,
    radius_arcsec: float = 600,
    catalogs: list[str] = ["dr3", "dr4", "dr5", "idr6"],
    reference: str = "idr6",
    bands: list[str] | None = None,
    aperture: str = "pstotal",
    mag_min: float = 14.0,
    mag_max: float = 19.0,
    match_radius_arcsec: float = 1.0,
) -> dict:
    """Mede a consistencia entre data releases num campo: casa as fontes de cada catalogo com as
    da referencia e reporta offset astrometrico e delta de magnitude por banda (outro - referencia),
    com mediana e dispersao robusta (sigma_mad). Usa so fontes com mag_min <= r <= mag_max na
    referencia, para evitar ruido de fontes fracas e saturacao.

    Util antes de misturar catalogos de DRs diferentes numa analise.
    """
    summary, matched, counts = backend.compare_releases(
        ra, dec, radius_arcsec, catalogs, reference, bands, aperture, match_radius_arcsec, (mag_min, mag_max))
    out_path = _unique_path(f"compare_{_coords(ra, dec)}_{radius_arcsec}as_{aperture}", ".csv")
    matched.to_csv(out_path, index=False)
    return {
        "ra": ra, "dec": dec, "radius_arcsec": radius_arcsec, "aperture": aperture,
        "mag_range_r": [mag_min, mag_max], "n_sources_in_cone": counts,
        "comparisons": summary, "saved_to": str(out_path),
    }


# ---------------------------------------------------------------- imagens

@tool
def get_cutout(
    ra: float,
    dec: float,
    size: float = 128,
    band: str = "R",
    data_release: str | None = None,
    size_unit: str = "pixels",
    weight: bool = False,
) -> dict:
    """Baixa um recorte (stamp) FITS em uma banda e salva em disco.

    Args:
        ra, dec: coordenadas em graus.
        size: lado do recorte, na unidade size_unit ('pixels', 'arcsec' ou 'arcmin').
        band: u, g, r, i, z ou filtros estreitos (F378/J0378, F395, F410, F430, F515, F660, F861).
        data_release: colecao de imagem (ex: 'dr4', 'dr5', 'idr6'); None = mais nova que cobre a posicao.
        weight: se True, baixa o mapa de peso em vez da imagem.
    """
    collection = backend.pick_image_collection(ra, dec, data_release)
    band = cat.image_band(band)
    out_path = _unique_path(f"cut_{collection['name']}_{_coords(ra, dec)}_{band}{'_weight' if weight else ''}", ".fits")
    backend.download_stamp(collection, ra, dec, size, band, size_unit, weight, out_path)
    return {"data_release": collection["name"], "band": band, "saved_to": str(out_path)}


@tool
def get_rgb_image(
    ra: float,
    dec: float,
    size: float = 256,
    r_band: str = "I",
    g_band: str = "R",
    b_band: str = "G",
    stretch: float = 3,
    q: float = 8,
    data_release: str | None = None,
    size_unit: str = "pixels",
) -> dict:
    """Gera uma imagem RGB (metodo Lupton) a partir de 3 bandas e salva como PNG.

    Args:
        ra, dec: coordenadas em graus.
        size: lado da imagem, na unidade size_unit.
        r_band, g_band, b_band: bandas de cada canal.
        stretch, q: parametros do make_lupton_rgb.
        data_release: colecao de imagem; None = mais nova que cobre a posicao.
    """
    collection = backend.pick_image_collection(ra, dec, data_release)
    out_path = _unique_path(f"rgb_{collection['name']}_{_coords(ra, dec)}", ".png")
    backend.download_rgb(collection, ra, dec, size, size_unit, cat.image_band(r_band), cat.image_band(g_band),
                         cat.image_band(b_band), stretch, q, out_path)
    return {"data_release": collection["name"], "saved_to": str(out_path)}


@tool
def get_trilogy_image(
    ra: float,
    dec: float,
    size: float = 256,
    r_bands: list[str] = ["R", "I", "F861", "Z"],
    g_bands: list[str] = ["G", "F515", "F660"],
    b_bands: list[str] = ["U", "F378", "F395", "F410", "F430"],
    noiselum: float = 0.15,
    satpercent: float = 0.15,
    colorsatfac: float = 2,
    data_release: str | None = None,
    size_unit: str = "pixels",
) -> dict:
    """Gera uma imagem colorida com as 12 bandas do S-PLUS (metodo Trilogy) e salva como PNG.

    Args:
        ra, dec: coordenadas em graus.
        size: lado da imagem, na unidade size_unit.
        r_bands, g_bands, b_bands: bandas somadas em cada canal.
        noiselum, satpercent, colorsatfac: parametros do Trilogy.
        data_release: colecao de imagem; None = mais nova que cobre a posicao.
    """
    collection = backend.pick_image_collection(ra, dec, data_release)
    out_path = _unique_path(f"trilogy_{collection['name']}_{_coords(ra, dec)}", ".png")
    backend.download_trilogy(collection, ra, dec, size, size_unit, [cat.image_band(b) for b in r_bands],
                             [cat.image_band(b) for b in g_bands], [cat.image_band(b) for b in b_bands],
                             noiselum, satpercent, colorsatfac, out_path)
    return {"data_release": collection["name"], "saved_to": str(out_path)}


@tool
def batch_cutouts(
    input_path: str | None = None,
    targets: list[dict] | None = None,
    kind: str = "fits",
    bands: list[str] = ["R"],
    size: float = 128,
    size_unit: str = "pixels",
    data_release: str | None = None,
    name_col: str | None = None,
    ra_col: str | None = None,
    dec_col: str | None = None,
    max_workers: int = 4,
) -> dict:
    """Baixa cutouts para uma lista de alvos, numa pasta nova com um manifest.csv
    (status e caminho de cada arquivo; falhas individuais nao interrompem o lote).

    Args:
        input_path / targets: arquivo (CSV, FITS, VOTable, parquet) ou lista de dicts {name?, ra, dec}.
        kind: 'fits' (um arquivo por banda), 'rgb' (Lupton I/R/G) ou 'trilogy' (12 bandas).
        bands: bandas para kind='fits'.
        data_release: colecao de imagem; None = mais nova que cobre cada alvo.
        name_col: coluna com o nome do alvo (padrao: 'name'/'id' se existir, senao o indice).
        max_workers: downloads simultaneos (1-8).
    """
    df = _targets_frame(input_path, targets)
    ra_c, dec_c = find_coord_columns(df, ra_col, dec_col)
    if name_col is None:
        name_col = next((c for c in df.columns if c.lower() in ("name", "id", "object", "obj", "target")), None)
    names = df[name_col].astype(str).tolist() if name_col else [f"t{i:05d}" for i in range(len(df))]

    if kind not in ("fits", "rgb", "trilogy"):
        raise ValueError("kind deve ser 'fits', 'rgb' ou 'trilogy'")
    img_bands = [cat.image_band(b) for b in bands] if kind == "fits" else [kind]
    if len(df) * len(img_bands) > MAX_BATCH_FILES:
        raise ValueError(f"lote grande demais ({len(df) * len(img_bands)} arquivos; maximo {MAX_BATCH_FILES})")

    out_dir = DOWNLOAD_DIR / f"batch_{datetime.now():%Y%m%d_%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=False)
    fixed = backend.image_collection(data_release) if data_release else None

    jobs = [(name, float(ra), float(dec), b)
            for name, ra, dec in zip(names, df[ra_c], df[dec_c]) for b in img_bands]

    def run(job):
        name, ra, dec, band = job
        row = {"name": name, "ra": ra, "dec": dec, "band": band}
        try:
            collection = fixed or backend.pick_image_collection(ra, dec, None)
            row["data_release"] = collection["name"]
            if kind == "fits":
                path = _unique_path(f"{name}_{band}", ".fits", out_dir)
                backend.download_stamp(collection, ra, dec, size, band, size_unit, False, path)
            elif kind == "rgb":
                path = _unique_path(f"{name}_rgb", ".png", out_dir)
                backend.download_rgb(collection, ra, dec, size, size_unit, "I", "R", "G", 3, 8, path)
            else:
                path = _unique_path(f"{name}_trilogy", ".png", out_dir)
                backend.download_trilogy(collection, ra, dec, size, size_unit, ["R", "I", "F861", "Z"],
                                         ["G", "F515", "F660"], ["U", "F378", "F395", "F410", "F430"],
                                         0.15, 0.15, 2, path)
            row.update(status="ok", path=str(path))
        except Exception as e:
            row.update(status="error", error=f"{type(e).__name__}: {str(e)[:300]}")
        return row

    with backend.quiet(), ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), 8))) as pool:
        rows = list(pool.map(run, jobs))

    manifest = pd.DataFrame(rows)
    manifest_path = out_dir / "manifest.csv"
    manifest.to_csv(manifest_path, index=False)
    n_ok = int((manifest["status"] == "ok").sum())
    errors = manifest[manifest["status"] == "error"]
    return {
        "n_targets": len(df),
        "n_files_ok": n_ok,
        "n_errors": len(errors),
        "output_dir": str(out_dir),
        "manifest": str(manifest_path),
        "first_errors": _records(errors, 5) if len(errors) else [],
    }


@tool
def get_field(field: str, band: str, data_release: str, weight: bool = False) -> dict:
    """Baixa a imagem FITS completa de um campo (~11k x 11k pixels, centenas de MB).

    Args:
        field: nome do campo, ex: 'SPLUS-s20s12' ou 'STRIPE82-0001' (check_coords informa o campo).
        band: banda fotometrica.
        data_release: colecao de imagem (ex: 'dr4', 'dr5', 'idr6').
        weight: se True, baixa o mapa de peso.
    """
    collection = backend.image_collection(data_release)
    band = cat.image_band(band)
    client = backend.get_core().client
    with backend.quiet():
        files = client.list_files(collection["id"], filter_str=field, filter_name=band)
        if not files and ("-" in field or "_" in field):
            alt = field.replace("-", "_") if "-" in field else field.replace("_", "-")
            files = client.list_files(collection["id"], filter_str=alt, filter_name=band)
    files = [f for f in files if ("weight" in f.get("filename", "")) == weight]
    if not files:
        raise RuntimeError(f"Campo {field} banda {band} nao encontrado em {collection['name']}.")

    chosen = next((f for f in files if f.get("file_type") == "fz"), files[0])
    out_path = _unique_path(Path(chosen["filename"]).name, "")
    with backend.quiet():
        client.download_file(chosen["id"], output_path=str(out_path), timeout=600)
    return {"data_release": collection["name"], "filename": chosen["filename"], "saved_to": str(out_path)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
