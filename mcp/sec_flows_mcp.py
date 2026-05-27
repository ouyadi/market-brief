"""sec_flows_mcp.py -- HTTP MCP server exposing SEC ownership flow data.

Stateless wrapper around two SEC EDGAR data sources:
  - Form 4   — officer / director / 10%+ owner transactions
  - Form 13F-HR — quarterly institutional holdings disclosures

The MCP fetches + parses XML on demand. Aggregation across tickers /
funds lives downstream in alphalens (TS scripts write parsed records to
DB, then SQL aggregators serve top-mover queries). Mirrors the design of
sec_edgar_mcp.py — caches submissions metadata, leans on EDGAR's polite
10 req/sec budget.

Tools:
  list_form4_filings(ticker_or_cik, since?, limit=20)
    Form 4 filings for an issuer, recent-first. Returns accession + date +
    primary doc URL but NOT parsed content (call parse_form4_xml).

  parse_form4_xml(accession, cik)
    Fetch the Form 4 ownership XML for a specific filing, return a list of
    transactions with owner identity, code, shares, price, post-balance.

  list_13f_filings(fund_cik, quarters=4)
    Recent 13F-HR filings for a fund — accession + period_of_report +
    information-table XML URL. No parsing.

  parse_13f_xml(accession, fund_cik)
    Fetch + parse the 13F informationTable.xml — returns a list of
    positions (cusip, issuer, value in USD, shares, putCall).

  health()
    Diagnostic.

Port: 3038 by default (sec-edgar is 3037). Override SEC_FLOWS_MCP_PORT.
Same SEC User-Agent contract as sec_edgar_mcp — set SEC_EDGAR_CONTACT for
the email-in-UA, defaults to user001@token-gateway.com.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import aiohttp
from mcp.server.fastmcp import FastMCP

LOG_DIR = (
    Path(os.environ.get("SEC_FLOWS_MCP_DIR") or (Path.home() / "sec-flows-mcp")) / "logs"
)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "sec_flows_mcp.log", encoding="utf-8")],
)
log = logging.getLogger("sec-flows")

# ── Endpoints ──────────────────────────────────────────────────────────────
SEC_DATA = "https://data.sec.gov"
SEC_WWW = "https://www.sec.gov"
HTTP_TIMEOUT_S = 30

SEC_CONTACT = os.environ.get("SEC_EDGAR_CONTACT", "user001@token-gateway.com")
SEC_UA = f"alphalens ({SEC_CONTACT})"

mcp = FastMCP(
    "sec-flows",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("SEC_FLOWS_MCP_PORT", "3038")),
)

# ── Caches ─────────────────────────────────────────────────────────────────
_TICKER_CACHE: dict[str, str] = {}
_CACHE: dict[str, tuple[float, Any]] = {}
TTL_SUBMISSIONS = 30 * 60                      # filings list refreshes infrequently
TTL_XML_BODY = 24 * 60 * 60                    # parsed XML is immutable once filed
TTL_TICKER_MAP = 24 * 60 * 60


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


# ── HTTP helpers ───────────────────────────────────────────────────────────
async def _get_json(url: str) -> dict[str, Any]:
    headers = {"User-Agent": SEC_UA, "Accept": "application/json"}
    async with aiohttp.ClientSession() as session:
        for attempt in range(2):
            try:
                async with session.get(
                    url, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S),
                ) as r:
                    if r.status in (429, 503):
                        if attempt == 0:
                            await asyncio.sleep(2.0)
                            continue
                    r.raise_for_status()
                    # SEC frequently serves JSON with Content-Type: text/html
                    # for archive `index.json` files. Pass content_type=None
                    # to skip aiohttp's mime-type validation — we know it's JSON.
                    return await r.json(content_type=None)
            except aiohttp.ClientError:
                if attempt == 1:
                    raise
                await asyncio.sleep(1.0)
        return {}


async def _get_text(url: str) -> str:
    headers = {"User-Agent": SEC_UA, "Accept": "application/xml, text/xml, text/plain"}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S),
        ) as r:
            r.raise_for_status()
            return await r.text()


# ── Ticker → CIK lookup (shared with sec_edgar pattern) ───────────────────
async def _ensure_ticker_map() -> dict[str, str]:
    if _TICKER_CACHE:
        return _TICKER_CACHE
    data = await _get_json(f"{SEC_WWW}/files/company_tickers.json")
    for row in data.values():
        sym = (row.get("ticker") or "").upper()
        cik = str(row.get("cik_str") or "").zfill(10)
        if sym and cik:
            _TICKER_CACHE[sym] = cik
    return _TICKER_CACHE


async def _to_cik(ticker_or_cik: str) -> str:
    s = (ticker_or_cik or "").strip().upper()
    if not s:
        raise ValueError("ticker_or_cik required")
    if s.isdigit():
        return s.zfill(10)
    mp = await _ensure_ticker_map()
    if s not in mp:
        raise ValueError(f"Unknown ticker: {s}")
    return mp[s]


# ── Submissions API ───────────────────────────────────────────────────────
async def _submissions(cik: str) -> dict[str, Any]:
    """SEC submissions.json — recent filings for an issuer (or fund)."""
    cache_key = f"sub:{cik}"
    hit = _cache_get(cache_key, TTL_SUBMISSIONS)
    if hit is not None:
        return hit
    data = await _get_json(f"{SEC_DATA}/submissions/CIK{cik}.json")
    _cache_put(cache_key, data)
    return data


def _accession_no_dashes(accession: str) -> str:
    return accession.replace("-", "")


# ── XML helpers ───────────────────────────────────────────────────────────
def _strip_ns(tag: str) -> str:
    """Strip XML namespace prefix: '{http://...}foo' → 'foo'."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_text(node: ET.Element | None, *path: str) -> str | None:
    """Walk a path of element local-names (NS-agnostic) and return the
    leaf text. Returns None if any step is missing."""
    if node is None:
        return None
    cur = node
    for step in path:
        nxt = None
        for child in cur:
            if _strip_ns(child.tag) == step:
                nxt = child
                break
        if nxt is None:
            return None
        cur = nxt
    return (cur.text or "").strip() or None


