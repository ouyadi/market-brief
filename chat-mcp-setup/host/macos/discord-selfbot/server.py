"""
Discord self-bot MCP server (read-only).

Runs a discord.py-self client and an MCP HTTP server in the same event loop.
Only read operations are exposed. No send/edit/delete/react tools.

Self-botting is against Discord ToS. Use a burner account.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
import uvicorn
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount


# Load env from ~/.discord-selfbot.env if present, else fall back to process env.
ENV_FILE = Path.home() / ".discord-selfbot.env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)

TOKEN = os.environ.get("DISCORD_USER_TOKEN", "").strip()
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "6280"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
STATUS = os.environ.get("DISCORD_STATUS", "invisible").lower()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("discord-selfbot-mcp")

if not TOKEN:
    log.error("DISCORD_USER_TOKEN missing. Set it in %s or env.", ENV_FILE)
    sys.exit(2)


_status_map = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.dnd,
    "invisible": discord.Status.invisible,
}
DISCORD_STATUS_ENUM = _status_map.get(STATUS, discord.Status.invisible)


# Discord client. We construct it but call .start() inside main().
client = discord.Client(
    chunk_guilds_at_startup=False,
    status=DISCORD_STATUS_ENUM,
    request_guilds=True,
)


@client.event
async def on_ready() -> None:
    log.info(
        "Discord ready as %s (id=%s) — %d guilds, %d private channels",
        client.user,
        client.user.id if client.user else "?",
        len(client.guilds),
        len(client.private_channels),
    )


# ---------- helpers ----------


def _ensure_ready() -> None:
    if client.is_closed():
        raise RuntimeError("discord client is closed")
    if not client.is_ready():
        raise RuntimeError("discord client not ready yet, retry in a few seconds")


def _msg_to_dict(m: discord.Message) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "channel_id": str(m.channel.id),
        "author_id": str(m.author.id),
        "author_name": str(m.author),
        "author_display": getattr(m.author, "display_name", str(m.author)),
        "author_bot": bool(getattr(m.author, "bot", False)),
        "timestamp": m.created_at.astimezone(timezone.utc).isoformat(),
        "edited_at": (
            m.edited_at.astimezone(timezone.utc).isoformat() if m.edited_at else None
        ),
        "content": m.content,
        "attachments": [
            {"filename": a.filename, "url": a.url, "size": a.size}
            for a in m.attachments
        ],
        "embeds_count": len(m.embeds),
        "reactions": [
            {"emoji": str(r.emoji), "count": r.count} for r in m.reactions
        ],
        "reference_id": str(m.reference.message_id) if m.reference and m.reference.message_id else None,
        "pinned": m.pinned,
        "mentions": [str(u.id) for u in m.mentions],
    }


def _can_read(channel: discord.abc.GuildChannel) -> bool:
    if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.ForumChannel, discord.VoiceChannel, discord.StageChannel)):
        return False
    me = channel.guild.me
    if me is None:
        return False
    perms = channel.permissions_for(me)
    return bool(perms.view_channel and perms.read_message_history)


# ---------- MCP server ----------

mcp = FastMCP("discord-selfbot")


@mcp.tool()
async def current_time() -> dict[str, Any]:
    """Current UTC time. Useful for resolving 'today' / 'this week' before calling other tools."""
    now = datetime.now(timezone.utc)
    return {"utc": now.isoformat(), "epoch": int(now.timestamp())}


@mcp.tool()
async def whoami() -> dict[str, Any]:
    """Return the logged-in account info and whether the gateway is ready."""
    if client.user is None:
        return {"ready": False}
    return {
        "ready": client.is_ready(),
        "id": str(client.user.id),
        "name": str(client.user),
        "guild_count": len(client.guilds),
        "dm_count": len(client.private_channels),
    }


@mcp.tool()
async def list_guilds() -> list[dict[str, Any]]:
    """List all servers (guilds) the account has joined."""
    _ensure_ready()
    out = []
    for g in client.guilds:
        out.append({
            "id": str(g.id),
            "name": g.name,
            "owner_id": str(g.owner_id) if g.owner_id else None,
            "member_count": g.member_count,
            "text_channels": len([c for c in g.text_channels if _can_read(c)]),
        })
    return out


@mcp.tool()
async def list_channels(guild_id: str) -> list[dict[str, Any]]:
    """List readable text/forum/thread/voice channels in a guild."""
    _ensure_ready()
    g = client.get_guild(int(guild_id))
    if g is None:
        raise ValueError(f"guild {guild_id} not found or not joined")
    out = []
    for c in g.channels:
        if not _can_read(c):
            continue
        out.append({
            "id": str(c.id),
            "name": c.name,
            "type": c.type.name,
            "category": c.category.name if c.category else None,
            "topic": getattr(c, "topic", None),
        })
    return out


@mcp.tool()
async def read_channel_messages(
    channel_id: str,
    limit: int = 50,
    before_id: str | None = None,
    after_id: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch recent messages from a channel (newest first by default).

    limit: 1-200. before_id / after_id are message IDs for pagination.
    """
    _ensure_ready()
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")
    channel = client.get_channel(int(channel_id))
    if channel is None:
        # Threads aren't always cached — try fetching.
        try:
            channel = await client.fetch_channel(int(channel_id))
        except discord.NotFound:
            raise ValueError(f"channel {channel_id} not found")
    if isinstance(channel, discord.abc.GuildChannel) and not _can_read(channel):
        raise PermissionError(f"no read permission on channel {channel_id}")

    kwargs: dict[str, Any] = {"limit": limit}
    if before_id:
        kwargs["before"] = discord.Object(id=int(before_id))
    if after_id:
        kwargs["after"] = discord.Object(id=int(after_id))
        kwargs["oldest_first"] = True

    msgs: list[dict[str, Any]] = []
    async for m in channel.history(**kwargs):
        msgs.append(_msg_to_dict(m))
    return msgs


