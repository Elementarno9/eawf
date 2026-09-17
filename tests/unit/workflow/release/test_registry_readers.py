"""The live registry readers behind release observe.

Under test: each of the three readers fetching exactly the registry URL
its adapter needs through an injected opener -- the package index's
per-version JSON document, the npm packument followed by the version's
``dist.tarball``, and the source host's release-by-tag object -- and
returning the recorded response the adapter judges, with the npm
tarball's sha256 recorded under the frozen filename and the source-host
repository recorded from the request identity. The urllib opener's
mapping of every transport failure onto status ``0`` is driven through
an injected ``urlopen``. No test here opens a socket: the autouse guard
fails any attempt.

The live ``0.7.0.dev2`` bodies are replayed too, against the manifest
that release approved, so the readers are shown to reproduce the match
the published artifacts support.
"""

from __future__ import annotations

import email.message
import hashlib
import http.client
import json
import socket
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import pytest

from eawf.kernel.spec.release_config import ObservationAdapter, load_release_config
from eawf.workflow.release.adapters import DEFAULT_REGISTRY_READERS, collect_observation
from eawf.workflow.release.observation import (
    FrozenArtifact,
    FrozenManifest,
    ObservationCode,
    ObservationRequest,
    RecordedResponse,
    observation_request,
)
from eawf.workflow.release.registry_readers import (
    NPM_TARBALL_DIGESTS_FIELD,
    READ_TIMEOUT_SECONDS,
    SOURCE_HOST_REPOSITORY_FIELD,
    USER_AGENT,
    HttpReply,
    NpmRegistryReader,
    PackageIndexReader,
    SourceHostReleaseReader,
    UrllibOpener,
    npm_packument_url,
    package_index_url,
    source_host_release_url,
)
from eawf.workflow.release.train import DEV2_RELEASE_CONFIG_YAML, V07_TRAIN
from tests._release_helpers import (
    FROZEN_NPM_TARBALL,
    NOW,
    OBSERVATION_FIXTURES,
    RecordedRegistry,
    read_back_request,
    recorded_registry,
    registry_answer,
    registry_reader,
)

pytestmark = pytest.mark.unit

PACKUMENT_URL = "https://registry.npmjs.org/@elementarno%2Feawf"
DEV1_TARBALL_URL = "https://registry.npmjs.org/@elementarno/eawf/-/eawf-0.7.0-dev.1.tgz"
DEV2_TARBALL_URL = "https://registry.npmjs.org/@elementarno/eawf/-/eawf-0.7.0-dev.2.tgz"


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any attempt to resolve or connect while a test runs."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"a registry reader test tried to open a socket: {args!r}")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def json_reply(payload: Any, status: int = 200) -> HttpReply:
    """Return a reply carrying *payload* encoded as JSON."""
    return HttpReply(status=status, body=json.dumps(payload).encode("utf-8"))


def dev2_request(target_id: str) -> ObservationRequest:
    """Return the read-back request ``0.7.0.dev2``'s approval implies."""
    manifest = FrozenManifest.model_validate(
        json.loads((OBSERVATION_FIXTURES / "manifest-dev2.json").read_text(encoding="utf-8"))
    )
    config = load_release_config(DEV2_RELEASE_CONFIG_YAML, train=V07_TRAIN)
    return observation_request(config, manifest, target_id=target_id)


def test_the_socket_guard_refuses_a_connection() -> None:
    with pytest.raises(AssertionError, match="tried to open a socket"):
        socket.create_connection(("registry.npmjs.org", 443))


# --- the default table --------------------------------------------------------


def test_default_registry_readers_map_every_adapter_to_its_urllib_reader() -> None:
    readers = DEFAULT_REGISTRY_READERS
    assert set(readers) == set(ObservationAdapter)
    pypi = readers[ObservationAdapter.PACKAGE_INDEX]
    npm = readers[ObservationAdapter.NPM_REGISTRY]
    github = readers[ObservationAdapter.SOURCE_HOST_RELEASE]
    assert isinstance(pypi, PackageIndexReader)
    assert isinstance(npm, NpmRegistryReader)
    assert isinstance(github, SourceHostReleaseReader)
    for opener in (pypi.opener, npm.opener, github.opener):
        assert isinstance(opener, UrllibOpener)
        assert opener.urlopen is urllib.request.urlopen
        assert opener.timeout_seconds == pytest.approx(READ_TIMEOUT_SECONDS)


# --- the package index --------------------------------------------------------


def test_package_index_url_names_the_project_and_version() -> None:
    assert package_index_url(read_back_request("pypi")) == (
        "https://pypi.org/pypi/eawf/0.7.0.dev1/json"
    )


