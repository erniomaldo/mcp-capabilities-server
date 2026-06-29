# MCP Capabilities Index

Indexa las tools de **todos** los MCP servers registrados en Hermes y expone
búsqueda semántica (embeddings multilingües) + FTS5 de respaldo.

## ¿Por qué?

Las skills se desactualizan, la memoria se corrompe. Este server es una fuente
de verdad **determinista** sobre qué tools existen y qué hacen. No depende de
skills ni de memoria.

## Cómo funciona

1. **Scrape** — al arrancar (o al llamar `refresh_index()`), lee
   `~/.hermes/config.yaml`, descubre los MCP servers registrados, y scrapea
   `tools/list` de cada uno (HTTP directo o stdio temporal).
2. **Indexa** — almacena nombre + descripción + parámetros en SQLite con FTS5
   + embeddings vectoriales (fastembed + sqlite-vec).
3. **Busca** — búsqueda híbrida: embeddings semánticos primero, FTS5 de
   respaldo si no hay suficientes resultados.

## Tools MCP expuestas

| Tool | Descripción |
|------|-------------|
| `search_capabilities(query, server?, limit?)` | Búsqueda semántica + keyword |
| `refresh_index()` | Re-scrapea **todos** los servers |
| `list_servers()` | Lista servers indexados con conteo de tools |

## ⚠️ Timeout — requisito crítico

El `refresh_index()` scrapea **cada** MCP server secuencialmente. Servidores
pesados como **nextcloud** (40+ tools entre calendar, deck, collectives, talk,
webdav, shares, contacts, cookbook, news, notes, tables…) pueden tomar
varios segundos cada uno. El embedding batch (fastembed cargando modelo
multilingüe) añade más tiempo.

**Si el timeout del MCP client es muy bajo, `refresh_index()` timetea y
deja servers sin indexar.**

### Valor recomendado: 300s (5 minutos)

```bash
hermes config set mcp-capabilities.timeout 300
```

Esto le da tiempo de sobra para scrapear todos los servers, generar
embeddings, y escribir la DB antes de que el MCP client corte la conexión.

### Cómo detectar que falta tiempo

Si `refresh_index()` devuelve error `TimeoutError`:

```
MCP call failed: TimeoutError: MCP call timed out after 60.0s
```

Y `list_servers()` muestra menos servers de los esperados (ej. falta
nextcloud), el timeout está muy bajo.

También puedes verificar cuántos servers se scrapearon vs cuántos hay
registrados:

```bash
# Servers registrados
grep -E "^\s{2}[a-z]" ~/.hermes/config.yaml | grep -A1 mcp_servers
# Servers indexados (desde Hermes, llama list_servers())
```

## Instalación

```bash
# 1. Clonar
git clone https://github.com/erniomaldo/mcp-capabilities-server.git ~/Proyectos/mcp-capabilities-server

# 2. Crear venv e instalar deps
cd ~/Proyectos/mcp-capabilities-server
python3 -m venv .venv
source .venv/bin/activate
pip install mcp httpx pyyaml fastembed sqlite-vec

# 3. Agregar a ~/.hermes/config.yaml
# ⚠️ Ajusta el timeout según la cantidad de servers (ver sección ⚠️ arriba)
cat >> ~/.hermes/config.yaml << 'EOF'
  mcp-capabilities:
    command: ~/Proyectos/mcp-capabilities-server/.venv/bin/python3
    args:
      - ~/Proyectos/mcp-capabilities-server/server.py
    timeout: 300
EOF

# 4. Subir el timeout (si usaste 60 en el paso anterior, cámbialo)
hermes config set mcp-capabilities.timeout 300

# 5. Verificar
hermes mcp list
hermes mcp test mcp-capabilities
```

> **Importante**: después de cambiar el timeout, **reinicia Hermes** para que
> recoja el nuevo valor. El MCP client no refresca timeouts en caliente.

## Scrape standalone (sin MCP)

Útil para depuración o CI:

```bash
# Scrape forzado (aunque la DB ya tenga datos)
.venv/bin/python3 server.py --force-scrape

# Scrape sin arrancar el server MCP
.venv/bin/python3 server.py --scrape-only
```

## Uso desde Hermes

```python
search_capabilities(query="create frame instance swap component")
# → [figma-mcp-go] create_component: Convert FRAME to COMPONENT
# → [figma-mcp-go] swap_component: Swap main component of INSTANCE

# Filtrar por servidor
search_capabilities(query="crear formulario", server="nc-forms")
```

## Troubleshooting

| Síntoma | Causa | Solución |
|---------|-------|----------|
| `TimeoutError` al llamar `refresh_index()` | Timeout MCP client muy bajo | `hermes config set mcp-capabilities.timeout 300` y reinicia Hermes |
| `list_servers()` muestra menos servers de los esperados | El refresh anterior timeteó antes de scrapearlos todos | Aumenta timeout y vuelve a llamar `refresh_index()` |
| `ClosedResourceError` al llamar tools | El proceso del server se mató externamente | Reinicia Hermes |
| No se genera el índice al arrancar | La DB ya tiene datos y `should_scrape()` retorna False | Usa `--force-scrape` o llama `refresh_index()` |
