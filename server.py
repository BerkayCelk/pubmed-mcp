"""
PubMed MCP Server — biomedical literature toolkit.

Wraps NCBI E-utilities (PubMed, MeSH, ESpell, ECitMatch, PMC), Europe PMC,
PMC ID Converter, Unpaywall, ClinicalTrials.gov, Semantic Scholar, OpenFDA
and DrugBank behind a single MCP (stdio) server.

NCBI rate limits:
  - No API key: 3 req/sec
  - With API key: 10 req/sec
Get free key at: https://www.ncbi.nlm.nih.gov/account/settings/
"""

from __future__ import annotations

from urllib.parse import quote

import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx
from fastmcp import FastMCP

mcp = FastMCP("PubMed")

BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EPMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest"
PMC_CONVERTER_URL = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles"

API_KEY = os.getenv("NCBI_API_KEY", "")
EUROPEPMC_EMAIL = os.getenv("EUROPEPMC_EMAIL", "")
UNPAYWALL_EMAIL = os.getenv("UNPAYWALL_EMAIL", "")
S2_API_KEY = os.getenv("S2_API_KEY", "")
S2_REQUEST_DELAY = 3.0  # Semantic Scholar: very aggressive rate limiting
REQUEST_DELAY = 0.1 if API_KEY else 0.35

_last_request = 0.0
_last_s2_request = 0.0


def _rate_limit():
    global _last_request
    elapsed = time.monotonic() - _last_request
    if elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)
    _last_request = time.monotonic()


def _s2_rate_limit():
    global _last_s2_request
    elapsed = time.monotonic() - _last_s2_request
    if elapsed < S2_REQUEST_DELAY:
        time.sleep(S2_REQUEST_DELAY - elapsed)
    _last_s2_request = time.monotonic()


def _request(endpoint: str, params: dict[str, Any]) -> httpx.Response:
    _rate_limit()
    url = f"{BASE_URL}/{endpoint}"
    if API_KEY:
        params["api_key"] = API_KEY
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        return resp


def _request_raw(url: str, params: dict[str, Any] | None = None, timeout: int = 30,
                 headers: dict[str, str] | None = None) -> httpx.Response:
    """Make a non-rate-limited request to external APIs."""
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        if params:
            resp = client.get(url, params=params)
        else:
            resp = client.get(url)
        resp.raise_for_status()
        return resp


def _parse_article_xml(xml_text: str) -> dict[str, Any]:
    root = ET.fromstring(xml_text)
    articles = []
    for article_elem in root.findall(".//PubmedArticle"):
        article = _parse_one_article(article_elem)
        articles.append(article)
    return {"articles": articles, "count": len(articles)}


def _parse_one_article(article_elem) -> dict[str, Any]:
    article = {}
    pmid_elem = article_elem.find(".//PMID")
    article["pmid"] = pmid_elem.text if pmid_elem is not None else ""

    title_elem = article_elem.find(".//ArticleTitle")
    article["title"] = title_elem.text or "" if title_elem is not None else ""

    abstract_parts = []
    for abs_elem in article_elem.findall(".//AbstractText"):
        label = abs_elem.get("Label", "")
        text = "".join(abs_elem.itertext()).strip()
        abstract_parts.append(f"{label}: {text}" if label else text)
    article["abstract"] = " ".join(abstract_parts)

    authors = []
    for author_elem in article_elem.findall(".//Author"):
        last = author_elem.findtext("LastName", "")
        fore = author_elem.findtext("ForeName", "")
        initials = author_elem.findtext("Initials", "")
        if last:
            authors.append({
                "last": last,
                "fore": fore,
                "initials": initials,
                "full": f"{last} {fore}".strip(),
            })
    article["authors"] = authors

    journal_elem = article_elem.find(".//Journal")
    if journal_elem is not None:
        article["journal"] = journal_elem.findtext("Title", "")
        article["journal_iso"] = journal_elem.findtext("ISOAbbreviation", "")
        jissue = journal_elem.find(".//JournalIssue")
        if jissue is not None:
            article["volume"] = jissue.findtext("Volume", "")
            article["issue"] = jissue.findtext("Issue", "")
            pubdate = jissue.find(".//PubDate")
            if pubdate is not None:
                article["pub_year"] = pubdate.findtext("Year", "")
                article["pub_month"] = pubdate.findtext("Month", "")
            # Pagination
            pages_elem = jissue.find(".//Pagination")
            if pages_elem is not None:
                mp = pages_elem.findtext("MedlinePgn", "")
                article["pages"] = mp

    for eid in article_elem.findall(".//ELocationID"):
        if eid.get("EIdType") == "doi":
            article["doi"] = eid.text or ""
            break
    else:
        article["doi"] = ""

    article["publication_types"] = [
        pt.text for pt in article_elem.findall(".//PublicationType") if pt.text
    ]

    mesh_terms = []
    for mesh in article_elem.findall(".//MeshHeading"):
        desc = mesh.findtext("DescriptorName", "")
        if desc:
            quals = [q.text for q in mesh.findall("QualifierName") if q.text]
            mesh_terms.append(f"{desc}/{'/'.join(quals)}" if quals else desc)
    article["mesh_terms"] = mesh_terms

    # Grant/support info
    grants = []
    for grant_elem in article_elem.findall(".//Grant"):
        grant_id = grant_elem.findtext("GrantID", "")
        agency = grant_elem.findtext("Agency", "")
        country = grant_elem.findtext("Country", "")
        if grant_id or agency:
            grants.append({"grant_id": grant_id, "agency": agency, "country": country})
    article["grants"] = grants

    return article


def _author_str(a: dict) -> str:
    """Author dict to formatted string. Used in citation formatters."""
    return a.get("full", "")


# ── FULL TEXT HELPERS ──


