"""sec_edgar_mcp.py -- HTTP MCP server exposing SEC EDGAR data.

EDGAR is the official source of US public-company filings (10-K, 10-Q, 8-K,
Form 4, 13F, S-1, etc.) and structured XBRL financial facts. Free, no API
key, just polite User-Agent header per SEC's rate-limit policy.

Tools:
  list_filings(ticker_or_cik, form?, since?, limit=20)
    Recent filings for a company, filter by form type.

  get_filing_metadata(cik_or_ticker, accession)
    Full metadata for one filing — document list, URLs, primary doc.

  get_financial_facts(ticker_or_cik, concept?)
    XBRL companyfacts. Without `concept`, returns the full structured
    fact set (huge — ~hundreds of tags × years). With `concept` (e.g.
    "Revenues", "NetIncomeLoss"), returns a time series of just that tag.

  get_filing_document(cik_or_ticker, accession, doc?)
    Fetch the primary filing document (or named secondary doc), strip
    HTML, return plain text.

  search_filings(query, ticker?, form?, since?, limit=10)
    EDGAR full-text search (https://efts.sec.gov/LATEST/search-index).

  health()
    Diagnostic — confirms UA is set and EDGAR is reachable.

SEC rate-limit policy (https://www.sec.gov/os/accessing-edgar-data):
  - Hard ceiling: 10 req/sec PER IP.
  - Soft ceiling: SEC asks to use a discoverable User-Agent that includes
    the requester's name + contact email. We send
    "alphalens (user001@token-gateway.com)".
  - Auto-block triggers when you exceed 10 rps or use a generic UA.

Run as HTTP MCP on 127.0.0.1:3037/mcp by default. Override via SEC_EDGAR_MCP_PORT.
Override the contact email via SEC_EDGAR_CONTACT env (default:
user001@token-gateway.com).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from html import unescape as html_unescape
from pathlib import Path
from typing import Any

import aiohttp
from mcp.server.fastmcp import FastMCP

LOG_DIR = (
    Path(os.environ.get("SEC_EDGAR_MCP_DIR") or (Path.home() / "sec-edgar-mcp")) / "logs"
)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "sec_edgar_mcp.log", encoding="utf-8")],
)
log = logging.getLogger("sec-edgar")

# ── Endpoints ──────────────────────────────────────────────────────────────
SEC_DATA = "https://data.sec.gov"
SEC_WWW = "https://www.sec.gov"
SEC_EFTS = "https://efts.sec.gov/LATEST"
HTTP_TIMEOUT_S = 30

# SEC's policy requires a UA that identifies the requester + contact.
# Don't change without updating sec.gov compliance reasoning.
SEC_CONTACT = os.environ.get("SEC_EDGAR_CONTACT", "user001@token-gateway.com")
SEC_UA = f"alphalens ({SEC_CONTACT})"

mcp = FastMCP(
    "sec-edgar",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("SEC_EDGAR_MCP_PORT", "3037")),
)

# ── Caches ─────────────────────────────────────────────────────────────────
_TICKER_CACHE: dict[str, str] = {}    # symbol → 10-digit zero-padded CIK
_CACHE: dict[str, tuple[float, Any]] = {}
TTL_SUBMISSIONS = 30 * 60             # filings refresh ~quarterly + 8-Ks any day
TTL_FACTS = 12 * 60 * 60              # facts only change on filing → 12h ample
TTL_TICKER_MAP = 24 * 60 * 60         # SEC's ticker→CIK file updates daily


def _cache_get(key: str, ttl: float) -> Any | None:
    hit = _CACHE.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > ttl:
        return None
    return val


def _cache_put(key: str, val: Any) -> None:
    _CACHE[key] = (time.time(), val)


# ── HTTP helper ────────────────────────────────────────────────────────────
async def _get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET an EDGAR JSON endpoint. SEC requires UA, polite throttle (we never
    exceed 10 rps with single MCP client). On 429 / 503 backoff once.
    """
    headers = {"User-Agent": SEC_UA, "Accept": "application/json"}
    async with aiohttp.ClientSession() as session:
        for attempt in range(2):
            try:
                async with session.get(
                    url, params=params, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S),
                ) as r:
                    if r.status == 429 or r.status == 503:
                        if attempt == 0:
                            await asyncio.sleep(2.0)
                            continue
                    r.raise_for_status()
                    # SEC's Archives static server serves .json files (e.g. a
                    # filing's index.json) with Content-Type: text/html, which
                    # aiohttp's .json() rejects with ContentTypeError even though
                    # the body IS json. content_type=None skips the mimetype
                    # check and parses the body regardless. (data.sec.gov
                    # endpoints already send application/json, so this is safe.)
                    return await r.json(content_type=None)
            except aiohttp.ClientError as e:
                if attempt == 1:
                    raise
                await asyncio.sleep(1.0)
        return {}


