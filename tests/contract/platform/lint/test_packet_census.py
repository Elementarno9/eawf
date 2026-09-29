"""The packet census lint over the fixtures its row names.

Requirement row proved here, by id:

- ``LINT-043``: the census scanner reads the proposal directory, regenerates the census
  with the revision it was read at, and exits non-zero naming the id, file and line on a
  duplicate definition, a dangling citation (ranges expanded), a headerless table
  fragment, a graph cycle, a node without a file or a file without a node, a scrub hit and
  a stored count that disagrees with the regeneration. An id inside a fenced block passes
  without backticks, a count in a dated amendment row is history, and where the packet is
  absent -- as it is in CI -- the lint says so and passes.

The fire-proof case is the defect the scanner found in the real packet on its first run:
the newest stored census still read 1168 distinct ids after two families had grown.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.packet_census import Finding, check_packet, main, regenerate, render_census

_GRAPH = """\
```mermaid
flowchart TD
    A["00 Authority"] --> R["10 Release"]
    R --> S["90 Source map"]
```
"""

_RELEASE = """\
# 10 Release

| ID | Requirement |
|---|---|
| `REL-001` | The train has rungs. |
| `REL-002` | Each rung has a profile. |
| `REL-004` | The last rung is stable. |

REL-003 is deliberately unallocated; see `REL-001` and `REL-002`.
"""

_SOURCE = """\
# 90 Source map

## Regenerated censuses

| Family | Home | Defined | Max | Next free | Gaps below max |
|---|---|---|---|---|---|
| `REL` | `10` | 3 | 004 | 005 | 003 |

Distinct defined requirement identifiers across the numbered files: **3**

## Amendments

| Date | Change |
|---|---|
| 2026-08-25 | Recorded 1168 identifiers then. |
"""


def _packet(root: Path, **overrides: str) -> Path:
    """Write a clean three-file packet under ``root``, with files replaced by ``overrides``."""
    packet = root / "packet"
    packet.mkdir()
    files = {
        "00-authority.md": f"# 00 Authority\n\n{_GRAPH}",
        "10-release.md": _RELEASE,
        "90-source.md": _SOURCE,
    }
    files.update({name.replace("_", "-") + ".md": text for name, text in overrides.items()})
    for name, text in files.items():
        (packet / name).write_text(text, encoding="utf-8")
    return packet


def _reasons(findings: list[Finding]) -> list[str]:
    return [finding.render() for finding in findings]


def test_lint_043_a_clean_packet_passes_and_regenerates_its_census(tmp_path: Path) -> None:
    """The fixture packet is consistent, and its census records the gap it leaves."""
    census, findings = check_packet(_packet(tmp_path))
    assert findings == []
    assert census["REL"].defined == 3
    assert census["REL"].gaps == (3,)


def test_lint_043_a_duplicate_definition_fails(tmp_path: Path) -> None:
    """Two rows defining one id name the second place and both locations."""
    doubled = _RELEASE.replace("| `REL-004` |", "| `REL-002` | Again. |\n| `REL-004` |")
    _census, findings = check_packet(_packet(tmp_path, **{"10_release": doubled}))
    assert "10-release.md:7: REL-002: defined twice (10-release.md:6, 10-release.md:7)" in (
        _reasons(findings)
    )


def test_lint_043_a_range_citation_spanning_a_gap_fails(tmp_path: Path) -> None:
    """A range claims every id in it, so one spanning the recorded gap is dangling."""
    cited = f"{_RELEASE}\nSee `REL-001` through `REL-004`.\n"
    _census, findings = check_packet(_packet(tmp_path, **{"10_release": cited}))
    assert _reasons(findings) == ["10-release.md:11: REL-003: cited but defined nowhere"]


def test_lint_043_a_gap_named_on_its_own_is_its_record(tmp_path: Path) -> None:
    """Naming the unallocated id to say it is unallocated is not a dangling citation."""
    _census, findings = check_packet(_packet(tmp_path))
    assert not [f for f in findings if f.subject == "REL-003"]


def test_lint_043_a_citation_above_the_family_maximum_fails(tmp_path: Path) -> None:
    """An id no row defines and no gap records is dangling."""
    cited = f"{_RELEASE}\nSee `REL-009`.\n"
    _census, findings = check_packet(_packet(tmp_path, **{"10_release": cited}))
    assert _reasons(findings) == ["10-release.md:11: REL-009: cited but defined nowhere"]


def test_lint_043_a_table_fragment_fails(tmp_path: Path) -> None:
    """A blank line inside a table leaves the rows after it without a header."""
    split = _RELEASE.replace("| `REL-002` |", "\n| `REL-002` |")
    _census, findings = check_packet(_packet(tmp_path, **{"10_release": split}))
    assert "10-release.md:7: REL-002: headerless definition-row fragment" in _reasons(findings)


def test_lint_043_a_stale_live_count_fails(tmp_path: Path) -> None:
    """The real defect: a stored census that was not regenerated after a family grew."""
    stale = _SOURCE.replace("| 3 | 004 | 005 |", "| 2 | 002 | 003 |").replace("**3**", "**2**")
    _census, findings = check_packet(_packet(tmp_path, **{"90_source": stale}))
    assert _reasons(findings) == [
        "90-source.md:7: REL: states defined 2 max 002 next 003; census defined 3 max 004 next 005",
        "90-source.md:9: distinct ids: states 2, census 3",
    ]


def test_lint_043_a_count_in_a_dated_amendment_row_is_history(tmp_path: Path) -> None:
    """The fixture's amendment row records 1168 and is never compared."""
    _census, findings = check_packet(_packet(tmp_path))
    assert not [f for f in findings if "1168" in f.reason]


