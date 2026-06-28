#!/usr/bin/env python3
"""
MCP Capabilities Index — RAG-powered tool discovery for Hermes.

Uses fastembed (local ONNX embeddings) + sqlite-vec (vector search in SQLite)
for semantic search across all MCP server tools. Falls back to FTS5 keyword
search when vector search has no results.
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    FastMCP = None

# Lazy-loaded embedding model
_embedding_model = None

HOME = Path.home()
CONFIG_PATH = HOME / ".hermes" / "config.yaml"
DB_PATH = HOME / ".local" / "share" / "mcp-capabilities.db"
NPX_CACHE = HOME / ".npm" / "_npx"

EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # 384d, 220MB, 50+ languages
VEC_DIMS = 384

def resolve_node() -> str:
    """Find node.js executable."""
    p = shutil.which("node") or shutil.which("nodejs")
    if p:
        return p
    # Check nvm
    for m in sorted(Path.home().glob(".nvm/versions/node/*/bin/node"), reverse=True):
        return str(m)
    return "node"  # last resort, will fail gracefully


def get_embedder():
    global _embedding_model
    if _embedding_model is None:
        from fastembed import TextEmbedding
        _embedding_model = TextEmbedding(model_name=EMBEDDING_MODEL)
    return _embedding_model


def embed_text(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for a list of texts."""
    model = get_embedder()
    return [list(vec) for vec in model.embed(texts)]


def embed_query(text: str) -> list[float]:
    """Generate embedding for a single query."""
    model = get_embedder()
    return list(list(model.query_embed(text))[0])


def get_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    # Load sqlite-vec extension
    conn.enable_load_extension(True)
    try:
        conn.load_extension("vec0")  # sqlite-vec auto-loads via pip
    except Exception:
        # Try loading via sqlite_vec module
        try:
            import sqlite_vec
            sqlite_vec.load(conn)
        except Exception as e:
            print(f"⚠ sqlite-vec not available, falling back to FTS5 only: {e}", file=sys.stderr)

    # Tools table (same as before)
    conn.execute("""CREATE TABLE IF NOT EXISTS tools (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        server TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        params TEXT NOT NULL DEFAULT '{}',
        scraped_at REAL NOT NULL DEFAULT 0,
        UNIQUE(server, tool_name)
    )""")

    # FTS5 for keyword fallback
    conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS tools_fts USING fts5(
        server, tool_name, description, params,
        content='tools', content_rowid='id'
    )""")

    # Vector table for embeddings (sqlite-vec)
    try:
        conn.execute(f"""CREATE VIRTUAL TABLE IF NOT EXISTS tools_vec USING vec0(
            embedding float[{VEC_DIMS}]
        )""")
    except Exception:
        pass  # sqlite-vec not available

    # FTS triggers
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS tools_ai AFTER INSERT ON tools BEGIN
            INSERT INTO tools_fts(rowid, server, tool_name, description, params)
            VALUES (new.id, new.server, new.tool_name, new.description, new.params);
        END;
        CREATE TRIGGER IF NOT EXISTS tools_ad AFTER DELETE ON tools BEGIN
            INSERT INTO tools_fts(tools_fts, rowid, server, tool_name, description, params)
            VALUES ('delete', old.id, old.server, old.tool_name, old.description, old.params);
            DELETE FROM tools_vec WHERE rowid = old.id;
        END;
    """)

    return conn


def store_embedding(conn: sqlite3.Connection, row_id: int, vector: list[float]):
    """Store a vector embedding for a tool."""
    vec_json = json.dumps([float(v) for v in vector])
    try:
        conn.execute("INSERT INTO tools_vec (rowid, embedding) VALUES (?, ?)", (row_id, vec_json))
    except Exception:
        pass  # vec table missing


