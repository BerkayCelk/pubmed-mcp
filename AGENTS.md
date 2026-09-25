# AGENTS.md

Guidance for AI coding agents that install, use, or modify this repo.

## What this is

`pubmed-mcp` -- a single-file MCP (stdio) server (17 tools) for biomedical
literature research: PubMed (NCBI E-utilities), Europe PMC, PMC full text /
Unpaywall, MeSH, citations, ClinicalTrials.gov, Semantic Scholar, OpenFDA
(adverse events / labels / enforcement), and DrugBank.

Read-only, all API keys optional (they raise rate limits / enable extras),
stdio transport, deps: `fastmcp` + `httpx` only.

## Run & install

- From a clone: `uv run --with fastmcp --with httpx python server.py`
  (or `pip install -r requirements.txt && python server.py`)
- Without cloning (the exact command users register in MCP clients):
  `uvx --from git+https://github.com/BerkayCelk/pubmed-mcp pubmed-mcp`
- Client setup (Claude Desktop/Code, Cursor, Windsurf, Hermes): see README
  sections "Client setup" and "For AI agents (automated setup)".

## Test before committing

- Smoke test (spawns the server over stdio, makes a few live calls -- keep it gentle):
  `uv run --with fastmcp --with httpx python smoke_test.py`
- Packaging check: `uv build`
- Latest-FastMCP compatibility: `uv run --with "fastmcp>=4" --with httpx python smoke_test.py`

## Conventions

- Tool docstrings are English and LLM-facing: they are the interface users'
  assistants see, so keep them precise (args, filters, caveats).
- Keep dependencies minimal (`fastmcp`, `httpx`); stay read-only with respect
  to upstream services; never hardcode API keys (env vars only, all optional).
- Keep the per-API throttles (`_rate_limit`, `_s2_rate_limit`): NCBI allows
  3 req/s without a key and 10 with; Semantic Scholar needs ~3 s spacing.
- When adding/removing/renaming tools, update in sync: README tool tables and
  counts ("17 tools"), `EXPECTED_TOOLS` in `smoke_test.py`, and the examples
  if the command changes.
- Be polite to the upstream public APIs: tests make only a handful of calls,
  no loops.
