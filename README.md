# pubmed-mcp

**An MCP server for biomedical literature research — 17 tools covering PubMed,
Europe PMC, full-text retrieval, MeSH, citations, clinical trials, Semantic
Scholar, OpenFDA and DrugBank.**

One file, deps: FastMCP + httpx only. Works out of the box — **all API keys are
optional** (they raise rate limits and enable a couple of extras). Read-only:
it never writes to upstream services.

**17 tools · optional API keys · read-only · works with any MCP client** (Claude Desktop, Claude Code, Cursor, Windsurf, Hermes, ...).

**The server itself is a single Python file: [`server.py`](server.py)** — everything else in this repo is documentation, packaging and tests.

## What it gives your assistant

| # | Tool | Source | What it does |
|---|---|---|---|
| 1 | `pubmed_search` | PubMed | Search with year / free-fulltext / abstract filters |
| 2 | `pubmed_fetch` | PubMed | Full metadata by PMID (abstract, authors, DOI, grants) |
| 3 | `pubmed_summary` | PubMed | Fast summaries for many PMIDs at once |
| 4 | `pubmed_find_related` | PubMed | Similar / citing / reference articles |
| 5 | `pubmed_citation_search` | PubMed | Partial citation → PMID resolution |
| 6 | `pubmed_europepmc_search` | Europe PMC | Broader corpus: preprints, patents, Agricola |
| 7 | `pubmed_spell_check` | NCBI ESpell | Biomedical query spell check |
| 8 | `pubmed_lookup_mesh` | MeSH | Medical Subject Headings vocabulary |
| 9 | `pubmed_convert_ids` | PMC ID Converter | DOI ↔ PMID ↔ PMCID conversion |
| 10 | `pubmed_fetch_fulltext` | PMC / Europe PMC / Unpaywall | Full text as structured sections |
| 11 | `pubmed_format_citations` | Local | APA, MLA, BibTeX, RIS, Vancouver |
| 12 | `pubmed_clinical_trials` | ClinicalTrials.gov | Search studies by condition / drug / status |
| 13 | `pubmed_semantic_scholar` | Semantic Scholar | Citation counts, influence, OA PDF links |
| 14 | `openfda_adverse_events` | FDA FAERS | Drug side-effect reports |
| 15 | `openfda_drug_label` | FDA labels | Indications, warnings, mechanism, PK |
| 16 | `openfda_drug_enforcement` | FDA | Recalls and market withdrawals |
| 17 | `drugbank_search` | DrugBank | Drug targets / interactions (needs free key) |

## Quick start