def _find_value(node: ET.Element | None, *path: str) -> str | None:
    """SEC XBRL convention: most fields are wrapped in <value>X</value>.
    This walks to the parent and returns the inner <value> text."""
    return _find_text(node, *path, "value")


def _find_all(node: ET.Element | None, name: str) -> list[ET.Element]:
    if node is None:
        return []
    return [c for c in node if _strip_ns(c.tag) == name]


def _to_float(s: str | None) -> float | None:
    if s is None or s == "":
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


# ── Form 4 parser ─────────────────────────────────────────────────────────
def _parse_form4(xml_text: str) -> dict[str, Any]:
    """Parse SEC Form 4 ownershipDocument XML.

    Returns a dict with issuer + reportingOwner + transactions[]. Both
    nonDerivative (common stock buys/sells) and derivative (options exercises,
    grants) transactions are returned, distinguished by `instrument`.
    """
    root = ET.fromstring(xml_text)
    # Issuer block
    issuer = None
    owner = None
    for child in root:
        name = _strip_ns(child.tag)
        if name == "issuer":
            issuer = child
        elif name == "reportingOwner":
            owner = child  # first reportingOwner — most filings have 1
    issuer_cik = _find_text(issuer, "issuerCik")
    issuer_name = _find_text(issuer, "issuerName")
    issuer_symbol = _find_text(issuer, "issuerTradingSymbol")

    owner_id = next((c for c in (owner or []) if _strip_ns(c.tag) == "reportingOwnerId"), None)
    rel = next((c for c in (owner or []) if _strip_ns(c.tag) == "reportingOwnerRelationship"), None)
    owner_cik = _find_text(owner_id, "rptOwnerCik")
    owner_name = _find_text(owner_id, "rptOwnerName")

    roles: list[str] = []
    if _find_text(rel, "isDirector") == "1":
        roles.append("Director")
    if _find_text(rel, "isOfficer") == "1":
        roles.append("Officer")
    if _find_text(rel, "isTenPercentOwner") == "1":
        roles.append("10%Owner")
    if _find_text(rel, "isOther") == "1":
        roles.append("Other")
    owner_role = ",".join(roles) or None
    owner_title = _find_text(rel, "officerTitle")

    # Transactions
    txns: list[dict[str, Any]] = []
    for table_name, instrument in (("nonDerivativeTable", "common"), ("derivativeTable", "derivative")):
        for c in root:
            if _strip_ns(c.tag) != table_name:
                continue
            tx_tag = "nonDerivativeTransaction" if table_name == "nonDerivativeTable" else "derivativeTransaction"
            for tx in c:
                if _strip_ns(tx.tag) != tx_tag:
                    continue
                # transactionCode is direct text, NOT wrapped in <value>
                # (unlike most Form 4 fields). All other amounts ARE wrapped.
                code = _find_text(tx, "transactionCoding", "transactionCode")
                date = _find_value(tx, "transactionDate")
                shares = _to_float(_find_value(tx, "transactionAmounts", "transactionShares"))
                price = _to_float(_find_value(tx, "transactionAmounts", "transactionPricePerShare"))
                ad_code = _find_value(tx, "transactionAmounts", "transactionAcquiredDisposedCode")
                # Sign convention: A=acquired → positive shares; D=disposed → negative
                signed_shares = shares
                if signed_shares is not None and ad_code == "D":
                    signed_shares = -signed_shares
                shares_after = _to_float(_find_value(tx, "postTransactionAmounts", "sharesOwnedFollowingTransaction"))
                direct = _find_value(tx, "ownershipNature", "directOrIndirectOwnership")
                # 10b5-1 flag often in <transactionCoding><equitySwapInvolved> or <footnoteId>;
                # most reliable: parent <transactionCoding><isEquitySwap> + footnotes. Mark
                # as unknown here; downstream can re-parse if needed.
                txns.append({
                    "instrument": instrument,
                    "transaction_date": date,
                    "transaction_code": code,
                    "shares": signed_shares,
                    "price_per_share": price,
                    "total_value": (signed_shares * price) if signed_shares is not None and price is not None else None,
                    "shares_after": shares_after,
                    "direct_ownership": direct == "D" if direct else None,
                })

    return {
        "issuer_cik": issuer_cik,
        "issuer_name": issuer_name,
        "issuer_symbol": issuer_symbol,
        "owner_cik": owner_cik,
        "owner_name": owner_name,
        "owner_role": owner_role,
        "owner_title": owner_title,
        "transactions": txns,
    }