def test_lint_043_a_graph_cycle_fails(tmp_path: Path) -> None:
    """An edge back to the root makes every node on the loop a finding."""
    cyclic = _GRAPH.replace("    R --> S", "    S --> A\n    R --> S")
    _census, findings = check_packet(_packet(tmp_path, **{"00_authority": f"# 00\n\n{cyclic}"}))
    assert _reasons(findings) == [
        "00-authority.md:0: node A: is on a cycle",
        "00-authority.md:0: node R: is on a cycle",
        "00-authority.md:0: node S: is on a cycle",
    ]


def test_lint_043_a_node_without_a_file_and_a_file_without_a_node_fail(tmp_path: Path) -> None:
    """The graph and the numbered files must name the same set."""
    graph = _GRAPH.replace('S["90 Source map"]', 'S["95 Missing"]')
    _census, findings = check_packet(_packet(tmp_path, **{"00_authority": f"# 00\n\n{graph}"}))
    assert _reasons(findings) == [
        "00-authority.md:0: node S: names file 95, which does not exist",
        "00-authority.md:0: file 90: has no node in the dependency graph",
    ]


def test_lint_043_a_fenced_unbackticked_id_passes(tmp_path: Path) -> None:
    """Inside a fence an id needs no backticks, and a table-shaped line is not a fragment."""
    fenced = f"{_RELEASE}\n```text\nREL-001 then REL-002\n| `REL-004` | quoted |\n```\n"
    _census, findings = check_packet(_packet(tmp_path, **{"10_release": fenced}))
    assert findings == []


def test_lint_043_a_scrub_hit_fails_without_echoing_it(tmp_path: Path) -> None:
    """A local path in the packet is a finding that names its kind, never the path."""
    local = "/".join(["", "home", "someone", "notes.md"])
    leaky = f"{_RELEASE}\nRead {local} first.\n"
    _census, findings = check_packet(_packet(tmp_path, **{"10_release": leaky}))
    assert _reasons(findings) == ["10-release.md:11: absolute_posix_path: scrub hit"]


def test_lint_043_a_family_with_two_home_files_fails(tmp_path: Path) -> None:
    """A family is defined by exactly one file."""
    stray = "# 90\n\n| ID | Requirement |\n|---|---|\n| `REL-005` | Stray. |\n"
    _census, findings = check_packet(
        _packet(tmp_path, **{"90_source": _SOURCE.replace("# 90 Source map\n", stray)})
    )
    assert "90-source.md:0: REL: defined in 2 home files" in _reasons(findings)


def test_lint_043_a_directory_without_numbered_files_is_an_error(tmp_path: Path) -> None:
    """The empty boundary: nothing to scan is refused rather than read as clean."""
    with pytest.raises(FileNotFoundError):
        check_packet(tmp_path)


def test_lint_043_the_census_output_carries_its_revision(tmp_path: Path) -> None:
    """The regenerated table is stamped with the revision it was read at."""
    census, _findings = check_packet(_packet(tmp_path))
    text = render_census(census, "abc12345")
    assert text.startswith("Read at revision `abc12345`.")
    assert "| `REL` | `10` | 3 | 004 | 005 | 003 |" in text
    assert text.endswith("**3**")


def test_lint_043_an_empty_definition_set_regenerates_an_empty_census() -> None:
    """The empty boundary of the generator."""
    assert regenerate({}) == {}


def test_lint_043_main_exits_non_zero_naming_the_offence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure surface: a non-zero exit with the id, file and line."""
    cited = f"{_RELEASE}\nSee `REL-009`.\n"
    packet = _packet(tmp_path, **{"10_release": cited})
    assert main(["--packet", str(packet)]) == 1
    assert "packet-census: 10-release.md:11: REL-009: cited but defined nowhere" in (
        capsys.readouterr().out
    )


def test_lint_043_an_absent_packet_is_skipped_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Where the local packet is absent, as in CI, the lint passes and states why."""
    assert main(["--packet", str(tmp_path / "absent")]) == 0
    assert "is absent (local, gitignored)" in capsys.readouterr().out
