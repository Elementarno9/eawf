"""SURF-172: ``eawf run report <RUN> [--parts ...]`` writes one Run's plain-text report.

The file lands under ``.ea/local/`` as ``<date>-run-report-<RUN>.txt``, one line per
fact; its head states the author, the projection revision, the redaction policy and the
purged ranges before the first fact; a purged range reads ``∅ purged`` in place; secrets
are never carried and the report says so; the consequence block prints before the
write; and nothing canonical moves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.kernel.store.paths import LOCAL_DIRNAME
from eawf.observability.reflect import run_report as rr
from eawf.observability.reflect.runs import read_tree_run
from eawf.platform.scrub.scan import redact_text, scan_text
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from tests.contract.surfaces.cli._reflect_tree import (
    command,
    gap,
    native_tree,
    refuse_egress,
    run_row,
    summarized,
    tree_digest,
)

__all__ = ["refuse_egress"]

runner = CliRunner()

#: A home path and an email the transcript carries; neither may reach the file.
LEAKY_SUMMARY = "read /Users/<name>/project/loader.py and mailed dev@example.com"


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a canary whose Run lost sequences 3 and 4 and quoted a local path."""
    monkeypatch.delenv("EA_STATE", raising=False)
    return native_tree(
        tmp_path,
        [run_row("RUN-00000001")],
        [
            summarized("RUN-00000001", 1, LEAKY_SUMMARY),
            command("RUN-00000001", 2),
            gap("RUN-00000001", 5, expected=3),
            command("RUN-00000001", 6),
        ],
        canonical_sequence=77,
    )


def _report(tree: Path, *args: str) -> tuple[Path, str, str]:
    result = runner.invoke(app, ["-w", str(tree.parent), "run", "report", "RUN-00000001", *args])
    assert result.exit_code == 0, result.stderr
    path = Path(result.stdout.strip())
    return path, path.read_text(encoding="utf-8"), result.stderr


def test_surf_172_the_file_lands_under_local_with_the_dated_name(tree: Path) -> None:
    """The report is a dated local artifact named for its Run."""
    path, _, _ = _report(tree)
    today = datetime.now(UTC).date().isoformat()
    assert path == tree / LOCAL_DIRNAME / f"{today}-run-report-RUN-00000001.txt"


def test_surf_172_the_head_precedes_every_fact(tree: Path) -> None:
    """Author, projection revision, policy and purged ranges come before the first fact."""
    _, text, _ = _report(tree, "--actor", "OP-0001")
    lines = text.splitlines()
    first_fact = next(i for i, line in enumerate(lines) if line.startswith("timeline "))
    head = "\n".join(lines[:first_fact])
    assert "author OP-0001" in head
    assert "projection revision 77 · run revision 3" in head
    assert f"redaction policy: {rr.REDACTION_POLICY}" in head
    assert "purged ranges: 3-4" in head
    assert rr.QUOTABILITY in head


def test_surf_172_a_purged_range_is_written_not_omitted(tree: Path) -> None:
    """Sequences 3-4 read as purged in both the timeline and the transcript."""
    _, text, _ = _report(tree)
    assert f"timeline 3-4 {rr.PURGED_TOKEN} (2 sequences)" in text
    assert f"transcript 3-4 {rr.PURGED_TOKEN}" in text


def test_surf_172_the_report_is_redacted_and_passes_the_scrub(tree: Path) -> None:
    """The home path and the email are rewritten, and the file passes the scrub scan."""
    _, text, stderr = _report(tree)
    assert "/Users/<name>" not in text
    assert "dev@example.com" not in text
    assert "<local-path>" in text
    assert scan_text(text) == []
    assert "redactions: 1 line scrubbed" in stderr


def test_surf_172_secrets_are_never_included_and_the_report_says_so(tree: Path) -> None:
    """Asking for secrets still carries none; the part states the policy instead."""
    _, text, stderr = _report(tree, "--parts", "secrets,timeline")
    assert rr.SECRETS_LINE in text
    assert "part secrets: never · redacted by policy" in stderr
    assert "transcript " not in text
    assert "parts: timeline · secrets never" in text


