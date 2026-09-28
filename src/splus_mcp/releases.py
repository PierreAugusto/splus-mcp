# Chaves de splusdata.vars.DR_POINTINGS -> nome da colecao de imagem no splus.cloud,
# em ordem cronologica (a ultima e a mais nova).
POINTING_DR_TO_COLLECTION = {
    "dr4": "dr1 - dr4",
    "dr5": "dr5",
    "dr6": "idr6",
}

_ALIASES = {
    "dr1": "dr1 - dr4",
    "dr2": "dr1 - dr4",
    "dr3": "dr1 - dr4",
    "dr4": "dr1 - dr4",
    "idr4": "dr1 - dr4",
    "idr5": "dr5",
    "dr6": "idr6",
}


def resolve_collection(data_release: str, collections: list[dict]) -> dict:
    """Resolve um nome de DR para a colecao de imagem correspondente.

    O splusdata casa por substring e na ordem do servidor, entao 'dr5' acaba em
    'idr5_fornax'. Aqui a ordem e: nome exato, alias conhecido, substring unica.
    """
    by_name = {c["name"].lower(): c for c in collections}
    key = data_release.strip().lower()

    if key in by_name:
        return by_name[key]
    if _ALIASES.get(key) in by_name:
        return by_name[_ALIASES[key]]

    matches = [c for name, c in by_name.items() if key in name]
    if len(matches) == 1:
        return matches[0]

    available = ", ".join(c["name"] for c in collections)
    if matches:
        raise ValueError(f"data_release '{data_release}' e ambiguo; opcoes: {available}")
    raise ValueError(f"data_release '{data_release}' nao encontrado; opcoes: {available}")
