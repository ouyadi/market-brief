# Bundle of five Python FastMCP servers for OCI Coolify deploy.
# One container, one supervisord, five processes — keeps Coolify dashboard
# tidy and shares the Python runtime / pip cache.
#
# Contains the API-only MCPs that don't need a desktop client:
#   - fred (3035)            St. Louis Fed REST + cache
#   - tradier (3036)         Tradier REST + NDJSON stream → Redis pubsub
#   - polymarket (3033)      Polymarket Gamma REST
#   - stock-price (3032)     yfinance wrapper (quote/info/options)
#   - financialjuice (3034)  FJ RSS / endpoint scraper
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
EXPOSE 3032 3033 3034 3035 3036

# Tiny TCP healthcheck against fred (lightest MCP).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS -o /dev/null -X POST -H "Content-Type: application/json" \
      -H "Accept: application/json, text/event-stream" \
      --data-raw '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"healthcheck","version":"1"}}}' \
      http://127.0.0.1:3035/mcp || exit 1

CMD ["supervisord", "-c", "/etc/supervisord.conf"]