def read_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else CONFIG_PATH
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def find_mcp_servers(config: dict, self_name: str = "mcp-capabilities") -> list[dict]:
    servers = []
    mcp_cfg = config.get("mcp_servers", config.get("mcp", {}))
    for name, sv in mcp_cfg.items():
        if not isinstance(sv, dict) or sv.get("enabled") is False:
            continue
        if name == self_name:
            continue
        entry = {"name": name}
        if "url" in sv:
            entry["transport"] = "http"
            entry["url"] = sv["url"]
            entry["timeout"] = sv.get("timeout", 30)
        elif "command" in sv:
            entry["transport"] = "stdio"
            entry["command"] = sv["command"]
            entry["args"] = sv.get("args", [])
            entry["env"] = sv.get("env", {})
            entry["timeout"] = sv.get("timeout", 30)
        else:
            continue
        tools_cfg = sv.get("tools", {})
        entry["include"] = tools_cfg.get("include", None) if isinstance(tools_cfg, dict) else None
        entry["exclude"] = tools_cfg.get("exclude", None) if isinstance(tools_cfg, dict) else None
        servers.append(entry)
    return servers


def resolve_stdio_command(entry: dict) -> list[str] | None:
    cmd = entry["command"]
    args = entry.get("args", [])

    if cmd in ("python3", "python"):
        script = args[0] if args else ""
        if script and os.path.isfile(script):
            return [cmd, script]
        return None

    if cmd.startswith("/") and os.path.isfile(cmd):
        return [cmd] + list(args)

    if cmd == "npx" or cmd.endswith("/npx"):
        npx_args = list(args)
        pkg_name = None
        for a in npx_args:
            if a.startswith("@") and not a.startswith("--"):
                pkg_name = a.split("@")[0] + "@" + a.split("@")[1] if a.count("@") >= 2 else a
                pkg_name = pkg_name.rsplit("@", 1)[0] if pkg_name.count("@") >= 2 else pkg_name
                break
        if not pkg_name:
            for a in npx_args:
                if not a.startswith("-") and not a.startswith("@"):
                    pkg_name = a
                    break

        if not pkg_name:
            return None

        pkg_dir_name = pkg_name.replace("/", os.sep)
        if NPX_CACHE.exists():
            for cache_dir in sorted(NPX_CACHE.iterdir(), reverse=True):
                pkg_path = cache_dir / "node_modules" / pkg_dir_name
                if not pkg_path.exists():
                    continue
                pkg_json = pkg_path / "package.json"
                if pkg_json.exists():
                    try:
                        pkg = json.loads(pkg_json.read_text())
                        bin_entry = pkg.get("bin", {})
                        if isinstance(bin_entry, str):
                            fp = pkg_path / bin_entry
                            if fp.exists():
                                return [resolve_node(), str(fp)]
                        elif isinstance(bin_entry, dict):
                            for _, bp in bin_entry.items():
                                fp = pkg_path / bp
                                if fp.exists():
                                    return [resolve_node(), str(fp)]
                    except Exception:
                        pass

        # Check globally installed npm packages
        global_cmd = shutil.which(pkg_name) if pkg_name else None
        if global_cmd:
            try:
                npm_root = subprocess.run(
                    ["node", "-e",
                     "console.log(require('path').dirname(require('fs').realpathSync(process.execPath))+'/../lib/node_modules')"],
                    capture_output=True, text=True, timeout=5
                ).stdout.strip()
                pkg_global = Path(npm_root) / pkg_name
                if pkg_global.exists():
                    pkg_json = pkg_global / "package.json"
                    if pkg_json.exists():
                        pkg = json.loads(pkg_json.read_text())
                        bin_entry = pkg.get("bin", {})
                        if isinstance(bin_entry, str):
                            fp = pkg_global / bin_entry
                            if fp.exists():
                                return [resolve_node(), str(fp)]
                        elif isinstance(bin_entry, dict):
                            for _, bp in bin_entry.items():
                                fp = pkg_global / bp
                                if fp.exists():
                                    return [resolve_node(), str(fp)]
            except Exception:
                pass
            return [global_cmd]

        npx_path = shutil.which("npx")
        if npx_path:
            filtered = [a for a in npx_args if not a.startswith("-")]
            return [npx_path, "--no-install"] + filtered

        return None

    if cmd == "uv":
        uv_path = shutil.which("uv")
        if uv_path:
            return [uv_path] + list(args)
        return None

    cmd_path = shutil.which(cmd)
    if cmd_path:
        return [cmd_path] + list(args)

    return None


