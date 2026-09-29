"""SURF-166: a report is served locally and read-only, and exported as a self-contained page.

The server refuses to start off loopback or outside the declared report path; once
bound, it answers only URLs carrying this invocation's nonce, only for files inside its
root, and only reads. A static export ships its data as a script chunk beside the page.
"""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from eawf.observability.reflect import report as rp
from eawf.observability.reflect import serve as sv
from eawf.observability.reflect.titles import TitleSource


@pytest.fixture
def collection(tmp_path: Path) -> Path:
    """Return a report collection holding one page, with a secret beside it."""
    root = tmp_path / ".ea" / "local" / "reflect"
    (root / "export" / "one").mkdir(parents=True)
    (root / "export" / "one" / "index.html").write_text("<p>report</p>")
    (tmp_path / ".ea" / "state.json").write_text('{"canonical": true}')
    return root


@pytest.fixture
def server(collection: Path) -> Iterator[sv.ReportServer]:
    """Serve ``collection`` on a free loopback port for the duration of a test."""
    served = sv.open_report_server(collection, report_path=collection, host="127.0.0.1", port=0)
    thread = threading.Thread(target=served.httpd.serve_forever, daemon=True)
    thread.start()
    yield served
    served.httpd.shutdown()
    served.httpd.server_close()
    thread.join(timeout=5)


def _request(server: sv.ReportServer, path: str, method: str = "GET") -> tuple[int, bytes]:
    host, port = server.httpd.server_address[:2]
    connection = http.client.HTTPConnection(str(host), int(port), timeout=5)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_surf_166_a_nonce_bearing_url_serves_the_page(server: sv.ReportServer) -> None:
    """The invocation's own URL answers the file inside the root."""
    status, body = _request(server, f"/{server.nonce}/export/one/index.html")
    assert status == 200
    assert body == b"<p>report</p>"
    assert server.url.startswith("http://127.0.0.1:")


def test_surf_166_a_stale_nonce_is_refused(server: sv.ReportServer, collection: Path) -> None:
    """A URL from another invocation -- another nonce, or none -- reaches nothing."""
    other = sv.open_report_server(collection, report_path=collection, host="127.0.0.1", port=0)
    other.httpd.server_close()
    assert other.nonce != server.nonce
    assert _request(server, f"/{other.nonce}/export/one/index.html")[0] == 403
    assert _request(server, "/export/one/index.html")[0] == 403


@pytest.mark.parametrize(
    "path",
    [
        "/{nonce}/../../state.json",
        "/{nonce}/%2e%2e/%2e%2e/state.json",
        "/{nonce}/export/../../../state.json",
    ],
)
def test_surf_166_a_path_traversal_is_refused(server: sv.ReportServer, path: str) -> None:
    """A path resolving outside the root is refused and leaks no canonical byte."""
    status, body = _request(server, path.format(nonce=server.nonce))
    assert status == 403
    assert b"canonical" not in body


def test_surf_166_a_symlink_out_of_the_root_is_refused(
    server: sv.ReportServer, collection: Path
) -> None:
    """A link inside the root that points out of it resolves outside and is refused."""
    (collection / "escape.json").symlink_to(collection.parents[1] / "state.json")
    assert _request(server, f"/{server.nonce}/escape.json")[0] == 403


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_surf_166_no_request_can_write(server: sv.ReportServer, method: str) -> None:
    """Every method but GET and HEAD is refused: the server has no mutation path."""
    assert _request(server, f"/{server.nonce}/export/one/index.html", method)[0] == 405


def test_surf_166_a_missing_file_is_not_found(server: sv.ReportServer) -> None:
    """Boundary: a nonce-bearing path to nothing is a plain not-found."""
    assert _request(server, f"/{server.nonce}/absent.html")[0] == 404


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.10", "::", "localhost", "example.org"])
def test_surf_166_an_off_loopback_bind_is_refused_before_any_socket(
    collection: Path, host: str
) -> None:
    """The server refuses to start on a non-loopback interface or a hostname."""
    with pytest.raises(sv.ServeRefusedError):
        sv.open_report_server(collection, report_path=collection, host=host, port=0)


def test_surf_166_the_ipv6_loopback_is_accepted(collection: Path) -> None:
    """Boundary: ::1 is loopback, and its URL brackets the address."""
    try:
        served = sv.open_report_server(collection, report_path=collection, host="::1", port=0)
    except OSError:
        pytest.skip("the host has no IPv6 loopback")
    served.httpd.server_close()
    assert served.url.startswith("http://[::1]:")


def test_surf_166_a_root_outside_the_report_path_is_refused(
    collection: Path, tmp_path: Path
) -> None:
    """The server refuses a root outside the declared report path, traversal included."""
    with pytest.raises(sv.ServeRefusedError):
        sv.open_report_server(tmp_path, report_path=collection, host="127.0.0.1", port=0)
    with pytest.raises(sv.ServeRefusedError):
        sv.open_report_server(
            collection / ".." / "..", report_path=collection, host="127.0.0.1", port=0
        )


def test_surf_166_a_static_export_ships_its_data_as_a_script_chunk(tmp_path: Path) -> None:
    """The page loads its data from a generated script beside it, never by fetch."""
    root = tmp_path / "reflect"
    report = rp.ReflectReport(
        projects=("PRJ-RFL",),
        projection_revision=7,
        title_fill="enabled",
        sessions=(),
    )
    document = root / "2026-09-29-reflect-report.json"
    root.mkdir()
    document.write_text(report.model_dump_json())
    page = rp.export_report(root, document, dest=None)
    html = page.read_text()
    chunk = (page.parent / rp.EXPORT_DATA_FILENAME).read_text()
    assert '<script src="report-data.js"></script>' in html
    assert "fetch(" not in html
    prefix = "window.EAWF_REFLECT_REPORT = "
    assert chunk.startswith(prefix)
    assert json.loads(chunk[len(prefix) :].rstrip().rstrip(";"))["projection_revision"] == 7


def test_surf_166_a_script_chunk_cannot_close_its_own_tag(tmp_path: Path) -> None:
    """Error path: a title carrying ``</script>`` is escaped inside the chunk."""
    root = tmp_path / "reflect"
    root.mkdir()
    line = rp.SessionLine(
        run_key="RUN-00000001",
        status="COMPLETED",
        runtime="claude-code",
        model_family="unknown",
        wall_hours=None,
        model_steps=0,
        tool_work=0,
        subagents=0,
        title="Close </script> early",
        title_source=TitleSource.MODEL_WRITTEN,
        quotable=True,
    )
    report = rp.ReflectReport(
        projects=("PRJ-RFL",), projection_revision=1, title_fill="enabled", sessions=(line,)
    )
    document = root / "2026-09-29-reflect-report.json"
    document.write_text(report.model_dump_json())
    page = rp.export_report(root, document, dest=None)
    assert "</script>" not in (page.parent / rp.EXPORT_DATA_FILENAME).read_text()
