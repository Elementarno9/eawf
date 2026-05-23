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

from eawf.cli.errors import UserError
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
        raise UserError("artifact uri must not be file:// or absolute path", kind="InvalidInput")


def add_artifact(
    state: State,
    *,
    artifact_id: str,
    kind: str,
    uri: str,
    scope_id: str,
    sha256: str | None = None,
    size_bytes: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> Envelope:
    """Register a new artifact in place; return the event envelope."""
    _validate_artifact_location(uri)

    artifacts: dict[str, Artifact] = dict(state.artifacts)
    if artifact_id in artifacts:
        raise UserError(f"artifact {artifact_id!r} already exists", kind="InvalidInput")

    now = datetime.now(UTC)
    artifact = Artifact(
        id=artifact_id,
        kind=kind,
        uri=uri,
        urn=_io.artifact_urn(scope_id, artifact_id),
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
        scope_id=scope_id,
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
        raise UserError(f"artifact {artifact_id!r} not found", kind="NotFound")
    return state.artifacts[artifact_id]


def update_artifact(
    state: State,
    *,
    artifact_id: str,
    sha256: str | None = None,
    size_bytes: int | None = None,
    uri: str | None = None,
) -> Envelope:
    """Update mutable fields on an existing artifact record.

    Only ``sha256``, ``size_bytes``, and ``uri`` are mutable; identity
    (``id``, ``kind``, ``urn``, ``scope_id``, ``created_at``) is fixed
    once registered. At least one of the three mutable fields MUST be
    supplied; passing all-``None`` raises :class:`UserError`
    (``kind="InvalidInput"``).

    Designed for the recompute-on-touch case: when a registered file's
    on-disk content drifts (typically because pre-commit hooks rewrite
    EOL or formatting), the registered hash falls out of sync with
    :func:`eawf.cli.commands.evidence.artifact_verify`. The update verb
    is the canonical mechanism for re-pinning the sha256/size_bytes.

    Args:
        state: Mutable state model under transaction.
        artifact_id: Existing artifact id to update.
        sha256: New sha256 (or ``None`` to leave unchanged).
        size_bytes: New byte size (or ``None`` to leave unchanged).
        uri: New uri (or ``None`` to leave unchanged); validated when
            non-``None`` against the file://-and-absolute-path block.

    Raises:
        UserError: when *artifact_id* is not registered
            (``kind="NotFound"``), or when no mutable field is supplied or
            *uri* is an absolute path / ``file://`` scheme
            (``kind="InvalidInput"``).

    Returns:
        Event envelope describing the update; caller appends to event.jsonl.
    """
    if sha256 is None and size_bytes is None and uri is None:
        raise UserError(
            "at least one of sha256, size_bytes, uri must be supplied", kind="InvalidInput"
        )
    if artifact_id not in state.artifacts:
        raise UserError(f"artifact {artifact_id!r} not found", kind="NotFound")
    if uri is not None:
        _validate_artifact_location(uri)

    artifacts: dict[str, Artifact] = dict(state.artifacts)
    existing = artifacts[artifact_id]
    updates: dict[str, Any] = {}
    if sha256 is not None:
        updates["sha256"] = sha256
    if size_bytes is not None:
        updates["size_bytes"] = size_bytes
    if uri is not None:
        updates["uri"] = uri
    updated = existing.model_copy(update=updates)
    artifacts[artifact_id] = updated
    state.artifacts = artifacts
    now = datetime.now(UTC)
    state.updated_at = now

    logger.info(
        f"update_artifact artifact_id={artifact_id!r} sha256={sha256!r} "
        f"size_bytes={size_bytes} uri={uri!r}"
    )
    return _io.event_envelope(
        event_id=f"EVT-artifact-update-{artifact_id}-{int(now.timestamp() * 1000)}",
        scope_id=updated.urn.split(":")[-1].split("/")[0],
        event_type="artifact.update",
        actor="cli",
        command="artifact update",
        args={
            "artifact_id": artifact_id,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "uri": uri,
        },
        summary=f"artifact {artifact_id} updated",
        artifact_ids=[artifact_id],
    )
