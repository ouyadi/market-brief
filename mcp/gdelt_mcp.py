"""gdelt_mcp.py -- HTTP MCP server exposing GDELT DOC 2.0 news data.

GDELT monitors global news in ~real time. The DOC 2.0 API gives, for any
free-text query: a TIMELINE of coverage volume (% of all monitored coverage),
a timeline of average TONE, and a list of matching ARTICLES. We use it as a
geopolitical / macro-narrative pulse -- e.g. is "China tariffs" coverage
spiking, and is its tone turning negative.

No auth. GDELT rate-limits aggressively per IP, so we enforce ~1 rps with
single concurrency (a global lock + min-gap). Queries are English.

DESKTOP-RUN BY DESIGN: this server is meant to run on the desktop (residential
IP). 2026-06-12 testing showed GDELT reputation-throttles datacenter IPs (OCI)
regardless of frequency, while the same residential IP recovers from a burst
cooldown within ~5min. The bundle keeps a copy of this process as a roll-back
safety net, but the live GDELT MCP is the desktop one (CF Access, like tme/wsj).

Endpoint: https://api.gdeltproject.org/api/v2/doc/doc
  mode=timelinevol|timelinetone -> {"timeline":[{"series","data":[{date,value}]}]}
  mode=artlist                  -> {"articles":[{url,title,seendate,domain,language,...}]}
  date/seendate format: YYYYMMDDTHHMMSSZ

Tools:
  news_volume(query, timespan="7d")    coverage-volume + tone timeline
  news_search(query, timespan="24h", max=20)   recent matching articles

Run as HTTP MCP on 127.0.0.1:3043/mcp by default. Override port via
GDELT_MCP_PORT env var.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

LOG_DIR = (
    Path(os.environ.get("GDELT_MCP_DIR") or (Path.home() / "gdelt-mcp")) / "logs"
)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "gdelt_mcp.log", encoding="utf-8")],
)
log = logging.getLogger("gdelt")

BASE = "https://api.gdeltproject.org/api/v2/doc/doc"
HTTP_TIMEOUT_S = 25  # GDELT can be slow on wide queries
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

mcp = FastMCP(
    "gdelt",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("GDELT_MCP_PORT", "3043")),
    # Desktop runs behind a Cloudflare tunnel, so requests arrive with
    # Host: gdelt-int.<domain> — the SDK's DNS-rebinding protection rejects
    # that ("Invalid Host header"). CF Access service tokens are the real
    # gate; disable like themarketear_mcp.py does.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

# ── caches + ~1 rps single-concurrency floor ────────────────────────────────
_CACHE: dict[str, tuple[float, Any]] = {}
# TTL_VOL must outlive the consumer's 4h cron interval: GDELT 429s this IP
# erratically (2026-06-12: 200 after 4min quiet, 429 after 27min quiet), so a
# sweep rarely lands all themes in one run. Themes that DID land must not be
# refetched next run — successful results accumulate across runs instead.
TTL_VOL = 5 * 60 * 60   # 5h
TTL_ART = 30 * 60       # 30min
_MIN_GAP_S = 6.0
# 2026-06-12 desktop testing: once GDELT 429s, a 40s-interval retry still 429s
# and the IP self-heals only after ~5min. So we DON'T back off in-process —
# holding the rate lock through a multi-minute sleep wedges the MCP client (the
# P1 smoke russia_ukraine case). Instead a 429 trips a process-level cooldown
# gate: every call inside the window fails fast with the remaining seconds, no
# lock held, no extra requests that would only dig the reputation hole deeper.
_COOLDOWN_S = 300.0
_rate_lock = asyncio.Lock()
_last_call = {"t": 0.0}
_cooldown_until = {"t": 0.0}


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


def _parse_ts(s: Any) -> str | None:
    """GDELT timestamps are YYYYMMDDTHHMMSSZ (or YYYYMMDDHHMMSS). -> ISO8601."""
    if not s:
        return None
    raw = str(s).strip().replace("Z", "").replace("T", "")
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d"):
        try:
            dt = datetime.datetime.strptime(raw, fmt).replace(tzinfo=datetime.timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue
    return str(s)


def _check_cooldown() -> None:
    """Fail fast if a prior 429 tripped the cooldown gate. Checked both before
    acquiring the rate lock (so blocked calls never queue behind it) and again
    after, in case the window opened while we were waiting on the lock."""
    left = _cooldown_until["t"] - time.time()
    if left > 0:
        raise RuntimeError(f"GDELT cooldown {int(left)}s left")


async def _fetch(params: dict) -> Any:
    """GET the GDELT DOC API with ~1 rps single-concurrency pacing. GDELT
    sometimes returns non-JSON error text -> raise a clear error. A 429 trips
    a process-level cooldown (no in-process retry/backoff)."""
    _check_cooldown()
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
    async with _rate_lock:
        _check_cooldown()
        gap = time.time() - _last_call["t"]
        if gap < _MIN_GAP_S:
            await asyncio.sleep(_MIN_GAP_S - gap)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(BASE, params=params, headers={"User-Agent": UA}) as r:
                text = await r.text()
                _last_call["t"] = time.time()
                if r.status == 429:
                    # Trip the cooldown and bail immediately — no backoff. 40s
                    # retries still 429; the IP self-heals after ~5min.
                    _cooldown_until["t"] = time.time() + _COOLDOWN_S
                    raise RuntimeError(f"GDELT cooldown {int(_COOLDOWN_S)}s left")
                if r.status != 200:
                    raise RuntimeError(f"GDELT -> {r.status} {text[:160]}")
                stripped = text.strip()
                if not stripped:
                    return {}
                if not stripped.startswith("{"):
                    # GDELT returns plain-text errors (e.g. malformed query) as 200
                    raise RuntimeError(f"GDELT non-JSON: {stripped[:160]}")
                return json.loads(stripped)


def _timeline_points(payload: dict, value_key: str) -> list[dict]:
    """Flatten the first timeline series into [{ts, <value_key>}]."""
    tl = payload.get("timeline") or []
    if not tl:
        return []
    data = tl[0].get("data") or []
    out = []
    for p in data:
        out.append({"ts": _parse_ts(p.get("date")), value_key: p.get("value")})
    return out


# ── news_volume ───────────────────────────────────────────────────────────────
async def _news_volume(query: str, timespan: str) -> dict:
    query = (query or "").strip()
    timespan = (timespan or "7d").strip()
    cache_key = f"vol:{query}:{timespan}"
    cached = _cache_get(cache_key, TTL_VOL)
    if cached is not None:
        return cached
    common = {"query": query, "format": "json", "timespan": timespan}
    vol = await _fetch({**common, "mode": "timelinevol"})
    # Tone is decorative (volume is the signal) — best-effort. A tone 429 still
    # arms the global cooldown (protecting later calls) but must not discard
    # the volume data we already paid a request for.
    try:
        tone = await _fetch({**common, "mode": "timelinetone"})
    except Exception as e:
        log.warning("timelinetone %r failed (kept vol): %s", query, e)
        tone = {}
    result = {
        "success": True,
        "query": query,
        "timespan": timespan,
        "points": [{"ts": p["ts"], "vol_pct": p["vol_pct"]} for p in _timeline_points(vol, "vol_pct")],
        "tone_points": [{"ts": p["ts"], "tone": p["tone"]} for p in _timeline_points(tone, "tone")],
    }
    _cache_put(cache_key, result)
    return result


@mcp.tool()
async def news_volume(query: str, timespan: str = "7d") -> str:
    """GDELT coverage-volume + tone timeline for a news query.

    query: English free-text (phrases in quotes, (a OR b), -exclude, theme:/
    domain: operators). timespan: '15min'..'3m' (e.g. '24h','7d','1m'). Returns
    points:[{ts, vol_pct}] (coverage as % of all monitored news) and
    tone_points:[{ts, tone}] (avg tone; negative = more negative coverage).
    """
    try:
        return json.dumps(await _news_volume(query, timespan))
    except Exception as e:
        log.warning("news_volume %r failed: %s", query, e)
        return json.dumps({"success": False, "query": query, "error": str(e)})


# ── news_search ───────────────────────────────────────────────────────────────
async def _news_search(query: str, timespan: str, max_records: int) -> dict:
    query = (query or "").strip()
    timespan = (timespan or "24h").strip()
    max_records = max(1, min(int(max_records), 250))
    cache_key = f"art:{query}:{timespan}:{max_records}"
    cached = _cache_get(cache_key, TTL_ART)
    if cached is not None:
        return cached
    payload = await _fetch(
        {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "timespan": timespan,
            "maxrecords": max_records,
            "sort": "datedesc",
        }
    )
    items = []
    for a in payload.get("articles") or []:
        items.append(
            {
                "ts": _parse_ts(a.get("seendate")),
                "title": a.get("title"),
                "url": a.get("url"),
                "domain": a.get("domain"),
                "lang": a.get("language"),
            }
        )
    result = {"success": True, "query": query, "items": items}
    _cache_put(cache_key, result)
    return result


@mcp.tool()
async def news_search(query: str, timespan: str = "24h", max: int = 20) -> str:
    """GDELT recent-article search for a query.

    query: English free-text. timespan: e.g. '24h','7d'. max: 1-250 (default 20),
    newest first. Each item: {ts (ISO8601), title, url, domain, lang}.
    """
    try:
        return json.dumps(await _news_search(query, timespan, max))
    except Exception as e:
        log.warning("news_search %r failed: %s", query, e)
        return json.dumps({"success": False, "query": query, "error": str(e)})


# ── --probe CLI ────────────────────────────────────────────────────────────
async def _probe(args: list[str]) -> None:
    # Pacing state (_last_call) is per-process: a --probe runs in its own
    # process and can't see the resident service's last call, so it might fire
    # <5s after it (or after a previous probe). This sleep is the only place
    # that could otherwise violate GDELT's ~1-per-5s rule from a fresh process.
    await asyncio.sleep(_MIN_GAP_S)
    query = args[0] if args else "Federal Reserve interest rate"
    out: dict[str, Any] = {}
    try:
        v = await _news_volume(query, "7d")
        out["news_volume"] = {
            "success": v.get("success"),
            "points": len(v.get("points", [])),
            "tone_points": len(v.get("tone_points", [])),
            "last_point": v.get("points", [None])[-1] if v.get("points") else None,
        }
    except Exception as e:
        out["news_volume"] = {"success": False, "error": str(e)}
    try:
        s = await _news_search(query, "24h", 5)
        out["news_search"] = {
            "success": s.get("success"),
            "items": len(s.get("items", [])),
            "first": s.get("items", [None])[0] if s.get("items") else None,
        }
    except Exception as e:
        out["news_search"] = {"success": False, "error": str(e)}
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    if "--probe" in sys.argv:
        rest = [a for a in sys.argv[1:] if a != "--probe"]
        asyncio.run(_probe(rest))
    else:
        log.info("gdelt MCP starting on http://%s:%s/mcp", mcp.settings.host, mcp.settings.port)
        from _mcp_auth import serve  # audit I1: opt-in MCP_SHARED_SECRET bearer gate
        serve(mcp)
