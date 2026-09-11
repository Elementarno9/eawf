"""Builders shared by every ``0.7.0.dev1`` release test.

The release suite spans two mirrors -- the kernel vocabulary under
``tests/unit/kernel/release/`` and the workflow behaviour under
``tests/unit/workflow/release/`` -- and both drive the same authored
checkpoint. Keeping the builders in one module rather than in each
directory's ``conftest`` is what stops the two mirrors from drifting
into two slightly different dev1 fixtures.

Every builder here works against the *authored* ``0.7.0.dev1``
configuration rather than a hand-built stand-in, so a change to the
checkpoint file reds these tests instead of leaving them agreeing with a
fixture nobody ships.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from eawf.kernel.release.signals import (
    ReleaseSignalContext,
    ReleaseSignalName,
    ReleaseSignalOutcome,
    ReleaseSignalProbe,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.release import (
    AdoptedTargetObservation,
    Release,
    ReleaseAdoption,
    ReleaseChannel,
    ReleaseStatus,
)
from eawf.kernel.spec.release_config import ReleaseConfig, load_release_config
from eawf.workflow.release.advance import draft_release_for
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
OBSERVATION_FIXTURES = Path(__file__).parent / "fixtures" / "release" / "observations"

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


#: The four targets ``0.7.0.dev1`` reached without a release record, and
#: the state each was independently read back in. ``plugins-dist`` is
#: deliberately here and deliberately absent from the authored
#: configuration: an uncontrolled publication can reach somewhere the
#: checkpoint never declared, and a fixture that drops that row would
#: test a tidier incident than the one that happened.
ADOPTED_TARGET_DETAIL: Mapping[str, tuple[str, str]] = {
    "pypi": (
        "observed_mismatch",
        "holds the 2026-09-07 wheel and source distribution, built from a "
        "commit neither later tag target names; the filename cannot be reused",
    ),
    "npm": (
        "observed_success",
        "the next dist-tag resolves 0.7.0-dev.1 from the final tag target; latest is untouched",
    ),
    "github": (
        "observed_mismatch",
        "the source-host release exists with zero assets and prerelease false, "
        "targeting the default branch rather than the tagged commit",
    ),
    "plugins-dist": (
        "observed_mismatch",
        "the version directory holds the 2026-09-07 render and refused both "
        "later publications; no configured leg declares this target",
    ),
}

#: The reason the dev1 adoption carries. A terminal record has no later
#: transition that could explain it, so the explanation arrives here.
ADOPTION_REASON = (
    "0.7.0.dev1 was published to four targets with no release record open, and "
    "the tag was then moved twice over already-published artifacts, so the "
    "targets disagree on which commit the version denotes; the version is spent "
    "and is superseded by 0.7.0.dev2"
)

#: The incident the dev1 adoption disposes of.
ADOPTION_INCIDENT_REF = "INC-P32-01"

#: Identity the adoption fixtures mint for the dev1 record.
DEV1_DRAFT_UID = UUID(int=744)


def adopted_observation(target_id: str, **overrides: Any) -> AdoptedTargetObservation:
    """Return the recorded read-back of one out-of-band dev1 target.

    Args:
        target_id: One key of :data:`ADOPTED_TARGET_DETAIL`.
        **overrides: Field overrides applied to the built row.

    Returns:
        The observation row.
    """
    status, detail = ADOPTED_TARGET_DETAIL[target_id]
    payload: dict[str, Any] = {
        "target_id": target_id,
        "observed_status": status,
        "observed_at": NOW,
        "evidence_ref": f"observation://adopted/{target_id}/eawf@0.7.0.dev1",
        "detail": detail,
    }
    payload.update(overrides)
    return AdoptedTargetObservation(**payload)


def dev1_adoption(*, targets: Sequence[str] | None = None, **overrides: Any) -> ReleaseAdoption:
    """Return the dev1 adoption over *targets*, defaulting to all four.

    Args:
        targets: Target ids to observe. ``None`` observes every row of
            :data:`ADOPTED_TARGET_DETAIL`.
        **overrides: Field overrides applied to the built adoption.

    Returns:
        The adoption record.
    """
    chosen = tuple(ADOPTED_TARGET_DETAIL) if targets is None else tuple(targets)
    payload: dict[str, Any] = {
        "adopted_at": NOW,
        "reason": ADOPTION_REASON,
        "incident_ref": ADOPTION_INCIDENT_REF,
        "observations": tuple(adopted_observation(target) for target in chosen),
    }
    payload.update(overrides)
    return ReleaseAdoption(**payload)


def dev1_draft(uid: UUID | None = None) -> Release:
    """Return a fresh DRAFT dev1 record, exactly as the train opens it."""
    return draft_release_for(
        V07_TRAIN.checkpoint_for_version("0.7.0.dev1"), uid=uid or DEV1_DRAFT_UID
    )


def fixed_probe(status: ReleaseSignalStatus) -> ReleaseSignalProbe:
    """Return a probe reporting *status* for whichever signal it is asked."""
    remediation = "" if status is ReleaseSignalStatus.PASS else f"repair the {status.value} row"

    def run(context: ReleaseSignalContext) -> ReleaseSignalOutcome:
        return ReleaseSignalOutcome(status=status, remediation=remediation)

    return run


def all_passing() -> dict[ReleaseSignalName, ReleaseSignalProbe]:
    """Return a probe registry in which every signal passes."""
    return dict.fromkeys(ReleaseSignalName, fixed_probe(ReleaseSignalStatus.PASS))
