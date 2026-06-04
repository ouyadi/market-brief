"""Shared inbound auth for the bundled FastMCP servers (audit finding I1).

The OCI mcp-bundle binds every server to 0.0.0.0 so sibling Coolify containers
(alphalens) can reach them, but the servers shipped with no inbound auth — any
host on the network could invoke every tool / use them as an outbound fetch
proxy. serve() adds a shared-secret bearer gate that is OPT-IN:

  * MCP_SHARED_SECRET set   -> a valid `Authorization: Bearer <secret>` header is
                              required on every HTTP request (401 otherwise).
  * MCP_SHARED_SECRET unset -> serve() is equivalent to
                              mcp.run(transport="streamable-http"), so nothing
                              changes until the secret is configured on BOTH the
                              bundle and the alphalens MCP client.

A pure-ASGI middleware is used (NOT starlette BaseHTTPMiddleware, which buffers
the streamable-http / SSE responses and would break MCP streaming).
"""
from __future__ import annotations

import hmac
import os


class _BearerGate:
    def __init__(self, app, secret: str) -> None:
        self.app = app
        self._secret = secret

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"authorization", b"").decode("latin-1")
        token = raw[7:].strip() if raw[:7].lower() == "bearer " else ""
        if not (token and hmac.compare_digest(token, self._secret)):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        await self.app(scope, receive, send)


def serve(mcp) -> None:
    """Run a FastMCP server over streamable-http, gated by MCP_SHARED_SECRET
    when it is set (see module docstring)."""
    secret = (os.environ.get("MCP_SHARED_SECRET") or "").strip()
    if not secret:
        mcp.run(transport="streamable-http")
        return
    import uvicorn

    app = mcp.streamable_http_app()
    app.add_middleware(_BearerGate, secret=secret)
    uvicorn.run(
        app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=(getattr(mcp.settings, "log_level", "info") or "info").lower(),
    )
