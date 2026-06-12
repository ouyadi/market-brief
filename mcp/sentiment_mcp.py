"""sentiment_mcp.py -- HTTP MCP server exposing free market-sentiment gauges.

Four independent free sources, each its own fault domain (one failing only
omits its tool's data / returns an error -- the others keep working):

  - CNN Fear & Greed Index  (production.dataviz.cnn.io graphdata; browser UA +
    Origin/Referer required, else 418). Cache 1h.
  - NAAIM Exposure Index    (naaim.org weekly active-manager equity exposure,
    parsed from the public "recent weeks" HTML table). Cache 6h.
  - AAII Investor Sentiment (aaii.com/sentimentsurvey bull/neutral/bear).
    NOTE: aaii.com is behind aggressive bot protection (403/503 from datacenter
    and many residential IPs as of 2026-06). Per design ruling #4 the project
    SHIPS THE THREE-SOURCE VERSION and tolerates AAII being unavailable -- the
    aaii_survey tool degrades gracefully to {success:false,...} and the
    downstream ingest skips it. The tool is kept (not removed) so it starts
    working automatically if/when the IP or a cookie makes the page reachable.
  - ApeWisdom retail buzz    (apewisdom.io public API; no key). Cache 15min.

Tools:
  fear_greed()                                  CNN F&G + 30d history
  naaim_exposure()                              NAAIM weekly exposure + ~12 wk history
  aaii_survey()                                 AAII bull/neutral/bear (best-effort)
  apewisdom_trending(filter, limit)             WSB / retail mention leaderboard

Run as HTTP MCP on 127.0.0.1:3039/mcp by default. Override port via
SENTIMENT_MCP_PORT env var. No external API keys required.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
from mcp.server.fastmcp import FastMCP

LOG_DIR = (
    Path(os.environ.get("SENTIMENT_MCP_DIR") or (Path.home() / "sentiment-mcp"))
    / "logs"
)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "sentiment_mcp.log", encoding="utf-8")],
)
log = logging.getLogger("sentiment")

# Real-browser UA + Origin/Referer: CNN's dataviz host 418s a bare UA. Verified
# 2026-06-12 that adding Origin/Referer makes graphdata return JSON.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT_S = 15

CNN_BASE = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
NAAIM_URL = "https://www.naaim.org/programs/naaim-exposure-index/"
AAII_URL = "https://www.aaii.com/sentimentsurvey"
APEWISDOM_BASE = "https://apewisdom.io/api/v1.0/filter"

mcp = FastMCP(
    "sentiment",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("SENTIMENT_MCP_PORT", "3039")),
)

# ── Per-source in-memory caches (independent TTLs) ───────────────────────────
_CACHE: dict[str, tuple[float, Any]] = {}
TTL_FG = 60 * 60          # 1h
TTL_NAAIM = 6 * 60 * 60   # 6h
TTL_AAII = 6 * 60 * 60    # 6h
TTL_APE = 15 * 60         # 15min


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


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


async def _get_json(url: str, headers: dict | None = None) -> Any:
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    if headers:
        h.update(headers)
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(url, headers=h) as r:
            text = await r.text()
            if r.status != 200:
                raise RuntimeError(f"GET {url} -> {r.status} {text[:160]}")
            return json.loads(text)


async def _get_text(url: str, headers: dict | None = None) -> str:
    h = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        h.update(headers)
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(url, headers=h) as r:
            text = await r.text()
            if r.status != 200:
                raise RuntimeError(f"GET {url} -> {r.status} {text[:160]}")
            return text


# ── CNN Fear & Greed ─────────────────────────────────────────────────────────
async def _fear_greed() -> dict:
    cached = _cache_get("fg", TTL_FG)
    if cached is not None:
        return cached
    # graphdata/<start-date> returns the full component breakdown + 30d history.
    start = (datetime.date.today() - datetime.timedelta(days=40)).isoformat()
    url = f"{CNN_BASE}/{start}"
    data = await _get_json(url, headers={"Origin": "https://www.cnn.com", "Referer": "https://www.cnn.com/"})
    fg = data.get("fear_and_greed") or {}
    score = fg.get("score")
    hist_raw = (data.get("fear_and_greed_historical") or {}).get("data") or []
    history: list[dict] = []
    for p in hist_raw[-30:]:
        try:
            x = p.get("x")
            d = datetime.datetime.fromtimestamp(float(x) / 1000.0, tz=datetime.timezone.utc).date().isoformat()
            history.append({"date": d, "value": round(float(p.get("y")), 2)})
        except (TypeError, ValueError):
            continue
    result = {
        "success": True,
        "as_of": fg.get("timestamp") or _now_iso(),
        "value": int(round(float(score))) if score is not None else None,
        "rating": fg.get("rating"),
        "history30d": history,
    }
    _cache_put("fg", result)
    return result


@mcp.tool()
async def fear_greed() -> str:
    """CNN Fear & Greed Index — the headline 0-100 market-sentiment gauge.

    0 = Extreme Fear, 100 = Extreme Greed. Returns the latest value, its
    text rating, and a 30-day daily history for sparklines. Cached 1h.
    """
    try:
        return json.dumps(await _fear_greed())
    except Exception as e:
        log.warning("fear_greed failed: %s", e)
        return json.dumps({"success": False, "error": str(e)})


# ── NAAIM Exposure Index ──────────────────────────────────────────────────────
# The public page renders the ~10 most-recent weekly readings in a table whose
# rows carry id="surveydata-subject"; the 2nd <td> is the mean/average exposure.
# Verified live 2026-06-12.
_NAAIM_ROW = re.compile(
    r'<tr[^>]*id="surveydata-subject"[^>]*>(.*?)</tr>',
    re.I | re.S,
)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.I | re.S)


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def parse_naaim(html: str) -> list[dict]:
    """Pure parser: extract [{date, value}] (newest first) from the NAAIM page.

    Skips the header row (whose 1st cell is the literal 'Date'). Robust to the
    table having extra columns -- only date (cell 0) + mean/average (cell 1).
    """
    out: list[dict] = []
    for row in _NAAIM_ROW.finditer(html):
        cells = [_strip_tags(c) for c in _TD.findall(row.group(1))]
        if len(cells) < 2:
            continue
        date_txt = cells[0]
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", date_txt)
        if not m:
            continue  # header row or non-data row
        mm, dd, yy = (int(g) for g in m.groups())
        try:
            iso = datetime.date(yy, mm, dd).isoformat()
            val = float(cells[1].replace(",", ""))
        except ValueError:
            continue
        out.append({"date": iso, "value": val})
    return out


async def _naaim() -> dict:
    cached = _cache_get("naaim", TTL_NAAIM)
    if cached is not None:
        return cached
    html = await _get_text(NAAIM_URL)
    rows = parse_naaim(html)
    if not rows:
        raise RuntimeError("NAAIM table parse yielded no rows")
    history = rows[:12]  # ~12 most-recent weeks (table shows ~10)
    result = {
        "success": True,
        "as_of": history[0]["date"],
        "exposure": history[0]["value"],
        "history": history,
    }
    _cache_put("naaim", result)
    return result


@mcp.tool()
async def naaim_exposure() -> str:
    """NAAIM Exposure Index — weekly mean equity exposure of active managers.

    Range roughly -200 (leveraged short) to +200 (leveraged long); ~0-100 is
    typical. Returns the latest weekly reading + up to 12 weeks of history.
    Cached 6h.
    """
    try:
        return json.dumps(await _naaim())
    except Exception as e:
        log.warning("naaim_exposure failed: %s", e)
        return json.dumps({"success": False, "error": str(e)})


# ── AAII Investor Sentiment Survey (best-effort, see module docstring) ─────────
def _aaii_plausible(bull: float, neutral: float, bear: float) -> bool:
    """Reject degenerate triplets. The page carries per-STATE breakdowns in JS
    vars (tiny samples like Alaska -> 100/0/0) — a naive first-match grabbed
    Alaska's 100/0/0 in prod (2026-06-12). Historical national extremes stay
    well inside (4, 82) for every component and the three always sum to ~100."""
    vals = (bull, neutral, bear)
    return all(4.0 <= v <= 82.0 for v in vals) and 97.0 <= sum(vals) <= 103.0


def parse_aaii(html: str) -> dict | None:
    """Pure parser: pull the NATIONAL weekly bull/neutral/bear % from the AAII
    public page.

    Returns None when no plausible national triplet is found. AAII frequently
    bot-blocks (403 / stub page) so callers must tolerate a None / error
    result (design ruling #4).
    """
    candidates: list[tuple[int, float, float, float]] = []  # (pos, bull, neutral, bear)

    # Primary: the survey renders as bar charts —
    #   <div class="bar bullish" style="width:41.2%">
    # Group bars into consecutive triplets in document order. The historical-
    # averages block uses the same markup, so candidates are also filtered on
    # nearby context below.
    bars = [
        (m.start(), m.group(1).lower(), float(m.group(2)))
        for m in re.finditer(
            r"class=\"bar (bullish|neutral|bearish)\"\s*style=\"width:\s*([0-9.]+)%",
            html,
            re.I,
        )
    ]
    for i in range(0, len(bars) - 2):
        trio = {label: v for _, label, v in bars[i : i + 3]}
        if set(trio) == {"bullish", "neutral", "bearish"}:
            candidates.append((bars[i][0], trio["bullish"], trio["neutral"], trio["bearish"]))

    # Fallback: label-near-percentage prose (legacy markup).
    pcts: dict[str, tuple[int, float]] = {}
    for label in ("bullish", "neutral", "bearish"):
        m = re.search(
            rf"{label}[^0-9%]{{0,80}}?([0-9]{{1,3}}(?:\.[0-9]+)?)\s*%",
            html,
            re.I | re.S,
        )
        if m:
            try:
                pcts[label] = (m.start(), float(m.group(1)))
            except ValueError:
                pass
    if len(pcts) == 3:
        candidates.append(
            (pcts["bullish"][0], pcts["bullish"][1], pcts["neutral"][1], pcts["bearish"][1])
        )

    for pos, bull, neutral, bear in candidates:
        if not _aaii_plausible(bull, neutral, bear):
            continue
        # Skip the long-run averages block — its heading sits directly above
        # its bars (~120 chars observed). Keep the lookback tight so it does
        # not bleed into a preceding sibling block.
        if re.search(r"(?i)average", html[max(0, pos - 200) : pos]):
            continue
        return {"bull": bull, "neutral": neutral, "bear": bear, "spread": round(bull - bear, 2)}
    return None


async def _aaii() -> dict:
    cached = _cache_get("aaii", TTL_AAII)
    if cached is not None:
        return cached
    html = await _get_text(AAII_URL)
    parsed = parse_aaii(html)
    if not parsed:
        raise RuntimeError("AAII survey structure not found on public page")
    result = {"success": True, "as_of": _now_iso(), **parsed}
    _cache_put("aaii", result)
    return result


@mcp.tool()
async def aaii_survey() -> str:
    """AAII Investor Sentiment Survey — retail bull/neutral/bear percentages.

    BEST-EFFORT: aaii.com is behind aggressive bot protection and frequently
    returns 403/503; on failure this returns {success:false,error:...} and the
    caller should skip it (the project ships the three-source sentiment version
    per design ruling #4). bull/neutral/bear are percentages 0-100; spread =
    bull - bear.
    """
    try:
        return json.dumps(await _aaii())
    except Exception as e:
        log.warning("aaii_survey failed (expected when bot-blocked): %s", e)
        return json.dumps({"success": False, "error": str(e)})


# ── ApeWisdom retail buzz ─────────────────────────────────────────────────────
async def _apewisdom(filter: str, limit: int) -> dict:
    filter = (filter or "all-stocks").strip().strip("/")
    limit = max(1, min(int(limit), 100))
    cache_key = f"ape:{filter}"
    cached = _cache_get(cache_key, TTL_APE)
    if cached is None:
        data = await _get_json(f"{APEWISDOM_BASE}/{filter}/page/1")
        cached = data
        _cache_put(cache_key, data)
    results = cached.get("results") or []
    items = []
    for r in results[:limit]:
        items.append(
            {
                "rank": r.get("rank"),
                "ticker": r.get("ticker"),
                "name": r.get("name"),
                "mentions": r.get("mentions"),
                "upvotes": r.get("upvotes"),
                "mentions_24h_ago": r.get("mentions_24h_ago"),
                "rank_24h_ago": r.get("rank_24h_ago"),
            }
        )
    return {"success": True, "as_of": _now_iso(), "items": items}


@mcp.tool()
async def apewisdom_trending(filter: str = "all-stocks", limit: int = 30) -> str:
    """ApeWisdom retail-mention leaderboard (Reddit WSB + finance subs).

    filter: 'all-stocks' (default), 'wallstreetbets', 'stocks', 'options', etc.
    limit: max rows (1-100). Each item carries current mentions/upvotes/rank and
    the 24h-ago mentions/rank so callers can compute momentum. Cached 15min.
    """
    try:
        return json.dumps(await _apewisdom(filter, limit))
    except Exception as e:
        log.warning("apewisdom_trending failed: %s", e)
        return json.dumps({"success": False, "error": str(e)})


# ── --probe CLI (pre-deploy smoke; no HTTP) ───────────────────────────────────
async def _probe() -> None:
    out: dict[str, Any] = {}
    for name, coro in (
        ("fear_greed", _fear_greed()),
        ("naaim", _naaim()),
        ("aaii", _aaii()),
        ("apewisdom", _apewisdom("all-stocks", 5)),
    ):
        try:
            out[name] = await coro
        except Exception as e:
            out[name] = {"success": False, "error": str(e)}
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    if "--probe" in sys.argv:
        asyncio.run(_probe())
    else:
        log.info("sentiment MCP starting on http://%s:%s/mcp", mcp.settings.host, mcp.settings.port)
        from _mcp_auth import serve  # audit I1: opt-in MCP_SHARED_SECRET bearer gate
        serve(mcp)
