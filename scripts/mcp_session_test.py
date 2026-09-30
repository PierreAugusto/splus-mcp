"""Teste pelo protocolo MCP, como uma sessao nova do cliente faria.

Sobe o servidor pelo comando do .mcp.json (ou `splus-mcp` do PATH), conecta por stdio,
confere que as 14 tools estao registradas e chama cada uma. Precisa do .env e de rede.
Uso: python scripts/mcp_session_test.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
DL = os.environ.get("SPLUS_DOWNLOAD_DIR") or tempfile.mkdtemp(prefix="splus_mcp_session_")

S82_STAR = {"ra": 0.55079, "dec": -0.34329}  # estrela r~13.7 em STRIPE82-0001 (dr3, dr4, dr5, idr6)
SOUTH = {"ra": 16.0, "dec": -25.0}           # SPLUS-s20s12 (dr5, idr5, idr6; fora do dr3/dr4)

# (tool, argumentos, deve_falhar)
CALLS = [
    ("list_data_releases", {}, False),
    ("list_tables", {"schema": "dr5_vacs"}, False),
    ("describe_table", {"schema": "dr5_vacs", "table": "dr5_photoz_v3", "name_filter": "zml"}, False),
    ("check_coords", SOUTH, False),
    ("query_catalog", {"query": "SELECT TOP 5 ra, dec, field, mag_pstotal_r FROM idr6.idr6 "
                                "WHERE field = 'SPLUS-s20s12' AND mag_pstotal_r < 15", "max_preview_rows": 2}, False),
    ("cone_search", {**SOUTH, "radius_arcsec": 120, "catalog": "dr5", "bands": ["r", "F660"], "mag_max": 19,
                     "max_preview_rows": 2}, False),
    ("crossmatch", {"targets": [{"name": "estrela", **S82_STAR}, {"name": "sul", "ra": 16.0001, "dec": -25.0},
                                {"name": "fora", "ra": 150.0, "dec": 60.0}],
                    "catalog": "dr4", "radius_arcsec": 2, "bands": ["r", "J0660"], "max_preview_rows": 3}, False),
    ("get_sed", {**S82_STAR, "catalogs": ["dr3", "dr4", "dr5", "idr6"]}, False),
    ("compare_releases", {"ra": 0.5, "dec": -0.5, "radius_arcsec": 300, "catalogs": ["dr4", "dr5"],
                          "reference": "idr6", "bands": ["g", "r", "i"]}, False),
    ("get_cutout", {**SOUTH, "size": 64, "band": "F660", "data_release": "dr5"}, False),
    ("get_rgb_image", {**S82_STAR, "size": 200}, False),
    ("get_trilogy_image", {**S82_STAR, "size": 200, "data_release": "idr6"}, False),
    ("batch_cutouts", {"targets": [{"name": "a", **S82_STAR}, {"name": "b", **SOUTH}], "kind": "rgb", "size": 96}, False),
    # Um campo real tem ~300 MB; aqui so o caminho de erro, que deve chegar ao cliente com a mensagem.
    ("get_field", {"field": "NAO-EXISTE", "band": "R", "data_release": "dr5"}, True),
]


def server_params() -> StdioServerParameters:
    mcp_json = ROOT / ".mcp.json"
    if mcp_json.exists():
        cfg = json.loads(mcp_json.read_text())["mcpServers"]["splus"]
        command, args, env = cfg["command"], cfg.get("args", []), cfg.get("env", {})
    else:
        command = shutil.which("splus-mcp") or str(Path(sys.executable).parent / "splus-mcp")
        args, env = [], {}
    print("servidor:", command)
    return StdioServerParameters(command=command, args=args, cwd=str(ROOT),
                                 env={**os.environ, **env, "SPLUS_DOWNLOAD_DIR": DL})


def summarize(name, d):
    if isinstance(d, str):
        return d
    if name == "list_data_releases":
        return f"imagens={d['image_collections']} | catalogos={list(d['standard_catalogs'])} | {len(d['catalog_schemas'])} schemas"
    if name == "list_tables":
        return str(d)
    if name == "describe_table":
        return f"{d['table']}: {[c['name'] for c in d['columns']]}"
    if name == "check_coords":
        return f"fontes por catalogo={ {c['catalog']: c.get('n_sources') for c in d['catalogs']} }"
    if name in ("query_catalog", "cone_search"):
        return f"{d['row_count']} linhas | 1a: {d['preview'][0] if d['preview'] else None}"
    if name == "crossmatch":
        return f"{d['n_matched']}/{d['n_input']} casados, sep mediana {d['median_sep_arcsec']}'' | " + \
            str([(p['name'], p['splus_id']) for p in d['preview']])
    if name == "get_sed":
        return f"{[(s['catalog'], s.get('dist_arcsec')) for s in d['sources']]} | dmag r vs idr6: " + \
            str({c: v['r'] for c, v in d['delta_mag_vs_idr6'].items()})
    if name == "compare_releases":
        return str([(c['catalog'], f"{c['n_matched']}/{c['n_reference']}", c['median_sep_arcsec'],
                     {b: v['median'] for b, v in c['delta_mag'].items()}) for c in d['comparisons']])
    if name == "batch_cutouts":
        return f"{d['n_files_ok']} ok, {d['n_errors']} erros"
    if isinstance(d, dict) and "saved_to" in d:
        return f"{d.get('data_release', '')} -> {Path(d['saved_to']).name}"
    return str(d)


async def main() -> int:
    t0 = time.time()
    failures = 0
    async with stdio_client(server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = sorted(t.name for t in (await session.list_tools()).tools)
            expected = sorted(name for name, _, _ in CALLS)
            print(f"pronto em {time.time() - t0:.1f}s com {len(tools)} tools\n")
            if tools != expected:
                print("tools diferentes do esperado:", sorted(set(tools) ^ set(expected)))
                return 1

            for name, args, should_fail in CALLS:
                t = time.time()
                r = await session.call_tool(name, args)
                texts = [c.text for c in r.content if hasattr(c, "text")]
                try:
                    data = json.loads(texts[0]) if len(texts) == 1 else [json.loads(x) for x in texts]
                except json.JSONDecodeError:
                    data = " ".join(texts)
                ok = r.is_error == should_fail
                failures += not ok
                label = ("OK  " if ok else "FAIL") + (" (erro esperado)" if should_fail and ok else "")
                print(f"{label} {name:20s} {time.time() - t:5.1f}s")
                print("     ", summarize(name, data)[:300])

    print(f"\n{len(CALLS) - failures}/{len(CALLS)} ok  (arquivos em {DL})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
