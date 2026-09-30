"""PLAN-050: an artifact revision carries what its card names without opening the content.

One case per media kind, the missing-digest and unknown-kind refusals, the
file-name bounds, and the size and digest mismatch between a record and the
content a card is about to render.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.artifact_revision import (
    ArtifactRevision,
    MediaKind,
    StoredArtifactRevision,
    verify_revision_content,
)

pytestmark = pytest.mark.unit

CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
CONTENT: Final = b"# Replay order\n\n40 events compared, 0 inversions.\n"


def revision(content: bytes = CONTENT, **overrides: Any) -> ArtifactRevision:
    fields: dict[str, Any] = {
        "artifact_ref": "urn:eawf:v1:artifact:EAWF/ART-0012",
        "revision": 1,
        "content_ref": "store:artifact/ART-0012/r1",
        "file_name": "reports/replay-order.md",
        "media_kind": "markdown",
        "size_bytes": len(content),
        "written_at": datetime(2026, 9, 18, 12, 0, tzinfo=UTC),
        "written_by": {"run_ref": f"{CONTAINER}/run/RUN-00000010", "step_ordinal": 3},
        "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
        "kept_with": f"{CONTAINER}/campaign/CAM-0001",
    }
    fields.update(overrides)
    return ArtifactRevision.model_validate(fields)


@pytest.mark.parametrize("kind", list(MediaKind))
def test_plan_050_each_media_kind_carries_its_card_facts(kind: MediaKind) -> None:
    r = revision(media_kind=kind.value)
    assert r.media_kind is kind
    assert (r.file_name, r.size_bytes, r.written_by.step_ordinal) == (
        "reports/replay-order.md",
        len(CONTENT),
        3,
    )
    verify_revision_content(r, CONTENT)


def test_plan_050_the_media_kinds_are_exactly_four() -> None:
    assert {k.value for k in MediaKind} == {"markdown", "table", "plain", "binary"}


def test_plan_050_a_run_that_is_not_a_step_names_no_ordinal() -> None:
    r = revision(written_by={"run_ref": f"{CONTAINER}/run/RUN-00000010"})
    assert r.written_by.step_ordinal is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"digest": None},
        {"digest": "sha256:XYZ"},
        {"media_kind": "image"},
        {"file_name": ""},
        {"file_name": "/etc/passwd"},
        {"file_name": "../outside.md"},
        {"file_name": "reports/../../outside.md"},
        {"file_name": "x" * 256},
        {"size_bytes": -1},
        {"revision": 0},
        {"kept_with": f"{CONTAINER}/milestone/MLS-0030"},
        {"written_by": {"run_ref": f"{CONTAINER}/run/RUN-00000010", "step_ordinal": 0}},
    ],
)
def test_plan_050_an_incomplete_revision_fails_validation(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        revision(**overrides)


def test_plan_050_a_bare_file_name_at_its_bound_validates() -> None:
    assert len(revision(file_name="x" * 255).file_name) == 255


def test_plan_050_digest_mismatch_fails_the_card() -> None:
    tampered = CONTENT.replace(b"0 inversions", b"1 inversion ")
    assert len(tampered) == len(CONTENT)
    with pytest.raises(ValueError, match="digests to"):
        verify_revision_content(revision(), tampered)


def test_plan_050_size_mismatch_fails_the_card() -> None:
    with pytest.raises(ValueError, match="records"):
        verify_revision_content(revision(), CONTENT + b"\n")


def test_plan_050_empty_content_verifies_against_an_empty_record() -> None:
    verify_revision_content(revision(b""), b"")


# ---- the stored ledger line keeps the revision's own text ----------------------


def test_plan_050_a_stored_revision_keeps_the_text_its_record_digests() -> None:
    stored = StoredArtifactRevision(revision=revision(), text=CONTENT.decode())
    assert stored.payload_kind == "artifact_revision"
    with pytest.raises(ValidationError, match="digests to"):
        StoredArtifactRevision(revision=revision(), text=CONTENT.decode().replace("0", "1"))


def test_plan_050_a_binary_revision_keeps_no_text_and_a_drawable_one_keeps_its_text() -> None:
    binary = revision(media_kind="binary")
    assert StoredArtifactRevision(revision=binary).text is None
    with pytest.raises(ValidationError, match="keeps no text"):
        StoredArtifactRevision(revision=binary, text=CONTENT.decode())
    with pytest.raises(ValidationError, match="keeps no text"):
        StoredArtifactRevision(revision=revision())