def scrape_http_server(name: str, url: str, timeout: int = 30) -> list[dict]:
    tools = []
    body = {"jsonrpc": "2.0", "id": "init", "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "mcp-capabilities", "version": "1.0"}}}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(url, json=body, headers=headers)
            sid = r.headers.get("mcp-session-id") or r.headers.get("MCP-Session-ID")
            if not sid:
                return tools
            c.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                   headers={**headers, "MCP-Session-ID": sid})
            req_id = str(uuid.uuid4())
            r2 = c.post(url, json={"jsonrpc": "2.0", "id": req_id, "method": "tools/list", "params": {}},
                        headers={**headers, "MCP-Session-ID": sid})
            for line in r2.text.split("\n"):
                if line.startswith("data:"):
                    data = json.loads(line[5:].strip())
                    if data.get("id") == req_id and "result" in data:
                        for t in data["result"].get("tools", []):
                            params = t.get("inputSchema", t.get("parameters", {}))
                            tools.append({
                                "server": name,
                                "tool_name": t["name"],
                                "description": t.get("description", ""),
                                "params": json.dumps(params) if params else "{}",
                            })
                        break
            try:
                c.delete(url, headers={**headers, "MCP-Session-ID": sid})
            except Exception:
                pass
    except Exception as e:
        print(f"  ⚠ HTTP error: {e}", file=sys.stderr)
    return tools


def scrape_stdio_server(name: str, entry: dict) -> list[dict]:
    tools = []
    resolved = resolve_stdio_command(entry)
    if not resolved:
        print(f"skip (unresolvable)", file=sys.stderr)
        return tools

    cmd = resolved
    merged_env = {**os.environ}
    env_vars = entry.get("env", {})
    if isinstance(env_vars, list):
        for var in env_vars:
            if var in os.environ:
                merged_env[var] = os.environ[var]
    elif isinstance(env_vars, dict):
        merged_env.update(env_vars)

    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=merged_env,
        )
    except FileNotFoundError:
        print(f"binary not found", file=sys.stderr)
        return tools

    def rpc(body: dict, timeout_s: int = 8) -> dict | None:
        if proc.stdin is None or proc.stdout is None:
            return None
        import select
        line = json.dumps(body) + "\n"
        try:
            proc.stdin.write(line.encode())
            proc.stdin.flush()
            end = time.time() + timeout_s
            while time.time() < end:
                ready, _, _ = select.select([proc.stdout], [], [], 0.5)
                if ready:
                    resp = proc.stdout.readline()
                    if resp:
                        return json.loads(resp.decode().strip())
                if proc.poll() is not None:
                    return None
        except Exception:
            pass
        return None

    try:
        init_resp = rpc({"jsonrpc": "2.0", "id": "init", "method": "initialize",
                         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                    "clientInfo": {"name": "mcp-capabilities", "version": "1.0"}}})
        if not init_resp or "result" not in init_resp:
            proc.kill()
            return tools
        rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        list_resp = rpc({"jsonrpc": "2.0", "id": "tools", "method": "tools/list", "params": {}})
        if list_resp and "result" in list_resp:
            for t in list_resp["result"].get("tools", []):
                params = t.get("inputSchema", t.get("parameters", {}))
                tools.append({
                    "server": name,
                    "tool_name": t["name"],
                    "description": t.get("description", ""),
                    "params": json.dumps(params) if params else "{}",
                })
        include = entry.get("include")
        exclude = entry.get("exclude")
        if include:
            include_set = set(include)
            tools = [t for t in tools if t["tool_name"] in include_set]
        if exclude:
            exclude_set = set(exclude)
            tools = [t for t in tools if t["tool_name"] not in exclude_set]
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
    return tools


