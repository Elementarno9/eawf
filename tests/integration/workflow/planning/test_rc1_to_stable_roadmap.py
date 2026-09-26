"""Applying the rc1, rc2 and stable plans leaves no requirement id unowned.

The tree here is a copy of this repository's own epoch-2 ``.ea``: the
committed generation, its selection and the requirement catalog. Whatever the
three committed proposals would create is removed from the copy first, so the
suite reads the same before and after the operator applies them live. The
proposals are then applied through the daemon's own plan verbs, and the trace
reads the result the way CI does. Every id must come out owned,
deferred or satisfied, and ``check --require-owned`` must exit zero.

The gate-fire proof leaves one plan unapplied: exactly that plan's ids come
back unowned and the same check exits nonzero.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.kernel.store.compaction import read_document, write_document
from tests.integration.workflow.planning._v07_plans import (
    PLAN_NAMES,
    REPO_ROOT,
    create_containers,
    land,
    load_proposal,
)
from tools.requirement_trace import (
    CATALOG_PATH,
    TraceStatus,
    load_catalog,
    load_state_view,
    main,
)

pytestmark = pytest.mark.integration

#: The committed epoch-2 tree, less what the trace and the plan verbs never read.
_TREE_ENTRIES = (
    "generations",
    "config.yaml",
    "epoch2-opt-in.json",
    "requirements.json",
    "rules.yaml",
)


@pytest.fixture(autouse=True)
def history_of_this_repository(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Resolve every git call against this repository's history.

    A satisfied disposition cites the commit that built its ids, so the copied
    tree must see the same object store the committed catalog was written
    against.
    """
    git_dir = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--absolute-git-dir"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    monkeypatch.setenv("GIT_DIR", git_dir)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _planned_keys() -> dict[str, set[str]]:
    """Return, per collection, the record keys the three proposals create."""
    keys: dict[str, set[str]] = {
        "plan_revision": set(),
        "milestone": set(),
        "batch": set(),
        "task": set(),
        "track": {"TRK-EAWF-CORE"},
        "repository": {"EAWF"},
    }
    for name in PLAN_NAMES:
        proposal = load_proposal(name)
        body = proposal["body"]
        keys["plan_revision"].add(proposal["key"])
        keys["milestone"].add(body["milestone"]["key"])
        keys["batch"].update(batch["urn"].rsplit("/", 1)[1] for batch in body["batches"])
        keys["task"].update(task["urn"].rsplit("/", 1)[1] for task in body["tasks"])
    return keys


def _copied_tree(tmp_path: Path) -> Path:
    """Copy this repository's epoch-2 tree without the plans, then create what they bind."""
    root = tmp_path / "repo"
    (root / ".ea").mkdir(parents=True)
    for entry in _TREE_ENTRIES:
        source = REPO_ROOT / ".ea" / entry
        target = root / ".ea" / entry
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    authority = require_native_authority(root / ".ea")
    assert authority.target is not None and authority.generation_id is not None
    document_path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(document_path)
    for collection, keys in _planned_keys().items():
        for key in keys:
            document.get(collection, {}).pop(key, None)
    write_document(document_path, document)
    create_containers(root, tmp_path / "runtime")
    return root


def _atom_ids(name: str) -> set[str]:
    return {atom["atom_id"] for atom in load_proposal(name)["body"]["source_atoms"]}


def _unowned(root: Path) -> set[str]:
    return {
        row.id
        for row in load_catalog(root / CATALOG_PATH).requirements
        if row.status is TraceStatus.UNOWNED
    }


def test_without_the_plans_exactly_the_planned_ids_are_unowned(tmp_path: Path) -> None:
    root = _copied_tree(tmp_path)

    assert main(["--repo-root", str(root), "write"]) == 0

    planned = set().union(*(_atom_ids(name) for name in PLAN_NAMES))
    assert _unowned(root) == planned
    assert main(["--repo-root", str(root), "check", "--require-owned"]) == 1


def test_after_the_three_plans_apply_every_id_is_accounted_for(tmp_path: Path) -> None:
    root = _copied_tree(tmp_path)

    for name in PLAN_NAMES:
        land(name, root, tmp_path / "runtime")
    assert main(["--repo-root", str(root), "write"]) == 0

    catalog = load_catalog(root / CATALOG_PATH)
    assert catalog.summary.unowned == 0
    assert catalog.summary.satisfied == sum(len(s.ids) for s in catalog.satisfactions)
    native_owners = {
        owner for row in catalog.requirements if row.id in _atom_ids("rc1") for owner in row.owners
    }
    assert native_owners, "the rc1 ids are owned by its native Tasks"
    assert all(owner.startswith("EAWF-01") for owner in native_owners)
    assert load_state_view(root).phases["P35"].closed
    assert main(["--repo-root", str(root), "check", "--require-owned"]) == 0


@pytest.mark.parametrize("left_out", PLAN_NAMES)
def test_leaving_one_plan_unapplied_returns_its_ids_unowned(tmp_path: Path, left_out: str) -> None:
    root = _copied_tree(tmp_path)

    for name in PLAN_NAMES:
        if name != left_out:
            land(name, root, tmp_path / "runtime")
    assert main(["--repo-root", str(root), "write"]) == 0

    assert _unowned(root) == _atom_ids(left_out)
    assert main(["--repo-root", str(root), "check", "--require-owned"]) == 1
