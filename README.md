# splus-mcp

Servidor [MCP](https://modelcontextprotocol.io) para consultar e baixar dados do
[S-PLUS](https://splus.cloud) (Southern Photometric Local Universe Survey) a partir de
um assistente como o Claude: queries em catálogo, cutouts FITS, imagens RGB e campos
completos, em qualquer data release disponível para a sua conta.

É uma camada fina sobre o [`splusdata`](https://github.com/Schwarzam/splusdata)
(API ADSS do splus.cloud).

## Tools

| Tool | O que faz |
|---|---|
| `list_data_releases` | Coleções de imagem e schemas de catálogo acessíveis pela sua conta |
| `list_tables(schema)` | Tabelas de um schema (`dr5`, `idr6`, `dr4_dual`, `dr5_vacs`, ...) |
| `describe_table(schema, table, name_filter)` | Colunas e tipos de uma tabela |
| `check_coords(ra, dec)` | Quais DRs (dr4, dr5, dr6) cobrem a posição, e o campo mais próximo em cada |
| `query_catalog(query)` | Executa ADQL/SQL, salva CSV completo, devolve preview |
| `get_cutout(ra, dec, size, band, data_release)` | Stamp FITS (imagem ou weight map) |
| `get_rgb_image(...)` | RGB Lupton com 3 bandas (PNG) |
| `get_trilogy_image(...)` | Composição Trilogy com as 12 bandas (PNG) |
| `get_field(field, band, data_release)` | Campo completo (~11k × 11k px) |

As tools de imagem aceitam `data_release` (`dr4`, `dr5`, `idr6`, `idr4_fornax`, ...).
Se omitido, é usado o DR mais novo que cobre a coordenada.

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
Arquivos baixados vão para `SPLUS_DOWNLOAD_DIR` (padrão `./splus_downloads`).

## Exemplos de pedidos

- "Quais DRs cobrem RA 16, Dec -25?"
- "Me dá um cutout de 100 arcsec na banda F660 dessa posição, no dr5 e no idr6."
- "Quais colunas de photo-z existem no dr5_vacs?"
- "Seleciona as 1000 galáxias mais brilhantes em r do campo SPLUS-s20s12."

## Testes

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                       # unitários, offline
.venv/bin/python scripts/smoke_test.py # ao vivo contra o splus.cloud (usa o .env)
```

## Observações

- O acesso a schemas depende da sua conta (alguns, como `dr4_psf`, podem retornar
  "Access denied"). Use `list_data_releases` / `list_tables` para ver o que está liberado.
- `check_coords` compara com os centros de campo publicados para dr4, dr5 e dr6
  (raio padrão de 1°), então é uma estimativa de cobertura, não um teste de máscara.

## Licença

MIT. Veja [LICENSE](LICENSE).
