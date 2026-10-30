"""Shared, bounded aiohttp session for Bilibili API and media requests.

The session is created lazily on the active AstrBot event loop and explicitly
closed during plugin termination. Per-request timeouts may override the default.
"""
import asyncio

import aiohttp

_SESSION: aiohttp.ClientSession | None = None
_SESSION_LOOP = None


def get_session() -> aiohttp.ClientSession:
    global _SESSION, _SESSION_LOOP
    loop = asyncio.get_running_loop()
    if _SESSION is not None and not _SESSION.closed:
        if _SESSION_LOOP is not loop:
            raise RuntimeError("Bilibili HTTP session belongs to another event loop; close it before reuse")
        return _SESSION
    connector = aiohttp.TCPConnector(
        limit=16,
        limit_per_host=8,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
    )
    _SESSION = aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=30),
        raise_for_status=False,
    )
    _SESSION_LOOP = loop
    return _SESSION


async def close_session() -> None:
    global _SESSION, _SESSION_LOOP
    session = _SESSION
    _SESSION = None
    _SESSION_LOOP = None
    if session is not None and not session.closed:
        await session.close()
