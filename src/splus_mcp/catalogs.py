"""Descricao dos catalogos de cada data release e construcao de queries.

Os DRs usam duas convencoes de nome:
  - "lower" (idr6, usdr2): ra, dec, field, mag_auto_r, err_mag_auto_j0660
  - "upper" (dr3, dr5, idr5, usdr1, dr4): RA, DEC, Field, r_auto, e_J0660_auto
O dr4 ainda separa cada banda numa tabela (dr4_dual.dr4_dual_<banda>), ligadas por ID.

Tudo aqui e puro (sem rede) para poder ser testado offline.
"""
from dataclasses import dataclass

# Ordem por comprimento de onda.
BANDS = ["u", "J0378", "J0395", "J0410", "J0430", "g", "J0515", "r", "J0660", "i", "J0861", "z"]

# Comprimento de onda pivot em Angstrom (splusdata.vars.BANDWAVEINFO, coluna pivot_wave).
PIVOT_WAVELENGTH = {
    "u": 3533.3, "J0378": 3773.2, "J0395": 3940.7, "J0410": 4094.9, "J0430": 4292.1, "g": 4758.5,
    "J0515": 5133.1, "r": 6251.8, "J0660": 6613.9, "i": 7670.6, "J0861": 8607.3, "z": 8941.5,
}

APERTURES = ["auto", "petro", "pstotal", "aper_3", "aper_6", "iso"]

# S-PLUS marca nao-deteccao com 99/-99 (DRs antigos) ou valores absurdos.
BAD_MAG_THRESHOLD = 50.0

_UPPER_APERTURE = {"auto": "auto", "petro": "petro", "pstotal": "PStotal", "aper_3": "aper_3", "aper_6": "aper_6", "iso": "iso"}
_LOWER_APERTURE = {"auto": "auto", "petro": "petro", "pstotal": "pstotal", "aper_3": "aper_3", "aper_6": "aper_6", "iso": "isophotal"}


def normalize_band(band: str) -> str:
    """'R', 'r', 'rSDSS' -> 'r'; 'F660', 'j0660', '660' -> 'J0660'."""
    b = band.strip()
    low = b.lower()
    for broad in ("u", "g", "r", "i", "z"):
        if low in (broad, f"{broad}sdss", f"{broad}_sdss", f"{broad}javaa", f"{broad}java"):
            return broad
    digits = "".join(ch for ch in low if ch.isdigit())
    if digits:
        canonical = f"J0{digits[-3:]}"
        if canonical in BANDS:
            return canonical
    raise ValueError(f"banda '{band}' desconhecida; use uma de {', '.join(BANDS)} (ou F378/F660...)")


def image_band(band: str) -> str:
    """Nome de filtro aceito pela API de imagens: 'r' -> 'R', 'J0660' -> 'F660'."""
    b = normalize_band(band)
    return b.upper() if b in ("u", "g", "r", "i", "z") else f"F{b[2:]}"


def normalize_aperture(aperture: str) -> str:
    ap = aperture.strip().lower()
    ap = {"isophotal": "iso", "aper3": "aper_3", "aper6": "aper_6", "ps_total": "pstotal"}.get(ap, ap)
    if ap not in APERTURES:
        raise ValueError(f"abertura '{aperture}' desconhecida; use uma de {', '.join(APERTURES)}")
    return ap


@dataclass(frozen=True)
class Catalog:
    name: str
    table: str
    ra: str
    dec: str
    id: str
    field: str | None
    style: str  # "lower" | "upper"
    band_table: str | None = None  # template com {band}, para DRs com uma tabela por banda
    q3c: bool = True  # q3c_join no servidor e rapido (dr3 nao tem o indice e estoura o timeout)
    description: str = ""

    def mag_col(self, band: str, aperture: str) -> str:
        if self.style == "lower":
            return f"mag_{_LOWER_APERTURE[aperture]}_{band.lower()}"
        return f"{band}_{_UPPER_APERTURE[aperture]}"

    def err_col(self, band: str, aperture: str) -> str:
        if self.style == "lower":
            return f"err_{self.mag_col(band, aperture)}"
        return f"e_{self.mag_col(band, aperture)}"

    def table_for_band(self, band: str) -> str:
        if not self.band_table:
            return self.table
        return self.band_table.format(band=band.lower())


CATALOGS = {
    c.name: c
    for c in [
        Catalog("idr6", "idr6.idr6", "ra", "dec", "id", "field", "lower",
                description="iDR6 (interno), dual-mode, 12 bandas numa tabela"),
        Catalog("dr5", "dr5.dr5_dual", "RA", "DEC", "ID", "Field", "upper",
                description="DR5 publico, dual-mode"),
        Catalog("idr5", "idr5.idr5_dual", "RA", "DEC", "ID", "Field", "upper",
                description="iDR5 (interno), dual-mode"),
        Catalog("dr4", "dr4_dual.dr4_dual_detection", "RA", "DEC", "ID", "Field", "upper",
                band_table="dr4_dual.dr4_dual_{band}",
                description="DR4 publico, dual-mode; uma tabela por banda"),
        Catalog("dr3", "dr3.all_dr3", "RA", "DEC", "ID", "Field", "upper", q3c=False,
                description="DR3 publico"),
    ]
}

