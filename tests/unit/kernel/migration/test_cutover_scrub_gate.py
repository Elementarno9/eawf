"""The scrub gate tells a documented placeholder from a real home path.

The corpus being imported carries the project's own secrets-hygiene rule,
whose text exists to show what a leak looks like. A naive home-directory
scan reds on that text, so the gate reuses the repository path-leak lint's
placeholder exemption rather than inventing a second rule -- and
:func:`test_placeholder_exemption_agrees_with_the_repository_lint` pins the
two predicates to each other so they cannot drift.

No concrete home-directory path is committed in this file. The anchors are
spelled in fragments the scanner's own regex cannot complete, and the
username segment is read from the running user's home at test time, so the
repository's path-leak lint stays green over the file that proves the gate
reds on a leak.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eawf.kernel.migration.epoch2.errors import MigrationHomePathLeakError
from eawf.kernel.migration.epoch2.scrub import (
    PLACEHOLDER_TOKENS,
    ScrubFinding,
    home_path_patterns,
    is_documented_placeholder,
    require_no_home_path_leaks,
    scan_payload,
    scan_staged_tree,
    scan_text,
)
from eawf.surfaces.cli.commands.hook import _is_placeholder_path

pytestmark = pytest.mark.unit

#: The three home-directory anchors the gate scans for, each split so this
#: file carries no substring the scanner's own pattern can match.
MACOS_ANCHOR = "/Users" + "/"
LINUX_ANCHOR = "/home" + "/"
WINDOWS_ANCHOR = "C:" + "\\Users" + "\\"

#: One documented placeholder per anchor: an angle-bracketed name to fill
#: in for two of them, an elided tail for the third. These are the forms
#: the imported corpus's own rule text carries.
PLACEHOLDER_FORMS = (
    f"{MACOS_ANCHOR}<name>/Workspace/project",
    f"{LINUX_ANCHOR}<user>/projects/project",
    f"{WINDOWS_ANCHOR}...",
)


def _real_home_path() -> str:
    """Return a concrete home-directory path, built at test time.

    Returns:
        A path under the macOS home anchor carrying the running user's own
        home-directory name, so the string is a genuine home path that no
        committed literal spells out. A home whose name cannot be read
        falls back to a concrete segment, which is equally non-placeholder.
    """
    return f"{MACOS_ANCHOR}{Path.home().name or 'operator'}/Workspace/corpus"


def _seed_tree(tmp_path: Path, *, intent: str) -> Path:
    """Write a one-row staged tree whose Task intent carries ``intent``."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"schema_version": "2", "task": {"W01": {"status": "DRAFT", "intent": intent}}}),
        encoding="utf-8",
    )
    return state_path


def test_gate_reports_no_hits_over_the_three_placeholder_forms() -> None:
    """The corpus's own rule text is documentation, so the gate stays quiet."""
    payload = {"rules": {"secrets_hygiene": list(PLACEHOLDER_FORMS)}}

    findings = scan_payload(locator="document", payload=payload)

    assert findings == ()
    require_no_home_path_leaks(findings)


@pytest.mark.parametrize("form", PLACEHOLDER_FORMS)
def test_each_placeholder_form_is_exempt_on_its_own(form: str) -> None:
    """Each anchor's placeholder is exempt independently of the other two."""
    assert scan_text(locator="document.rule", text=form) == ()


def test_gate_reports_one_hit_over_a_real_home_directory_path() -> None:
    """A concrete home path carries no placeholder token, so it is reported."""
    leak = _real_home_path()

    findings = scan_text(locator="waves.W01.intent", text=leak)

    assert len(findings) == 1
    assert findings[0].locator == "waves.W01.intent"
    assert findings[0].snippet in leak
    assert not is_documented_placeholder(findings[0].snippet)


@pytest.mark.parametrize("anchor", [MACOS_ANCHOR, LINUX_ANCHOR, WINDOWS_ANCHOR])
def test_every_anchor_reports_its_own_concrete_path(anchor: str) -> None:
    """All three home anchors are scanned, not just the host platform's."""
    assert len(scan_text(locator="document", text=f"{anchor}devuser/corpus")) == 1


def test_the_gate_refuses_a_staged_tree_carrying_a_real_path(tmp_path: Path) -> None:
    """A leak in the written tree refuses the cutover and names the row."""
    state_path = _seed_tree(tmp_path, intent=f"ship from {_real_home_path()}")

    with pytest.raises(MigrationHomePathLeakError) as excinfo:
        require_no_home_path_leaks(scan_staged_tree(state_path))

    assert "document.task.W01.intent" in str(excinfo.value)
    assert excinfo.value.code == "migration_home_path_leak"