async def _get_text(url: str) -> str:
    """GET an EDGAR HTML / text endpoint. Used for filing-document bodies."""
    headers = {"User-Agent": SEC_UA, "Accept": "text/html, application/xml, text/plain"}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S),
        ) as r:
            r.raise_for_status()
            return await r.text()


# ── Ticker → CIK lookup ────────────────────────────────────────────────────
async def _ensure_ticker_map() -> dict[str, str]:
    """Load SEC's ticker → CIK mapping once per day. Returns dict keyed by
    UPPERCASE ticker → 10-digit zero-padded CIK string.
    """
    if _TICKER_CACHE:
        return _TICKER_CACHE
    cached = _cache_get("ticker_map", TTL_TICKER_MAP)
    if cached:
        _TICKER_CACHE.update(cached)
        return _TICKER_CACHE

    # SEC's canonical ticker file:
    #   {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    payload = await _get_json(f"{SEC_WWW}/files/company_tickers.json")
    out: dict[str, str] = {}
    for _, row in (payload or {}).items():
        sym = str(row.get("ticker") or "").upper().strip()
        cik = int(row.get("cik_str") or 0)
        if sym and cik:
            out[sym] = f"{cik:010d}"
    if out:
        _TICKER_CACHE.update(out)
        _cache_put("ticker_map", out)
    log.info("loaded %d ticker→CIK rows", len(out))
    return _TICKER_CACHE


async def _resolve_cik(ticker_or_cik: str) -> str | None:
    """Accept 'AAPL' or '320193' or '0000320193' → return 10-digit zero-padded
    CIK string. None if not found.
    """
    s = (ticker_or_cik or "").strip().upper()
    if not s:
        return None
    # All digits → treat as CIK (zero-pad if short).
    if s.isdigit():
        return s.zfill(10)
    mp = await _ensure_ticker_map()
    return mp.get(s)


# iXBRL "R-files" — R1.htm, R22.htm, … — are per-statement financial-report
# fragments EDGAR generates from the inline XBRL. They're .htm but almost pure
# markup (a few hundred chars of text after stripping), so the naïve
# "first non-exhibit .htm" picker used to land on R1.htm and return ~770 chars
# instead of the real 10-K body. Exclude them explicitly.
_IXBRL_R_RE = re.compile(r"(^|/)r\d+\.htm$", re.IGNORECASE)


def _pick_primary_doc(documents: list[dict[str, Any]]) -> str | None:
    """Choose the primary filing document (the actual 10-K / 10-Q / 8-K body)
    from a filing's document list.

    EDGAR's index.json `type` field is unreliable on the static Archives host
    (it frequently carries the index *icon* name like "text.gif" rather than a
    form type), so we cannot key off document type. Instead we pick the
    LARGEST .htm document that is not an index page, not an exhibit, and not an
    iXBRL R-fragment. The real filing body is reliably the biggest such file
    (e.g. NVDA's nvda-YYYYMMDD.htm at ~2 MB dwarfs every R-file).
    """
    best_name: str | None = None
    best_size = -1
    fallback_name: str | None = None  # any .htm, used if every doc looks excluded
    fallback_size = -1
    for d in documents:
        name = (d.get("name") or "")
        n = name.lower()
        if not n.endswith(".htm") and not n.endswith(".html"):
            continue
        # Prefer exact byte size; fall back to rounded size_kb if absent.
        size = int(d.get("size_bytes") or round(float(d.get("size_kb") or 0) * 1024))
        if size > fallback_size:
            fallback_size, fallback_name = size, name
        if "index" in n or n.startswith("ex") or _IXBRL_R_RE.search(n):
            continue
        if size > best_size:
            best_size, best_name = size, name
    return best_name or fallback_name


