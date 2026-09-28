"""Teste ao vivo contra o splus.cloud (precisa de SPLUS_USER/SPLUS_PASS no .env).

Chama as tools do servidor diretamente, cobrindo os DRs dr4, dr5 e idr6.
Uso: python scripts/smoke_test.py
"""
import time
import traceback

from splus_mcp import server

# (ra, dec) dentro de cada footprint, conferidos em 2026-09.
STRIPE82 = (0.5, -0.5)       # dr4 e idr6
SOUTH = (16.0, -25.0)        # dr5 e idr6

CASES = [
    ("list_data_releases", lambda: server.list_data_releases()),
    ("list_tables dr5", lambda: server.list_tables("dr5")),
    ("describe_table idr6.idr6", lambda: server.describe_table("idr6", "idr6", name_filter="mag_auto_r")),
    ("check_coords STRIPE82", lambda: server.check_coords(*STRIPE82)),
    ("check_coords SOUTH", lambda: server.check_coords(*SOUTH)),
    ("check_coords fora", lambda: server.check_coords(150.0, 60.0)),
    ("query idr6", lambda: server.query_catalog("SELECT TOP 5 ra, dec, field FROM idr6.idr6", 3)),
    ("query dr5_vacs", lambda: server.query_catalog("SELECT TOP 5 * FROM dr5_vacs.dr5_photoz_v3", 2)),
    ("cutout dr4", lambda: server.get_cutout(*STRIPE82, size=64, data_release="dr4")),
    ("cutout dr5", lambda: server.get_cutout(*SOUTH, size=64, data_release="dr5")),
    ("cutout idr6", lambda: server.get_cutout(*SOUTH, size=64, data_release="idr6")),
    ("cutout auto", lambda: server.get_cutout(*STRIPE82, size=64)),
    ("cutout weight dr5", lambda: server.get_cutout(*SOUTH, size=64, data_release="dr5", weight=True)),
    ("rgb dr5", lambda: server.get_rgb_image(*SOUTH, size=128, data_release="dr5")),
    ("trilogy idr6", lambda: server.get_trilogy_image(*SOUTH, size=128, data_release="idr6")),
]


def main():
    failures = 0
    for name, fn in CASES:
        t = time.time()
        try:
            result = fn()
            summary = str(result)
            print(f"OK   {name:32s} {time.time() - t:5.1f}s  {summary[:160]}")
        except Exception:
            failures += 1
            print(f"FAIL {name:32s} {time.time() - t:5.1f}s")
            traceback.print_exc()
    print(f"\n{len(CASES) - failures}/{len(CASES)} ok")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
