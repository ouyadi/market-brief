"""seekingalpha_rss_mcp.py -- HTTP MCP server for SeekingAlpha free RSS feeds.

This is Phase A of the SA integration -- public/free RSS only, no auth.
Phase B/C (Premium scraping with Playwright + Google OAuth) is a later effort.

Three feed sources confirmed working as of 2026-05-26:
  - `/api/sa/combined/{TICKER}.xml`  per-ticker articles + analysis
  - `/market_currents.xml`           breaking-news-style headlines with tickers
  - `/feed.xml`                      all-articles front page

The feeds carry titles, links, pubDate, author, and (for combined) tickers via
the `sa:stock` namespace or (for market_currents) `<category>` tags. There are
NO `<description>` / `<summary>` bodies in any of these three feeds, so the
`summary` field will normally be empty -- the caller should fetch the article
URL separately if a snippet is needed (or wait for Phase B Premium scraping).

Tools exposed:
  get_articles_for_ticker(ticker, limit)   per-ticker SA combined feed
  get_market_news(limit)                   front-page market_currents wire
  get_breaking_news(limit)                 all-articles front page

Run as HTTP MCP on 127.0.0.1:7050/mcp by default. Override via SA_RSS_MCP_PORT.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import aiohttp
from mcp.server.fastmcp import FastMCP

LOG_DIR = Path(os.environ.get("SA_RSS_MCP_DIR") or (Path.home() / "sa-rss-mcp")) / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "sa_rss_mcp.log", encoding="utf-8")],
)
log = logging.getLogger("sa-rss")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT_S = 15
BASE = "https://seekingalpha.com"

mcp = FastMCP(
    "seekingalpha-rss",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("SA_RSS_MCP_PORT", "7050")),
)


# ──────────────────────── helpers ─────────────────────────


def _parse_rss_date(s: str) -> tuple[int | None, str | None]:
    """Parse RFC 822 pubDate. Returns (epoch_sec, iso_z) or (None, None)."""
    if not s:
        return None, None
    s = s.strip()
    # SA emits e.g. 'Tue, 26 May 2026 01:54:48 -0400'
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S GMT",
    ):
        try:
            dt = datetime.datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            ts = int(dt.timestamp())
            iso = dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
            return ts, iso
        except ValueError:
            continue
    return None, None


async def _fetch_xml(url: str) -> str:
    headers = {
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
        async with s.get(url) as r:
            r.raise_for_status()
            return await r.text()


# RSS namespaces SA uses in the combined per-ticker feed.
_NS = {
    "sa": "https://seekingalpha.com/api/1.0",
    "media": "http://search.yahoo.com/mrss/",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def _extract_item(item: ET.Element) -> dict[str, Any]:
    """Pull common fields out of one <item>. Handles both feed schemas."""
    title = (item.findtext("title") or "").strip()
    url = (item.findtext("link") or "").strip()
    guid = (item.findtext("guid") or "").strip()
    pub = item.findtext("pubDate") or ""
    ts, ts_iso = _parse_rss_date(pub)

    # Author: combined feed uses <sa:author_name>, market_currents has none,
    # feed.xml uses <category type="author">.
    author = item.findtext("sa:author_name", namespaces=_NS) or ""
    if not author:
        for cat in item.findall("category"):
            if cat.attrib.get("type") == "author":
                author = (cat.text or "").strip()
                break

    # Tickers: combined feed → <sa:stock><sa:symbol>; market_currents →
    # <category domain=".../symbol/XXX">; feed.xml → <category type="symbol">.
    tickers: list[str] = []
    for st in item.findall("sa:stock", namespaces=_NS):
        sym = st.findtext("sa:symbol", namespaces=_NS)
        if sym:
            tickers.append(sym.strip())
    for cat in item.findall("category"):
        text = (cat.text or "").strip()
        attr_type = cat.attrib.get("type", "")
        domain = cat.attrib.get("domain", "")
        if (attr_type == "symbol" or "/symbol/" in domain) and text:
            tickers.append(text.upper())
    # dedupe preserving order
    seen: set[str] = set()
    tickers_u: list[str] = []
    for t in tickers:
        tu = t.upper()
        if tu not in seen:
            seen.add(tu)
            tickers_u.append(tu)

    # SA feeds do NOT include <description> or content:encoded bodies.
    # Keep the field present (empty string) for API consistency so the
    # alphalens wrapper has a stable shape across all three tools.
    summary = (item.findtext("description") or "").strip()

    return {
        "title": title,
        "url": url,
        "guid": guid or url,
        "published_at": pub.strip() or None,
        "ts": ts,
        "ts_iso": ts_iso,
        "author": author or None,
        "tickers": tickers_u,
        "summary": summary[:600] if summary else "",
    }


def _parse_feed(xml_text: str, limit: int) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("RSS parse failed: %s", e)
        return []
    items: list[dict[str, Any]] = []
    for item in root.iter("item"):
        try:
            items.append(_extract_item(item))
        except Exception:
            log.exception("item parse failed")
            continue
        if len(items) >= limit:
            break
    return items


# ───────────────────────────── Tools ────────────────────────────────


@mcp.tool()
async def get_articles_for_ticker(ticker: str, limit: int = 10) -> dict:
    """Latest SA articles + analysis for a single ticker (free combined RSS).

    ticker: stock symbol, case-insensitive (e.g. 'NVDA', 'aapl').
    limit:  1-30, default 10.

    Uses `https://seekingalpha.com/api/sa/combined/{TICKER}.xml` which mixes
    Premium analyst articles and free news items into one feed. Items carry
    title, link, pubDate, sa:author_name, and an sa:stock list of related
    tickers. No body text -- the article URL must be fetched separately for
    the full content (or wait for Phase B Premium scraping).
    """
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return {"success": False, "error": "ticker required"}
    limit = max(1, min(30, int(limit)))
    url = f"{BASE}/api/sa/combined/{ticker}.xml"
    try:
        xml_text = await _fetch_xml(url)
        items = _parse_feed(xml_text, limit)
        log.info("articles_for_ticker %s -> %d items", ticker, len(items))
        return {
            "success": True,
            "ticker": ticker,
            "source_url": url,
            "count": len(items),
            "articles": items,
        }
    except aiohttp.ClientResponseError as e:
        return {
            "success": False,
            "ticker": ticker,
            "error": f"HTTP {e.status} from SA",
            "source_url": url,
        }
    except Exception as e:
        return {
            "success": False,
            "ticker": ticker,
            "error": str(e)[:200],
            "source_url": url,
        }


@mcp.tool()
async def get_market_news(limit: int = 20) -> dict:
    """SA breaking-news wire (cross-ticker market headlines).

    limit: 1-50, default 20.

    Uses `https://seekingalpha.com/market_currents.xml`. Each item is one
    news headline tagged with one-or-more related tickers via <category
    domain=".../symbol/XYZ">. Title text + link + timestamp only -- no
    summary body. Equivalent to a real-time market wire (refreshes every
    few minutes during market hours).
    """
    limit = max(1, min(50, int(limit)))
    url = f"{BASE}/market_currents.xml"
    try:
        xml_text = await _fetch_xml(url)
        items = _parse_feed(xml_text, limit)
        log.info("market_news -> %d items", len(items))
        return {
            "success": True,
            "source_url": url,
            "count": len(items),
            "articles": items,
        }
    except aiohttp.ClientResponseError as e:
        return {"success": False, "error": f"HTTP {e.status} from SA", "source_url": url}
    except Exception as e:
        return {"success": False, "error": str(e)[:200], "source_url": url}


@mcp.tool()
async def get_breaking_news(limit: int = 20) -> dict:
    """SA all-articles front page (analyst pieces, not the wire).

    limit: 1-50, default 20.

    Uses `https://seekingalpha.com/feed.xml` -- this is the front-page
    "All Articles on Seeking Alpha" feed (analyst write-ups, not the news
    wire). For pure breaking news, callers should prefer get_market_news.

    Note: SA does NOT publish a public RSS feed for "Wall Street Breakfast"
    specifically -- the daily newsletter is delivered via email + an HTML
    landing page only. Title kept as "breaking_news" for the eventual
    callers; the feed content is best described as "front-page articles".
    """
    limit = max(1, min(50, int(limit)))
    url = f"{BASE}/feed.xml"
    try:
        xml_text = await _fetch_xml(url)
        items = _parse_feed(xml_text, limit)
        log.info("breaking_news (front-page) -> %d items", len(items))
        return {
            "success": True,
            "source_url": url,
            "count": len(items),
            "articles": items,
        }
    except aiohttp.ClientResponseError as e:
        return {"success": False, "error": f"HTTP {e.status} from SA", "source_url": url}
    except Exception as e:
        return {"success": False, "error": str(e)[:200], "source_url": url}


# ───────────────────────── Entrypoint ───────────────────────────────


if __name__ == "__main__":
    log.info(
        "seekingalpha-rss MCP starting on http://%s:%s/mcp",
        mcp.settings.host,
        mcp.settings.port,
    )
    from _mcp_auth import serve  # audit I1: opt-in MCP_SHARED_SECRET bearer gate
    serve(mcp)