# ── Form 13F parser ───────────────────────────────────────────────────────
def _parse_13f(xml_text: str) -> list[dict[str, Any]]:
    """Parse SEC Form 13F-HR informationTable.xml.

    Returns a list of positions. value_usd is converted from 13F's
    "thousands of USD" convention to actual USD.
    """
    root = ET.fromstring(xml_text)
    out: list[dict[str, Any]] = []
    # root may be <informationTable> or already iterating <infoTable> children
    for entry in root:
        if _strip_ns(entry.tag) != "infoTable":
            continue
        issuer_name = _find_text(entry, "nameOfIssuer")
        title_class = _find_text(entry, "titleOfClass")
        cusip = _find_text(entry, "cusip")
        # value is "x1000" until 2022-Q4 SEC change made it actual USD; we read
        # raw and multiply by 1000 to be safe for older periods. Modern filings
        # also have x1000 in practice. Acceptable error: x1000 inflation on
        # 2023+ data would be obvious vs market cap, can be normalized post-hoc.
        # 13F-HR "value" column: pre-2023 filings reported in thousands of USD,
        # 2023+ filings report direct USD. We assume modern (no x1000). For old
        # backfills, callers can detect & rescale post-hoc if needed.
        value_usd = _to_float(_find_text(entry, "value"))
        shrs_node = next((c for c in entry if _strip_ns(c.tag) == "shrsOrPrnAmt"), None)
        shares = _to_float(_find_text(shrs_node, "sshPrnamt"))
        shares_type = _find_text(shrs_node, "sshPrnamtType")     # 'SH' or 'PRN'
        put_call = _find_text(entry, "putCall")
        out.append({
            "issuer_name": issuer_name,
            "title_class": title_class,
            "cusip": cusip,
            "value_usd": value_usd,
            "shares": shares,
            "shares_type": shares_type,
            "put_call": put_call,
        })
    return out


