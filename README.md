# splus-mcp

[![tests](https://github.com/PierreAugusto/splus-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/PierreAugusto/splus-mcp/actions/workflows/tests.yml)

Servidor [MCP](https://modelcontextprotocol.io) para consultar e baixar dados do
[S-PLUS](https://splus.cloud) (Southern Photometric Local Universe Survey) a partir de
um assistente como o Claude: buscas cônicas, cross-match, SEDs de 12 bandas, cutouts e
queries livres, em qualquer data release disponível para a sua conta.

Usa o [`splusdata`](https://github.com/Schwarzam/splusdata) (API ADSS do splus.cloud).

## Tools

**Catálogos com esquema padronizado** (`idr6`, `dr5`, `idr5`, `dr4`, `dr3`). A saída tem
sempre as mesmas colunas, qualquer que seja o DR: `id, ra, dec, field, mag_<banda>, err_<banda>`,
com as bandas `u J0378 J0395 J0410 J0430 g J0515 r J0660 i J0861 z`. Não-detecções (99)
viram vazio. Bandas aceitam `r`/`R`, `F660`/`J0660`...

| Tool | O que faz |
|---|---|
| `cone_search(ra, dec, radius_arcsec, catalog, ...)` | Fontes num raio, ordenadas por distância, com filtro de magnitude opcional |
| `crossmatch(input_path \| targets, catalog, radius_arcsec, ...)` | Casa uma lista (CSV, FITS, VOTable, parquet) com o catálogo, mantendo todas as linhas de entrada |
| `get_sed(ra, dec, catalogs, aperture)` | SED de 12 bandas (mag AB e mJy) + gráfico; com vários DRs, mostra as diferenças |
| `compare_releases(ra, dec, catalogs, reference)` | Consistência entre DRs num campo: offset astrométrico e Δmag por banda |

**Imagens** (`data_release`: `dr4`, `dr5`, `idr6`, `idr4_fornax`...; se omitido, o DR mais novo que cobre a posição)

| Tool | O que faz |
|---|---|
| `get_cutout(ra, dec, size, band, ...)` | Stamp FITS (imagem ou weight map) |
| `batch_cutouts(input_path \| targets, kind, bands, ...)` | Cutouts FITS/RGB/Trilogy para uma lista, com `manifest.csv` |
| `get_rgb_image(...)` / `get_trilogy_image(...)` | RGB Lupton (3 bandas) / Trilogy (12 bandas), PNG |
| `get_field(field, band, data_release)` | Campo completo (~11k × 11k px) |

**Descoberta e queries livres**

| Tool | O que faz |
|---|---|
| `list_data_releases` | Coleções de imagem, schemas e catálogos padronizados acessíveis pela sua conta |
| `list_tables(schema)` / `describe_table(schema, table)` | Tabelas e colunas (ex.: `dr5_vacs`, photo-z) |
| `check_coords(ra, dec)` | Cobertura: apontamentos de dr4/dr5/dr6 e contagem real de fontes em cada catálogo |
| `query_catalog(query)` | ADQL/SQL livre; salva CSV e devolve preview |

Os resultados vão para disco (`SPLUS_DOWNLOAD_DIR`, padrão `./splus_downloads`), e o caminho
vem na resposta. Erros (catálogo inválido, coluna inexistente...) chegam ao assistente com a
mensagem original, para ele corrigir a chamada.

## Instalação

```bash
git clone https://github.com/PierreAugusto/splus-mcp.git
cd splus-mcp
python -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # e preencha SPLUS_USER / SPLUS_PASS
```

> Se o repositório estiver num disco de rede (sshfs, NFS), crie a venv num disco local
> (ex.: `~/.venvs/splus-mcp`): importar scipy/pandas pela rede pode levar mais de um minuto
> e estourar o timeout do cliente MCP.

## Configuração no cliente MCP

**Claude Code:**

```bash
claude mcp add splus -- /caminho/para/.venv/bin/splus-mcp
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "splus": { "command": "/caminho/para/.venv/bin/splus-mcp" }
  }
}
```

As credenciais são lidas do `.env` na raiz do repositório (ou de variáveis de ambiente).

## Exemplos de pedidos

- "Quais DRs cobrem RA 16, Dec -25?"
- "Faz o cross-match da minha lista `aglomerados.csv` com o idr6 e o dr5, raio de 1''."
- "Mostra a SED dessa estrela no dr3, dr4, dr5 e idr6 no mesmo gráfico."
- "Os DRs são consistentes em STRIPE82? Compara dr4 e dr5 com o idr6."
- "Baixa cutouts FITS em r e F660 de todos os objetos da lista, 64 px."
- "Quais colunas de photo-z existem no dr5_vacs?"

## Consistência entre data releases

Medido com `compare_releases` num único campo, STRIPE82-0001 (raio de 10′ em RA 0.5,
Dec −0.5; 123 fontes com 14 < r < 19 no idr6; abertura PStotal; setembro de 2026).
Δmag = DR − idr6 (mediana); σ = dispersão robusta em r.

| DR | casadas | sep. mediana | ΔRA cos δ | u | g | r | J0660 | i | z | σ(r) |
|---|---|---|---|---|---|---|---|---|---|---|
| dr5 | 98% | 0.24″ | −0.17″ | +0.16 | +0.05 | +0.06 | +0.04 | +0.04 | +0.03 | 0.015 |
| dr4 | 98% | 0.20″ | −0.18″ | +0.27 | +0.05 | +0.07 | +0.06 | +0.04 | +0.02 | 0.026 |
| dr3 | 98% | 0.19″ | −0.18″ | +0.33 | +0.07 | +0.10 | +0.10 | +0.08 | +0.06 | 0.014 |

Nesse campo, o idr6 sai sistematicamente **mais brilhante** que os DRs anteriores, com
diferença maior na banda u. Em `auto` o efeito é menor no dr4/dr5 (Δr ≈ +0.03) e igual no
dr3 (+0.09). Há também um deslocamento de ~0.17″ em RA. É uma medida de um campo só: ao
combinar DRs, rode `compare_releases` no seu próprio campo.

## Detalhes de implementação

- O splus.cloud aceita no máximo 4 queries assíncronas simultâneas por usuário; o servidor
  limita a própria concorrência e refaz a query se receber HTTP 429.
- `crossmatch` usa `q3c_join` no servidor para idr6, dr5, idr5 e dr4 (2000 fontes em ~3 s).
  O dr3 não tem esse índice, então usa buscas cônicas agrupadas e casamento local.
- O dr4 guarda cada banda numa tabela; o JOIN no servidor passa de minutos, então as
  bandas são buscadas em paralelo e juntadas por `ID` localmente.

## Testes

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                              # unitários, offline (rodam no CI)
.venv/bin/python scripts/smoke_test.py        # ao vivo contra o splus.cloud (usa o .env)
.venv/bin/python scripts/mcp_session_test.py  # ao vivo, pelo protocolo MCP
```

O `smoke_test.py` chama cada tool em todos os catálogos e confere o resultado (esquema,
fração de contrapartidas, consistência entre DRs, arquivos FITS válidos). O
`mcp_session_test.py` faz o que uma sessão nova do cliente faria: sobe o servidor pelo
comando do `.mcp.json`, conecta por stdio e chama as 14 tools. Serve para confirmar que o
cliente vai enxergar a versão atual.

## Observações

- O acesso a schemas depende da sua conta (alguns, como `dr4_psf`, podem retornar
  "Access denied"). Use `list_data_releases` / `list_tables` para ver o que está liberado.
- A escolha automática de DR nas tools de imagem usa os centros de campo publicados
  (raio de 1°); é uma estimativa. `check_coords` também conta as fontes nos catálogos.

## Licença

MIT. Veja [LICENSE](LICENSE).