No installation needed — run it straight from GitHub with [uv](https://docs.astral.sh/uv/):

```bash
uvx --from git+https://github.com/BerkayCelk/pubmed-mcp pubmed-mcp
```

or from a clone:

```bash
pip install -r requirements.txt
python server.py
```

## Client setup

**Claude Desktop** — merge into `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pubmed": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/BerkayCelk/pubmed-mcp", "pubmed-mcp"]
    }
  }
}
```

**Claude Code**:

```bash
claude mcp add pubmed -- uvx --from git+https://github.com/BerkayCelk/pubmed-mcp pubmed-mcp
```

**Cursor** (`~/.cursor/mcp.json`) and **Windsurf**: same JSON shape as Claude Desktop.
**Hermes**: add to your `config.yaml` under `mcp_servers` (see `examples/hermes_config.yaml`).

Ready-made config files live in [`examples/`](examples/).

## For AI agents (automated setup)

Written so an AI coding agent (Claude Code, Codex, Cursor, Hermes, ...) can install and verify this server on its own.

**1. Install command (no clone needed):**

```bash
uvx --from git+https://github.com/BerkayCelk/pubmed-mcp pubmed-mcp
```

**2. Register with a client:**

- **Claude Code:**
  ```bash
  claude mcp add pubmed -- uvx --from git+https://github.com/BerkayCelk/pubmed-mcp pubmed-mcp
  ```
- **Claude Desktop** — merge into `claude_desktop_config.json` (`~/Library/Application Support/Claude/` on macOS, `%APPDATA%\Claude\` on Windows, `~/.config/Claude/` on Linux):
  ```json
  {"mcpServers": {"pubmed": {"command": "uvx", "args": ["--from", "git+https://github.com/BerkayCelk/pubmed-mcp", "pubmed-mcp"]}}}
  ```
- **Cursor** — same JSON in `~/.cursor/mcp.json` (project scope: `.cursor/mcp.json`).
- **Hermes** — see [`examples/hermes_config.yaml`](examples/hermes_config.yaml).
- **Any other MCP client** — register this stdio command:
  `uvx --from git+https://github.com/BerkayCelk/pubmed-mcp pubmed-mcp`

**3. Verify (no client needed):**

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' | uvx --from git+https://github.com/BerkayCelk/pubmed-mcp pubmed-mcp | head -c 400
```

Expect JSON containing `"serverInfo":{"name":"PubMed"`.

**4. Usage notes for agents:**

- Literature questions -> `pubmed_search`, then `pubmed_fetch` / `pubmed_summary` for the hits; use `pubmed_europepmc_search` for preprints and grey literature.
- Full text -> `pubmed_fetch_fulltext` (chain: PMC → Europe PMC → Unpaywall; set `UNPAYWALL_EMAIL` to enable the last step).
- Drug safety -> `openfda_*` tools (no key needed); `drugbank_search` requires a free `DRUGBANK_API_KEY`.
- Batch PMIDs whenever possible (up to 50 per call) and don't remove the built-in throttles — these are shared public APIs.

**5. Copy-paste prompt for users:**

> Set up the PubMed MCP server for me: register `uvx --from git+https://github.com/BerkayCelk/pubmed-mcp pubmed-mcp` as a stdio MCP server named `pubmed` in my client config, restart/reload the client, then verify by calling the `pubmed_search` tool with a small test query.

## Optional API keys

Everything works without keys. Keys only raise rate limits or enable extras:

| Env var | Effect | Where to get it |
|---|---|---|
| `NCBI_API_KEY` | 3 → 10 requests/sec on NCBI | https://www.ncbi.nlm.nih.gov/account/settings/ |
| `UNPAYWALL_EMAIL` | Enables the Unpaywall fallback in `pubmed_fetch_fulltext` (any email works) | — |
| `S2_API_KEY` | Relaxes Semantic Scholar rate limits | https://www.semanticscholar.org/product/api |
| `EUROPEPMC_EMAIL` | Polite-pool identifier for Europe PMC | — |
| `DRUGBANK_API_KEY` | Enables `drugbank_search` | https://go.drugbank.com |

## Full text chain & rate limits

`pubmed_fetch_fulltext` tries, in order: **NCBI PMC → Europe PMC → Unpaywall**,
returning structured sections (Introduction, Methods, Results, ...) when available.

The server throttles itself: 0.35 s between NCBI calls (0.1 s with a key) and
3 s for Semantic Scholar. This wraps shared public APIs — please keep the
throttles. Data sources: [NCBI](https://www.ncbi.nlm.nih.gov/home/about/policies/),
[Europe PMC](https://europepmc.org/), [OpenFDA](https://open.fda.gov/),
[ClinicalTrials.gov](https://clinicaltrials.gov/),
[Semantic Scholar](https://www.semanticscholar.org/),
[Unpaywall](https://unpaywall.org/), [DrugBank](https://go.drugbank.com/).

## Development

- Run from a clone: `uv run --with fastmcp --with httpx python server.py`
- Smoke test (spawns the server over stdio, a few live calls — keep it gentle):
  `uv run --with fastmcp --with httpx python smoke_test.py`
- Packaging check: `uv build`

## Disclaimer

Unofficial community project — not affiliated with or endorsed by NCBI/NLM,
Europe PMC, the FDA, ClinicalTrials.gov, Semantic Scholar, OurResearch, or
DrugBank. For research assistance only; **not medical advice**.

## License

MIT — see [LICENSE](LICENSE).
