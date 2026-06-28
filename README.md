# MCP Capabilities Index

MCP server determinista que indexa las tools de TODOS los MCP servers registrados en Hermes y expone búsqueda FTS5.

## ¿Por qué?

Las skills se desactualizan, la memoria se corrompe. Este server es una fuente de verdad **determinista** sobre qué tools existen y qué hacen. No depende de skills ni de memoria.

## Cómo funciona

1. **Scrape**: al iniciar, lee `~/.hermes/config.yaml`, descubre los MCP servers registrados, y scrapea `tools/list` de cada uno (HTTP directo o spawn temporal para stdio)
2. **Indexa**: almacena nombre + descripción + parámetros en SQLite con FTS5
3. **Busca**: expone tools de búsqueda para consultar en runtime

## Tools expuestas

- `search_capabilities(query, server?, limit?)` — busca tools por keyword
- `refresh_index()` — re-scrapea todos los servers
- `list_servers()` — lista servers indexados y conteo de tools

## Instalación

```bash
# 1. Clonar (si no lo tienes)
git clone https://github.com/erniomaldo/mcp-capabilities-server.git ~/Proyectos/mcp-capabilities-server

# 2. Crear venv e instalar deps
cd ~/Proyectos/mcp-capabilities-server
python3 -m venv .venv
source .venv/bin/activate
pip install mcp httpx pyyaml fastembed sqlite-vec

# 3. Agregar a ~/.hermes/config.yaml
hermes config set mcp_servers.mcp-capabilities "
  command: ~/Proyectos/mcp-capabilities-server/.venv/bin/python3
  args:
    - ~/Proyectos/mcp-capabilities-server/server.py
  timeout: 60
"

# 4. Verificar
hermes mcp list
hermes mcp test mcp-capabilities
```

Requiere: `pip install mcp httpx pyyaml fastembed sqlite-vec` (en el venv)

## Scrape standalone (sin MCP)

```bash
.venv/bin/python3 server.py --scrape-only
```

## Uso

```
search_capabilities(query="create frame instance swap component")
→ [figma-mcp-go] create_component: Convert FRAME to COMPONENT
→ [figma-mcp-go] clone_node: Clone an existing node
→ [figma-mcp-go] swap_component: Swap main component of INSTANCE
→ [figma-mcp-go] reparent_nodes: Move nodes to different parent
```