def test_the_gate_admits_a_staged_tree_carrying_only_placeholders(tmp_path: Path) -> None:
    """The same tree with the documented forms passes untouched."""
    state_path = _seed_tree(tmp_path, intent=" and ".join(PLACEHOLDER_FORMS))

    require_no_home_path_leaks(scan_staged_tree(state_path))


def test_the_gate_rewrites_no_source_string(tmp_path: Path) -> None:
    """Scanning is a read: the bytes on disk are identical afterwards."""
    leak = _real_home_path()
    state_path = _seed_tree(tmp_path, intent=leak)
    before = state_path.read_bytes()

    findings = scan_staged_tree(state_path)

    assert len(findings) == 1
    assert state_path.read_bytes() == before
    assert json.loads(state_path.read_text("utf-8"))["task"]["W01"]["intent"] == leak


def test_the_gate_scans_the_ledger_lines_it_is_given(tmp_path: Path) -> None:
    """A leak that landed in a ledger line is reported against that line."""
    state_path = _seed_tree(tmp_path, intent="clean")
    ledger = tmp_path / ".ea" / "ledger" / "task.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({"record_key": "W02", "payload": {"intent": "clean"}})
        + "\n"
        + json.dumps({"record_key": "W03", "payload": {"intent": _real_home_path()}})
        + "\n",
        encoding="utf-8",
    )

    findings = scan_staged_tree(state_path, ledger_paths=[ledger])

    assert [finding.locator for finding in findings] == ["task.jsonl:2.payload.intent"]


def test_an_object_key_is_scanned_as_well_as_a_value(tmp_path: Path) -> None:
    """A row keyed by a path leaks exactly as much as one valued by it."""
    findings = scan_payload(locator="document", payload={_real_home_path(): "x"})

    assert len(findings) == 1


def test_empty_and_missing_surfaces_report_nothing(tmp_path: Path) -> None:
    """An absent document and an empty string are both quiet, not errors."""
    assert scan_staged_tree(tmp_path / ".ea" / "state.json") == ()
    assert scan_text(locator="document", text="") == ()
    assert scan_payload(locator="document", payload={}) == ()
    assert scan_payload(locator="document", payload=None) == ()


def test_the_gate_admits_an_empty_finding_list() -> None:
    """Nothing found is a pass, and says so by returning rather than raising."""
    assert require_no_home_path_leaks(()) is None


def test_the_refusal_reports_the_exact_total_it_truncates(tmp_path: Path) -> None:
    """A long finding list names a bounded prefix and counts them all."""
    findings = tuple(
        ScrubFinding(locator=f"document.row{index}", snippet=f"{MACOS_ANCHOR}dev{index}")
        for index in range(25)
    )

    with pytest.raises(MigrationHomePathLeakError) as excinfo:
        require_no_home_path_leaks(findings)

    message = str(excinfo.value)
    assert "carries 25 concrete home-directory paths" in message
    assert message.endswith(", ...")
    assert "document.row9:" in message
    assert "document.row10:" not in message


def test_a_finding_rejects_an_empty_locator() -> None:
    """A finding an operator cannot locate is not a usable report."""
    with pytest.raises(ValueError, match="locator"):
        ScrubFinding(locator="", snippet=f"{MACOS_ANCHOR}devuser")


def test_a_malformed_staged_document_raises_rather_than_passing(tmp_path: Path) -> None:
    """A document the writer did not leave behind is an error, not a clean scan."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        scan_staged_tree(state_path)


def test_placeholder_exemption_agrees_with_the_repository_lint() -> None:
    """The cutover gate and the repository path-leak lint share one exemption."""
    samples = (
        *PLACEHOLDER_FORMS,
        _real_home_path(),
        f"{MACOS_ANCHOR}devuser",
        *PLACEHOLDER_TOKENS,
    )

    for sample in samples:
        assert is_documented_placeholder(sample) == _is_placeholder_path(sample)


def test_the_anchors_are_the_scrubbers_own_patterns() -> None:
    """The gate selects its anchors from the emit-time scrubber's table."""
    from eawf.observability.logging.scrub import SensitiveScrubber

    patterns = home_path_patterns()

    assert len(patterns) == 3
    assert all(pattern in SensitiveScrubber.PATTERNS for pattern in patterns)