_CATALOG_ALIASES = {"dr6": "idr6"}


def get_catalog(name: str) -> Catalog:
    key = name.strip().lower()
    key = _CATALOG_ALIASES.get(key, key)
    if key not in CATALOGS:
        raise ValueError(f"catalogo '{name}' nao suportado; opcoes: {', '.join(CATALOGS)}")
    return CATALOGS[key]


def cone_condition(ra_col: str, dec_col: str, ra: float, dec: float, radius_deg: float) -> str:
    return f"1=CONTAINS(POINT('ICRS', {ra_col}, {dec_col}), CIRCLE('ICRS', {ra!r}, {dec!r}, {radius_deg!r}))"


def build_cone_queries(
    catalog: Catalog,
    ra: float,
    dec: float,
    radius_deg: float,
    bands: list[str],
    aperture: str,
    max_rows: int | None = None,
) -> list[str]:
    """Queries para uma busca conica. Uma so para catalogos de tabela unica; para o dr4,
    uma para a deteccao e outra por banda (juntadas por ID no cliente)."""
    top = f"TOP {int(max_rows)} " if max_rows else ""

    if not catalog.band_table:
        cols = [catalog.id, catalog.ra, catalog.dec]
        if catalog.field:
            cols.append(catalog.field)
        for b in bands:
            cols += [catalog.mag_col(b, aperture), catalog.err_col(b, aperture)]
        where = cone_condition(catalog.ra, catalog.dec, ra, dec, radius_deg)
        return [f"SELECT {top}{', '.join(cols)} FROM {catalog.table} WHERE {where}"]

    det_cols = [catalog.id, catalog.ra, catalog.dec] + ([catalog.field] if catalog.field else [])
    where = cone_condition(catalog.ra, catalog.dec, ra, dec, radius_deg)
    queries = [f"SELECT {top}{', '.join(det_cols)} FROM {catalog.table} WHERE {where}"]
    for b in bands:
        cols = [catalog.id, catalog.mag_col(b, aperture), catalog.err_col(b, aperture)]
        queries.append(f"SELECT {', '.join(cols)} FROM {catalog.table_for_band(b)} WHERE {where}")
    return queries


def build_xmatch_queries(
    catalog: Catalog,
    upload_table: str,
    radius_deg: float,
    bands: list[str],
    aperture: str,
) -> list[str]:
    """Queries de cross-match no servidor com q3c_join contra uma tabela enviada com as
    colunas xm_row, xm_ra, xm_dec (referenciada como upload.<nome>). Retornam todos os
    pares dentro do raio; a escolha do mais proximo e feita no cliente."""
    join = f"q3c_join(u.xm_ra, u.xm_dec, c.{catalog.ra}, c.{catalog.dec}, {radius_deg!r})"

    def source(table: str) -> str:
        return f"FROM upload.{upload_table} AS u JOIN {table} AS c ON {join}"

    pos_cols = [f"c.{catalog.id}", f"c.{catalog.ra}", f"c.{catalog.dec}"]
    if catalog.field:
        pos_cols.append(f"c.{catalog.field}")

    if not catalog.band_table:
        cols = ["u.xm_row"] + pos_cols
        for b in bands:
            cols += [f"c.{catalog.mag_col(b, aperture)}", f"c.{catalog.err_col(b, aperture)}"]
        return [f"SELECT {', '.join(cols)} {source(catalog.table)}"]

    queries = [f"SELECT u.xm_row, {', '.join(pos_cols)} {source(catalog.table)}"]
    for b in bands:
        cols = ["u.xm_row", f"c.{catalog.id}", f"c.{catalog.mag_col(b, aperture)}", f"c.{catalog.err_col(b, aperture)}"]
        queries.append(f"SELECT {', '.join(cols)} {source(catalog.table_for_band(b))}")
    return queries


def standardize_columns(df, catalog: Catalog, bands: list[str], aperture: str):
    """Renomeia para um esquema unico entre DRs: id, ra, dec, field, mag_<banda>, err_<banda>.
    Magnitudes de nao-deteccao (|mag| >= 50) viram NaN."""
    import numpy as np

    lookup = {c.lower(): c for c in df.columns}
    rename = {}
    for src, dst in [(catalog.id, "id"), (catalog.ra, "ra"), (catalog.dec, "dec"), (catalog.field, "field")]:
        if src and src.lower() in lookup:
            rename[lookup[src.lower()]] = dst
    for b in bands:
        for src, dst in [(catalog.mag_col(b, aperture), f"mag_{b}"), (catalog.err_col(b, aperture), f"err_{b}")]:
            if src.lower() in lookup:
                rename[lookup[src.lower()]] = dst
    out = df.rename(columns=rename)

    for b in bands:
        m, e = f"mag_{b}", f"err_{b}"
        if m in out:
            bad = ~np.isfinite(out[m].astype(float)) | (out[m].astype(float).abs() >= BAD_MAG_THRESHOLD)
            out[m] = out[m].astype(float).where(~bad)
            if e in out:
                out[e] = out[e].astype(float).where(~bad)
    return out
