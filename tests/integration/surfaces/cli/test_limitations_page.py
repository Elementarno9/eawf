"""The limitations help topic names what this release defers and certifies.

The page is prose, so it drifts silently when a deferral row, a
certification or the CI matrix changes. Each test reads the committed
record the page summarises and fails when the page leaves a part of it out.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import help as help_cmd
from eawf.workflow.release.train import STABLE_RELEASE_CONFIG_YAML

pytestmark = pytest.mark.integration

_REPO_ROOT: Final = Path(__file__).resolve().parents[4]
_PAGE: Final = _REPO_ROOT / "docs" / "help" / "limitations.md"
_REQUIREMENTS: Final = _REPO_ROOT / ".ea" / "requirements.json"
_EXPORT: Final = (
    _REPO_ROOT
    / ".ea"
    / "artifacts"
    / "evidence"
    / "2026-10-30-stable-native-linux"
    / "native-canary-evidence.json"
)
_CI: Final = _REPO_ROOT / ".github" / "workflows" / "ci.yaml"

_ID_RANGE: Final = re.compile(r"\b([A-Z]+)-(\d{3})\.\.(\d{3})\b")
_ID: Final = re.compile(r"\b([A-Z]+-\d{3})\b")
_OS_NAMES: Final = {"linux": "Linux", "macos": "macOS", "windows": "Windows"}

runner = CliRunner()


def _page() -> str:
    """Return the authored limitations page."""
    return _PAGE.read_text(encoding="utf-8")


def _deferral(decision: str) -> dict[str, Any]:
    """Return the committed deferral row that cites *decision*."""
    data = json.loads(_REQUIREMENTS.read_text(encoding="utf-8"))
    rows = [row for row in data["deferrals"] if row["decision"] == decision]
    assert len(rows) == 1, f"expected one {decision} deferral row, got {len(rows)}"
    return rows[0]


def _paragraph(text: str, needle: str) -> str:
    """Return the one paragraph of *text* that contains *needle*."""
    found = [line for line in text.splitlines() if needle in line]
    assert len(found) == 1, f"expected one paragraph naming {needle!r}, got {len(found)}"
    return found[0]


def _named_ids(text: str) -> set[str]:
    """Return every requirement id *text* names, expanding ``RUN-031..039`` ranges."""
    ids = set(_ID.findall(text))
    for prefix, low, high in _ID_RANGE.findall(text):
        ids.update(f"{prefix}-{n:03d}" for n in range(int(low), int(high) + 1))
    return ids


def _export() -> dict[str, Any]:
    """Return the committed native canary evidence export."""
    data: dict[str, Any] = json.loads(_EXPORT.read_text(encoding="utf-8"))
    return data


# --- Helpers ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", set()),
        ("RUN-031", {"RUN-031"}),
        ("RUN-031..031", {"RUN-031"}),
        ("RUN-031..033", {"RUN-031", "RUN-032", "RUN-033"}),
        ("QUAL-051..055 and RUN-039", {f"QUAL-{n:03d}" for n in range(51, 56)} | {"RUN-039"}),
    ],
)
def test_named_ids_expands_ranges(text: str, expected: set[str]) -> None:
    """Single ids and inclusive ``..`` ranges, from empty to several, expand exactly."""
    assert _named_ids(text) == expected


def test_paragraph_refuses_a_missing_or_repeated_needle() -> None:
    """A needle on no line or on two lines is an ambiguous page, not a match."""
    with pytest.raises(AssertionError, match="got 0"):
        _paragraph("a\nb", "D64")
    with pytest.raises(AssertionError, match="got 2"):
        _paragraph("D64 a\nD64 b", "D64")


def test_deferral_refuses_an_unknown_decision() -> None:
    """A decision with no deferral row fails instead of yielding an empty list."""
    with pytest.raises(AssertionError, match="got 0"):
        _deferral("D00")


# --- The page ---------------------------------------------------------------


def test_limitations_is_a_registered_topic() -> None:
    """``eawf help limitations`` renders the authored page, exit 0."""
    assert "limitations" in help_cmd.TOPICS
    result = runner.invoke(app, ["help", "limitations"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == _page().splitlines()[0]


def test_page_names_every_d64_deferral() -> None:
    """Every id the D64 row defers, its count and its release appear in one paragraph."""
    row = _deferral("D64")
    paragraph = _paragraph(_page(), "D64")
    missing = sorted(set(row["ids"]) - _named_ids(paragraph))
    assert not missing, f"D64 deferrals missing from the page: {missing}"
    assert f"{len(row['ids'])} requirement ids" in paragraph
    assert row["release"] in paragraph


def test_page_states_the_0_8_deferred_id_count() -> None:
    """The 0.8.0 paragraph names the committed row's decision, its id count and its release."""
    data = json.loads(_REQUIREMENTS.read_text(encoding="utf-8"))
    (row,) = [row for row in data["deferrals"] if row["release"] == "0.8.0"]
    assert row["decision"] == "D91"
    paragraph = _paragraph(_page(), f"Decision {row['decision']} ")
    assert f"{row['decision']} defers {len(row['ids'])} requirement ids to 0.8.0" in paragraph


