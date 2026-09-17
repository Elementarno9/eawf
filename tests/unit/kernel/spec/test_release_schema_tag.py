"""Tests for the record tag of :class:`eawf.kernel.spec.release.Release`.

Readers accept ``release/v1`` and ``release/v2``; every row written now is
``release/v2``. The committed release-record collection is read from a
copy under ``tmp_path``, so nothing here writes the real store.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.release import RELEASE_SCHEMA_VERSION, Release, ReleaseChannel
from eawf.workflow.release.records import (
    read_release_record,
    read_release_records,
    record_release,
    release_records_path,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_COMMITTED_RECORDS = _REPO_ROOT / ".ea" / "store" / "release_record.jsonl"
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"
_NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
_SECTION_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\]\s*$")


def _release(**overrides: Any) -> Release:
    """Build a DRAFT ``0.7.0.dev2`` record with *overrides* applied."""
    payload: dict[str, Any] = {
        "uid": UUID(int=2),
        "key": "REL-0.7.0.dev2",
        "version": "0.7.0.dev2",
        "channel": ReleaseChannel.DEV,
        "authority_epoch": 1,
    }
    payload.update(overrides)
    return Release(**payload)


def _state_path(tmp_path: Path) -> Path:
    """Return a ``state.json`` path whose store directory lives under *tmp_path*."""
    return tmp_path / ".ea" / "state.json"


def _committed_lines() -> list[str]:
    """Return the non-blank lines of the committed release-record collection."""
    text = _COMMITTED_RECORDS.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


def _payload_tag(line: str) -> str:
    """Return the record tag carried by one committed envelope line."""
    tag = json.loads(line)["payload"]["schema_version"]
    assert isinstance(tag, str)
    return tag


def _seed_store(state_path: Path, lines: list[str]) -> None:
    """Write *lines* as the release-record collection beside *state_path*."""
    path = release_records_path(state_path)
    path.parent.mkdir(parents=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _changelog_sections(text: str) -> list[tuple[str, list[str]]]:
    """Split a changelog into ``(version, body lines)`` pairs, newest first."""
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        heading = _SECTION_HEADING.match(line)
        if heading is not None:
            sections.append((heading["version"], []))
        elif sections:
            sections[-1][1].append(line)
    return sections


def _subsection(body: list[str], title: str) -> list[str]:
    """Return the lines under ``### <title>`` up to the next subsection."""
    start = body.index(f"### {title}") + 1
    end = next(
        (index for index in range(start, len(body)) if body[index].startswith("### ")),
        len(body),
    )
    return body[start:end]


def test_release_schema_version_defaults_new_records_to_release_v2() -> None:
    record = _release()

    assert RELEASE_SCHEMA_VERSION == "release/v2"
    assert record.schema_version == "release/v2"
    assert record.model_dump(mode="json")["schema_version"] == "release/v2"


@pytest.mark.parametrize("tag", ["release/v1", "release/v2"])
def test_release_schema_version_keeps_an_accepted_tag(tag: str) -> None:
    record = Release.model_validate(_release().model_dump(mode="json") | {"schema_version": tag})

    assert record.schema_version == tag


@pytest.mark.parametrize(
    "tag",
    ["release/v0", "release/v3", "release/V2", "release/v2 ", "release", "", "1.0"],
)
def test_release_schema_version_refuses_any_other_tag(tag: str) -> None:
    payload = _release().model_dump(mode="json") | {"schema_version": tag}

    with pytest.raises(ValidationError, match="schema_version"):
        Release.model_validate(payload)


def test_record_release_round_trips_a_release_v2_row(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    record = _release()

    written = record_release(state_path, record, recorded_at=_NOW, summary="draft")

    row = json.loads(release_records_path(state_path).read_text(encoding="utf-8"))
    assert row["payload"]["schema_version"] == "release/v2"
    assert written == record
    assert read_release_record(state_path, record.key) == record


def test_record_release_writes_release_v2_for_a_record_read_as_release_v1(
    tmp_path: Path,
) -> None:
    state_path = _state_path(tmp_path)
    legacy = _release(schema_version="release/v1", revision=3)

    written = record_release(state_path, legacy, recorded_at=_NOW, summary="publish")

    assert written.schema_version == "release/v2"
    assert written.model_dump(exclude={"schema_version"}) == legacy.model_dump(
        exclude={"schema_version"}
    )
    row = json.loads(release_records_path(state_path).read_text(encoding="utf-8"))
    assert row["id"] == "REL-0.7.0.dev2@3"
    assert row["payload"]["schema_version"] == "release/v2"
    assert read_release_record(state_path, legacy.key) == written


def test_read_release_records_loads_the_committed_release_v1_rows(tmp_path: Path) -> None:
    v1_lines = [line for line in _committed_lines() if _payload_tag(line) == "release/v1"]
    assert v1_lines, f"{_COMMITTED_RECORDS.name} holds no release/v1 row to read"
    state_path = _state_path(tmp_path)
    _seed_store(state_path, v1_lines)

    records = read_release_records(state_path)

    assert {"REL-0.7.0.dev1", "REL-0.7.0.dev2"} <= records.keys()
    assert {record.schema_version for record in records.values()} == {"release/v1"}
    assert records["REL-0.7.0.dev1"].adoption is not None


def test_read_release_records_loads_the_committed_collection_verbatim(tmp_path: Path) -> None:
    lines = _committed_lines()
    state_path = _state_path(tmp_path)
    _seed_store(state_path, lines)

    records = read_release_records(state_path)

    assert {_payload_tag(line) for line in lines} <= {"release/v1", "release/v2"}
    assert {"REL-0.7.0.dev1", "REL-0.7.0.dev2"} <= records.keys()


def test_read_release_records_refuses_a_row_with_an_unknown_tag(tmp_path: Path) -> None:
    envelope = json.loads(_committed_lines()[0])
    envelope["payload"]["schema_version"] = "release/v3"
    state_path = _state_path(tmp_path)
    _seed_store(state_path, [json.dumps(envelope)])

    with pytest.raises(ValueError, match="line 1 payload is not a release"):
        read_release_records(state_path)


def test_changelog_names_the_release_v2_tag() -> None:
    sections = _changelog_sections(_CHANGELOG.read_text(encoding="utf-8"))
    versions = [version for version, _body in sections]
    dev2_index = versions.index("0.7.0.dev2")
    dev2_body = sections[dev2_index][1]

    notes = [
        line
        for line in _subsection(dev2_body, "Limitations")
        if line.startswith("- ") and "`Release.adoption`" in line
    ]

    assert len(notes) == 1, notes
    note = notes[0]
    assert "cannot distinguish a record written before the field existed" not in note
    assert "rejects every such row outright rather than misreading it" in note
    assert "Corrected after publication" in note
    assert "`release/v1`" in note
    assert "The next checkpoint writes `release/v2`" in note
    if dev2_index > 0:
        next_version, next_body = sections[dev2_index - 1]
        assert any("`release/v2`" in line for line in next_body), (
            f"the {next_version} changelog section must record the release/v2 record tag"
        )