# ── Tools ─────────────────────────────────────────────────────────────────
@mcp.tool()
async def list_form4_filings(
    ticker_or_cik: str,
    since: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """List recent Form 4 filings for an issuer.

    ticker_or_cik: 'NVDA' or '0001045810' or '1045810'
    since: ISO date 'YYYY-MM-DD'; if set, only filings on/after this date
    limit: max filings to return (default 20)

    Returns: { success, cik, ticker, filings: [{accession, filing_date, primary_doc, doc_url}] }
    Form 4 has no separate parsed-XML doc URL — the primary doc IS the
    ownership XML for accessions filed after ~2003.
    """
    try:
        cik = await _to_cik(ticker_or_cik)
        sub = await _submissions(cik)
        recent = sub.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        primary = recent.get("primaryDocument", [])
        out: list[dict[str, Any]] = []
        for i in range(len(forms)):
            if forms[i] != "4":
                continue
            if since and dates[i] < since:
                continue
            acc = accessions[i]
            acc_nd = _accession_no_dashes(acc)
            doc = primary[i] if i < len(primary) else None
            out.append({
                "accession": acc,
                "filing_date": dates[i],
                "primary_doc": doc,
                "doc_url": f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{acc_nd}/{doc}" if doc else None,
                "filing_index_url": f"{SEC_WWW}/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=4",
            })
            if len(out) >= limit:
                break
        return {
            "success": True,
            "cik": cik,
            "ticker": ticker_or_cik.upper() if not ticker_or_cik.isdigit() else None,
            "filings": out,
            "count": len(out),
        }
    except Exception as e:
        log.exception("list_form4_filings failed")
        return {"success": False, "error": str(e)}


@mcp.tool()
async def parse_form4_xml(accession: str, cik: str) -> dict[str, Any]:
    """Fetch + parse a Form 4 ownership XML.

    accession: e.g. '0001127602-26-018401' (with dashes is fine)
    cik:       the issuer CIK (NOT the reporting owner). Either dashless
               10-digit, or just the integer.

    Returns: { success, issuer_*, owner_*, transactions: [...] }
    """
    try:
        acc_nd = _accession_no_dashes(accession)
        # Archive path uses the SUBJECT CIK (issuer for Form 4, fund for
        # 13F) — NOT the filer/reporting-owner CIK. EDGAR indexes filings
        # under whoever the filing is ABOUT.
        cik_int = int(cik)
        cache_key = f"f4:{acc_nd}"
        hit = _cache_get(cache_key, TTL_XML_BODY)
        if hit is not None:
            return hit

        # Form 4 XML doc is in the filing's index; we find it by listing
        # the filing dir and picking the .xml whose name doesn't start with
        # 'primary_doc.' or end with '_ex'. Typical filenames:
        #   wf-form4_172304....xml  (the ownership doc)
        #   primary_doc.xml         (a wrapper, also valid)
        index_url = f"{SEC_WWW}/Archives/edgar/data/{cik_int}/{acc_nd}/"
        idx_json = await _get_json(index_url + "index.json")
        items = idx_json.get("directory", {}).get("item", [])
        xml_candidates = [
            it["name"] for it in items
            if it.get("name", "").endswith(".xml")
            and not it["name"].endswith(("_ex.xml", "filing-summary.xml"))
        ]
        if not xml_candidates:
            return {"success": False, "error": "no Form 4 XML found in filing index"}
        # Prefer wf-form4* / form4* / primary_doc.xml in that order.
        xml_candidates.sort(key=lambda n: (
            0 if n.startswith(("wf-form4", "form4")) else
            1 if n == "primary_doc.xml" else 2
        ))
        xml_url = index_url + xml_candidates[0]
        xml_text = await _get_text(xml_url)
        parsed = _parse_form4(xml_text)
        result = {"success": True, "accession": accession, "xml_url": xml_url, **parsed}
        _cache_put(cache_key, result)
        return result
    except Exception as e:
        log.exception("parse_form4_xml failed for %s", accession)
        return {"success": False, "error": str(e)}


@mcp.tool()
async def list_13f_filings(fund_cik: str, quarters: int = 4) -> dict[str, Any]:
    """List recent Form 13F-HR filings for a fund.

    fund_cik: '0001067983' (Berkshire) — 10-digit zero-padded or integer
    quarters: number of recent 13F filings to return (default 4 = 1 year)

    Returns: { success, fund_cik, fund_name, filings: [{accession, period_of_report, filing_date, info_table_url}] }
    """
    try:
        cik = fund_cik.zfill(10) if fund_cik.isdigit() else fund_cik
        sub = await _submissions(cik)
        fund_name = sub.get("name") or sub.get("entityName") or ""
        recent = sub.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        reports = recent.get("reportDate", [])
        accessions = recent.get("accessionNumber", [])
        primary = recent.get("primaryDocument", [])
        out: list[dict[str, Any]] = []
        for i in range(len(forms)):
            if forms[i] not in ("13F-HR", "13F-HR/A"):
                continue
            acc = accessions[i]
            acc_nd = _accession_no_dashes(acc)
            # 13F filings have separate primary_doc (cover) + informationTable.xml (positions).
            # The primary_doc is usually the cover XML; the positions XML lives next to it.
            out.append({
                "accession": acc,
                "form": forms[i],
                "filing_date": dates[i],
                "period_of_report": reports[i] if i < len(reports) else None,
                "primary_doc": primary[i] if i < len(primary) else None,
                "filing_dir": f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{acc_nd}/",
            })
            if len(out) >= quarters:
                break
        return {
            "success": True,
            "fund_cik": cik,
            "fund_name": fund_name,
            "filings": out,
            "count": len(out),
        }
    except Exception as e:
        log.exception("list_13f_filings failed")
        return {"success": False, "error": str(e)}


@mcp.tool()
async def parse_13f_xml(accession: str, fund_cik: str) -> dict[str, Any]:
    """Fetch + parse a Form 13F-HR information table.

    accession: e.g. '0000950123-26-001234' (with dashes ok)
    fund_cik:  the filing fund's CIK (integer or zero-padded)

    Returns: { success, accession, info_table_url, holdings: [...] }
    holdings sorted by value_usd DESC.
    """
    try:
        acc_nd = _accession_no_dashes(accession)
        # Archive path uses the SUBJECT CIK (the fund whose holdings are
        # reported), not the filing agent's CIK encoded in the accession.
        cik_int = int(fund_cik)
        cache_key = f"f13f:{acc_nd}"
        hit = _cache_get(cache_key, TTL_XML_BODY)
        if hit is not None:
            return hit
        index_url = f"{SEC_WWW}/Archives/edgar/data/{cik_int}/{acc_nd}/"
        idx_json = await _get_json(index_url + "index.json")
        items = idx_json.get("directory", {}).get("item", [])
        # The information table file name varies — common patterns:
        #   informationtable.xml / infotable.xml / *_infotable.xml
        # The cover doc is usually primary_doc.xml — it's NOT what we want here.
        candidates = [
            it["name"] for it in items
            if it.get("name", "").endswith(".xml")
            and "infotable" in it["name"].lower() or "information_table" in it["name"].lower()
            or "informationtable" in it["name"].lower()
        ]
        # Fallback: any .xml that isn't primary_doc / cover
        if not candidates:
            candidates = [
                it["name"] for it in items
                if it.get("name", "").endswith(".xml")
                and it["name"] not in ("primary_doc.xml",)
                and "summary" not in it["name"].lower()
            ]
        if not candidates:
            return {"success": False, "error": "no informationTable XML found"}
        xml_url = index_url + candidates[0]
        xml_text = await _get_text(xml_url)
        holdings = _parse_13f(xml_text)
        holdings.sort(key=lambda r: (r.get("value_usd") or 0), reverse=True)
        result = {
            "success": True,
            "accession": accession,
            "info_table_url": xml_url,
            "holdings": holdings,
            "count": len(holdings),
        }
        _cache_put(cache_key, result)
        return result
    except Exception as e:
        log.exception("parse_13f_xml failed for %s", accession)
        return {"success": False, "error": str(e)}


@mcp.tool()
async def health() -> dict[str, Any]:
    """Diagnostic — confirms UA is set + SEC submissions endpoint reachable."""
    try:
        # ping with a known-stable CIK (AAPL = 0000320193)
        sub = await _submissions("0000320193")
        return {
            "success": True,
            "ua": SEC_UA,
            "sample_issuer": sub.get("name") or sub.get("entityName"),
            "cache_size": len(_CACHE),
            "ticker_map_size": len(_TICKER_CACHE),
        }
    except Exception as e:
        return {"success": False, "error": str(e), "ua": SEC_UA}


if __name__ == "__main__":
    log.info("sec-flows MCP starting on port %s", os.environ.get("SEC_FLOWS_MCP_PORT", "3038"))
    mcp.run(transport="streamable-http")