def test_package_index_url_quotes_each_segment() -> None:
    request = replace(read_back_request("pypi"), identity="odd/name")
    assert package_index_url(request) == "https://pypi.org/pypi/odd%2Fname/0.7.0.dev1/json"


def test_package_index_reader_fetches_the_per_version_document() -> None:
    request = read_back_request("pypi")
    registry = recorded_registry("pypi", "match", request)
    response = PackageIndexReader(opener=registry)(request)
    _status, body = registry_answer("pypi", "match")
    assert response == RecordedResponse(status=200, payload=body)
    assert registry.requests == [
        ("https://pypi.org/pypi/eawf/0.7.0.dev1/json", {"Accept": "application/json"})
    ]


@pytest.mark.parametrize(("case", "status"), (("missing", 404), ("unknown", 503)))
def test_package_index_reader_passes_a_non_ok_status_through(case: str, status: int) -> None:
    request = read_back_request("pypi")
    response = PackageIndexReader(opener=recorded_registry("pypi", case, request))(request)
    assert response == RecordedResponse(status=status)


@pytest.mark.parametrize("body", (b"<html>maintenance</html>", b"[1, 2]", b"", b"\xff\xfe"))
def test_package_index_reader_records_an_unreadable_body_as_no_payload(body: bytes) -> None:
    request = read_back_request("pypi")
    opener = RecordedRegistry(answers={package_index_url(request): HttpReply(200, body)})
    response = PackageIndexReader(opener=opener)(request)
    assert response == RecordedResponse(status=200)
    observation = collect_observation(request, response=response, observed_at=NOW)
    assert observation.code is ObservationCode.RESPONSE_UNREADABLE


# --- the npm registry ---------------------------------------------------------


def test_npm_packument_url_encodes_the_scope_separator() -> None:
    assert npm_packument_url(read_back_request("npm")) == PACKUMENT_URL


def test_npm_packument_url_leaves_a_bare_package_name_alone() -> None:
    request = replace(read_back_request("npm"), identity="eawf")
    assert npm_packument_url(request) == "https://registry.npmjs.org/eawf"


def test_npm_registry_reader_fetches_the_packument_then_the_tarball() -> None:
    request = read_back_request("npm")
    registry = recorded_registry("npm", "match", request)
    NpmRegistryReader(opener=registry)(request)
    assert registry.requests == [
        (PACKUMENT_URL, {"Accept": "application/json"}),
        (DEV1_TARBALL_URL, {"Accept": "application/octet-stream"}),
    ]


def test_npm_registry_reader_hashes_the_tarball_under_the_frozen_filename() -> None:
    request = read_back_request("npm")
    response = NpmRegistryReader(opener=recorded_registry("npm", "match", request))(request)
    assert response.payload is not None
    dist = response.payload["versions"]["0.7.0-dev.1"]["dist"]
    assert dist[NPM_TARBALL_DIGESTS_FIELD] == {
        "elementarno-eawf-0.7.0-dev.1.tgz": (
            f"sha256:{hashlib.sha256(FROZEN_NPM_TARBALL).hexdigest()}"
        )
    }
    assert dist["tarball"] == DEV1_TARBALL_URL


def test_npm_registry_reader_leaves_the_rest_of_the_packument_untouched() -> None:
    request = read_back_request("npm")
    response = NpmRegistryReader(opener=recorded_registry("npm", "match", request))(request)
    _status, body = registry_answer("npm", "match")
    assert response.payload is not None
    recorded = json.loads(json.dumps(response.payload))
    del recorded["versions"]["0.7.0-dev.1"]["dist"][NPM_TARBALL_DIGESTS_FIELD]
    assert recorded == body


def test_npm_registry_reader_on_a_stale_packument_fetches_no_tarball() -> None:
    request = read_back_request("npm")
    registry = recorded_registry("npm", "stale", request)
    response = NpmRegistryReader(opener=registry)(request)
    _status, body = registry_answer("npm", "stale")
    assert response == RecordedResponse(status=200, payload=body)
    assert [url for url, _headers in registry.requests] == [PACKUMENT_URL]


@pytest.mark.parametrize(("case", "status"), (("missing", 404), ("unknown", 503)))
def test_npm_registry_reader_passes_a_non_ok_packument_status_through(
    case: str, status: int
) -> None:
    request = read_back_request("npm")
    registry = recorded_registry("npm", case, request)
    assert NpmRegistryReader(opener=registry)(request) == RecordedResponse(status=status)
    assert len(registry.requests) == 1