def _resolve_pmcid(pmid: str) -> str | None:
    """Resolve PMID to PMCID via PMC ID Converter."""
    try:
        resp = _request_raw(
            f"{PMC_CONVERTER_URL}",
            params={"ids": pmid, "idtype": "pmid", "format": "json", "versions": "no"},
            timeout=15,
        )
        records = resp.json().get("records", [])
        if records and records[0].get("pmcid"):
            return records[0]["pmcid"]
    except Exception:
        pass
    return None


def _fetch_pmc_text(pmcid: str, sections: list[str] | None = None, max_sections: int = 20) -> dict | None:
    """Fetch full text from NCBI PMC as structured sections."""
    try:
        resp = _request("efetch.fcgi", {
            "db": "pmc", "id": pmcid,
            "retmode": "xml", "rettype": "full",
        })
        root = ET.fromstring(resp.text)

        # Extract body sections
        body = root.find(".//body")
        if body is None:
            return None

        result_sections: list[dict] = []
        for sec in body.findall(".//sec"):
            title_elem = sec.find("title")
            title = "".join(title_elem.itertext()).strip() if title_elem is not None else ""
            paras = ["".join(p.itertext()).strip() for p in sec.findall(".//p") if p.text or len(p)]
            content = "\n\n".join(paras)

            if sections:
                # Filter requested sections (case-insensitive)
                if not any(s.lower() in title.lower() for s in sections):
                    continue

            if content:
                result_sections.append({"title": title, "content": content})
                if len(result_sections) >= max_sections:
                    break

        # Also try front matter
        front = root.find(".//front")
        abstract = ""
        if front is not None:
            abstract_elem = front.find(".//abstract")
            if abstract_elem is not None:
                abstract = "".join(abstract_elem.itertext()).strip()

        return {
            "source": "pmc",
            "pmcid": pmcid,
            "abstract": abstract[:2000] if abstract else "",
            "sections": result_sections,
            "section_count": len(result_sections),
        }
    except Exception:
        return None


def _fetch_epmc_text(pmcid: str, sections_filter: list[str] | None = None) -> dict | None:
    """Fetch full text from Europe PMC fullTextXML."""
    try:
        resp = _request_raw(
            f"{EPMC_URL}/{pmcid}/fullTextXML",
            timeout=20,
        )
        if resp.status_code == 404:
            return None

        root = ET.fromstring(resp.text)
        sections: list[dict] = []
        for sec in root.findall(".//body//sec"):
            title = sec.findtext("title", "")
            paras = sec.findall(".//p")
            content = "\n\n".join("".join(p.itertext()).strip() for p in paras if p.text or len(p))
            if sections_filter and not any(s.lower() in title.lower() for s in sections_filter):
                continue
            if content:
                sections.append({"title": title, "content": content})

        return {
            "source": "europepmc",
            "pmcid": pmcid,
            "sections": sections[:20],
            "section_count": min(len(sections), 20),
        }
    except Exception:
        return None


def _fetch_unpaywall(doi: str) -> dict | None:
    """Find open-access full text via Unpaywall."""
    if not UNPAYWALL_EMAIL:
        return None
    try:
        resp = _request_raw(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": UNPAYWALL_EMAIL},
            timeout=15,
        )
        data = resp.json()
        best = data.get("best_oa_location") or {}
        oa_url = best.get("url_for_pdf") or best.get("url_for_landing_page") or ""

        if not oa_url:
            return None

        return {
            "source": "unpaywall",
            "doi": doi,
            "oa_url": oa_url,
            "oa_status": data.get("oa_status", ""),
            "is_pdf": bool(best.get("url_for_pdf")),
            "note": "Full text available via external link. Use the oa_url to access.",
        }
    except Exception:
        return None


# ═══════════════════════════════════════════════
# TOOLS
# ═══════════════════════════════════════════════

@mcp.tool
def pubmed_search(
    query: str,
    max_results: int = 20,
    min_year: int | None = None,
    max_year: int | None = None,
    sort: str = "relevance",
    only_free_fulltext: bool = False,
    only_with_abstract: bool = False,
) -> dict[str, Any]:
    """
    Search PubMed for biomedical articles.

    Args:
        query: Search query using PubMed syntax (e.g., 'CRISPR gene editing',
               'smith j[Author] AND cancer', 'diabetes AND clinical trial[ptyp]').
        max_results: Maximum results to return (1-100).
        min_year: Filter by minimum publication year.
        max_year: Filter by maximum publication year.
        sort: 'relevance', 'date', or 'pub_date'.
        only_free_fulltext: Only free full-text articles.
        only_with_abstract: Only articles with abstracts.
    """
    full_query = query.strip()
    if min_year and max_year:
        full_query += f" AND ({min_year}:{max_year}[dp])"
    elif min_year:
        full_query += f" AND ({min_year}:3000[dp])"
    elif max_year:
        full_query += f" AND (1800:{max_year}[dp])"
    if only_free_fulltext:
        full_query += " AND free full text[sb]"
    if only_with_abstract:
        full_query += " AND hasabstract"

    sort_map = {"relevance": "relevance", "date": "pub+date", "pub_date": "pub+date"}

    resp = _request("esearch.fcgi", {
        "db": "pubmed", "term": full_query,
        "retmax": min(max_results, 100), "retmode": "json",
        "sort": sort_map.get(sort, "relevance"),
    })
    data = resp.json()
    r = data.get("esearchresult", {})
    return {
        "query_applied": full_query,
        "original_query": query,
        "total_count": int(r.get("count", 0)),
        "returned_count": int(r.get("retmax", 0)),
        "pmids": r.get("idlist", []),
    }


