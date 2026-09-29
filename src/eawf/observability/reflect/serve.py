"""Serve a report root to a local browser: loopback, read-only, nonce-bound, confined.

The listening socket is a surface of its own, not a daemon: it carries no RPC authority
and lives exactly as long as the invocation that started it. Four guards hold it there.

- It binds a loopback address or refuses to start.
- It serves only a root inside the declared report path, or refuses to start.
- Every URL carries the nonce minted for this invocation; a request naming any other
  nonce -- a stale link from an earlier invocation included -- is refused.
- A request path resolves inside the served root or is refused, so ``..`` and a
  symlink out of the root reach nothing. Only ``GET`` and ``HEAD`` are answered: no
  request can change a byte.
"""

from __future__ import annotations

import ipaddress
import logging
import mimetypes
import secrets
import socket
from dataclasses import dataclass
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

#: The address a server binds when the operator names none.
DEFAULT_HOST: Final = "127.0.0.1"


class ServeRefusedError(ValueError):
    """The server was asked to bind off loopback or to serve outside the report path."""


def require_loopback(host: str) -> str:
    """Return ``host`` when it is a loopback address.

    Raises:
        ServeRefusedError: ``host`` is not an IP literal, or is one off loopback. A
            hostname is refused rather than resolved, because what it resolves to can
            change between the check and the bind.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ServeRefusedError(f"{host!r} is not a loopback address literal") from exc
    if not address.is_loopback:
        raise ServeRefusedError(f"{host} is not a loopback interface; a report is served locally")
    return host


def require_confined_root(root: Path, report_path: Path) -> Path:
    """Return ``root`` resolved when it lies inside the declared report path.

    Raises:
        ServeRefusedError: ``root`` resolves outside ``report_path`` or is not a directory.
    """
    resolved = root.resolve()
    if not resolved.is_relative_to(report_path.resolve()):
        raise ServeRefusedError(f"{root} is outside the declared report path")
    if not resolved.is_dir():
        raise ServeRefusedError(f"{root} is not a directory of reports")
    return resolved


class _ReportHandler(BaseHTTPRequestHandler):
    """Answer ``GET`` and ``HEAD`` for files under one root, behind one nonce."""

    server_version = "eawf-reflect"

    def __init__(self, *args: object, root: Path, nonce: str) -> None:
        self._root = root
        self._nonce = nonce
        super().__init__(*args)  # type: ignore[arg-type]

    def log_message(self, format: str, *args: object) -> None:
        """Route the access log to the module logger instead of stderr."""
        fields = " ".join(str(item) for item in args)
        logger.debug(f"log_message client={self.address_string()} fields={fields!r}")

    def _resolve(self) -> Path | None:
        """Return the file the request names, or ``None`` after answering a refusal."""
        parts = unquote(urlsplit(self.path).path).lstrip("/").split("/", 1)
        if parts[0] != self._nonce:
            self.send_error(HTTPStatus.FORBIDDEN, "stale or missing nonce")
            return None
        relative = parts[1] if len(parts) > 1 and parts[1] else "index.html"
        target = (self._root / relative).resolve()
        if not target.is_relative_to(self._root):
            self.send_error(HTTPStatus.FORBIDDEN, "outside the report root")
            return None
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return None
        return target

    def _answer(self, *, body: bool) -> None:
        """Send the named file's headers, and its bytes when ``body`` is set."""
        target = self._resolve()
        if target is None:
            return
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(data)

    def do_GET(self) -> None:
        """Answer a read."""
        self._answer(body=True)

    def do_HEAD(self) -> None:
        """Answer a read of the headers alone."""
        self._answer(body=False)

    def _refuse_write(self) -> None:
        """Refuse a method that could change a byte."""
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "the report server is read-only")

    def do_POST(self) -> None:
        """Refuse a write."""
        self._refuse_write()

    def do_PUT(self) -> None:
        """Refuse a write."""
        self._refuse_write()

    def do_DELETE(self) -> None:
        """Refuse a write."""
        self._refuse_write()

    def do_PATCH(self) -> None:
        """Refuse a write."""
        self._refuse_write()


class _V6Server(ThreadingHTTPServer):
    """The same server on the IPv6 loopback."""

    address_family = socket.AF_INET6


@dataclass(frozen=True, slots=True)
class ReportServer:
    """One invocation's report server.

    Attributes:
        httpd: The bound listening server.
        nonce: The nonce every URL of this invocation carries.
    """

    httpd: ThreadingHTTPServer
    nonce: str

    @property
    def url(self) -> str:
        """Return the URL the operator opens."""
        host, port = self.httpd.server_address[:2]
        name = host.decode() if isinstance(host, bytes) else host
        shown = f"[{name}]" if ":" in name else name
        return f"http://{shown}:{port}/{self.nonce}/"

    def serve_until_interrupted(self) -> None:
        """Serve in the foreground until the invocation is interrupted, then close."""
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            logger.debug("serve interrupted")
        finally:
            self.httpd.server_close()


def open_report_server(root: Path, *, report_path: Path, host: str, port: int) -> ReportServer:
    """Bind a report server over ``root``, or refuse before any socket opens.

    Args:
        root: The directory to serve.
        report_path: The declared report path ``root`` must lie inside.
        host: The interface to bind; a loopback literal.
        port: The port to bind; ``0`` picks a free one.

    Returns:
        The bound server with a freshly minted nonce.

    Raises:
        ServeRefusedError: ``host`` is off loopback or ``root`` is outside ``report_path``.
    """
    confined = require_confined_root(root, report_path)
    bound = require_loopback(host)
    nonce = secrets.token_urlsafe(18)
    handler = partial(_ReportHandler, root=confined, nonce=nonce)
    server = _V6Server if ipaddress.ip_address(bound).version == 6 else ThreadingHTTPServer
    httpd = server((bound, port), handler)
    return ReportServer(httpd=httpd, nonce=nonce)


__all__ = [
    "DEFAULT_HOST",
    "ReportServer",
    "ServeRefusedError",
    "open_report_server",
    "require_confined_root",
    "require_loopback",
]
