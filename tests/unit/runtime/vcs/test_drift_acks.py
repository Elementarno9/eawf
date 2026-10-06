"""Unit tests for the committed git/state drift acknowledgement record.

The release pipeline reads the ack file so a wave the repository accepted as
drifted keeps its stale pin: :func:`eawf.runtime.vcs.wave_sha.load_drift_acks`
must resolve the file outside ``.ea/`` and degrade gracefully on a missing or
malformed file.
"""

from __future__ import annotations

import json
from pathlib import Path

from eawf.runtime.vcs.wave_sha import (
    DRIFT_ACKS_DIRNAME,
    DRIFT_ACKS_FILENAME,
    drift_acks_path,
    load_drift_acks,
)


def test_drift_acks_path_lives_outside_dot_ea(tmp_path: Path) -> None:
    """The ack file lives at ``<repo>/.eawf/drift-acks.json`` -- NOT under .ea/."""
    path = drift_acks_path(tmp_path)
    assert path == tmp_path / DRIFT_ACKS_DIRNAME / DRIFT_ACKS_FILENAME
    assert ".ea" not in path.parts  # daemon state authority untouched
    assert path.parts[-2] == ".eawf"


def test_load_drift_acks_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_drift_acks(tmp_path) == set()


def test_load_drift_acks_malformed_payload_is_empty(tmp_path: Path) -> None:
    """A corrupt ack file degrades to empty -- never crashes the reader."""
    path = drift_acks_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert load_drift_acks(tmp_path) == set()


def test_load_drift_acks_wrong_shape_is_empty(tmp_path: Path) -> None:
    """A JSON file missing the ``acked_wave_ids`` list yields an empty set."""
    path = drift_acks_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"other": [1, 2]}), encoding="utf-8")
    assert load_drift_acks(tmp_path) == set()


def test_load_drift_acks_single_entry(tmp_path: Path) -> None:
    path = drift_acks_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"acked_wave_ids": ["P22-I01-W05"]}), encoding="utf-8")
    assert load_drift_acks(tmp_path) == {"P22-I01-W05"}


def test_load_drift_acks_skips_non_string_entries(tmp_path: Path) -> None:
    path = drift_acks_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"acked_wave_ids": ["P22-I01-W05", 42, None, "P30-I05-W09"]}),
        encoding="utf-8",
    )
    assert load_drift_acks(tmp_path) == {"P22-I01-W05", "P30-I05-W09"}