@pytest.mark.parametrize("status", (0, 404, 503))
def test_npm_registry_reader_answers_with_the_tarball_status_when_it_fails(status: int) -> None:
    request = read_back_request("npm")
    registry = recorded_registry("npm", "match", request)
    registry.answers[DEV1_TARBALL_URL] = HttpReply(status)
    assert NpmRegistryReader(opener=registry)(request) == RecordedResponse(status=status)


def test_npm_registry_reader_on_a_version_without_a_tarball_hashes_nothing() -> None:
    request = read_back_request("npm")
    _status, body = registry_answer("npm", "match")
    del body["versions"]["0.7.0-dev.1"]["dist"]["tarball"]
    registry = RecordedRegistry(answers={PACKUMENT_URL: json_reply(body)})
    response = NpmRegistryReader(opener=registry)(request)
    assert response == RecordedResponse(status=200, payload=body)
    observation = collect_observation(request, response=response, observed_at=NOW)
    assert observation.code is ObservationCode.ARTIFACT_ABSENT


@pytest.mark.parametrize(
    "tarball",
    (
        "http://registry.npmjs.org/@elementarno/eawf/-/eawf-0.7.0-dev.1.tgz",
        "https://registry.example.test/@elementarno/eawf/-/eawf-0.7.0-dev.1.tgz",
        "file:///eawf-0.7.0-dev.1.tgz",
    ),
)
def test_npm_registry_reader_refuses_a_tarball_off_the_registry(tarball: str) -> None:
    request = read_back_request("npm")
    _status, body = registry_answer("npm", "match")
    body["versions"]["0.7.0-dev.1"]["dist"]["tarball"] = tarball
    registry = RecordedRegistry(answers={PACKUMENT_URL: json_reply(body)})
    response = NpmRegistryReader(opener=registry)(request)
    assert response == RecordedResponse(status=0)
    assert [url for url, _headers in registry.requests] == [PACKUMENT_URL]


@pytest.mark.parametrize("count", (0, 2))
def test_npm_registry_reader_refuses_a_frozen_set_other_than_one_tarball(count: int) -> None:
    artifacts = tuple(
        FrozenArtifact(kind="codex_plugin", filename=f"t{index}.tgz", digest=f"sha256:{'1' * 64}")
        for index in range(count)
    )
    request = replace(read_back_request("npm"), artifacts=artifacts)
    registry = RecordedRegistry(answers={})
    with pytest.raises(ValueError, match="an npm version publishes one tarball"):
        NpmRegistryReader(opener=registry)(request)
    assert registry.requests == []


# --- the source host ----------------------------------------------------------


def test_source_host_release_url_names_the_repository_and_tag() -> None:
    assert source_host_release_url(read_back_request("github")) == (
        "https://api.github.com/repos/Elementarno9/eawf/releases/tags/v0.7.0.dev1"
    )


@pytest.mark.parametrize(
    "identity",
    ("eawf", "Elementarno9/eawf/extra", "Elementarno9/..", "Elementarno9/.", "/eawf", "a b/c", ""),
)
def test_source_host_release_url_refuses_an_identity_that_is_not_owner_repo(
    identity: str,
) -> None:
    request = replace(read_back_request("github"), identity=identity)
    with pytest.raises(ValueError, match="is not an 'owner/repo' pair"):
        source_host_release_url(request)


