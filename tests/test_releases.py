import pytest

from splus_mcp.releases import resolve_collection

# Ordem igual a retornada pelo splus.cloud em 2026-09.
COLLECTIONS = [
    {"id": 3, "name": "idr6"},
    {"id": 7, "name": "dr1 - dr4"},
    {"id": 8, "name": "idr4_fornax"},
    {"id": 26, "name": "idr5_fornax"},
    {"id": 27, "name": "dr5"},
]


@pytest.mark.parametrize(
    "data_release, expected",
    [
        ("dr5", "dr5"),
        ("DR5", "dr5"),
        ("idr6", "idr6"),
        ("dr6", "idr6"),
        ("dr4", "dr1 - dr4"),
        ("dr2", "dr1 - dr4"),
        ("dr1 - dr4", "dr1 - dr4"),
        ("idr4_fornax", "idr4_fornax"),
        ("idr5_fornax", "idr5_fornax"),
    ],
)
def test_resolve(data_release, expected):
    assert resolve_collection(data_release, COLLECTIONS)["name"] == expected


def test_dr5_does_not_match_fornax():
    # splusdata.Core.get_collection_id_by_pattern('dr5') devolveria idr5_fornax.
    assert resolve_collection("dr5", COLLECTIONS)["id"] == 27


def test_ambiguous():
    with pytest.raises(ValueError, match="ambiguo"):
        resolve_collection("fornax", COLLECTIONS)


def test_unknown():
    with pytest.raises(ValueError, match="nao encontrado"):
        resolve_collection("dr9", COLLECTIONS)
