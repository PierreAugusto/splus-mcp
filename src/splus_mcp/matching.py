"""Logica de casamento posicional, sem acesso a rede."""
import numpy as np
import pandas as pd

_RA_NAMES = ["ra", "ra_deg", "raj2000", "_raj2000", "ra_icrs", "alpha_j2000", "ra_j2000", "ra_1"]
_DEC_NAMES = ["dec", "dec_deg", "dej2000", "_dej2000", "decj2000", "dec_icrs", "delta_j2000", "dec_j2000", "de", "dec_1"]


def find_coord_columns(df: pd.DataFrame, ra_col: str | None = None, dec_col: str | None = None) -> tuple[str, str]:
    """Acha as colunas de RA/Dec (em graus) por nome, ignorando maiusculas."""
    lookup = {c.lower(): c for c in df.columns}

    def pick(explicit, names, label):
        if explicit:
            if explicit in df.columns:
                return explicit
            if explicit.lower() in lookup:
                return lookup[explicit.lower()]
            raise ValueError(f"coluna '{explicit}' nao existe; colunas: {list(df.columns)}")
        for n in names:
            if n in lookup:
                return lookup[n]
        raise ValueError(f"nao achei a coluna de {label}; informe {label.lower()}_col. Colunas: {list(df.columns)}")

    return pick(ra_col, _RA_NAMES, "RA"), pick(dec_col, _DEC_NAMES, "DEC")


def angular_sep_arcsec(ra1, dec1, ra2, dec2) -> np.ndarray:
    """Separacao angular (haversine) em arcsec; entradas em graus, com broadcasting."""
    ra1, dec1, ra2, dec2 = (np.radians(np.asarray(x, dtype=float)) for x in (ra1, dec1, ra2, dec2))
    h = np.sin((dec2 - dec1) / 2) ** 2 + np.cos(dec1) * np.cos(dec2) * np.sin((ra2 - ra1) / 2) ** 2
    return np.degrees(2 * np.arcsin(np.sqrt(np.clip(h, 0, 1)))) * 3600


def group_targets(ra, dec, max_radius_deg: float = 0.25) -> list[tuple[float, float, float, np.ndarray]]:
    """Agrupa posicoes em cones de raio <= max_radius_deg (guloso), para cobrir muitos alvos
    proximos com poucas buscas conicas. Retorna (ra_c, dec_c, raio_deg, indices)."""
    ra = np.asarray(ra, dtype=float)
    dec = np.asarray(dec, dtype=float)
    remaining = np.arange(len(ra))
    groups = []
    while remaining.size:
        seed = remaining[0]
        sep_deg = angular_sep_arcsec(ra[seed], dec[seed], ra[remaining], dec[remaining]) / 3600
        members = remaining[sep_deg <= max_radius_deg]
        radius = float(sep_deg[sep_deg <= max_radius_deg].max())
        groups.append((float(ra[seed]), float(dec[seed]), radius, members))
        remaining = remaining[sep_deg > max_radius_deg]
    return groups


def pairs_within(in_ra, in_dec, cand_ra, cand_dec, radius_arcsec: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Todos os pares (i_entrada, j_candidato, sep_arcsec) com separacao <= raio."""
    in_ra, in_dec = np.asarray(in_ra, float), np.asarray(in_dec, float)
    cand_ra, cand_dec = np.asarray(cand_ra, float), np.asarray(cand_dec, float)
    out_i, out_j, out_s = [np.array([], dtype=int)], [np.array([], dtype=int)], [np.array([], dtype=float)]
    # Em blocos para limitar a matriz N x M em memoria.
    for start in range(0, len(in_ra), 256):
        sl = slice(start, start + 256)
        sep = angular_sep_arcsec(in_ra[sl, None], in_dec[sl, None], cand_ra[None, :], cand_dec[None, :])
        i, j = np.nonzero(sep <= radius_arcsec)
        out_i.append(i + start)
        out_j.append(j)
        out_s.append(sep[i, j])
    return np.concatenate(out_i), np.concatenate(out_j), np.concatenate(out_s)


def nearest_per_row(pairs: pd.DataFrame, row_col: str = "xm_row", sep_col: str = "dist_arcsec") -> pd.DataFrame:
    """Mantem so o candidato mais proximo de cada linha de entrada e conta os candidatos."""
    if pairs.empty:
        return pairs.assign(n_candidates=pd.Series(dtype=int))
    counts = pairs.groupby(row_col).size().rename("n_candidates")
    best = pairs.sort_values([row_col, sep_col]).drop_duplicates(row_col, keep="first")
    return best.merge(counts, left_on=row_col, right_index=True)