def test_page_names_every_exported_certification() -> None:
    """The export paragraph names the export and each certification it records."""
    export = _export()
    paragraph = _paragraph(_page(), "committed evidence export")
    assert f"`{_EXPORT.relative_to(_REPO_ROOT).as_posix()}`" in paragraph
    records = export["certifications"]
    # The page counts the certifications in words, so a third one must reword it.
    assert len(records) == 2 and "both certifications" in paragraph, paragraph
    for record in records:
        version = record["certification"]["distribution_version"]
        assert f"{record['runtime_id']} {version}" in paragraph, paragraph
    if all(record["version_range"] is not None for record in records):
        assert "local store" not in paragraph, paragraph


def test_page_states_each_platform_claim_as_certified_or_ci_tested() -> None:
    """Each platform claim of the 0.7.0 config is named certified or CI-tested only."""
    claims = yaml.safe_load(STABLE_RELEASE_CONFIG_YAML)["release"]["platform_claims"]
    certified = {c["certification"]["os_class"] for c in _export()["certifications"]}
    paragraph = _paragraph(_page(), "platform claims:")
    named = re.findall(r"`([a-z0-9_-]+)`, which is (certified|CI-tested only)", paragraph)
    expected = [
        (
            claim["platform_id"],
            "certified" if claim["platform_id"].split("-")[0] in certified else "CI-tested only",
        )
        for claim in claims
    ]
    assert sorted(named) == sorted(expected), paragraph


def test_page_names_every_certified_runtime_range() -> None:
    """Each advertised runtime names its exported range and certified version."""
    page = _page()
    export = _export()
    advertised = {claim["runtime_id"] for claim in export["advertised"]}
    exported = {record["runtime_id"]: record for record in export["certifications"]}
    assert set(exported) == advertised, f"advertised {advertised}, certified {set(exported)}"
    for runtime, record in exported.items():
        line = _paragraph(page, f"`{runtime}`")
        version_range = record["version_range"]
        assert version_range is not None, f"{runtime} exported without its certified range"
        low, high = version_range["min_version"], version_range["max_version"]
        assert f"{low} to {high}" in line, f"{runtime} range missing: {line}"
        assert record["certification"]["distribution_version"] in line


def test_page_names_every_certified_and_ci_tested_os() -> None:
    """Every exported OS class is named certified; every CI matrix OS is named tested."""
    export = _export()
    certified = {c["certification"]["os_class"] for c in export["certifications"]}
    certified |= {os for entry in export["advertised"] for os in entry["os_classes"]}
    claim = _paragraph(_page(), "certified on")
    for os_class in sorted(certified):
        assert _OS_NAMES[os_class] in claim, f"certified OS {os_class} missing: {claim}"
    matrix = yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]
    runners = {
        runner_os
        for job in matrix.values()
        for runner_os in job.get("strategy", {}).get("matrix", {}).get("os", [])
    }
    tested = _paragraph(_page(), "CI runs")
    for runner_os in sorted(runners):
        assert f"`{runner_os}`" in tested, f"CI OS {runner_os} missing: {tested}"
