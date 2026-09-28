import contextlib
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

from splus_mcp.releases import POINTING_DR_TO_COLLECTION, resolve_collection

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# O adss loga cada requisicao HTTP em INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)

DOWNLOAD_DIR = Path(os.environ.get("SPLUS_DOWNLOAD_DIR", "./splus_downloads")).expanduser().resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

mcp = MCPServer(
    "splus",
    instructions=(
        "Acesso ao S-PLUS (splus.cloud). Use list_data_releases/list_tables/describe_table "
        "para descobrir o que existe antes de escrever queries; check_coords para saber quais "
        "DRs cobrem uma posicao. Tools de imagem aceitam data_release (ex: 'dr4', 'dr5', 'idr6'); "
        "se omitido, usa o DR mais novo que cobre a coordenada."
    ),
)

_core = None


@contextlib.contextmanager
def _quiet():
    # splusdata/adss usam print(); em transporte stdio isso corromperia o JSON-RPC.
    with contextlib.redirect_stdout(sys.stderr):
        yield


def _get_core():
    global _core
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

    with _quiet():
        _core = splusdata.Core(username, password)
    return _core


def _collection(data_release: str) -> dict:
    with _quiet():
        collections = _get_core().client.get_collections()
    return resolve_collection(data_release, collections)


def _covering_releases(ra: float, dec: float, radius_deg: float) -> list[dict]:
    import astropy.units as u
    from astropy.coordinates import SkyCoord
    from splusdata.features.find_pointings import _load_dr

    target = SkyCoord(ra=ra * u.deg, dec=dec * u.deg)
    hits = []
    for dr in POINTING_DR_TO_COLLECTION:
        with _quiet():
            df = _load_dr(dr)
        sep = target.separation(SkyCoord(df["ra"].values * u.deg, df["dec"].values * u.deg))
        idx = int(sep.argmin())
        if sep[idx].deg <= radius_deg:
            hits.append({
                "data_release": dr,
                "collection": POINTING_DR_TO_COLLECTION[dr],
                "field": str(df["field"].iloc[idx]),
                "distance_deg": round(float(sep[idx].deg), 4),
            })
    return hits


def _pick_release(ra: float, dec: float, data_release: str | None) -> dict:
    if data_release:
        return _collection(data_release)
    hits = _covering_releases(ra, dec, radius_deg=1.0)
    if not hits:
        raise RuntimeError(f"Nenhum DR do S-PLUS cobre (ra={ra}, dec={dec}); informe data_release explicitamente.")
    return _collection(hits[-1]["collection"])


def _unique_path(stem: str, suffix: str) -> Path:
    stem = stem.replace(" ", "")
    path = DOWNLOAD_DIR / f"{stem}{suffix}"
    i = 1
    while path.exists():
        path = DOWNLOAD_DIR / f"{stem}_{i}{suffix}"
        i += 1
    return path


@mcp.tool()
def list_data_releases() -> dict:
    """Lista os data releases disponiveis para a sua conta: colecoes de imagem (usadas por
    get_cutout/get_rgb_image/get_trilogy_image/get_field) e schemas de catalogo (usados em
    query_catalog)."""
    core = _get_core()
    with _quiet():
        collections = core.client.get_collections()
        schemas = core.client.get_schemas()
    return {
        "image_collections": [c["name"] for c in collections],
        "catalog_schemas": sorted(schemas),
    }


@mcp.tool()
def list_tables(schema: str) -> list[str]:
    """Lista as tabelas de um schema de catalogo (ex: 'dr5', 'idr6', 'dr4_dual', 'dr5_vacs')."""
    with _quiet():
        return _get_core().client.get_tables(schema)


@mcp.tool()
def describe_table(schema: str, table: str, name_filter: str | None = None) -> dict:
    """Lista as colunas (nome e tipo) de uma tabela de catalogo. Tabelas grandes tem centenas
    de colunas; use name_filter (substring, case-insensitive) para restringir, ex: 'r_' ou 'photoz'."""
    with _quiet():
        columns = _get_core().client.get_columns(schema, table)
    cols = [{"name": c.name, "type": c.data_type} for c in columns]
    if name_filter:
        cols = [c for c in cols if name_filter.lower() in c["name"].lower()]
    return {"table": f"{schema}.{table}", "n_columns": len(cols), "columns": cols}


@mcp.tool()
def check_coords(ra: float, dec: float, radius_deg: float = 1.0) -> dict:
    """Verifica quais data releases do S-PLUS (dr4, dr5, dr6) tem um campo com centro a ate
    radius_deg graus da coordenada, e qual o campo mais proximo em cada um."""
    hits = _covering_releases(ra, dec, radius_deg)
    return {"ra": ra, "dec": dec, "covered": bool(hits), "releases": hits}


