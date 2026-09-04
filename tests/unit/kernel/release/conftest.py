"""Fixtures shared by the dev1 gate-binding test modules.

Every module here works against the *authored* ``0.7.0.dev1``
configuration rather than a hand-built stand-in, so a change to the
checkpoint file reds these tests instead of leaving them agreeing with a
fixture nobody ships.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from eawf.kernel.release.signals import (
    ReleaseSignalContext,
    ReleaseSignalName,
    ReleaseSignalOutcome,
    ReleaseSignalProbe,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.release import Release, ReleaseChannel, ReleaseStatus
from eawf.kernel.spec.release_config import ReleaseConfig, load_release_config
from eawf.workflow.release.observation import (
    FrozenManifest,
    ObservationRequest,
    RecordedResponse,
    observation_request,
)
from eawf.workflow.release.train import (
    DEV1_GATE_BINDINGS_YAML,
    DEV1_RELEASE_CONFIG_YAML,
    V07_TRAIN,
)

#: Instant every sweep in this package is computed at.
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

#: A pinned candidate's source commit, tree and manifest digest.
SOURCE_SHA = "a" * 40
TREE_SHA = "b" * 40
MANIFEST_DIGEST = f"sha256:{'c' * 64}"

#: Recorded registry answers and the frozen manifest they are judged
#: against. Committed rather than generated so a fixture drifting from
#: what an adapter reads shows up as a diff, not as a passing test.
OBSERVATION_FIXTURES = Path(__file__).parents[3] / "fixtures" / "release" / "observations"

#: The recorded-response stem of each adapter, by target id.
ADAPTER_STEMS: Mapping[str, str] = {
    "pypi": "package_index",
    "npm": "npm_registry",
    "github": "source_host_release",
}


def frozen_manifest() -> FrozenManifest:
    """Return the committed ``0.7.0.dev1`` frozen manifest."""
    return FrozenManifest.model_validate(
        json.loads((OBSERVATION_FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    )


def recorded_response(target_id: str, case: str) -> RecordedResponse:
    """Return the recorded registry answer for *target_id* in *case*.

    Args:
        target_id: Publication target whose adapter recorded it.
        case: Fixture case stem, e.g. ``match`` or ``default-channel``.

    Returns:
        The recorded answer.
    """
    path = OBSERVATION_FIXTURES / f"{ADAPTER_STEMS[target_id]}-{case}.json"
    return RecordedResponse.model_validate(json.loads(path.read_text(encoding="utf-8")))


def read_back_request(target_id: str, *, config: ReleaseConfig | None = None) -> ObservationRequest:
    """Return the read-back request for one configured target."""
    return observation_request(config or dev1_config(), frozen_manifest(), target_id=target_id)


def dev1_config(**overrides: Any) -> ReleaseConfig:
    """Return the authored dev1 configuration with top-level *overrides*."""
    body: dict[str, Any] = copy.deepcopy(yaml.safe_load(DEV1_RELEASE_CONFIG_YAML))["release"]
    body.update(overrides)
    return load_release_config({"release": body}, train=V07_TRAIN)


def dev1_binding_rows() -> list[dict[str, Any]]:
    """Return the authored dev1 binding rows as mutable dicts."""
    decoded: dict[str, Any] = yaml.safe_load(DEV1_GATE_BINDINGS_YAML)
    rows: list[dict[str, Any]] = copy.deepcopy(decoded["bindings"])
    return rows


def release_record(**overrides: Any) -> Release:
    """Return a pinned ``0.7.0.dev1`` candidate with *overrides* applied."""
    payload: dict[str, Any] = {
        "uid": UUID(int=29),
        "key": "REL-0.7.0.dev1",
        "version": "0.7.0.dev1",
        "channel": ReleaseChannel.DEV,
        "authority_epoch": 1,
        "status": ReleaseStatus.CANDIDATE,
        "source_sha": SOURCE_SHA,
        "source_tree_sha": TREE_SHA,
        "manifest_ref": "artifact://release/manifest",
        "manifest_digest": MANIFEST_DIGEST,
    }
    payload.update(overrides)
    return Release(**payload)


def fixed_probe(status: ReleaseSignalStatus) -> ReleaseSignalProbe:
    """Return a probe reporting *status* for whichever signal it is asked."""
    remediation = "" if status is ReleaseSignalStatus.PASS else f"repair the {status.value} row"

    def run(context: ReleaseSignalContext) -> ReleaseSignalOutcome:
        return ReleaseSignalOutcome(status=status, remediation=remediation)

    return run


def all_passing() -> dict[ReleaseSignalName, ReleaseSignalProbe]:
    """Return a probe registry in which every signal passes."""
    return dict.fromkeys(ReleaseSignalName, fixed_probe(ReleaseSignalStatus.PASS))


@pytest.fixture
def config() -> ReleaseConfig:
    """Return the authored dev1 checkpoint configuration."""
    return dev1_config()


@pytest.fixture
def green_probes() -> Mapping[ReleaseSignalName, ReleaseSignalProbe]:
    """Return a probe registry in which every signal passes."""
    return all_passing()
