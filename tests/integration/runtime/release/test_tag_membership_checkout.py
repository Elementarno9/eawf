"""``eawf release preflight`` resolves dev3 membership refs against a real checkout.

The unit-tier suite
(``tests/unit/surfaces/cli/test_release_tag_membership.py``) pins
``_checkpoint_config`` and ``committed_membership_refs`` as pure
functions. This module proves the same thing through the actual CLI
entry point, against a real git checkout holding nothing but a copy of
this repository's committed canary export -- the shape CI's own
preflight job meets, since it runs from a fresh clone with no
``--membership-ref`` (or any other membership option) to pass and no
release record open yet to read refs from either
(``.github/workflows/release.yaml``, the ``release-preflight`` job).

The checkout is minimal on purpose: nothing here claims the sweep goes
fully green (proof-command gates report ``unavailable`` with no CI
receipts staged, which is a different, already-covered concern). The
only thing under test is that the ``membership`` row resolves and
passes, and that config resolution itself never refuses
``invalid_membership_cardinality`` -- the defect this module's sibling
wave fixed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

DEV3_VERSION = "0.7.0.dev3"

_REPO_ROOT = Path(__file__).resolve().parents[4]

#: Where the committed canary export lives, relative to a checkout root.
_EVIDENCE_RELATIVE = Path(
    ".ea/artifacts/evidence/2026-09-18-dev3-conformance/native-canary-evidence.json"
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command inside *repo*, refusing on failure."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=30
    )


def _bare_checkout(tmp_path: Path, *, with_evidence: bool) -> Path:
    """Return a minimal one-commit checkout, optionally holding the committed export.

    Args:
        tmp_path: Directory the checkout is created under.
        with_evidence: Whether to copy this repository's committed
            canary export into the checkout before the commit.

    Returns:
        The checkout root, with ``origin/main`` pointed at its HEAD.
    """
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.name", "EAWF Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "commit.gpgSign", "false")
    if with_evidence:
        source = _REPO_ROOT / _EVIDENCE_RELATIVE
        destination = repo / _EVIDENCE_RELATIVE
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    else:
        (repo / ".gitkeep").write_text("", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "--message", "seed checkout")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/remotes/origin/main", head)
    return repo


def _preflight(repo: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, dict[str, object]]:
    """Run ``eawf --json release preflight`` inside *repo* and decode its output."""
    monkeypatch.chdir(repo)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    result = CliRunner().invoke(
        app, ["--json", "release", "preflight", DEV3_VERSION, "--source", head]
    )
    return result.exit_code, json.loads(result.output)


def test_preflight_resolves_membership_from_the_committed_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate-fire proof: a checkout holding only the committed export sweeps clean."""
    repo = _bare_checkout(tmp_path, with_evidence=True)
    exit_code, payload = _preflight(repo, monkeypatch)
    membership_rows = [row for row in payload["signals"] if row["signal"] == "membership"]
    assert membership_rows == [membership_rows[0]], membership_rows
    assert membership_rows[0]["status"] == "pass"
    assert membership_rows[0]["evidence_refs"], "expected the accepted Milestone evidence ref"
    # Non-membership gates go `unavailable` with no CI receipts staged; only
    # the cardinality refusal under test would abort before any row prints.
    assert exit_code != 0
    assert "invalid_membership_cardinality" not in json.dumps(payload)


def test_preflight_refuses_dev3_with_no_committed_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_checkout(tmp_path, with_evidence=False)
    exit_code, payload = _preflight(repo, monkeypatch)
    assert exit_code == 2  # VALIDATION_ERROR
    assert "invalid_membership_cardinality" in json.dumps(payload)
