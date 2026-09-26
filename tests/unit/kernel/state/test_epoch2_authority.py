"""The authority resolver grants epoch 2 only to a declared, marked tree.

Each tree here is assembled from the two files the resolver reads -- the
disposable-canary declaration and the epoch marker -- so every case names
exactly which evidence is present. The both-present tree is the only one
that may resolve to epoch 2; every tree missing either file, or carrying
one that does not parse, resolves to epoch 1 with the gap that says why.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.migration.epoch2.canary import (
    CanaryDeclaration,
    DisposableTarget,
    declaration_path,
)
from eawf.kernel.migration.epoch2.generation import atomic_write_json, write_marker
from eawf.kernel.state.epoch2.authority import (
    NATIVE_AUTHORITY_REQUIRED,
    AuthorityGap,
    CanaryRepositoryRef,
    NativeAuthorityRequiredError,
    RootAuthority,
    require_native_authority,
    resolve_authority,
)
from eawf.observability.doctor.checks_authority import check_authority_epoch

GENERATION_ID = "gen-0123456789abcdef"
WRITTEN_AT = datetime(2026, 1, 1, tzinfo=UTC)
CANARY_URN = "eawf://WSP-CANARY/PRJ-CANARY/REP-CANARY/repository/REP-CANARY"


def _declare(root: Path) -> None:
    """Write a valid disposable-canary declaration into ``root``."""
    atomic_write_json(
        declaration_path(root),
        CanaryDeclaration(disposable=True, declared_by="authority-test", purpose="throwaway"),
    )


def _mark(root: Path) -> None:
    """Write a valid epoch marker into ``root``, declaring it only for the write."""
    declared = declaration_path(root).exists()
    if not declared:
        _declare(root)
    write_marker(
        target=DisposableTarget.require(root),
        generation_id=GENERATION_ID,
        manifest_digest="a" * 64,
        published_digest="b" * 64,
        written_at=WRITTEN_AT,
    )
    if not declared:
        declaration_path(root).unlink()


def _tree(tmp_path: Path, *, declared: bool, marked: bool) -> Path:
    """Return a tree root carrying the requested evidence."""
    root = tmp_path / ".ea"
    root.mkdir()
    (root / "state.json").write_text('{"schema_version": "1.20"}\n', encoding="utf-8")
    if marked:
        _mark(root)
    if declared:
        _declare(root)
    return root


def test_resolve_authority_both_present_grants_epoch2(tmp_path: Path) -> None:
    root = _tree(tmp_path, declared=True, marked=True)

    authority = resolve_authority(root)

    assert authority.epoch == 2
    assert authority.gap is None
    assert authority.generation_id == GENERATION_ID
    assert authority.target is not None
    assert authority.target.root == root


@pytest.mark.parametrize(
    ("declared", "marked", "gap"),
    [
        pytest.param(False, True, AuthorityGap.UNDECLARED, id="declaration-missing"),
        pytest.param(True, False, AuthorityGap.MARKER_ABSENT, id="marker-missing"),
        pytest.param(False, False, AuthorityGap.UNDECLARED, id="both-missing"),
    ],
)
def test_resolve_authority_missing_file_is_epoch1(
    tmp_path: Path, declared: bool, marked: bool, gap: AuthorityGap
) -> None:
    root = _tree(tmp_path, declared=declared, marked=marked)

    authority = resolve_authority(root)

    assert authority.epoch == 1
    assert authority.gap is gap
    assert authority.target is None
    assert authority.generation_id is None


def test_resolve_authority_nonexistent_root_is_epoch1(tmp_path: Path) -> None:
    authority = resolve_authority(tmp_path / "absent")

    assert authority.epoch == 1
    assert authority.gap is AuthorityGap.UNDECLARED


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("{", id="not-json"),
        pytest.param('{"disposable": false, "declared_by": "x", "purpose": "y"}', id="not-true"),
        pytest.param('{"disposable": true}', id="fields-missing"),
    ],
)
def test_resolve_authority_malformed_declaration_is_epoch1(tmp_path: Path, body: str) -> None:
    root = _tree(tmp_path, declared=True, marked=True)
    declaration_path(root).write_text(body, encoding="utf-8")

    authority = resolve_authority(root)

    assert (authority.epoch, authority.gap) == (1, AuthorityGap.UNDECLARED)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("{", id="not-json"),
        pytest.param('{"schema_version": "1", "epoch": 3}', id="wrong-epoch"),
        pytest.param("[]", id="not-an-object"),
    ],
)
def test_resolve_authority_malformed_marker_is_epoch1(tmp_path: Path, body: str) -> None:
    root = _tree(tmp_path, declared=True, marked=True)
    (root / "generations" / "EPOCH2_ACTIVE.json").write_text(body, encoding="utf-8")

    authority = resolve_authority(root)

    assert (authority.epoch, authority.gap) == (1, AuthorityGap.MARKER_UNREADABLE)


def test_resolve_authority_rereads_tree_after_marker_removed(tmp_path: Path) -> None:
    root = _tree(tmp_path, declared=True, marked=True)
    assert resolve_authority(root).epoch == 2

    (root / "generations" / "EPOCH2_ACTIVE.json").unlink()

    assert resolve_authority(root).gap is AuthorityGap.MARKER_ABSENT


def test_resolve_authority_writes_nothing(tmp_path: Path) -> None:
    root = _tree(tmp_path, declared=False, marked=True)
    before = sorted(path.relative_to(root) for path in root.rglob("*"))

    resolve_authority(root)

    assert sorted(path.relative_to(root) for path in root.rglob("*")) == before


def test_require_native_authority_epoch2_returns_target(tmp_path: Path) -> None:
    root = _tree(tmp_path, declared=True, marked=True)

    authority = require_native_authority(root)

    assert authority.epoch == 2
    assert authority.target is not None


@pytest.mark.parametrize(
    ("declared", "marked", "gap"),
    [
        pytest.param(False, True, AuthorityGap.UNDECLARED, id="declaration-missing"),
        pytest.param(True, False, AuthorityGap.MARKER_ABSENT, id="marker-missing"),
    ],
)
def test_require_native_authority_epoch1_raises_typed_code(
    tmp_path: Path, declared: bool, marked: bool, gap: AuthorityGap
) -> None:
    root = _tree(tmp_path, declared=declared, marked=marked)

    with pytest.raises(NativeAuthorityRequiredError) as caught:
        require_native_authority(root)

    assert caught.value.code == NATIVE_AUTHORITY_REQUIRED == "native_authority_required"
    assert caught.value.gap is gap
    assert gap.value in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_root_authority_epoch2_without_target_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="epoch-2 answer"):
        RootAuthority(root=tmp_path, epoch=2, generation_id=GENERATION_ID)


def test_root_authority_epoch1_without_gap_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="epoch-1 answer"):
        RootAuthority(root=tmp_path, epoch=1)


def test_root_authority_epoch1_with_generation_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="epoch-1 answer"):
        RootAuthority(
            root=tmp_path, epoch=1, gap=AuthorityGap.MARKER_ABSENT, generation_id=GENERATION_ID
        )


def test_root_authority_unknown_epoch_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        RootAuthority.model_validate({"root": str(tmp_path), "epoch": 3, "gap": "undeclared"})


def test_canary_repository_ref_round_trips() -> None:
    ref = CanaryRepositoryRef.model_validate({"repository": CANARY_URN, "project_code": "CANARY"})

    assert json.loads(ref.model_dump_json()) == {
        "repository": CANARY_URN,
        "project_code": "CANARY",
    }


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {
                "repository": "eawf://WSP-CANARY/PRJ-CANARY/REP-CANARY/milestone/MLS-0001",
                "project_code": "CANARY",
            },
            id="milestone-urn",
        ),
        pytest.param({"repository": CANARY_URN, "project_code": "canary"}, id="lowercase-code"),
        pytest.param({"repository": CANARY_URN}, id="code-missing"),
        pytest.param(
            {"repository": CANARY_URN, "project_code": "CANARY", "root": "x"}, id="extra-key"
        ),
    ],
)
def test_canary_repository_ref_rejects_invalid_payload(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        CanaryRepositoryRef.model_validate(payload)


# ---- doctor surface -------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "marked", "status", "detail"),
    [
        (False, False, "ok", "epoch 1 (undeclared)"),
        (False, True, "warn", "epoch 1 (undeclared): generations/EPOCH2_ACTIVE.json exists"),
        (True, False, "warn", "epoch 1 (marker_absent)"),
        (True, True, "ok", f"epoch 2 (generation {GENERATION_ID})"),
    ],
)
def test_check_authority_epoch_reports_each_tree(
    tmp_path: Path, declared: bool, marked: bool, status: str, detail: str
) -> None:
    _tree(tmp_path, declared=declared, marked=marked)

    result = check_authority_epoch(workspace=tmp_path)

    assert result.name == "authority_epoch"
    assert result.status == status
    assert result.detail is not None
    assert result.detail.startswith(detail)


def test_check_authority_epoch_unreadable_marker_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, declared=True, marked=False)
    (root / "generations").mkdir()
    (root / "generations" / "EPOCH2_ACTIVE.json").write_text("{", encoding="utf-8")

    result = check_authority_epoch(workspace=tmp_path)

    assert result.status == "fail"
    assert (
        result.detail
        == "epoch 1 (marker_unreadable): generations/EPOCH2_ACTIVE.json does not parse"
    )


def test_check_authority_epoch_no_anchor_is_ok() -> None:
    result = check_authority_epoch(workspace=None)

    assert result.status == "ok"
    assert result.detail == "no workspace anchor"


def test_check_authority_epoch_anchor_without_ea_is_undeclared(tmp_path: Path) -> None:
    result = check_authority_epoch(workspace=tmp_path)

    assert result.status == "ok"
    assert result.detail == "epoch 1 (undeclared)"
