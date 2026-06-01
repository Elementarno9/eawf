"""UDS egress proxy: the enforcement seam for the default-deny policy.

A spawned agent reaches the network ONLY through this proxy. It binds a
Unix-domain-socket OUTSIDE the sandbox; the FS jail (W04) wires the child
to that socket so the child has no direct outbound path. Per connection,
the proxy reads the requested target ``host:port``, runs the pure
:func:`eawf.runtime.sandbox.egress.classify_egress` policy, and:

- on ALLOW, opens an outbound TCP connection to the target and pipes
  bytes both ways until either side closes;
- on DENY, writes a one-line refusal and closes WITHOUT opening any
  outbound connection -- a denied host never touches the network.

Wire framing (minimal, documented -- this is an enforcement seam, NOT a
full RFC1928 SOCKS5 stack): the child sends a single request line

    ``CONNECT <host>:<port>\\n``

The proxy replies with one status line before any piping:

    ``OK\\n``       -- allowed; the byte stream that follows is tunnelled
    ``DENY <reason>\\n`` -- refused; the connection then closes.

Keeping the framing this thin is deliberate (KISS): the security property
lives entirely in :func:`classify_egress`, and the proxy is a byte-piping
enforcer gated by that decision. TLS is end-to-end between the child and
the upstream host; the proxy never intercepts or terminates it.

Windows gap (honest, decision-ready -- it does NOT block the build):
Windows has no per-process egress-proxy parity, because a firewall rule
cannot be matched to a non-principal synthetic SID. We do NOT attempt a
Windows proxy. Callers branch on :func:`egress_proxy_supported` (``False``
on Windows) or catch :class:`EgressUnavailableOnWindowsError`; the
operator-facing Windows policy is env-scrub + ACL write-scope +
offline-by-default, or WSL2. See the Platform matrix in
``.ea/local/research/2026-05-30-safety-floor.md``.

Authoritative spec: ``.ea/local/research/2026-05-30-safety-floor.md``
(section "The floor: env-scrub + egress proxy" + "Platform matrix").
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from eawf.runtime.sandbox.egress import EgressDecision, classify_egress

logger = logging.getLogger(__name__)

#: The request verb the child sends. Anything else is a malformed request
#: and is refused without opening outbound.
_CONNECT_VERB: str = "CONNECT"

#: Status lines the proxy writes back before tunnelling / closing.
_STATUS_OK: bytes = b"OK\n"

#: Socket-directory permission bits: owner-only (rwx------). The proxy
#: refuses to bind under a directory more permissive than this so a peer
#: principal cannot connect to the enforcement socket.
_SOCKET_DIR_MODE: int = 0o700

#: Type of the injectable outbound-connect. Production passes
#: :func:`asyncio.open_connection`; tests pass a fake that records the
#: target and never touches the network.
OutboundConnector = Callable[
    [str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
]


class SandboxError(RuntimeError):
    """Base error for runtime-sandbox enforcement failures."""


class EgressUnavailableOnWindowsError(SandboxError):
    """Raised when egress-proxy enforcement is requested on Windows.

    Windows cannot match a firewall rule to a non-principal synthetic
    SID, so there is no per-process egress-proxy parity. Callers that
    cannot branch on :func:`egress_proxy_supported` catch this to fall
    back to the env-scrub + ACL + offline-by-default policy (or WSL2).
    """


def egress_proxy_supported() -> bool:
    """Return ``True`` when the egress proxy can run on this platform.

    POSIX (Linux / macOS) supports the UDS proxy; Windows does not (no
    per-process firewall-rule-to-synthetic-SID binding). The daemon checks
    this before standing the proxy up so a Windows host degrades to the
    documented offline-default policy instead of crashing.

    Returns:
        ``True`` on POSIX, ``False`` on Windows.
    """
    return os.name != "nt" and sys.platform != "win32"


def _decode_request(line: bytes) -> tuple[str, int] | None:
    """Parse one ``CONNECT host:port`` request line.

    Args:
        line: The raw request line (without the trailing newline).

    Returns:
        The ``(host, port)`` pair, or ``None`` when the line is malformed
        (wrong verb, missing ``host:port``, or a non-integer port). A
        ``None`` here is refused by the caller WITHOUT opening outbound;
        the host string itself is still handed to the policy when present
        so the canonicalization guard logs it.
    """
    try:
        text = line.decode("ascii")
    except UnicodeDecodeError:
        return None
    parts = text.strip().split(" ", 1)
    if len(parts) != 2 or parts[0] != _CONNECT_VERB:
        return None
    target = parts[1]
    host, sep, port_str = target.rpartition(":")
    if not sep or not host:
        return None
    try:
        port = int(port_str)
    except ValueError:
        return None
    return host, port


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    """Pump bytes from *src* to *dst* until EOF, then close the writer.

    One direction of the bidirectional tunnel. Exits on clean EOF or a
    transport error (the peer closed); the paired direction is cancelled
    by :func:`_tunnel` once either side finishes.
    """
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except ConnectionError, asyncio.IncompleteReadError:
        pass
    finally:
        if not dst.is_closing():
            dst.close()


async def _tunnel(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    """Run both pipe directions until either side closes."""
    await asyncio.gather(
        _pipe(client_reader, upstream_writer),
        _pipe(upstream_reader, client_writer),
    )


async def handle_egress_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    lane: str,
    gate_phase: bool = False,
    extra_allow: frozenset[str] = frozenset(),
    connector: OutboundConnector | None = None,
) -> EgressDecision:
    """Serve one child connection: read the request, enforce, tunnel/deny.

    Reads the ``CONNECT host:port`` line, runs the policy, and either
    opens an outbound connection and tunnels, or writes ``DENY <reason>``
    and closes. A denied (or malformed) request NEVER calls *connector*,
    so a refused host cannot reach the network.

    Args:
        reader: The client (child) read stream.
        writer: The client (child) write stream.
        lane: The auth lane (``"claude"`` / ``"codex"``) for the policy.
        gate_phase: Threaded into the policy -- opens the gate/push set.
        extra_allow: Extra exact hostnames for the policy under
            ``gate_phase`` (typically the git-remote host).
        connector: The outbound-connect coroutine. Defaults to
            :func:`asyncio.open_connection`; tests inject a fake so no
            real network is touched.

    Returns:
        The :class:`EgressDecision` the request produced -- returned so the
        server (and tests) can observe the verdict without scraping logs.
    """
    open_conn = connector if connector is not None else asyncio.open_connection

    line = await reader.readline()
    parsed = _decode_request(line)
    if parsed is None:
        # Malformed framing -- still feed a best-effort host to the policy
        # so the log names what was attempted, then refuse.
        decision = EgressDecision(allowed=False, host="", reason="malformed-request")
        writer.write(f"DENY {decision.reason}\n".encode("ascii"))
        await writer.drain()
        writer.close()
        logger.warning(f"handle_egress_connection lane={lane!r} reason={decision.reason}")
        return decision

    host, port = parsed
    decision = classify_egress(
        host,
        lane=lane,
        gate_phase=gate_phase,
        extra_allow=extra_allow,
    )

    if not decision.allowed:
        writer.write(f"DENY {decision.reason}\n".encode("ascii"))
        await writer.drain()
        writer.close()
        return decision

    upstream_reader, upstream_writer = await open_conn(decision.host, port)
    writer.write(_STATUS_OK)
    await writer.drain()
    logger.info(
        f"handle_egress_connection lane={lane!r} host={decision.host!r} port={port} tunnelling=1"
    )
    await _tunnel(reader, writer, upstream_reader, upstream_writer)
    return decision


async def start_egress_proxy(
    socket_path: Path,
    *,
    lane: str,
    gate_phase: bool = False,
    extra_allow: frozenset[str] = frozenset(),
    connector: OutboundConnector | None = None,
) -> asyncio.Server:
    """Bind the UDS egress proxy at *socket_path* and start serving.

    The socket lives under the runtime / a temp dir (the caller supplies
    the path -- no machine path is hardcoded). Its parent directory must
    already exist with owner-only (``0700``) permissions; the proxy
    refuses to bind otherwise so a peer principal cannot reach the
    enforcement socket.

    Args:
        socket_path: The UDS path to bind. Its parent must be a ``0700``
            directory.
        lane: The auth lane (``"claude"`` / ``"codex"``) every connection
            is classified under.
        gate_phase: Threaded into the policy for every connection.
        extra_allow: Extra exact hostnames threaded into the policy under
            ``gate_phase``.
        connector: The outbound-connect coroutine each connection uses.
            Defaults to :func:`asyncio.open_connection`.

    Returns:
        The started :class:`asyncio.Server`; the caller owns its lifetime
        (``async with server`` or ``server.close()`` at teardown).

    Raises:
        EgressUnavailableOnWindowsError: When called on Windows -- there is no
            UDS egress-proxy parity there.
        PermissionError: When the parent directory is more permissive than
            ``0700`` (group/other bits set).
    """
    if not egress_proxy_supported():
        raise EgressUnavailableOnWindowsError(
            "egress proxy unavailable on windows; use env-scrub + acl + offline-default or wsl2"
        )

    socket_dir = socket_path.parent
    mode = socket_dir.stat().st_mode & 0o777
    if mode & ~_SOCKET_DIR_MODE:
        raise PermissionError(
            f"egress socket dir {socket_dir} mode {mode:#o} more permissive "
            f"than {_SOCKET_DIR_MODE:#o}"
        )

    if socket_path.exists():
        socket_path.unlink()

    async def _on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_egress_connection(
            reader,
            writer,
            lane=lane,
            gate_phase=gate_phase,
            extra_allow=extra_allow,
            connector=connector,
        )

    server = await asyncio.start_unix_server(_on_connect, path=os.fspath(socket_path))
    os.chmod(socket_path, _SOCKET_DIR_MODE)
    logger.info(f"start_egress_proxy socket={socket_path!s} lane={lane!r} gate_phase={gate_phase}")
    return server


__all__ = [
    "EgressUnavailableOnWindowsError",
    "OutboundConnector",
    "SandboxError",
    "egress_proxy_supported",
    "handle_egress_connection",
    "start_egress_proxy",
]
