# MCP connector (Tier 1 — public, read-only)

AuctionScope can act as a **remote [MCP](https://modelcontextprotocol.io)
connector**, so Claude.ai / Claude Desktop, ChatGPT, and the Claude + OpenAI
APIs can search, compare, and score auctions directly. MCP is a cross-vendor
standard — **one server is the connector for every client**.

This is **Tier 1**: the PUBLIC, read-only tools only, with **no per-user auth**.
The per-user tools (`watch_property`, `list_alerts`, `query_user_dossier`) and
the Tavily-backed `internet_search` are intentionally excluded — they need an
authenticated identity or spend a metered quota (a later, OAuth-gated tier).

## How it works

`api/mcp_server.py` re-exposes the same `api/tools/cypher_tools.py` and
`scoring/auction_scorer.py` functions the in-app PydanticAI agent uses — the
graph logic isn't duplicated, each MCP tool is a thin adapter. The server is
mounted into the existing FastAPI app at **`/mcp`** over MCP's Streamable-HTTP
transport.

It **ships dark**: `api/main.py` only builds and mounts it when `MCP_ENABLED`
is set, and the `mcp` package is imported lazily, so the default build (and prod
until you flip the flag) is byte-for-byte unchanged.

## Tools exposed

| Tool | Purpose |
| --- | --- |
| `search_auctions` | Structured filters (price/city/area/type/bank/date) + aggregates (min/max/avg/median/p25/p75) |
| `semantic_search` | Qualitative free-text search across description + notice-markdown + notice-image vectors |
| `match_pasted_listing` | Anchor a pasted broker/WhatsApp blurb to an auction by reserve price + date |
| `get_auction_detail` | Full record for one `auction_id` (incl. re-auction `price_history`) |
| `score_auction` | 10-dimension investment score for one `auction_id` |
| `list_distinct` | Distinct values + per-value counts for breakdown/distribution questions |
| `upcoming_auctions` | Auctions with a deadline within N days |
| `borrower_lookup` | Other auctions tied to a borrower |
| `describe_schema` | Live graph schema (labels, enums, ranges, Cypher hints) |
| `run_cypher` | READ-ONLY Cypher escape hatch (writes rejected; 10s / 500-row caps). Disable with `MCP_EXPOSE_CYPHER=false` |
| `search` / `fetch` | ChatGPT-connector aliases over `semantic_search` / `get_auction_detail` |

## Enabling it

1. **`mcp` is already installed** — it ships in `requirements.lock` (pulled in by
   `pydantic-ai`'s `fastmcp` extra), so CI and Render already have it. Nothing to
   regenerate. `requirements.txt` lists it explicitly because `api/mcp_server.py`
   imports it directly.
2. **Set the flag** (Render env / `.env`):
   ```
   MCP_ENABLED=true
   # Optional:
   MCP_EXPOSE_CYPHER=true                       # false to withhold run_cypher
   MCP_ALLOWED_HOSTS=api.auctionscope.in        # only if you front it elsewhere
   MCP_PUBLIC_SITE_BASE=https://www.auctionscope.in
   ```
3. **Deploy.** The connector lives at `https://<your-api-host>/mcp`.

> **Host allow-list.** FastMCP enables DNS-rebinding protection by default and
> `421`s any non-whitelisted `Host`. We seed the allow-list with the
> `auctionscope.in` hosts + localhost, so the known deployment works out of the
> box. If the `Host` reaching the app differs (a new domain, a proxy), set
> `MCP_ALLOWED_HOSTS` (comma-separated; `host:*` matches any port) or requests
> fail with `Invalid Host header`.

> **Trailing slash.** The endpoint is served at `/mcp/`; a request to `/mcp`
> 307-redirects there. Compliant MCP clients follow it, so advertise
> `https://<host>/mcp` — if a client chokes on the redirect, use `/mcp/`.

## Connecting each client

**Claude.ai / Claude Desktop** — Settings → Connectors → *Add custom connector*
→ paste `https://<host>/mcp`. No auth needed (public, read-only).

**ChatGPT** — Settings → Connectors (or Developer Mode) → add an MCP server →
`https://<host>/mcp`. The `search` + `fetch` tools satisfy the connector
contract; Developer Mode also surfaces the richer domain tools.

**Claude API (Messages)** — server-side MCP connector, bearer-token field unused
for the public tier:
```python
client.beta.messages.create(
    model="claude-sonnet-4-6", max_tokens=1024,
    betas=["mcp-client-2025-11-20"],
    mcp_servers=[{"type": "url", "url": "https://<host>/mcp", "name": "auctionscope"}],
    tools=[{"type": "mcp_toolset", "mcp_server_name": "auctionscope"}],  # both halves required
    messages=[{"role": "user", "content": "Cheapest 3 residential plots in Chennai under 30 lakhs?"}],
)
```

**OpenAI Responses API** — remote MCP tool:
```python
client.responses.create(
    model="gpt-5",
    tools=[{"type": "mcp", "server_label": "auctionscope", "server_url": "https://<host>/mcp"}],
    input="Score auction TN-1234 and explain the weakest dimension.",
)
```

## Local testing

```bash
MCP_ENABLED=true MCP_ALLOWED_HOSTS=localhost:* uvicorn api.main:app --reload
# then point the MCP Inspector or any MCP client at http://localhost:8000/mcp
npx @modelcontextprotocol/inspector
```

## Security notes

- **Public + read-only by design.** Data is already public on auctionscope.in;
  writes are impossible (`run_cypher` rejects write clauses; every other tool is
  a read). No user identity is involved.
- **`run_cypher`** is a broad read surface (full-graph scans within the 10s /
  500-row caps). Set `MCP_EXPOSE_CYPHER=false` to withhold it from an
  unauthenticated connector.
- **No rate limiting on `/mcp` yet.** The connector is a new unauthenticated,
  potentially heavy entry point. Consider a Cloudflare rate-limit rule or
  extending the existing slowapi limiter before publishing it widely.
- **Auth is the next tier.** For the per-user tools and a one-click *authorized*
  connector, add OAuth 2.1 (the Supabase JWT verification in
  `api/auth/supabase_jwt.py` is the building block). Out of scope for Tier 1.