def scrape_all(servers: list[dict], db_path: str | Path | None = None) -> tuple[int, int]:
    conn = get_db(db_path)
    now = time.time()
    total = 0
    failures = 0
    all_tools = []  # collect for batch embedding

    for sv in servers:
        name = sv["name"]
        print(f"  {name}...", end=" ", file=sys.stderr)
        sys.stderr.flush()

        sv_tools = []
        if sv["transport"] == "http":
            sv_tools = scrape_http_server(name, sv["url"], sv.get("timeout", 30))
        elif sv["transport"] == "stdio":
            sv_tools = scrape_stdio_server(name, sv)
        else:
            print("unknown transport", file=sys.stderr)
            continue

        if not sv_tools:
            failures += 1
            print("no tools", file=sys.stderr)
            continue

        # Insert tools into DB
        for t in sv_tools:
            conn.execute(
                "INSERT OR REPLACE INTO tools (server, tool_name, description, params, scraped_at) VALUES (?, ?, ?, ?, ?)",
                (t["server"], t["tool_name"], t["description"], t["params"], now),
            )
            # Get the rowid for embedding
            row = conn.execute(
                "SELECT id FROM tools WHERE server = ? AND tool_name = ?",
                (t["server"], t["tool_name"])
            ).fetchone()
            if row:
                t["_rowid"] = row[0]

        total += len(sv_tools)
        conn.commit()
        print(f"{len(sv_tools)} tools", file=sys.stderr)
        all_tools.extend(sv_tools)

    # Generate embeddings in batch
    if all_tools:
        print(f"  Generating embeddings for {len(all_tools)} tools...", file=sys.stderr)
        texts = []
        tool_ids = []
        for t in all_tools:
            if "_rowid" not in t:
                continue
            text = f"{t['server']} {t['tool_name']} {t['description']} {t['params']}"
            texts.append(text)
            tool_ids.append(t["_rowid"])

        if texts:
            try:
                vectors = embed_text(texts)
                for row_id, vec in zip(tool_ids, vectors):
                    store_embedding(conn, row_id, vec)
                conn.commit()
                print(f"  Embeddings stored: {len(vectors)} vectors", file=sys.stderr)
            except Exception as e:
                print(f"  ⚠ Embedding error: {e}", file=sys.stderr)

    conn.close()
    return total, failures


def search_hybrid(query: str, server: str | None = None,
                  limit: int = 10, db_path: str | Path | None = None) -> list[dict]:
    """Hybrid search: vector first, FTS5 fallback."""
    conn = get_db(db_path)

    # Strategy 1: Vector search (semantic)
    vec_results = _vector_search(conn, query, server, limit)
    if len(vec_results) >= limit:
        conn.close()
        return vec_results

    # Strategy 2: FTS5 fallback (keyword)
    fts_results = _fts_search(conn, query, server, limit - len(vec_results))
    conn.close()

    # Merge: vec results first, then FTS5, deduplicate
    seen = set(r["tool"] + r["server"] for r in vec_results)
    merged = list(vec_results)
    for r in fts_results:
        key = r["tool"] + r["server"]
        if key not in seen:
            seen.add(key)
            merged.append(r)

    return merged[:limit]


def _vector_search(conn: sqlite3.Connection, query: str, server: str | None = None,
                   limit: int = 10) -> list[dict]:
    """Search using vector similarity."""
    try:
        vec = embed_query(query)
        vec_json = json.dumps([float(v) for v in vec])

        sql = """
            SELECT t.server, t.tool_name, t.description, t.params, v.distance
            FROM tools_vec v
            JOIN tools t ON t.id = v.rowid
            WHERE v.embedding MATCH ?
            AND k = ?
        """
        params: list[Any] = [vec_json, limit]

        if server:
            sql += " AND t.server = ?"
            params.append(server)

        sql += " ORDER BY v.distance"

        rows = conn.execute(sql, params).fetchall()
        return [{"server": r[0], "tool": r[1], "description": r[2],
                 "params": json.loads(r[3]) if r[3] and r[3] != "{}" else {},
                 "score": float(r[4]), "method": "vector"} for r in rows]
    except Exception:
        return []


def _fts_search(conn: sqlite3.Connection, query: str, server: str | None = None,
                limit: int = 10) -> list[dict]:
    """Fallback keyword search using FTS5. Uses OR + prefix matching."""
    # Split into terms and make each a prefix match
    terms = re.findall(r'\w+', query)
    if not terms:
        safe_query = "*"
    else:
        # FTS5: term* for prefix, OR between terms
        safe_query = " OR ".join(f"{t}*" for t in terms)

    sql = """SELECT t.server, t.tool_name, t.description, t.params
             FROM tools_fts f JOIN tools t ON t.id = f.rowid
             WHERE tools_fts MATCH ?"""
    params: list[Any] = [safe_query]

    if server:
        sql += " AND t.server = ?"
        params.append(server)

    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [{"server": r[0], "tool": r[1], "description": r[2],
             "params": json.loads(r[3]) if r[3] and r[3] != "{}" else {},
             "method": "fts"} for r in rows]