# ── Tools ──────────────────────────────────────────────────────────────────
@mcp.tool()
async def health() -> dict[str, Any]:
    """Confirm EDGAR is reachable + UA accepted. Returns SEC contact + ticker
    map size if cached.
    """
    out: dict[str, Any] = {"success": False, "ua": SEC_UA, "contact": SEC_CONTACT}
    try:
        # Cheap probe — fetch ticker map (cached after first call).
        mp = await _ensure_ticker_map()
        out["success"] = bool(mp)
        out["ticker_count"] = len(mp)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"probe failed: {e!r}"
    return out


@mcp.tool()
async def list_filings(
    ticker_or_cik: str,
    form: str | None = None,
    since: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Recent SEC filings for a company.

    Source: /submissions/CIK{padded}.json — SEC's per-company submissions
    document. Includes the last ~1000 filings + reference to older "files"
    for paging beyond that.

    Args:
      ticker_or_cik: 'NVDA' / '320193' / '0000320193'
      form: optional form filter — '10-K', '10-Q', '8-K', '4' (insider),
        '13F-HR' (institutional holdings), 'S-1', 'DEF 14A' (proxy), etc.
      since: optional ISO date 'YYYY-MM-DD' lower bound on filing date.
      limit: max filings to return (default 20).

    Returns:
      {
        success, cik, ticker, name, sic_description,
        filings: [
          {accession, form, filing_date, report_date, primary_doc,
           filing_url, doc_url, items, size_kb, is_xbrl, is_inline_xbrl,
           xbrl_report_url},
          ...
        ],
      }
    """
    out: dict[str, Any] = {"success": False, "ticker_or_cik": ticker_or_cik}

    cik = await _resolve_cik(ticker_or_cik)
    if not cik:
        out["error"] = f"unknown ticker/CIK: {ticker_or_cik!r}"
        return out
    out["cik"] = cik

    cache_key = f"sub:{cik}"
    payload = _cache_get(cache_key, TTL_SUBMISSIONS)
    if payload is None:
        try:
            payload = await _get_json(f"{SEC_DATA}/submissions/CIK{cik}.json")
        except Exception as e:  # noqa: BLE001
            out["error"] = f"submissions fetch failed: {e!r}"
            return out
        _cache_put(cache_key, payload)

    out["name"] = payload.get("name")
    out["ticker"] = (payload.get("tickers") or [None])[0]
    out["sic_description"] = payload.get("sicDescription")

    recent = (payload.get("filings") or {}).get("recent") or {}
    accessions = recent.get("accessionNumber") or []
    n = len(accessions)
    rows: list[dict[str, Any]] = []
    # Column-store layout — every list is the same length aligned by index.
    for i in range(n):
        f = (recent.get("form") or [""] * n)[i]
        if form and f.upper() != form.upper():
            continue
        fd = (recent.get("filingDate") or [""] * n)[i]
        if since and fd < since:
            continue
        acc = accessions[i]
        acc_clean = acc.replace("-", "")
        primary = (recent.get("primaryDocument") or [""] * n)[i]
        rows.append({
            "accession": acc,
            "form": f,
            "filing_date": fd,
            "report_date": (recent.get("reportDate") or [""] * n)[i],
            "items": (recent.get("items") or [""] * n)[i],
            "primary_doc": primary,
            "filing_url": (
                f"{SEC_WWW}/cgi-bin/browse-edgar?"
                f"action=getcompany&CIK={cik}&type={f}"
            ),
            "doc_url": (
                f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{acc_clean}/{primary}"
                if primary else None
            ),
            "size_kb": round(((recent.get("size") or [0] * n)[i]) / 1024, 1),
            "is_xbrl": bool((recent.get("isXBRL") or [0] * n)[i]),
            "is_inline_xbrl": bool((recent.get("isInlineXBRL") or [0] * n)[i]),
            "xbrl_report_url": (
                f"{SEC_WWW}/cgi-bin/viewer?action=view&cik={int(cik)}&accession_number={acc}"
                if (recent.get("isXBRL") or [0] * n)[i] else None
            ),
        })
        if len(rows) >= max(1, int(limit)):
            break

    out.update({"success": True, "filings": rows})
    return out


@mcp.tool()
async def get_filing_metadata(
    ticker_or_cik: str,
    accession: str,
) -> dict[str, Any]:
    """Detailed metadata for one filing — full document list with URLs.

    Args:
      ticker_or_cik: 'NVDA' or CIK
      accession: '0001045810-25-000123' (with or without dashes)

    Returns:
      {success, cik, accession, primary_doc, documents: [{name, type, size, url}, ...]}
    """
    out: dict[str, Any] = {"success": False, "accession": accession}

    cik = await _resolve_cik(ticker_or_cik)
    if not cik:
        out["error"] = f"unknown ticker/CIK: {ticker_or_cik!r}"
        return out

    acc_clean = accession.replace("-", "")
    # EDGAR filing index JSON lives at:
    #   /Archives/edgar/data/{cik}/{accession-clean}/index.json
    idx_url = f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{acc_clean}/index.json"
    try:
        idx = await _get_json(idx_url)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"filing index fetch failed: {e!r}"
        return out

    items = ((idx.get("directory") or {}).get("item") or [])
    documents = []
    base = f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{acc_clean}"
    for it in items:
        name = it.get("name") or ""
        size_bytes = int(it.get("size") or 0)
        documents.append({
            "name": name,
            "type": it.get("type"),
            "size_bytes": size_bytes,
            "size_kb": round(size_bytes / 1024, 1),
            "url": f"{base}/{name}" if name else None,
        })

    # Pick the primary doc — largest non-exhibit, non-iXBRL-fragment .htm.
    # (See _pick_primary_doc for why size, not the unreliable `type`, drives this.)
    primary = _pick_primary_doc(documents)

    out.update({
        "success": True,
        "cik": cik,
        "accession": accession,
        "primary_doc": primary,
        "documents": documents,
    })
    return out


@mcp.tool()
async def get_financial_facts(
    ticker_or_cik: str,
    concept: str | None = None,
    taxonomy: str = "us-gaap",
) -> dict[str, Any]:
    """XBRL-tagged financial facts from a company's filings.

    With `concept` (e.g. 'Revenues', 'NetIncomeLoss', 'Assets'), returns ONE
    time series. Without `concept`, returns the LIST of concepts available
    for the company (avoiding a multi-MB payload). Set `taxonomy='dei'` for
    document-related tags (EntityRegistrantName, etc.).

    Args:
      ticker_or_cik: 'NVDA' or CIK
      concept: e.g. 'Revenues', 'NetIncomeLoss', 'EarningsPerShareBasic',
        'OperatingIncomeLoss', 'CashAndCashEquivalentsAtCarryingValue', etc.
        Browse all available via the concept-listing mode (concept=None).
      taxonomy: 'us-gaap' (default) or 'dei' or 'ifrs-full' for non-US.

    Returns when concept is provided:
      {
        success, cik, concept, label, description,
        units: { "USD": [...], "shares": [...], ... },
        per_unit_count: { "USD": 32, "shares": 5 },
      }

    Returns when concept is None (concept listing):
      {success, cik, taxonomy, concepts: ['Revenues', 'NetIncomeLoss', ...]}
    """
    out: dict[str, Any] = {"success": False, "ticker_or_cik": ticker_or_cik}

    cik = await _resolve_cik(ticker_or_cik)
    if not cik:
        out["error"] = f"unknown ticker/CIK: {ticker_or_cik!r}"
        return out
    out["cik"] = cik

    if concept:
        url = f"{SEC_DATA}/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{concept}.json"
        cache_key = f"concept:{cik}:{taxonomy}:{concept}"
        payload = _cache_get(cache_key, TTL_FACTS)
        if payload is None:
            try:
                payload = await _get_json(url)
            except Exception as e:  # noqa: BLE001
                out["error"] = f"concept fetch failed: {e!r}"
                return out
            _cache_put(cache_key, payload)
        units = payload.get("units") or {}
        out.update({
            "success": True,
            "concept": concept,
            "taxonomy": taxonomy,
            "label": payload.get("label"),
            "description": payload.get("description"),
            "units": units,
            "per_unit_count": {u: len(v) for u, v in units.items()},
        })
        return out

    # No concept → concept LISTING mode. Cached separately because the full
    # companyfacts payload is huge (~MB for a megacap) and we just need
    # the keys.
    cache_key = f"facts_keys:{cik}"
    keys = _cache_get(cache_key, TTL_FACTS)
    if keys is None:
        try:
            payload = await _get_json(f"{SEC_DATA}/api/xbrl/companyfacts/CIK{cik}.json")
        except Exception as e:  # noqa: BLE001
            out["error"] = f"companyfacts fetch failed: {e!r}"
            return out
        facts = (payload.get("facts") or {})
        keys = {tax: sorted(list((facts.get(tax) or {}).keys())) for tax in facts.keys()}
        _cache_put(cache_key, keys)
    out.update({
        "success": True,
        "taxonomy": taxonomy,
        "available_taxonomies": list(keys.keys()),
        "concepts": keys.get(taxonomy, []),
        "concept_count": len(keys.get(taxonomy, [])),
    })
    return out


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RUN_RE = re.compile(r"\s{3,}")
_DOC_MAX_CHARS = 60_000  # cap returned text to keep MCP payload sane


@mcp.tool()
async def get_filing_document(
    ticker_or_cik: str,
    accession: str,
    doc: str | None = None,
    max_chars: int = _DOC_MAX_CHARS,
    offset: int = 0,
) -> dict[str, Any]:
    """Fetch a filing document, strip HTML, return plain text.

    Args:
      ticker_or_cik: 'NVDA' or CIK
      accession: '0001045810-25-000123'
      doc: specific document name (e.g. 'nvda-20250126.htm'). If omitted,
        we auto-pick the primary document (largest non-exhibit .htm).
      max_chars: cap on returned text (default 60K to keep interactive MCP
        payloads sane; clamped to [1_000, 1_000_000]). Batch callers like
        alphalens' 10-K/10-Q section ingest pass 1_000_000 to get the whole
        document in one call.
      offset: start position into the stripped text (paging for callers that
        keep the small default cap).

    Returns:
      {
        success, cik, accession, doc_name, doc_url,
        text: <plain text window [offset, offset+max_chars)>,
        truncated: bool  (true when text continues past the window),
        original_chars: int,
        offset: int,
      }
    """
    out: dict[str, Any] = {"success": False, "accession": accession}

    meta = await get_filing_metadata(ticker_or_cik, accession)
    if not meta.get("success"):
        return meta
    cik = meta["cik"]
    doc_name = doc or meta.get("primary_doc")
    if not doc_name:
        out["error"] = "no document name provided and no primary doc detected"
        return out

    acc_clean = accession.replace("-", "")
    url = f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{acc_clean}/{doc_name}"
    try:
        html = await _get_text(url)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"doc fetch failed: {e!r}"
        return out

    # Strip HTML for text mode. SEC docs are highly tabular so the result is
    # imperfect but adequate for keyword search / LLM grounding. Callers who
    # need the raw HTML / iXBRL should hit doc_url directly.
    # Unescape entities BEFORE whitespace collapse: filings routinely encode
    # nbsp/apostrophes as &#160;/&#8217; etc., which otherwise leak into the
    # text verbatim and defeat downstream regex parsing (e.g. "Item&#160;1A.").
    text = _HTML_TAG_RE.sub(" ", html)
    text = html_unescape(text)
    text = _WS_RUN_RE.sub("\n", text).strip()
    original_chars = len(text)
    cap = max(1_000, min(int(max_chars), 1_000_000))
    start = max(0, int(offset))
    truncated = original_chars > start + cap

    out.update({
        "success": True,
        "cik": cik,
        "doc_name": doc_name,
        "doc_url": url,
        "text": text[start:start + cap] + ("\n\n[truncated]" if truncated else ""),
        "truncated": truncated,
        "original_chars": original_chars,
        "offset": start,
    })
    return out


@mcp.tool()
async def search_filings(
    query: str,
    ticker: str | None = None,
    form: str | None = None,
    since: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Full-text search across SEC EDGAR filings.

    Source: https://efts.sec.gov/LATEST/search-index?q=...&forms=...&dateRange=custom

    Args:
      query: free-text terms. EDGAR search supports phrases ("net interest
        margin") and operators (AND / OR / NOT).
      ticker: limit to one company (resolved to CIK).
      form: comma-separated form types e.g. '8-K' or '10-K,10-Q'.
      since: ISO date 'YYYY-MM-DD' lower bound.
      limit: max hits (default 10, EDGAR caps at 100 per page).

    Returns:
      {
        success, query, hits: [
          {accession, form, filing_date, ticker, name, snippets[], doc_url}, ...
        ],
      }
    """
    out: dict[str, Any] = {"success": False, "query": query}

    params: dict[str, Any] = {"q": query}
    if form:
        params["forms"] = form
    if since:
        # EDGAR wants dateRange=custom + startdt
        params["dateRange"] = "custom"
        params["startdt"] = since
    if ticker:
        cik = await _resolve_cik(ticker)
        if cik:
            params["ciks"] = cik
        else:
            out["error"] = f"unknown ticker for ciks filter: {ticker!r}"
            return out

    try:
        payload = await _get_json(f"{SEC_EFTS}/search-index", params=params)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"search request failed: {e!r}"
        return out

    hits = ((payload.get("hits") or {}).get("hits") or [])
    rows = []
    for h in hits[: max(1, int(limit))]:
        src = h.get("_source") or {}
        # EDGAR's _id is "{accession}:{filename}" — we want the accession.
        acc_raw = (h.get("_id") or "").split(":", 1)[0]
        acc = acc_raw  # already dashed in EFTS
        adsh = src.get("adsh") or acc_raw
        adsh_clean = adsh.replace("-", "")
        cik = src.get("ciks") or src.get("display_names") or []
        primary_cik = (cik[0] if isinstance(cik, list) and cik else "")
        primary_doc = (h.get("_source") or {}).get("file") or src.get("file_type") or ""
        rows.append({
            "accession": adsh,
            "form": src.get("form"),
            "filing_date": src.get("file_date") or src.get("display_date"),
            "ticker": (src.get("tickers") or [None])[0] if isinstance(src.get("tickers"), list) else src.get("tickers"),
            "company_name": (src.get("display_names") or [None])[0] if isinstance(src.get("display_names"), list) else src.get("display_names"),
            "doc_url": (
                f"{SEC_WWW}/Archives/edgar/data/{primary_cik}/{adsh_clean}/{primary_doc}"
                if primary_cik and primary_doc else None
            ),
            "snippets": (h.get("highlight") or {}).get("body", []) if isinstance(h.get("highlight"), dict) else [],
        })

    out.update({"success": True, "hits": rows})
    return out


if __name__ == "__main__":
    print(
        f"sec-edgar MCP listening on "
        f"http://{mcp.settings.host}:{mcp.settings.port}/mcp  (UA={SEC_UA})"
    )
    from _mcp_auth import serve  # audit I1: opt-in MCP_SHARED_SECRET bearer gate
    serve(mcp)