@mcp.tool
def pubmed_fetch(
    pmids: list[str],
    include_mesh: bool = False,
) -> dict[str, Any]:
    """
    Fetch detailed article metadata by PubMed IDs.

    Args:
        pmids: List of PubMed IDs (up to 50 per request).
        include_mesh: Include MeSH terms in results.
    """
    if not pmids:
        return {"error": "No PMIDs provided", "articles": [], "count": 0}

    resp = _request("efetch.fcgi", {
        "db": "pubmed", "id": ",".join(str(p) for p in pmids[:50]),
        "retmode": "xml", "rettype": "abstract",
    })
    result = _parse_article_xml(resp.text)
    if not include_mesh:
        for a in result["articles"]:
            a.pop("mesh_terms", None)
    return result


@mcp.tool
def pubmed_summary(pmids: list[str]) -> dict[str, Any]:
    """
    Get concise summaries of articles — faster than pubmed_fetch.
    Up to 50 PMIDs per request.
    """
    if not pmids:
        return {"error": "No PMIDs provided", "summaries": [], "count": 0}

    resp = _request("esummary.fcgi", {
        "db": "pubmed", "id": ",".join(str(p) for p in pmids[:50]), "retmode": "json",
    })
    data = resp.json()
    result = data.get("result", {})
    uids = result.get("uids", [])

    summaries = []
    for uid in uids:
        s = result.get(uid, {})
        authors_list = [a.get("name", "") for a in s.get("authors", [])]
        summaries.append({
            "pmid": uid,
            "title": s.get("title", ""),
            "first_author": authors_list[0] if authors_list else "",
            "last_author": authors_list[-1] if len(authors_list) > 1 else "",
            "journal": s.get("source", ""),
            "pub_year": (s.get("pubdate", "") or "")[:4],
            "doi": (s.get("elocationid", "") or "").replace("doi: ", ""),
            "pub_types": s.get("pubtype", []),
        })
    return {"summaries": summaries, "count": len(summaries)}


@mcp.tool
def pubmed_find_related(
    pmid: str,
    relationship: str = "similar",
    max_results: int = 20,
) -> dict[str, Any]:
    """
    Find articles related to a source PubMed article.

    Args:
        pmid: Source PubMed ID.
        relationship: 'similar', 'cited_by', or 'references'.
        max_results: Maximum results to return.
    """
    link_map = {
        "similar": "pubmed_pubmed",
        "cited_by": "pubmed_pubmed_citedin",
        "references": "pubmed_pubmed_refs",
    }
    resp = _request("elink.fcgi", {
        "dbfrom": "pubmed", "db": "pubmed",
        "linkname": link_map.get(relationship, "pubmed_pubmed"),
        "id": pmid, "retmode": "json",
    })
    data = resp.json()
    linksets = data.get("linksets", [])
    if not linksets:
        return {"source_pmid": pmid, "relationship": relationship, "pmids": [], "count": 0}

    related_ids = linksets[0].get("linksetdbs", [{}])[0].get("links", [])
    total = len(related_ids)
    related_ids = related_ids[:max_results]

    summaries = pubmed_summary([str(pid) for pid in related_ids]) if related_ids else {"summaries": [], "count": 0}

    return {
        "source_pmid": pmid, "relationship": relationship,
        "total_related": total, "pmids": related_ids,
        "articles": summaries["summaries"], "count": len(related_ids),
    }