def search_tools(query: str, server: str | None = None,
                 limit: int = 10, db_path: str | Path | None = None) -> list[dict]:
    """Main search entry point. Uses hybrid search."""
    return search_hybrid(query, server, limit, db_path)


def create_mcp_server(db_path: str | Path | None = None):
    mcp = FastMCP("mcp-capabilities",
                  instructions="Index and search MCP server tool capabilities. Uses RAG (embeddings + semantic search) to find relevant tools.")

    @mcp.tool()
    def search_capabilities(query: str, server: str | None = None, limit: int = 10) -> str:
        """Search MCP server tool capabilities using semantic search (RAG).

        Args:
            query: Natural language query (e.g. 'create a component with instance slots')
            server: Optional filter by server name
            limit: Max results (default 10, max 30)
        """
        results = search_tools(query, server, min(limit, 30), db_path)
        if not results:
            return "No matching tools found."

        method = "semantic" if results[0].get("method") == "vector" else "keyword"
        lines = [f"Found {len(results)} tool(s) [{method}]:\n"]
        for i, r in enumerate(results, 1):
            score = r.get("score")
            score_str = f" (score: {score:.3f})" if score is not None else ""
            lines.append(f"{i}. [{r['server']}] {r['tool']}{score_str}")
            if r['description']:
                lines.append(f"   {r['description']}")
        return "\n".join(lines)

    @mcp.tool()
    def refresh_index() -> str:
        """Re-scrape all MCP servers and rebuild the tool index with embeddings."""
        config = read_config()
        servers = find_mcp_servers(config)
        if not servers:
            return "No MCP servers found in config."
        count, fails = scrape_all(servers, db_path)
        return f"Index refreshed: {count} tools from {len(servers)} servers ({fails} unreachable)."

    @mcp.tool()
    def list_servers() -> str:
        """List indexed MCP servers with tool counts."""
        conn = get_db(db_path)
        rows = conn.execute(
            "SELECT server, COUNT(*) FROM tools GROUP BY server ORDER BY server"
        ).fetchall()
        conn.close()
        if not rows:
            return "No servers indexed. Run refresh_index() first."
        return "Indexed MCP servers:\n" + "\n".join(f"  {r[0]}: {r[1]} tools" for r in rows)

    return mcp


def should_scrape(db_path: str | Path) -> bool:
    """Skip scraping if DB already has fresh data."""
    p = Path(db_path)
    if not p.exists():
        return True
    try:
        conn = sqlite3.connect(str(p), timeout=3)
        cnt = conn.execute("SELECT COUNT(*) FROM tools").fetchone()[0]
        conn.close()
        return cnt < 10  # less than 10 tools = stale/unpopulated
    except Exception:
        return True


def main():
    parser = argparse.ArgumentParser(description="MCP Capabilities Index (RAG)")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--scrape-only", action="store_true")
    parser.add_argument("--force-scrape", action="store_true",
                        help="Force re-scrape even if DB has data")
    args = parser.parse_args()

    db_path = Path(args.db_path) if args.db_path else DB_PATH

    if args.force_scrape or should_scrape(db_path) or args.scrape_only:
        config = read_config()
        servers = find_mcp_servers(config)
        if servers:
            count, fails = scrape_all(servers, db_path)
            print(f"Done: {count} tools from {len(servers)} servers ({fails} unreachable).", file=sys.stderr)
        else:
            print("No MCP servers found.", file=sys.stderr)
    else:
        print(f"DB has data, skipping scrape. Use --force-scrape to refresh.", file=sys.stderr)

    if args.scrape_only:
        return

    if FastMCP is None:
        print("ERROR: mcp package not installed. Run: pip install mcp", file=sys.stderr)
        sys.exit(1)

    mcp = create_mcp_server(db_path)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