def test_source_host_release_reader_fetches_the_release_by_tag() -> None:
    request = read_back_request("github")
    registry = recorded_registry("github", "match", request)
    SourceHostReleaseReader(opener=registry)(request)
    assert registry.requests == [
        (
            "https://api.github.com/repos/Elementarno9/eawf/releases/tags/v0.7.0.dev1",
            {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        )
    ]


def test_source_host_release_reader_injects_the_requested_repository() -> None:
    request = read_back_request("github")
    response = SourceHostReleaseReader(opener=recorded_registry("github", "match", request))(
        request
    )
    _status, body = registry_answer("github", "match")
    assert SOURCE_HOST_REPOSITORY_FIELD not in body
    assert response == RecordedResponse(
        status=200, payload={**body, SOURCE_HOST_REPOSITORY_FIELD: "Elementarno9/eawf"}
    )


@pytest.mark.parametrize(("case", "status"), (("missing", 404), ("unknown", 503)))
def test_source_host_release_reader_injects_nothing_into_a_non_ok_answer(
    case: str, status: int
) -> None:
    request = read_back_request("github")
    response = SourceHostReleaseReader(opener=recorded_registry("github", case, request))(request)
    assert response == RecordedResponse(status=status)


def test_source_host_release_reader_refuses_a_malformed_identity_before_fetching() -> None:
    request = replace(read_back_request("github"), identity="not-a-pair")
    registry = RecordedRegistry(answers={})
    with pytest.raises(ValueError, match="is not an 'owner/repo' pair"):
        SourceHostReleaseReader(opener=registry)(request)
    assert registry.requests == []


# --- the recorded live 0.7.0.dev2 answers --------------------------------------


@pytest.mark.parametrize("target_id", ("pypi", "github"))
def test_the_live_dev2_answers_match_the_manifest_dev2_approved(target_id: str) -> None:
    request = dev2_request(target_id)
    reader = registry_reader(target_id, recorded_registry(target_id, "dev2", request))
    observation = collect_observation(
        request,
        observed_at=NOW,
        readers={request.target.observe_adapter: reader},
    )
    assert observation.code is ObservationCode.MATCHED
    assert dict(observation.observed_digests) == {
        artifact.filename: artifact.digest for artifact in request.artifacts
    }


def test_the_live_dev2_packument_leads_the_reader_to_the_published_tarball() -> None:
    request = dev2_request("npm")
    registry = recorded_registry("npm", "dev2", request)
    response = NpmRegistryReader(opener=registry)(request)
    assert [url for url, _headers in registry.requests] == [PACKUMENT_URL, DEV2_TARBALL_URL]
    assert response.payload is not None
    recorded = response.payload["versions"]["0.7.0-dev.2"]["dist"][NPM_TARBALL_DIGESTS_FIELD]
    assert list(recorded) == ["elementarno-eawf-0.7.0-dev.2.tgz"]


# --- the urllib opener --------------------------------------------------------


class _Answer:
    """A stand-in for the response object urlopen yields."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body


class _FakeUrlopen:
    """Records each urlopen call and answers or raises as configured."""

    def __init__(self, *, answer: _Answer | None = None, error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[tuple[urllib.request.Request, float]] = []

    @contextmanager
    def _opened(self) -> Iterator[_Answer]:
        assert self.answer is not None
        yield self.answer

    def __call__(self, url: urllib.request.Request, *, timeout: float) -> Any:
        self.calls.append((url, timeout))
        if self.error is not None:
            raise self.error
        return self._opened()


def test_urllib_opener_returns_the_status_and_body() -> None:
    urlopen = _FakeUrlopen(answer=_Answer(200, b'{"ok": true}'))
    reply = UrllibOpener(urlopen=urlopen, timeout_seconds=5.0)(
        "https://pypi.org/pypi/eawf/0.7.0.dev1/json", headers={"Accept": "application/json"}
    )
    assert reply == HttpReply(status=200, body=b'{"ok": true}')
    [(request, timeout)] = urlopen.calls
    assert timeout == pytest.approx(5.0)
    assert request.full_url == "https://pypi.org/pypi/eawf/0.7.0.dev1/json"
    assert request.get_method() == "GET"
    assert request.get_header("Accept") == "application/json"
    assert request.get_header("User-agent") == USER_AGENT
    assert request.data is None


def test_urllib_opener_sends_no_credentials() -> None:
    urlopen = _FakeUrlopen(answer=_Answer(200, b"{}"))
    UrllibOpener(urlopen=urlopen)("https://api.github.com/repos/o/r", headers={})
    [(request, _timeout)] = urlopen.calls
    assert {name.lower() for name, _value in request.header_items()} == {"user-agent"}


@pytest.mark.parametrize("status", (404, 403, 503))
def test_urllib_opener_passes_an_http_error_status_through(status: int) -> None:
    error = urllib.error.HTTPError(
        "https://pypi.org/x", status, "error", email.message.Message(), None
    )
    reply = UrllibOpener(urlopen=_FakeUrlopen(error=error))("https://pypi.org/x", headers={})
    assert reply == HttpReply(status=status)


@pytest.mark.parametrize(
    "error",
    (
        urllib.error.URLError("name resolution failed"),
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
        http.client.RemoteDisconnected("closed"),
        http.client.IncompleteRead(b"partial"),
    ),
)
def test_urllib_opener_maps_a_missing_answer_to_status_zero(error: Exception) -> None:
    reply = UrllibOpener(urlopen=_FakeUrlopen(error=error))("https://pypi.org/x", headers={})
    assert reply == HttpReply(status=0)


def test_urllib_opener_lets_a_programming_error_raise() -> None:
    opener = UrllibOpener(urlopen=_FakeUrlopen(error=TypeError("bad call")))
    with pytest.raises(TypeError, match="bad call"):
        opener("https://pypi.org/x", headers={})
