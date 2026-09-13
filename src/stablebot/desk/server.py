"""A very small HTTP endpoint that publishes DeskState as JSON.

No framework: the desk already owns an asyncio loop, and one read-only route
does not justify a dependency. The daemon runs the money; this only hands out a
snapshot for a remote terminal to draw.

Binding: loopback by default, because the snapshot carries positions, equity and
P&L. Reach it from a laptop with an SSH tunnel:

    ssh -N -L 8787:127.0.0.1:8787 you@desk-host
    stablebot desk --remote http://127.0.0.1:8787

Binding to any other interface publishes that data to the network, so it is
refused unless STABLEBOT_DESK_TOKEN is set, and then every request must carry
`Authorization: Bearer <token>`.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from typing import Any, Callable

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
TOKEN_ENV = "STABLEBOT_DESK_TOKEN"
MAX_REQUEST_BYTES = 16384


class ExposureRefused(RuntimeError):
    """Binding off-loopback without a token would publish the book."""


def _response(status: str, body: bytes, ctype: str = "application/json") -> bytes:
    return (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {ctype}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body


def parse_request(raw: bytes) -> tuple[str, str, dict[str, str]]:
    """-> (method, path, headers). Raises ValueError on anything malformed."""
    head = raw.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = head.split("\r\n")
    if not lines or len(lines[0].split()) < 2:
        raise ValueError("bad request line")
    method, path = lines[0].split()[:2]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return method.upper(), path, headers


def _authorised(headers: dict[str, str], token: str | None) -> bool:
    if not token:
        return True
    # compare_digest, not ==: a plain compare leaks the token prefix by timing.
    return hmac.compare_digest(headers.get("authorization", ""), f"Bearer {token}")


async def serve_state(
    snapshot: Callable[[], dict[str, Any]],
    host: str = "127.0.0.1",
    port: int = 8787,
) -> asyncio.AbstractServer:
    """Serve `snapshot()` at GET /state. Returns the server; caller closes it."""
    token = os.environ.get(TOKEN_ENV) or None
    if host not in LOOPBACK and not token:
        raise ExposureRefused(
            f"refusing to bind {host}: the state feed carries positions and P&L. "
            f"Bind 127.0.0.1 and use an SSH tunnel, or set {TOKEN_ENV}."
        )

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                raw = await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"), timeout=5.0
                )
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
                writer.write(_response("400 Bad Request", b'{"error":"bad request"}'))
                await writer.drain()
                return
            if not raw:
                return
            try:
                method, path, headers = parse_request(raw)
            except ValueError:
                writer.write(_response("400 Bad Request", b'{"error":"bad request"}'))
                return
            if method not in {"GET", "HEAD"}:
                writer.write(_response("405 Method Not Allowed", b'{"error":"GET only"}'))
                return
            route = path.split("?", 1)[0].rstrip("/") or "/"
            # /health carries no book data and must stay reachable without the
            # token: the container HEALTHCHECK cannot send one, and gating it
            # marks the desk permanently unhealthy.
            if route in {"/", "/health"}:
                writer.write(_response("200 OK", b'{"ok":true}'))
                await writer.drain()
                return
            if not _authorised(headers, token):
                writer.write(_response("401 Unauthorized", b'{"error":"bad token"}'))
                return
            if route == "/state":
                body = json.dumps(snapshot()).encode()
                writer.write(_response("200 OK", b"" if method == "HEAD" else body))
            else:
                writer.write(_response("404 Not Found", b'{"error":"no such route"}'))
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()

    # limit= caps the read buffer; readuntil then raises LimitOverrunError,
    # which the handler already turns into a 400. Without it the constant was
    # decorative and asyncio's 64KB default applied.
    return await asyncio.start_server(handle, host, port, limit=MAX_REQUEST_BYTES)