@mcp.tool()
async def search_messages(
    guild_id: str,
    query: str,
    channel_id: str | None = None,
    author_id: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Search a guild for messages containing `query` via Discord search API.

    Note: Discord search is eventually consistent — very recent messages may not show up.
    """
    _ensure_ready()
    if limit < 1 or limit > 25:
        raise ValueError("limit must be between 1 and 25 (Discord search cap per page)")
    g = client.get_guild(int(guild_id))
    if g is None:
        raise ValueError(f"guild {guild_id} not found or not joined")

    kwargs: dict[str, Any] = {"limit": limit, "content": query}
    if channel_id:
        ch = g.get_channel(int(channel_id))
        if ch is None:
            raise ValueError(f"channel {channel_id} not found in guild")
        kwargs["channels"] = [ch]
    if author_id:
        kwargs["authors"] = [discord.Object(id=int(author_id))]

    out = []
    async for m in g.search(**kwargs):
        out.append(_msg_to_dict(m))
    return out


@mcp.tool()
async def list_dms() -> list[dict[str, Any]]:
    """List recent DM (private) channels."""
    _ensure_ready()
    out = []
    for c in client.private_channels:
        if isinstance(c, discord.DMChannel):
            other = c.recipient
            out.append({
                "id": str(c.id),
                "type": "dm",
                "user_id": str(other.id) if other else None,
                "user_name": str(other) if other else None,
            })
        elif isinstance(c, discord.GroupChannel):
            out.append({
                "id": str(c.id),
                "type": "group_dm",
                "name": c.name or "",
                "recipients": [str(u) for u in c.recipients],
            })
    return out


@mcp.tool()
async def read_dm_messages(
    user_id: str,
    limit: int = 50,
    before_id: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch recent DM messages with a user."""
    _ensure_ready()
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")
    user = client.get_user(int(user_id))
    if user is None:
        try:
            user = await client.fetch_user(int(user_id))
        except discord.NotFound:
            raise ValueError(f"user {user_id} not found")
    dm = user.dm_channel or await user.create_dm()

    kwargs: dict[str, Any] = {"limit": limit}
    if before_id:
        kwargs["before"] = discord.Object(id=int(before_id))

    msgs: list[dict[str, Any]] = []
    async for m in dm.history(**kwargs):
        msgs.append(_msg_to_dict(m))
    return msgs


@mcp.tool()
async def get_user_info(user_id: str) -> dict[str, Any]:
    """Look up a user's basic public profile."""
    _ensure_ready()
    user = client.get_user(int(user_id))
    if user is None:
        try:
            user = await client.fetch_user(int(user_id))
        except discord.NotFound:
            raise ValueError(f"user {user_id} not found")
    return {
        "id": str(user.id),
        "name": str(user),
        "display": getattr(user, "display_name", str(user)),
        "bot": user.bot,
        "system": user.system,
        "avatar_url": str(user.display_avatar.url) if user.display_avatar else None,
        "created_at": user.created_at.astimezone(timezone.utc).isoformat(),
    }


@mcp.tool()
async def list_guild_members(
    guild_id: str,
    name_query: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List members of a guild. Use `name_query` to filter by username/display name (substring, case-insensitive)."""
    _ensure_ready()
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")
    g = client.get_guild(int(guild_id))
    if g is None:
        raise ValueError(f"guild {guild_id} not found or not joined")

    members_iter = g.members
    if not members_iter:
        members_iter = [m async for m in g.fetch_members(limit=limit)]

    q = name_query.lower() if name_query else None
    out: list[dict[str, Any]] = []
    for m in members_iter:
        if q and q not in str(m).lower() and q not in m.display_name.lower():
            continue
        out.append({
            "id": str(m.id),
            "name": str(m),
            "display": m.display_name,
            "bot": m.bot,
            "joined_at": m.joined_at.astimezone(timezone.utc).isoformat() if m.joined_at else None,
            "roles": [r.name for r in m.roles if r.name != "@everyone"],
        })
        if len(out) >= limit:
            break
    return out


# ---------- runtime ----------


def build_app() -> Starlette:
    # Must call streamable_http_app() before accessing session_manager.
    inner_app = mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with mcp.session_manager.run():
            yield

    return Starlette(
        routes=[Mount("/", app=inner_app)],
        lifespan=lifespan,
    )


async def main() -> None:
    app = build_app()
    config = uvicorn.Config(
        app,
        host=HOST,
        port=PORT,
        log_level=LOG_LEVEL.lower(),
        access_log=False,
    )
    server = uvicorn.Server(config)

    discord_task = asyncio.create_task(client.start(TOKEN), name="discord")
    http_task = asyncio.create_task(server.serve(), name="mcp-http")

    stop = asyncio.Event()

    def _shutdown(*_: Any) -> None:
        log.info("shutdown requested")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _shutdown)

    done_task = asyncio.create_task(stop.wait())
    done, pending = await asyncio.wait(
        {discord_task, http_task, done_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for t in done:
        if t is done_task:
            continue
        exc = t.exception()
        if exc:
            log.error("%s task failed: %r", t.get_name(), exc)

    log.info("stopping discord and http")
    server.should_exit = True
    if not client.is_closed():
        await client.close()
    for t in (discord_task, http_task):
        if not t.done():
            t.cancel()
            with contextlib.suppress(BaseException):
                await t


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