def test_surf_172_the_consequence_block_names_parts_sizes_and_destination(tree: Path) -> None:
    """The block prints on stderr with each part's size and the destination."""
    path, _, stderr = _report(tree, "--parts", "timeline,sandbox_decisions")
    assert stderr.startswith("consequence: run report ")
    assert "part timeline: 4 lines" in stderr
    assert "part sandbox_decisions: 1 line" in stderr
    assert f"destination: .ea/{LOCAL_DIRNAME}/{path.name}" in stderr
    assert "not: no canonical record moves; nothing leaves the machine" in stderr


def test_surf_172_a_part_with_nothing_to_state_says_so(tree: Path) -> None:
    """A Run the gateway decided nothing for and an uncaptured runtime say so, not a blank."""
    _, text, _ = _report(tree, "--parts", "sandbox_decisions,usage_and_cost")
    assert rr.NO_SANDBOX_DECISIONS in text
    assert "usage_and_cost unavailable: the Run holds no captured runtime" in text


def test_surf_172_nothing_canonical_moves_and_nothing_leaves(
    tree: Path, refuse_egress: list[str]
) -> None:
    """The verb is a local file write: no canonical byte changes and no egress."""
    before = tree_digest(tree, skip=tree / LOCAL_DIRNAME)
    _report(tree)
    assert tree_digest(tree, skip=tree / LOCAL_DIRNAME) == before
    assert refuse_egress == []


def test_surf_172_a_run_the_tree_does_not_hold_is_not_found(tree: Path) -> None:
    """Error path: an unknown Run key is a user error naming it."""
    result = runner.invoke(app, ["-w", str(tree.parent), "run", "report", "RUN-0000FFFF"])
    assert result.exit_code == exit_codes.USER_ERROR
    assert "RUN-0000FFFF" in result.stdout


def test_surf_172_an_unknown_part_is_refused_before_anything_is_read(tree: Path) -> None:
    """Error path: a part outside the declared set is an invalid input."""
    result = runner.invoke(
        app, ["-w", str(tree.parent), "run", "report", "RUN-00000001", "--parts", "billing"]
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert list((tree / LOCAL_DIRNAME).glob("*-run-report-*")) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, rr.DEFAULT_PARTS),
        ("transcript", (rr.ReportPartName.TRANSCRIPT,)),
        (
            "transcript, timeline,transcript",
            (rr.ReportPartName.TIMELINE, rr.ReportPartName.TRANSCRIPT),
        ),
        (",".join(part.value for part in rr.ReportPartName), tuple(rr.ReportPartName)),
    ],
)
def test_surf_172_parts_parse_in_render_order(
    text: str | None, expected: tuple[rr.ReportPartName, ...]
) -> None:
    """Default, single, duplicate and every part, always in render order."""
    assert rr.parse_parts(text) == expected


@pytest.mark.parametrize("text", ["", " , ", "timeline,billing"])
def test_surf_172_an_empty_or_unknown_parts_value_is_refused(text: str) -> None:
    """Error path: no part or an undeclared one is a ValueError."""
    with pytest.raises(ValueError, match="part"):
        rr.parse_parts(text)


def test_surf_172_a_run_is_found_by_its_urn_as_well_as_its_key(tree: Path) -> None:
    """Boundary: the qualified URN names the same Run as its key."""
    by_key = read_tree_run(tree, "RUN-00000001")
    assert read_tree_run(tree, str(by_key.run.urn)).run.key == "RUN-00000001"


def test_surf_172_an_epoch_one_tree_holds_no_run(tmp_path: Path) -> None:
    """Boundary: a tree without a native generation reports the Run as absent."""
    (tmp_path / ".ea").mkdir()
    with pytest.raises(LookupError):
        read_tree_run(tmp_path / ".ea", "RUN-00000001")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ""),
        ("plain words", "plain words"),
        ("mail dev@example.com now", "mail <email> now"),
        ("keep noreply@anthropic.com", "keep noreply@anthropic.com"),
        ("at ~/code and ops@example.com", "at <local-path> and <email>"),
    ],
)
def test_surf_172_the_report_scrub_leaves_nothing_the_scan_flags(text: str, expected: str) -> None:
    """Empty, clean, email, allowlisted email and a mixed line; each passes the scan."""
    assert redact_text(text) == expected
    assert scan_text(redact_text(text)) == []
