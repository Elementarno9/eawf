"""Artifact-area mutators: add / show.

Artifacts are durable evidence pointers (file/blob/external). The ``urn``
field is built from the project scope plus artifact id so the artifact has a
canonical ``urn:eawf:v1:artifact:<scope>/<id>`` identity even when the local
``uri`` varies across machines.

Mutators take a typed :class:`State` and mutate it in place; the CLI handler
runs them inside :func:`eawf.cli._mutation.state_transaction`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from eawf.cli.errors import InvalidInput, NotFound
from eawf.evidence import _io
from eawf.state.models import Artifact, State
from eawf.store.envelope import Envelope

logger = logging.getLogger(__name__)


def _validate_artifact_location(uri: str) -> None:
    parsed = urlsplit(uri)
    if (
        parsed.scheme == "file"
        or PurePosixPath(uri).is_absolute()
        or PureWindowsPath(uri).is_absolute()
    ):
        raise InvalidInput("artifact uri must not be file:// or absolute path")


def add_artifact(
    state: State,
    *,
    artifact_id: str,
    kind: str,
    uri: str,
    scope: str,
    sha256: str | None = None,
    size_bytes: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> Envelope:
    """Register a new artifact in place; return the event envelope."""
    _validate_artifact_location(uri)

    artifacts: dict[str, Artifact] = dict(state.artifacts)
    if artifact_id in artifacts:
        raise InvalidInput(f"artifact {artifact_id!r} already exists")

    now = datetime.now(UTC)
    artifact = Artifact(
        id=artifact_id,
        kind=kind,
        uri=uri,
        urn=_io.artifact_urn(scope, artifact_id),
        sha256=sha256,
        size_bytes=size_bytes,
        created_at=now,
        metadata=dict(metadata or {}),
    )
    artifacts[artifact_id] = artifact
    state.artifacts = artifacts
    state.updated_at = now

    return _io.event_envelope(
        event_id=f"EVT-artifact-add-{artifact_id}-{int(now.timestamp() * 1000)}",
        scope_id=scope,
        event_type="artifact.add",
        actor="cli",
        command="artifact add",
        args={
            "artifact_id": artifact_id,
            "kind": kind,
            "uri": uri,
            "sha256": sha256,
        },
        summary=f"artifact {artifact_id} added kind={kind}",
        artifact_ids=[artifact_id],
    )


def show_artifact(state: State, artifact_id: str) -> Artifact:
    """Read-only lookup."""
    if artifact_id not in state.artifacts:
        raise NotFound(f"artifact {artifact_id!r} not found")
    return state.artifacts[artifact_id]