@mcp.tool()
def query_catalog(query: str, max_preview_rows: int = 20) -> dict:
    """Executa uma query (ADQL/SQL) no banco do S-PLUS e salva o resultado completo em CSV.
    Retorna preview, contagem de linhas e caminho do arquivo.

    Tabelas sao referenciadas como schema.tabela, ex:
      SELECT TOP 10 ra, dec, field FROM idr6.idr6
      SELECT TOP 10 * FROM dr5_vacs.dr5_photoz_v3
    Use list_tables/describe_table para descobrir nomes de tabelas e colunas.
    """
    with _quiet():
        df = _get_core().query(query)

    out_path = _unique_path("query_result", ".csv")
    df.to_csv(out_path, index=False)
    preview = df.head(max_preview_rows)
    return {
        "row_count": len(df),
        "columns": list(df.columns),
        "preview": preview.to_dict(orient="records"),
        "saved_to": str(out_path),
    }


@mcp.tool()
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
        band: U, F378, F395, F410, F430, G, F515, R, F660, I, F861, Z.
        data_release: colecao de imagem (ex: 'dr4', 'dr5', 'idr6'); None = mais nova que cobre a posicao.
        weight: se True, baixa o mapa de peso em vez da imagem.
    """
    collection = _pick_release(ra, dec, data_release)
    out_path = _unique_path(f"cut_{collection['name']}_{ra}_{dec}_{band}{'_weight' if weight else ''}", ".fits")
    with _quiet():
        _get_core().client.create_stamp_by_coordinates(
            collection_id=collection["id"],
            ra=ra,
            dec=dec,
            size=size,
            filter=band,
            size_unit=size_unit,
            pattern="weight" if weight else "",
            output_path=str(out_path),
            timeout=120,
        )
    return {"data_release": collection["name"], "saved_to": str(out_path)}


@mcp.tool()
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
    collection = _pick_release(ra, dec, data_release)
    out_path = _unique_path(f"rgb_{collection['name']}_{ra}_{dec}", ".png")
    with _quiet():
        _get_core().client.create_rgb_image_by_coordinates(
            collection_id=collection["id"],
            ra=ra,
            dec=dec,
            size=size,
            r_filter=r_band,
            g_filter=g_band,
            b_filter=b_band,
            stretch=stretch,
            Q=q,
            size_unit=size_unit,
            output_path=str(out_path),
        )
    return {"data_release": collection["name"], "saved_to": str(out_path)}


@mcp.tool()
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
    collection = _pick_release(ra, dec, data_release)
    out_path = _unique_path(f"trilogy_{collection['name']}_{ra}_{dec}", ".png")
    with _quiet():
        _get_core().client.trilogy_images.create_trilogy_rgb_by_coordinates(
            collection_id=collection["id"],
            ra=ra,
            dec=dec,
            size=size,
            r_filters=r_bands,
            g_filters=g_bands,
            b_filters=b_bands,
            size_unit=size_unit,
            noiselum=noiselum,
            satpercent=satpercent,
            colorsatfac=colorsatfac,
            output_path=str(out_path),
        )
    return {"data_release": collection["name"], "saved_to": str(out_path)}


@mcp.tool()
def get_field(field: str, band: str, data_release: str, weight: bool = False) -> dict:
    """Baixa a imagem FITS completa de um campo (~11k x 11k pixels, centenas de MB).

    Args:
        field: nome do campo, ex: 'SPLUS-s20s12' ou 'STRIPE82-0001' (check_coords informa o campo).
        band: banda fotometrica.
        data_release: colecao de imagem (ex: 'dr4', 'dr5', 'idr6').
        weight: se True, baixa o mapa de peso.
    """
    collection = _collection(data_release)
    core = _get_core()
    with _quiet():
        files = core.client.list_files(collection["id"], filter_str=field, filter_name=band)
        if not files and ("-" in field or "_" in field):
            alt = field.replace("-", "_") if "-" in field else field.replace("_", "-")
            files = core.client.list_files(collection["id"], filter_str=alt, filter_name=band)
    files = [f for f in files if ("weight" in f.get("filename", "")) == weight]
    if not files:
        raise RuntimeError(f"Campo {field} banda {band} nao encontrado em {collection['name']}.")

    chosen = next((f for f in files if f.get("file_type") == "fz"), files[0])
    out_path = _unique_path(Path(chosen["filename"]).name, "")
    with _quiet():
        core.client.download_file(chosen["id"], output_path=str(out_path), timeout=600)
    return {"data_release": collection["name"], "filename": chosen["filename"], "saved_to": str(out_path)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