@mcp.tool
def pubmed_citation_search(
    journal: str | None = None,
    year: str | None = None,
    volume: str | None = None,
    page: str | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    """
    Resolve partial citation to PMID using NCBI's citation matcher.
    At least one field required. More fields = better match.
    """
    fields = {"journal": journal or "", "year": year or "", "volume": volume or "",
              "page": page or "", "author": author or ""}
    if not any(fields.values()):
        return {"error": "At least one field required", "matched": False}

    citation = f"{fields['journal']}|{fields['year']}|{fields['volume']}|{fields['page']}|{fields['author']}||"
    resp = _request("ecitmatch.cgi", {"db": "pubmed", "retmode": "xml", "bdata": citation})
    text = resp.text.strip()
    if not text:
        return {"matched": False, "reason": "No match found"}

    try:
        parts = text.split("|")
        digits = [f.strip() for f in parts[5:] if f.strip().isdigit()]
        if digits:
            pmid_val = digits[-1]
            fetched = pubmed_fetch([pmid_val])
            article = fetched["articles"][0] if fetched.get("articles") else {}
            return {"matched": True, "pmid": pmid_val, "article": article}
        if "AMBIGUOUS" in text:
            candidates = re.findall(r"\b\d{7,8}\b", text)
            return {"matched": False,
                    "reason": "Ambiguous citation — add volume/page for a unique match",
                    "candidate_pmids": candidates}
        if "NOT_FOUND" in text:
            return {"matched": False, "reason": "No match found"}
        status = parts[7].strip() if len(parts) > 7 else ""
        if status:
            return {"matched": False, "reason": status}
    except (IndexError, ValueError):
        pass
    return {"matched": False, "reason": f"Could not parse: {text}"}


# ── EUROPE PMC SEARCH ──


@mcp.tool
def pubmed_europepmc_search(
    query: str,
    max_results: int = 20,
    sources: str = "MED,PMC,PPR",
    min_year: int | None = None,
    sort: str = "relevance",
) -> dict[str, Any]:
    """
    Search Europe PMC — broader than PubMed: includes preprints, patents, Agricola.

    Args:
        query: Search query (same syntax as PubMed).
        max_results: Maximum results (1-100).
        sources: Comma-separated source types: MED (PubMed), PMC (OA subset),
                 PPR (preprints), PAT (patents), AGR (Agricola).
        min_year: Minimum publication year.
        sort: 'relevance', 'date', 'cited'.
    """
    params: dict[str, Any] = {
        "query": query,
        "resultType": "core",
        "pageSize": min(max_results, 100),
        "format": "json",
        "source": sources,
    }
    if min_year:
        params["query"] += f" AND FIRST_PDATE:[{min_year}-01-01 TO 3000-12-31]"
    if sort == "cited":
        params["sort"] = "CITED desc"
    elif sort == "date":
        params["sort"] = "FIRST_PDATE desc"

    if EUROPEPMC_EMAIL:
        params["email"] = EUROPEPMC_EMAIL

    _rate_limit()
    resp = _request_raw(f"{EPMC_URL}/search", params, timeout=30)
    data = resp.json()
    results = data.get("resultList", {}).get("result", [])

    articles = []
    for r in results:
        articles.append({
            "id": r.get("id", ""),
            "source": r.get("source", ""),
            "title": r.get("title", ""),
            "authors": r.get("authorString", ""),
            "journal": r.get("journalTitle", ""),
            "pub_year": r.get("pubYear", ""),
            "doi": r.get("doi", ""),
            "pmid": r.get("pmid", ""),
            "pmcid": r.get("pmcid", ""),
            "cited_count": r.get("citedByCount", 0),
            "has_fulltext": r.get("hasPDF", "N") == "Y",
        })

    return {
        "query": query,
        "total_count": data.get("hitCount", 0),
        "returned_count": len(articles),
        "articles": articles,
    }


# ── SPELL CHECK ──


@mcp.tool
def pubmed_spell_check(query: str) -> dict[str, Any]:
    """
    Spell-check a biomedical query using NCBI's ESpell service.
    Useful for query refinement before searching.
    """
    resp = _request("espell.fcgi", {"db": "pubmed", "term": query})
    root = ET.fromstring(resp.text)

    original = root.findtext("Query", "") or query
    corrected = root.findtext("CorrectedQuery", "")
    replacements = [
        e.text.strip() for e in root.findall(".//Replaced") if e.text and e.text.strip()
    ]

    return {
        "original": original,
        "corrected": corrected,
        "corrected_available": bool(corrected and corrected != original),
        "replacements": replacements,
    }


# ── MeSH LOOKUP ──


@mcp.tool
def pubmed_lookup_mesh(term: str, max_results: int = 10) -> dict[str, Any]:
    """
    Search and explore MeSH (Medical Subject Headings) vocabulary.

    Args:
        term: MeSH term or partial name to search.
        max_results: Maximum results.
    """
    # Search for matching MeSH terms
    resp = _request("esearch.fcgi", {
        "db": "mesh", "term": f"{term}[mh]",
        "retmax": min(max_results, 50), "retmode": "json",
    })
    data = resp.json()
    mesh_ids = data.get("esearchresult", {}).get("idlist", [])

    if not mesh_ids:
        return {"term": term, "results": [], "count": 0}

    # Fetch MeSH records (returns plain text, not XML)
    resp2 = _request("efetch.fcgi", {
        "db": "mesh", "id": ",".join(mesh_ids),
        "rettype": "abstract",
    })
    text = resp2.text

    # Parse plain text MeSH records
    results = []
    records = text.strip().split("\n\n")
    for rec_text in records:
        if not rec_text.strip():
            continue
        lines = rec_text.strip().split("\n")
        # First line: "UID: Name"
        first = lines[0].split(": ", 1)
        ui = first[0].strip() if len(first) > 0 else ""
        name = first[1].strip() if len(first) > 1 else ""

        scope = ""
        entry_terms = []
        tree_numbers = []
        current_section = "description"
        description_lines = []

        for line in lines[1:]:
            line = line.strip()
            if line == "Subheadings:":
                current_section = "subheadings"
                continue
            elif line == "Entry Terms:":
                current_section = "entry_terms"
                continue
            elif line == "Tree Numbers:":
                current_section = "tree_numbers"
                continue
            elif line == "See Also:" or line == "Consider Also:":
                current_section = "skip"
                continue

            if current_section == "description":
                description_lines.append(line)
            elif current_section == "entry_terms":
                entry_terms.append(line)
            elif current_section == "tree_numbers":
                tree_numbers.append(line)

        scope = " ".join(description_lines)

        results.append({
            "ui": ui,
            "name": name,
            "scope_note": scope[:500],
            "tree_numbers": tree_numbers[:10],
            "entry_terms": entry_terms[:10],
        })

    return {"term": term, "results": results, "count": len(results)}


# ── ID CONVERSION ──


@mcp.tool
def pubmed_convert_ids(ids: list[str], input_type: str = "pmid") -> dict[str, Any]:
    """
    Convert between DOI, PMID, and PMCID.
    Only resolves articles indexed in PubMed Central.

    Args:
        ids: List of IDs to convert (up to 50).
        input_type: 'pmid', 'pmcid', or 'doi'.
    """
    if input_type not in ("pmid", "pmcid", "doi"):
        return {
            "error": "input_type must be 'pmid', 'pmcid', or 'doi'",
            "input_type": input_type,
            "results": [],
            "not_found": [],
            "converted_count": 0,
        }

    all_ids = ids[:50]
    results: list[dict] = []
    not_found: list[str] = []

    for i in range(0, len(all_ids), 10):
        batch = [str(x) for x in all_ids[i:i + 10]]
        params = {
            "ids": ",".join(batch),
            "idtype": input_type,
            "format": "json",
            "versions": "no",
        }
        resp = _request_raw(f"{PMC_CONVERTER_URL}", params=params, timeout=15)
        data = resp.json()
        records = data.get("records", [])

        for j, rec in enumerate(records):
            if rec.get("status") == "error":
                not_found.append(batch[j])
            else:
                results.append({
                    "pmid": rec.get("pmid", ""),
                    "pmcid": rec.get("pmcid", ""),
                    "doi": rec.get("doi", ""),
                    "live": rec.get("live", False),
                })

    return {
        "input_type": input_type,
        "results": results,
        "not_found": not_found,
        "converted_count": len(results),
    }


# ── FULL TEXT ──


@mcp.tool
def pubmed_fetch_fulltext(
    pmids: list[str],
    sections_filter: list[str] | None = None,
    max_sections: int = 20,
) -> dict[str, Any]:
    """
    Fetch full-text articles via PMC → Europe PMC → Unpaywall chain.

    Args:
        pmids: List of PubMed IDs (up to 10).
        sections_filter: Only return sections matching these names
                        (case-insensitive, e.g. ['methods', 'results']).
        max_sections: Maximum sections per article (default 20).

    Returns:
        Full text available as structured sections, or OA link for Unpaywall.
    """
    if not pmids:
        return {"error": "No PMIDs provided", "articles": [], "count": 0}

    results = []

    for pmid in [str(p) for p in pmids[:10]]:
        entry: dict[str, Any] = {"pmid": pmid, "fulltext": None, "source": None}

        # Step 1: Resolve PMID → PMCID
        pmcid = _resolve_pmcid(pmid)

        if pmcid:
            # Step 2: Try NCBI PMC
            fulltext = _fetch_pmc_text(pmcid, sections=sections_filter, max_sections=max_sections)
            if fulltext:
                entry["fulltext"] = fulltext
                entry["source"] = "pmc"
                entry["pmcid"] = pmcid
                results.append(entry)
                continue

            # Step 3: Try Europe PMC fullTextXML
            fulltext = _fetch_epmc_text(pmcid, sections_filter)
            if fulltext:
                entry["fulltext"] = fulltext
                entry["source"] = "europepmc"
                entry["pmcid"] = pmcid
                results.append(entry)
                continue

        # Step 4: Get DOI and try Unpaywall
        fetched = pubmed_fetch([pmid])
        doi = fetched["articles"][0].get("doi", "") if fetched.get("articles") else ""

        if doi:
            uwall = _fetch_unpaywall(doi)
            if uwall:
                entry["fulltext"] = uwall
                entry["source"] = "unpaywall"
                entry["doi"] = doi
                entry["pmcid"] = pmcid
                results.append(entry)
                continue

        # Not found
        reason = "No full text available via PMC, Europe PMC, or Unpaywall"
        if not UNPAYWALL_EMAIL:
            reason += " (set UNPAYWALL_EMAIL for the Unpaywall fallback)"
        entry["reason"] = reason
        entry["pmcid"] = pmcid
        results.append(entry)

    found = sum(1 for r in results if r["source"])
    return {
        "articles": results,
        "count": len(results),
        "found_fulltext": found,
    }


# ── CITATION FORMATTING ──


@mcp.tool
def pubmed_format_citations(
    pmids: list[str],
    style: str = "apa",
) -> dict[str, Any]:
    """
    Generate formatted citations for articles.

    Args:
        pmids: List of PubMed IDs (up to 20).
        style: Citation style — 'apa' (APA 7th), 'mla' (MLA 9th),
               'bibtex', 'ris', or 'vancouver' (ICMJE/NLM).
    """
    if not pmids:
        return {"error": "No PMIDs provided", "citations": [], "count": 0}

    fetched = pubmed_fetch(pmids[:20], include_mesh=False)
    articles = fetched.get("articles", [])

    formatters = {
        "apa": _format_apa,
        "mla": _format_mla,
        "bibtex": _format_bibtex,
        "ris": _format_ris,
        "vancouver": _format_vancouver,
    }
    fmt = formatters.get(style, _format_apa)

    citations = []
    for a in articles:
        citations.append({"pmid": a.get("pmid", ""), "citation": fmt(a), "style": style})

    return {"citations": citations, "count": len(citations), "style": style}


def _ensure_period(t: str) -> str:
    """Return text with exactly one trailing period (keeps ? and ! intact)."""
    t = (t or "").strip()
    if t and t[-1] not in ".!?":
        t += "."
    return t


def _format_apa(a: dict) -> str:
    """APA 7th edition."""
    authors = a.get("authors", [])
    year = a.get("pub_year", "")
    title = a.get("title", "")
    journal = a.get("journal", "")
    volume = a.get("volume", "")
    issue = a.get("issue", "")
    pages = a.get("pages", "")
    doi = a.get("doi", "")

    # Author list
    if not authors:
        author_str = ""
    elif len(authors) == 1:
        author_str = _author_str(authors[0])
    elif len(authors) == 2:
        author_str = f"{_author_str(authors[0])} & {_author_str(authors[1])}"
    elif len(authors) <= 20:
        author_str = ", ".join(_author_str(a) for a in authors[:-1]) + f", & {_author_str(authors[-1])}"
    else:
        author_str = ", ".join(_author_str(a) for a in authors[:19]) + f", ... {_author_str(authors[-1])}"

    parts = []
    if author_str:
        parts.append(author_str)
    parts.append(f"({year})" if year else "(n.d.)")
    if title:
        parts.append(_ensure_period(title))
    journal_part = f"*{journal}*" if journal else ""
    if volume:
        journal_part += f", *{volume}*"
        if issue:
            journal_part += f"({issue})"
    if pages:
        journal_part += f", {pages}"
    if journal_part:
        parts.append(journal_part)
    if doi:
        parts.append(f"https://doi.org/{doi}")

    return " ".join(parts)


def _format_mla(a: dict) -> str:
    """MLA 9th edition."""
    authors = a.get("authors", [])
    title = a.get("title", "")
    journal = a.get("journal", "")
    volume = a.get("volume", "")
    issue = a.get("issue", "")
    year = a.get("pub_year", "")
    pages = a.get("pages", "")
    doi = a.get("doi", "")

    # First author: Last, First
    if not authors:
        author_str = ""
    elif len(authors) == 1:
        a0 = authors[0]
        author_str = f"{a0['last']}, {a0['fore']}" if a0['fore'] else a0['last']
    elif len(authors) == 2:
        a0, a1 = authors
        author_str = f"{a0['last']}, {a0['fore']}, and {a1['full']}" if a0.get('fore') else f"{a0['last']} and {a1['full']}"
    elif len(authors) >= 3:
        a0 = authors[0]
        author_str = f"{a0['last']}, {a0['fore']}, et al." if a0.get('fore') else f"{a0['last']}, et al."

    parts = []
    if author_str:
        parts.append(_ensure_period(author_str))
    parts.append(f'"{_ensure_period(title)}"')
    journal_str = f"*{journal}*" if journal else ""
    if volume:
        journal_str += f", vol. {volume}"
        if issue:
            journal_str += f", no. {issue}"
    if journal_str:
        if year:
            journal_str += ","
        parts.append(journal_str)
    if year:
        parts.append(f"{year},")
    if pages:
        parts.append(f"pp. {pages}.")
    if doi:
        parts.append(f"doi:{doi}.")

    return " ".join(parts)


def _format_bibtex(a: dict) -> str:
    """BibTeX format."""
    authors = a.get("authors", [])
    title = a.get("title", "")
    journal = a.get("journal", "")
    volume = a.get("volume", "")
    issue = a.get("issue", "")
    year = a.get("pub_year", "")
    pages = a.get("pages", "")
    doi = a.get("doi", "")
    pmid = a.get("pmid", "")

    # Key: FirstAuthorLastNameYear
    first_last = authors[0]["last"].lower() if authors else "unknown"
    key = f"{first_last}{year}" if year else first_last

    author_tex = " and ".join(
        f"{a['last']}, {a['fore']}" if a.get('fore') else a['last']
        for a in authors
    )

    lines = ["@article{" + key + ","]
    if authors:
        lines.append(f"  author = {{{author_tex}}},")
    lines.append(f"  title = {{{title}}},")
    if journal:
        lines.append(f"  journal = {{{journal}}},")
    if year:
        lines.append(f"  year = {{{year}}},")
    if volume:
        lines.append(f"  volume = {{{volume}}},")
    if issue:
        lines.append(f"  number = {{{issue}}},")
    if pages:
        lines.append(f"  pages = {{{pages}}},")
    if doi:
        lines.append(f"  doi = {{{doi}}},")
    lines.append(f"  pmid = {{{pmid}}}")
    lines.append("}")

    return "\n".join(lines)


def _format_ris(a: dict) -> str:
    """RIS format."""
    authors = a.get("authors", [])
    lines = ["TY  - JOUR"]
    for au in authors:
        lines.append(f"AU  - {_author_str(au)}")
    lines.append(f"TI  - {a.get('title', '')}")
    if a.get("journal"):
        lines.append(f"JO  - {a.get('journal', '')}")
        if a.get("journal_iso"):
            lines.append(f"JA  - {a.get('journal_iso', '')}")
    if a.get("pub_year"):
        lines.append(f"PY  - {a.get('pub_year', '')}")
    if a.get("volume"):
        lines.append(f"VL  - {a.get('volume', '')}")
    if a.get("issue"):
        lines.append(f"IS  - {a.get('issue', '')}")
    if a.get("pages"):
        lines.append(f"SP  - {a.get('pages', '')}")
    if a.get("doi"):
        lines.append(f"DO  - {a.get('doi', '')}")
    lines.append(f"AN  - {a.get('pmid', '')}")
    lines.append("ER  - ")
    return "\n".join(lines)


def _format_vancouver(a: dict) -> str:
    """Vancouver (ICMJE/NLM) style."""
    authors = a.get("authors", [])
    title = a.get("title", "")
    journal = a.get("journal_iso", "") or a.get("journal", "")
    volume = a.get("volume", "")
    issue = a.get("issue", "")
    year = a.get("pub_year", "")
    pages = a.get("pages", "")
    doi = a.get("doi", "")

    # Authors: Last FM, Last FM, Last FM.
    author_parts = []
    for au in authors:
        initials = au.get("initials", "")
        author_parts.append(f"{au['last']} {initials}")
    author_str = ", ".join(author_parts[:6])
    if len(authors) > 6:
        author_str += ", et al."

    parts = [_ensure_period(author_str)]
    parts.append(_ensure_period(title))
    parts.append(f"{journal}." if journal else "")
    if year:
        parts.append(f"{year}")
    if volume or issue:
        parts[-1] = f"{parts[-1]};" if parts[-1] else ""
        vol_iss = f"{volume}" if volume else ""
        if issue:
            vol_iss += f"({issue})"
        parts.append(vol_iss)
    if pages:
        parts.append(f":{pages}.")
    if doi:
        parts.append(f"doi: {doi}.")

    return " ".join(p for p in parts if p)


# ── CLINICAL TRIALS ──

CLINICAL_TRIALS_URL = "https://clinicaltrials.gov/api/v2"


@mcp.tool
def pubmed_clinical_trials(
    condition: str = "",
    intervention: str = "",
    status: str = "",
    max_results: int = 20,
) -> dict[str, Any]:
    """
    Search ClinicalTrials.gov for clinical studies.

    Args:
        condition: Medical condition/disease (e.g., 'diabetes', 'breast cancer').
        intervention: Treatment/drug being studied (e.g., 'metformin', 'immunotherapy').
        status: Study status — 'recruiting', 'active', 'completed', 'terminated'.
                Empty = all statuses.
        max_results: Maximum results (1-100).
    """
    query_parts = []
    if condition:
        query_parts.append(condition.strip())
    if intervention:
        query_parts.append(intervention.strip())
    if status:
        query_parts.append(f"AREA[OverallStatus]{status}")

    params: dict[str, Any] = {
        "format": "json",
        "pageSize": min(max_results, 100),
        "countTotal": "true",
    }
    if query_parts:
        params["query.term"] = " AND ".join(query_parts)

    resp = _request_raw(f"{CLINICAL_TRIALS_URL}/studies", params=params, timeout=20)
    data = resp.json()

    studies = []
    for s in data.get("studies", []):
        protocol = s.get("protocolSection", {})
        ident = protocol.get("identificationModule", {})
        status_mod = protocol.get("statusModule", {})
        design = protocol.get("designModule", {})
        desc = protocol.get("descriptionModule", {})

        conditions = protocol.get("conditionsModule", {}).get("conditions", [])
        interventions = protocol.get("armsInterventionsModule", {}).get("interventions", [])

        studies.append({
            "nct_id": ident.get("nctId", ""),
            "title": ident.get("briefTitle", ""),
            "status": status_mod.get("overallStatus", ""),
            "phase": design.get("phases", []),
            "conditions": conditions,
            "interventions": [
                f"{inv.get('type','')}: {inv.get('name','')}"
                for inv in interventions
            ],
            "description": (desc.get("briefSummary", "") or "")[:500],
            "start_date": status_mod.get("startDateStruct", {}).get("date", ""),
            "completion_date": status_mod.get("primaryCompletionDateStruct", {}).get("date", ""),
            "enrollment": design.get("enrollmentInfo", {}).get("count", 0),
            "locations": [loc.get("facility", "") for loc in protocol.get("contactsLocationsModule", {}).get("locations", [])[:5]],
            "url": f"https://clinicaltrials.gov/study/{ident.get('nctId','')}",
        })

    return {
        "query": {"condition": condition, "intervention": intervention, "status": status},
        "total_count": data.get("totalCount", 0),
        "returned_count": len(studies),
        "studies": studies,
    }


# ── SEMANTIC SCHOLAR ──

SEMANTIC_SCHOLAR_URL = "https://api.semanticscholar.org/graph/v1"


@mcp.tool
def pubmed_semantic_scholar(
    query: str = "",
    paper_id: str = "",
    max_results: int = 10,
    search_mode: str = "search",
) -> dict[str, Any]:
    """
    Search or analyze papers via Semantic Scholar API (citations, influence, references).

    Use search_mode='search' for keyword search, or 'paper' with paper_id
    (DOI, PMID prefixed with 'PMID:', or Semantic Scholar ID) for deep analysis.

    Args:
        query: Keyword search query (for search_mode='search').
        paper_id: Paper identifier for search_mode='paper' —
                  DOI (e.g., '10.1038/nature12373'),
                  PMID:12345, or Semantic Scholar CorpusId.
        max_results: Maximum results (1-100).
        search_mode: 'search' for keyword, 'paper' for single paper details.
    """
    fields = ("title,abstract,authors,year,citationCount,influentialCitationCount,"
              "publicationTypes,journal,externalIds,url,publicationDate,"
              "citationStyles,openAccessPdf")

    if search_mode == "paper" and paper_id:
        # Resolve paper ID
        _s2_rate_limit()
        try:
            resp = _request_raw(
                f"{SEMANTIC_SCHOLAR_URL}/paper/{paper_id}",
                params={"fields": fields},
                timeout=15,
                headers={"x-api-key": S2_API_KEY} if S2_API_KEY else None,
            )
            return {"paper": _parse_s2_paper(resp.json())}
        except Exception as e:
            return {
                "paper_id": paper_id,
                "error": str(e),
                "hint": "Semantic Scholar rate limits are strict without an API key. "
                        "Get a free key at https://www.semanticscholar.org/product/api#api-key-form "
                        "and set S2_API_KEY env var, or use pubmed_search instead.",
            }

    elif search_mode == "search" and query:
        # Semantic Scholar rate limit: ~1 req/sec
        _s2_rate_limit()
        try:
            resp = _request_raw(
                f"{SEMANTIC_SCHOLAR_URL}/paper/search",
                params={
                    "query": query,
                    "limit": min(max_results, 100),
                    "fields": fields,
                },
                timeout=20,
                headers={"x-api-key": S2_API_KEY} if S2_API_KEY else None,
            )
            data = resp.json()
            papers = [_parse_s2_paper(p) for p in data.get("data", [])]
            return {
                "query": query,
                "total": data.get("total", 0),
                "offset": data.get("offset", 0),
                "papers": papers,
            }
        except Exception as e:
            return {
                "query": query,
                "error": str(e),
                "hint": "Semantic Scholar rate limits are strict without an API key. "
                        "Get a free key at https://www.semanticscholar.org/product/api#api-key-form "
                        "and set S2_API_KEY env var, or use pubmed_search instead.",
                "papers": [],
            }

    return {"error": "Provide query (search mode) or paper_id (paper mode)"}


def _parse_s2_paper(p: dict) -> dict:
    authors = [a.get("name", "") for a in p.get("authors", [])]
    ext_ids = p.get("externalIds", {})
    oa = p.get("openAccessPdf", {}) or {}
    citation_style = (p.get("citationStyles", {}) or {}).get("bibtex", "")

    return {
        "paper_id": p.get("paperId", ""),
        "title": p.get("title", ""),
        "abstract": (p.get("abstract", "") or "")[:800],
        "authors": authors,
        "year": p.get("year", ""),
        "citation_count": p.get("citationCount", 0),
        "influential_citations": p.get("influentialCitationCount", 0),
        "journal": (p.get("journal", {}) or {}).get("name", ""),
        "doi": ext_ids.get("DOI", ""),
        "pmid": ext_ids.get("PMID", ""),
        "url": p.get("url", ""),
        "open_access_pdf": oa.get("url", ""),
        "publication_types": p.get("publicationTypes", []),
        "bibtex": citation_style,
    }


# ── OpenFDA ──

OPENFDA_URL = "https://api.fda.gov"

# FAERS drugcharacterization codes -> human labels
_CHARACTERIZATION = {"1": "suspect", "2": "concomitant", "3": "interacting"}

# FAERS seriousness: serious code + reason flags
_SERIOUS = {"1": "serious", "2": "not serious"}
_SERIOUSNESS_FLAGS = (
    ("death", "seriousnessdeath"),
    ("life-threatening", "seriousnesslifethreatening"),
    ("hospitalization", "seriousnesshospitalization"),
    ("disabling", "seriousnessdisabling"),
    ("congenital-anomaly", "seriousnesscongenitalanomali"),
    ("other", "seriousnessother"),
)


@mcp.tool
def openfda_adverse_events(
    drug_name: str,
    limit: int = 10,
) -> dict[str, Any]:
    """
    Query FDA Adverse Event Reporting System (FAERS) for drug side effects.

    Args:
        drug_name: Drug name (brand or generic, e.g., 'metformin', 'ozempic').
        limit: Max results (1-100).
    """
    drug_name = drug_name.replace('"', "").strip()
    search_term = f'patient.drug.medicinalproduct:"{drug_name}"'
    params = {
        "search": search_term,
        "limit": min(limit, 100),
    }
    try:
        resp = _request_raw(f"{OPENFDA_URL}/drug/event.json", params=params, timeout=20)
        data = resp.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
        data = {}

    results = []
    for r in data.get("results", []):
        patient = r.get("patient", {})
        reactions = patient.get("reaction", [])
        drugs = patient.get("drug", [])

        results.append({
            "serious": _SERIOUS.get(str(r.get("serious", "")), str(r.get("serious", ""))),
            "seriousness": [
                label
                for label, key in _SERIOUSNESS_FLAGS
                if str(r.get(key, "")) == "1"
            ],
            "reactions": [rx.get("reactionmeddrapt", "") for rx in reactions[:10]],
            "drugs": [
                f"{d.get('medicinalproduct','')} "
                f"({_CHARACTERIZATION.get(d.get('drugcharacterization',''), d.get('drugcharacterization',''))})"
                for d in drugs[:5]
            ],
            "patient_age": patient.get("patientonsetage", ""),
            "patient_sex": patient.get("patientsex", ""),
        })

    meta = data.get("meta", {}).get("results", {})
    return {
        "drug": drug_name,
        "total": meta.get("total", 0),
        "returned": len(results),
        "events": results,
    }


@mcp.tool
def openfda_drug_label(
    drug_name: str,
) -> dict[str, Any]:
    """
    Get FDA drug label information: indications, warnings, dosage, mechanism.

    Args:
        drug_name: Drug name (brand or generic, e.g., 'metformin', 'ozempic').
    """
    drug_name = drug_name.replace('"', "").strip()
    results: list = []
    for field in ("openfda.brand_name", "openfda.generic_name"):
        search = f'{field}:"{drug_name}"'
        url = f"{OPENFDA_URL}/drug/label.json?search={quote(search)}&limit=1"
        try:
            resp = _request_raw(url, timeout=20)
            results = resp.json().get("results", [])
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise
            results = []
        if results:
            break

    if not results:
        return {"drug": drug_name, "found": False}

    label = results[0]
    openfda_info = label.get("openfda", {})

    return {
        "drug": drug_name,
        "found": True,
        "brand_names": openfda_info.get("brand_name", []),
        "generic_names": openfda_info.get("generic_name", []),
        "manufacturer": openfda_info.get("manufacturer_name", []),
        "indications": (label.get("indications_and_usage", [""])[0] or "")[:500],
        "warnings": (label.get("warnings_and_cautions", [""])[0] or "")[:500],
        "mechanism": (label.get("mechanism_of_action", [""])[0] or "")[:500],
        "pharmacokinetics": (label.get("pharmacokinetics", [""])[0] or "")[:500],
    }


@mcp.tool
def openfda_drug_enforcement(
    drug_name: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    """
    Query FDA enforcement reports (recalls, warnings, market withdrawals).

    Args:
        drug_name: Optional drug name filter.
        limit: Max results (1-25).
    """
    drug_name = drug_name.replace('"', "").strip()
    params: dict[str, Any] = {"limit": min(limit, 25)}
    if drug_name:
        params["search"] = f'product_description:"{drug_name}"'

    try:
        resp = _request_raw(f"{OPENFDA_URL}/drug/enforcement.json", params=params, timeout=20)
        data = resp.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
        data = {}

    reports = []
    for r in data.get("results", []):
        reports.append({
            "report_date": r.get("report_date", ""),
            "classification": r.get("classification", ""),
            "product": r.get("product_description", ""),
            "reason": r.get("reason_for_recall", "")[:300],
            "recalling_firm": r.get("recalling_firm", ""),
            "distribution": r.get("distribution_pattern", ""),
        })

    return {
        "drug_filter": drug_name,
        "total": data.get("meta", {}).get("results", {}).get("total", 0),
        "returned": len(reports),
        "reports": reports,
    }


# ── DrugBank ──

DRUGBANK_API_KEY = os.getenv("DRUGBANK_API_KEY", "")


@mcp.tool
def drugbank_search(
    query: str,
    max_results: int = 10,
) -> dict[str, Any]:
    """
    Search DrugBank for drug information: targets, enzymes, interactions.

    Requires DRUGBANK_API_KEY env var (free key at https://go.drugbank.com).

    Args:
        query: Drug name (e.g., 'metformin', 'aspirin').
        max_results: Max results (1-50).
    """
    if not DRUGBANK_API_KEY:
        return {"error": "DRUGBANK_API_KEY not set", "hint": "Get a free key at https://go.drugbank.com"}

    try:
        resp = _request_raw(
            "https://api.drugbank.com/v1/drug_names",
            params={"q": query, "max_results": min(max_results, 50)},
            timeout=20,
            headers={"Authorization": f"Bearer {DRUGBANK_API_KEY}"},
        )
        return {"drugs": resp.json()}
    except Exception as e:
        return {"error": f"DrugBank request failed: {e}", "hint": "Check your DRUGBANK_API_KEY"}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
