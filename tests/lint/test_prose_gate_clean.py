"""The strict prose gate exits 0 over the Markdown this branch changes.

The ``prose-gate`` CI job runs ``eawf hook validate-prose --strict`` over the
Markdown a pull request touches, and any composed finding reds the PR. That job
only exists on a pull request, so the first time anyone sees it is the moment it
blocks a merge. This gate brings the same assertion forward into the test suite.

Scope is the branch delta, not the whole corpus: the repo carries ~1,250
deterministic findings in Markdown written long before these lints existed, and
the CI job never reads those files. Scanning everything here would red for
reasons the branch did not cause, so this test scans exactly what the job would.

The Vale leg is deliberately excluded. Vale is a subprocess that fails open when
its binary is absent or its ``StylesPath`` is unsynced, so asserting on it would
make this test pass or fail by machine. The deterministic EAWF013 / EAWF014 /
EAWF017 / EAWF021 / EAWF022 legs are pure functions and are the blocking floor
that ``--strict`` enforces everywhere.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.platform.lint.validate_prose import ProseFinding, validate_prose
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Diff bases tried in order. ``origin/main`` mirrors the CI job's default; the
#: local ``main`` covers a clone whose remote ref was never fetched.
_DIFF_BASES: tuple[str, ...] = ("origin/main", "main")

#: A known-bad surface: a bare inline URL trips EAWF017, and the second prose
#: line trips EAWF014. Used to prove this gate can actually fail -- a gate that
#: cannot red on a broken input is not a gate.
_KNOWN_BAD_MD = (
    "The runner budget was raised, see https://example.org/jitter for the\n"
    "trace that motivated it.\n"
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _changed_markdown() -> list[str] | None:
    """Return branch-changed ``.md`` repo-relative paths, or ``None`` if unknown.

    Returns:
        The repo-relative paths that still exist on disk, or ``None`` when no
        diff base resolves (a shallow clone, or a fork with no ``main``).
    """
    for base in _DIFF_BASES:
        if _git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").returncode != 0:
            continue
        proc = _git("diff", "--name-only", f"{base}...HEAD", "--", "*.md")
        if proc.returncode != 0:
            continue
        return [
            rel for rel in proc.stdout.splitlines() if rel.strip() and (_REPO_ROOT / rel).is_file()
        ]
    return None


def _deterministic_findings(rel: str) -> list[ProseFinding]:
    source = (_REPO_ROOT / rel).read_text(encoding="utf-8")
    return list(validate_prose(source, strict=True).findings)


def test_changed_markdown_has_no_deterministic_prose_finding() -> None:
    """Every ``.md`` this branch changes passes the strict chokepoint."""
    changed = _changed_markdown()
    if changed is None:
        pytest.skip("no diff base (origin/main or main) resolves in this checkout")
    if not changed:
        pytest.skip("this branch changes no Markdown")

    offenders: dict[str, list[str]] = {}
    for rel in changed:
        findings = _deterministic_findings(rel)
        if findings:
            offenders[rel] = [f.render() for f in findings]

    assert not offenders, "the strict prose gate would red on this branch:\n" + "\n".join(
        f"  {rel}\n" + "\n".join(f"    {row}" for row in rows)
        for rel, rows in sorted(offenders.items())
    )


def test_strict_gate_exits_zero_over_changed_markdown(monkeypatch) -> None:
    """The CLI itself exits 0 -- the criterion is the exit code, not the count."""
    changed = _changed_markdown()
    if changed is None:
        pytest.skip("no diff base (origin/main or main) resolves in this checkout")
    if not changed:
        pytest.skip("this branch changes no Markdown")

    # Force the Vale leg absent so the assertion is on the deterministic floor
    # and does not depend on whether this machine ran `vale sync`.
    monkeypatch.setattr("shutil.which", lambda _name: None)
    result = CliRunner().invoke(
        app,
        ["hook", "validate-prose", "--strict", *(str(_REPO_ROOT / rel) for rel in changed)],
    )
    assert result.exit_code == 0, result.stdout


def test_strict_gate_reds_on_a_known_bad_surface(tmp_path, monkeypatch) -> None:
    """The falsifier: the same code path must exit 1 on a broken input."""
    monkeypatch.setattr("shutil.which", lambda _name: None)
    bad = tmp_path / "bad.md"
    bad.write_text(_KNOWN_BAD_MD, encoding="utf-8")

    assert validate_prose(_KNOWN_BAD_MD, strict=True).findings
    result = CliRunner().invoke(app, ["hook", "validate-prose", "--strict", str(bad)])
    assert result.exit_code == 1, result.stdout


def test_changed_markdown_helper_tolerates_a_missing_base(monkeypatch) -> None:
    """An unresolvable diff base yields ``None`` rather than raising."""
    monkeypatch.setattr(
        f"{__name__}._git",
        lambda *args: subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr=""),
    )
    assert _changed_markdown() is None
