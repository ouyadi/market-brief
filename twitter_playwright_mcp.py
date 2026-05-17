"""
twitter_playwright_mcp.py -- HTTP MCP server (transport=streamable-http).

Uses headless Chromium + the user's own X cookies to fetch tweets the user's
browser would see. Avoids the broken scraper-lib ecosystem (twscrape /
agent-twitter-client) by going through a real browser instead of reverse-
engineering X's GraphQL endpoint.

Tools:
  fetch_tweet_by_url(url)
  fetch_user_tweets(username, limit=10)
  search_tweets(query, limit=10, mode='live'|'top')

Cookies are read once from ~/twitter-mcp/.env at first request. Only the
cookie VALUE is extracted; Domain= in the .env line is decorative because
_read_cookies() always injects them with domain=.x.com.
  TWITTER_COOKIES=["auth_token=...; Domain=.x.com", "ct0=...; ...", "twid=...; ..."]

A single headless Chromium instance is kept alive across requests so we
don't pay 3-5s launch cost per call.

Read-only by design: no like/retweet/follow/post tools exposed even though
cookies could in principle authorize them. Rationale: lowest possible ban
risk on user's main X account.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright, BrowserContext, Page

ENV = Path(os.environ.get("TWITTER_MCP_DIR") or (Path.home() / "twitter-mcp")) / ".env"
LOG_DIR = Path(os.environ.get("TWITTER_MCP_DIR") or (Path.home() / "twitter-mcp")) / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "twitter_mcp.log", encoding="utf-8")],
)
log = logging.getLogger("x-mcp")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

mcp = FastMCP(
    "twitter-playwright",
    host="127.0.0.1",
    port=int(os.environ.get("TWITTER_MCP_PORT", "3031")),
)

_ctx: BrowserContext | None = None
_playwright = None
_browser = None
_ctx_lock = asyncio.Lock()


def _read_cookies() -> list[dict]:
    env = ENV.read_text(encoding="utf-8")
    pairs = []
    for name in ("auth_token", "ct0", "twid"):
        m = re.search(rf"{name}=([^;]+)", env)
        if not m:
            raise RuntimeError(f"{name} not found in {ENV}")
        val = m.group(1).strip()
        pairs.append(
            {
                "name": name,
                "value": val,
                "domain": ".x.com",
                "path": "/",
                "secure": True,
                "httpOnly": name == "auth_token",
            }
        )
    return pairs


async def _ensure_ctx() -> BrowserContext:
    global _ctx, _playwright, _browser
    async with _ctx_lock:
        if _ctx is not None:
            try:
                # Cheap liveness check: open a 1x1 page; if browser is dead this throws.
                if not _browser.is_connected():
                    _ctx = None
            except Exception:
                _ctx = None
        if _ctx is not None:
            return _ctx

        log.info("launching headless Chromium")
        _playwright = await async_playwright().start()
        # IMPORTANT: when this runs from a scheduled task context (TwitterMCP)
        # the python process cannot see files under %LOCALAPPDATA%\ms-playwright
        # (same Defender-quarantine-like symptom we hit with uv-managed Python).
        # Workaround: launch user-installed Chrome from Program Files instead
        # of Playwright's bundled chromium. channel="chrome" tells Playwright
        # to use the system Chrome which lives in a Defender-trusted path.
        _browser = await _playwright.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        _ctx = await _browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 1600},
            locale="en-US",
        )
        await _ctx.add_cookies(_read_cookies())
        log.info("browser context ready (cookies injected)")
    return _ctx


async def _extract_one(art) -> dict:
    """Pull text + time + handle + tweet URL out of an <article> locator."""
    out: dict[str, Any] = {}
    try:
        out["text"] = await art.locator("div[data-testid='tweetText']").first.inner_text(timeout=2500)
    except Exception:
        out["text"] = ""
    try:
        out["posted_at"] = await art.locator("time").first.get_attribute("datetime", timeout=2000)
    except Exception:
        out["posted_at"] = None
    try:
        # User-Name container has an <a> to the user profile
        out["author_handle"] = await art.locator(
            "div[data-testid='User-Name'] a"
        ).first.get_attribute("href", timeout=2000)
    except Exception:
        out["author_handle"] = None
    try:
        tweet_url = await art.locator("time").first.locator("xpath=..").get_attribute(
            "href", timeout=2000
        )
        if tweet_url and not tweet_url.startswith("http"):
            tweet_url = "https://x.com" + tweet_url
        out["tweet_url"] = tweet_url
    except Exception:
        out["tweet_url"] = None
    return out


async def _scroll_for(page: Page, target_count: int, max_scrolls: int = 10) -> None:
    """Scroll the timeline until target_count articles are loaded (or maxed)."""
    for i in range(max_scrolls):
        n = await page.locator("article[data-testid='tweet']").count()
        if n >= target_count:
            return
        await page.evaluate("window.scrollBy(0, 2000)")
        await asyncio.sleep(0.9)


@mcp.tool()
async def fetch_tweet_by_url(url: str) -> dict:
    """
    Fetch a single tweet by its X URL. Use this for X links the user is
    asking about, or to follow up on a URL spotted in market-brief input.

    Example: fetch_tweet_by_url("https://x.com/cnbc/status/1929112233")
    """
    ctx = await _ensure_ctx()
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector("article[data-testid='tweet']", timeout=20000)
        except Exception:
            body = (await page.locator("body").inner_text(timeout=3000))[:300]
            return {"success": False, "url": url, "error": "tweet article not found", "body_hint": body}
        art = page.locator("article[data-testid='tweet']").first
        data = await _extract_one(art)
        data["success"] = True
        data["url"] = url
        log.info("fetch_tweet_by_url: ok %s", url)
        return data
    except Exception as e:
        log.exception("fetch_tweet_by_url failed: %s", url)
        return {"success": False, "url": url, "error": f"{type(e).__name__}: {e}"}
    finally:
        await page.close()


@mcp.tool()
async def fetch_user_tweets(username: str, limit: int = 10) -> dict:
    """
    Fetch the N most recent tweets from a user's profile timeline.
    username: handle without '@' (e.g. 'cnbc', 'elonmusk', 'cathiedwood').
    limit: 1-30 (capped).
    """
    limit = max(1, min(30, limit))
    ctx = await _ensure_ctx()
    page = await ctx.new_page()
    try:
        url = f"https://x.com/{username.lstrip('@')}"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector("article[data-testid='tweet']", timeout=20000)
        except Exception:
            return {"success": False, "username": username, "error": "timeline not found / user not exists"}
        await _scroll_for(page, limit)
        articles = await page.locator("article[data-testid='tweet']").all()
        tweets = [await _extract_one(a) for a in articles[:limit]]
        log.info("fetch_user_tweets: %s -> %d tweets", username, len(tweets))
        return {"success": True, "username": username, "count": len(tweets), "tweets": tweets}
    except Exception as e:
        log.exception("fetch_user_tweets failed: %s", username)
        return {"success": False, "username": username, "error": f"{type(e).__name__}: {e}"}
    finally:
        await page.close()


@mcp.tool()
async def search_tweets(query: str, limit: int = 10, mode: str = "live") -> dict:
    """
    Search X for tweets matching a query. Use for monitoring ticker
    sentiment, macro keywords, etc.

    query: any X search string (supports operators like 'TSLA -is:retweet').
    limit: 1-30.
    mode: 'live' (newest first) or 'top' (most engaged).
    """
    limit = max(1, min(30, limit))
    f = "live" if mode == "live" else "top"
    ctx = await _ensure_ctx()
    page = await ctx.new_page()
    try:
        url = f"https://x.com/search?q={quote(query)}&src=typed_query&f={f}"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector("article[data-testid='tweet']", timeout=20000)
        except Exception:
            return {"success": False, "query": query, "error": "search returned no tweets"}
        await _scroll_for(page, limit)
        articles = await page.locator("article[data-testid='tweet']").all()
        tweets = [await _extract_one(a) for a in articles[:limit]]
        log.info("search_tweets: %r mode=%s -> %d", query, mode, len(tweets))
        return {"success": True, "query": query, "mode": mode, "count": len(tweets), "tweets": tweets}
    except Exception as e:
        log.exception("search_tweets failed: %s", query)
        return {"success": False, "query": query, "error": f"{type(e).__name__}: {e}"}
    finally:
        await page.close()


@mcp.tool()
async def fetch_home_timeline(tab: str = "for_you", limit: int = 15) -> dict:
    """
    Fetch the user's personalized X home timeline. Requires cookies in
    ~/twitter-mcp/.env from a logged-in account (which is what we have).

    tab: 'for_you' (X algo) or 'following' (chrono, only from accounts the user follows).
    limit: 1-30 (capped).

    Returns each tweet with text/posted_at/author_handle/tweet_url just
    like the other fetch_* tools, plus the requested tab in the response.
    """
    limit = max(1, min(30, limit))
    tab = (tab or "for_you").lower().strip()
    if tab not in ("for_you", "following"):
        return {"success": False, "error": f"tab must be 'for_you' or 'following', got {tab!r}"}

    ctx = await _ensure_ctx()
    page = await ctx.new_page()
    try:
        await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector("article[data-testid='tweet']", timeout=20000)
        except Exception:
            return {"success": False, "tab": tab,
                    "error": "home timeline didn't load -- cookies may have expired"}

        if tab == "following":
            # X exposes two role=tab anchors on /home: 'For you' and 'Following'.
            # Try a few selector strategies because the DOM occasionally shifts.
            clicked = False
            for selector in [
                "div[role='tablist'] a[role='tab']",  # 2nd one is Following
                "div[data-testid='ScrollSnap-List'] a[role='tab']",
                "a[role='tab']",
            ]:
                tabs = page.locator(selector)
                count = await tabs.count()
                if count >= 2:
                    try:
                        await tabs.nth(1).click(timeout=4000)
                        clicked = True
                        break
                    except Exception:
                        continue
            if not clicked:
                # Last resort: try get_by_role text-match
                try:
                    await page.get_by_role("tab", name="Following").click(timeout=4000)
                    clicked = True
                except Exception:
                    pass
            if not clicked:
                log.warning("fetch_home_timeline: could not click Following tab")
                return {"success": False, "tab": "following",
                        "error": "could not locate Following tab; X DOM may have changed"}
            await asyncio.sleep(2.5)  # wait for tab switch + content rerender
            # After clicking, the previous articles get replaced; wait for new ones
            try:
                await page.wait_for_selector("article[data-testid='tweet']", timeout=10000)
            except Exception:
                return {"success": False, "tab": "following",
                        "error": "Following tab loaded no tweets"}

        await _scroll_for(page, limit)
        articles = await page.locator("article[data-testid='tweet']").all()
        tweets = [await _extract_one(a) for a in articles[:limit]]
        log.info("fetch_home_timeline tab=%s -> %d tweets", tab, len(tweets))
        return {"success": True, "tab": tab, "count": len(tweets), "tweets": tweets}
    except Exception as e:
        log.exception("fetch_home_timeline failed: tab=%s", tab)
        return {"success": False, "tab": tab, "error": f"{type(e).__name__}: {e}"}
    finally:
        await page.close()


if __name__ == "__main__":
    log.info("twitter-playwright MCP starting on http://127.0.0.1:%s/mcp", mcp.settings.port)
    # Diagnostic: confirm we can see Playwright's bundled chromium dir at all
    # (we don't actually use it -- channel='chrome' uses user-installed Chrome,
    # see _ensure_ctx -- but 0 hits here historically signaled the Defender-
    # quarantine-style sandbox issue that pushed us to channel='chrome').
    import glob as _glob
    if sys.platform == "win32":
        pw_browsers = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    else:  # macOS / Linux
        pw_browsers = Path.home() / "Library" / "Caches" / "ms-playwright"
    cand = _glob.glob(str(pw_browsers / "chromium-*"))
    log.info("playwright bundled chromium dirs visible: %d (%s)", len(cand), pw_browsers)
    mcp.run(transport="streamable-http")
