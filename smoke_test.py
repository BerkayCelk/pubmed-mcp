#!/usr/bin/env python3
"""Smoke test: spawn the server over stdio and exercise a few tools.

Usage:
    uv run --with fastmcp --with httpx python smoke_test.py

Hits live public APIs (NCBI E-utilities, Europe PMC, OpenFDA), so keep it
gentle: it makes only a handful of calls. Works without any API keys.
"""
import asyncio
import sys
from pathlib import Path

from fastmcp import Client

SERVER = Path(__file__).resolve().parent / "server.py"

EXPECTED_TOOLS = {
    "pubmed_search", "pubmed_fetch", "pubmed_summary", "pubmed_find_related",
    "pubmed_citation_search", "pubmed_europepmc_search", "pubmed_spell_check",
    "pubmed_lookup_mesh", "pubmed_convert_ids", "pubmed_fetch_fulltext",
    "pubmed_format_citations", "pubmed_clinical_trials", "pubmed_semantic_scholar",
    "openfda_adverse_events", "openfda_drug_label", "openfda_drug_enforcement",
    "drugbank_search",
}


async def run() -> int:
    failures: list[str] = []

    async with Client(SERVER) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        print(f"ok  connected over stdio; {len(names)} tools registered")

        missing = EXPECTED_TOOLS - names
        if missing:
            failures.append(f"missing tools: {sorted(missing)}")

        res = (await client.call_tool(
            "pubmed_search", {"query": "CRISPR base editing", "max_results": 2}
        )).data
        print(f"ok  pubmed_search: total={res.get('total_count')} pmids={res.get('pmids')}")
        if not res.get("pmids"):
            failures.append("pubmed_search returned no PMIDs")

        mesh = (await client.call_tool(
            "pubmed_lookup_mesh", {"term": "diabetes mellitus", "max_results": 1}
        )).data
        print(f"ok  pubmed_lookup_mesh: {len(mesh.get('results', []))} result(s)")
        if not mesh.get("results"):
            failures.append("pubmed_lookup_mesh returned nothing")

        epmc = (await client.call_tool(
            "pubmed_europepmc_search", {"query": "CRISPR", "max_results": 1}
        )).data
        print(f"ok  pubmed_europepmc_search: total={epmc.get('total_count')}")
        if not epmc.get("total_count"):
            failures.append("europepmc total_count is 0")

        fda = (await client.call_tool(
            "openfda_adverse_events", {"drug_name": "metformin", "limit": 1}
        )).data
        print(f"ok  openfda_adverse_events: total={fda.get('total')}")
        if not fda.get("events"):
            failures.append("openfda_adverse_events returned no events")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nsmoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
