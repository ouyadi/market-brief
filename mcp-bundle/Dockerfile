# Bundle of Python FastMCP servers for OCI Coolify deploy.
# One container, one supervisord, one process per MCP — keeps Coolify
# dashboard tidy and shares the Python runtime / pip cache.
#
# Contains the API-only MCPs that don't need a desktop client:
#   - fred (3035)            St. Louis Fed REST + cache
#   - tradier (3036)         Tradier REST + NDJSON stream → Redis pubsub
#   - polymarket (3033)      Polymarket Gamma REST
#   - stock-price (3032)     yfinance wrapper (quote/info/options)
#   - financialjuice (3034)  FJ RSS / endpoint scraper
#   - sa-rss (7050)          SeekingAlpha public RSS feeds (Phase A SA integration)
#   - sec-edgar (3037)       SEC EDGAR — filings, XBRL facts, full-text search
#   - sec-flows (3038)       SEC ownership flows — Form 4 (insider) + 13F-HR (institutional)
#   - sentiment (3039)       CNN F&G / NAAIM / AAII / ApeWisdom (P1)
#   - shorts (3040)          FINRA short vol/interest + IBKR borrow (P1)
#   - finnhub (3041)         Finnhub company news / rec trends / earnings cal (P1)
#   - defeatbeta (3042)      defeatbeta-api transcripts + financials (P1)
#   - gdelt (3043)           GDELT DOC 2.0 news volume/tone/search (P1)
#
# NOT bundled (kept on Windows desktop): chatlog/wxstore (need WeChat
# client), twitter (Playwright + browser cookies), discord-selfbot
# (datacenter IP = ban risk).

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build tools: yfinance pulls pandas/numpy which need C compilers on slim.
# curl/ca-certificates for healthcheck + outbound HTTPS roots.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for layer cache friendliness.
COPY mcp-bundle/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# Copy the actual MCP source files (kept in market-brief/mcp/).
COPY mcp/ /app/mcp/

# Supervisor config — one process per MCP.
COPY mcp-bundle/supervisord.conf /etc/supervisord.conf

# Document which ports this container exposes. Coolify maps them
# internally via the coolify network so sibling containers can reach
# alphalens-mcp-bundle:3032 etc.
EXPOSE 3032 3033 3034 3035 3036 3037 3038 3039 3040 3041 3042 3043 7050

# Tiny TCP healthcheck against fred (lightest MCP).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS -o /dev/null -X POST -H "Content-Type: application/json" \
      -H "Accept: application/json, text/event-stream" \
      -H "Authorization: Bearer $MCP_SHARED_SECRET" \
      --data-raw '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"healthcheck","version":"1"}}}' \
      http://127.0.0.1:3035/mcp || exit 1

CMD ["supervisord", "-c", "/etc/supervisord.conf"]
