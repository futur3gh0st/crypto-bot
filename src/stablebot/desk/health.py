"""Venue reachability probe.

A sleeve that cannot reach its venue is not "quiet", it is broken, and the two
look identical on a scan table full of dashes. The desk probes each venue once
at startup and says which ones answered, so an unreachable venue is a labelled
condition rather than a mystery.

The probe also recognises the specific failure where a hostname resolves to a
filtering appliance instead of the venue — a DNS-level block — because that
presents as a TLS hostname mismatch and reads like a bug in the bot when it is
not one.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
from dataclasses import dataclass

import httpx

from stablebot.exchanges.base import USER_AGENT
from stablebot.kalshi.client import HOST as KALSHI_HOST
from stablebot.poly.client import CLOB, GAMMA
from stablebot.desk.reference import VISION

# Probe the exact hosts the clients trade against. These are imported rather
# than retyped because the two drifted once: the probe hit
# api.elections.kalshi.com, which is a CloudFront edge (AWS GLOBAL), while
# orders went to external-api.kalshi.com in us-east-2. A CDN edge answers from
# near the caller, so the probe could report a healthy venue while the endpoint
# that actually matters was slow or unreachable.
PROBES: dict[str, tuple[str, str]] = {
    # venue -> (label, probe url)
    "binance": ("Binance spot", f"{VISION}/api/v3/ping"),
    "poly": ("Polymarket gamma", f"{GAMMA}/events?slug=probe"),
    "poly_clob": ("Polymarket CLOB", f"{CLOB}/ok"),
    "kalshi": ("Kalshi", f"{KALSHI_HOST}/exchange/status"),
}

# Which venues each sleeve actually needs to function.
SLEEVE_VENUES: dict[str, tuple[str, ...]] = {
    "spot_lag": ("binance", "poly", "poly_clob"),
    "poly_lock": ("poly", "poly_clob"),
    "kalshi_lock": ("kalshi",),
    "kalshi_lag": ("binance", "kalshi"),
}


@dataclass
class VenueHealth:
    venue: str
    label: str
    ok: bool
    status: str            # "ok" | "blocked" | "dns" | "tls" | "http" | "error"
    detail: str = ""
    resolved: str = ""
    latency_ms: float | None = None

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"


def _resolve(host: str) -> str:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return ", ".join(sorted({str(i[4][0]) for i in infos}))
    except OSError as exc:
        return f"unresolved ({exc.strerror or exc})"


def _base_domain(host: str) -> str:
    parts = host.rsplit(".", 2)
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def canonical_name(host: str) -> str | None:
    """The name the resolver actually lands on, following CNAMEs.

    When a hostname is redirected at the DNS layer, this is where it really
    goes — which is far more legible than a TLS hostname-mismatch traceback.
    """
    try:
        canon, _aliases, _ips = socket.gethostbyname_ex(host)
    except OSError:
        return None
    canon = canon.rstrip(".")
    if canon and _base_domain(canon) != _base_domain(host):
        return canon
    return None


def _peer_name(host: str, timeout: float = 6.0) -> str | None:
    """Whose certificate is actually being served for this hostname?"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
                if not cert:
                    # CERT_NONE gives a dict only on some builds; fall back to DER
                    der = tls.getpeercert(binary_form=True)
                    return "unknown" if der else None
                subject = dict(x[0] for x in cert.get("subject", ()) if x)
                return subject.get("commonName")
    except OSError:
        return None


async def probe_one(venue: str, timeout: float = 8.0) -> VenueHealth:
    label, url = PROBES[venue]
    host = httpx.URL(url).host
    loop = asyncio.get_running_loop()
    resolved = await loop.run_in_executor(None, _resolve, host)

    started = loop.time()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        ) as http:
            r = await http.get(url)
        ms = (loop.time() - started) * 1000
        # any answer at all means the route is open; 4xx on a probe slug is fine
        return VenueHealth(
            venue, label, True, "ok", f"HTTP {r.status_code}", resolved, ms
        )
    except httpx.ConnectError as exc:
        msg = str(exc)
        if "CERTIFICATE_VERIFY_FAILED" in msg or "Hostname mismatch" in msg:
            canon = await loop.run_in_executor(None, canonical_name, host)
            if canon is None:
                served = await loop.run_in_executor(None, _peer_name, host)
                canon = served if served and served != "unknown" else None
            if canon:
                return VenueHealth(
                    venue,
                    label,
                    False,
                    "blocked",
                    f"resolves to {canon} — requests are not reaching the venue",
                    resolved,
                )
            return VenueHealth(venue, label, False, "tls", msg[:120], resolved)
        return VenueHealth(venue, label, False, "error", f"{type(exc).__name__}: {msg[:100]}", resolved)
    except Exception as exc:  # noqa: BLE001
        return VenueHealth(
            venue, label, False, "error", f"{type(exc).__name__}: {str(exc)[:100]}", resolved
        )


async def probe_all(venues: list[str] | None = None) -> dict[str, VenueHealth]:
    want = venues or list(PROBES)
    results = await asyncio.gather(*(probe_one(v) for v in want), return_exceptions=True)
    out: dict[str, VenueHealth] = {}
    for venue, res in zip(want, results, strict=True):   # gather: one result per input
        if isinstance(res, BaseException):
            label = PROBES[venue][0]
            out[venue] = VenueHealth(venue, label, False, "error", str(res)[:100])
        else:
            out[venue] = res
    return out


def sleeve_blockers(sleeve: str, health: dict[str, VenueHealth]) -> list[VenueHealth]:
    """Which venues this sleeve needs that are not answering."""
    return [
        health[v]
        for v in SLEEVE_VENUES.get(sleeve, ())
        if v in health and not health[v].ok
    ]
