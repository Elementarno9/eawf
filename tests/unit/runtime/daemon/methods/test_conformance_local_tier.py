"""Certification probe rows land in the machine-local tier, never the committed store.

A background probe runs on whatever machine starts a Run, so the rows it
appends describe that machine and must not dirty the committed tree. New
rows go to ``.ea/local/conformance_stage.jsonl``; the committed
``.ea/store/conformance_stage.jsonl`` is frozen history every reader still
sees beside them.
"""

from __future__ import annotations

import shutil
import stat

# The probe gate is judged by git status on a fixture repository.
import subprocess  # noqa: EAWF024
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.runtime.certification import ConformanceStageRecord
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import local_store_path, store_path
from eawf.observability.doctor.models import RUNTIME_TUPLE_CHECK_PREFIX
from eawf.observability.doctor.runtime_health import run_runtime_tuple_health_checks
from eawf.runtime.daemon.methods.conformance import StoreStageJournal
from eawf.runtime.daemon.runtime_certifier import RuntimeCertifier

pytestmark = pytest.mark.unit

#: Resolved before a test narrows ``PATH`` to the stand-in harness.
GIT: Final = shutil.which("git") or "git"

NOW: Final = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
TUPLE_A: Final = f"sha256:{'a' * 64}"
TUPLE_B: Final = f"sha256:{'b' * 64}"
CLAUDE_VERSION: Final = "2.1.288"
CLAUDE_HELP: Final = "Options: --allowedTools --output-format --resume --continue --session-id"


def _record(stage: str = "probe") -> ConformanceStageRecord:
    """Return one passed stage record of *stage*."""
    document: dict[str, Any] = {
        "stage": stage,
        "outcome": "passed",
        "evidence_ref": "artifact://conformance/fixture",
        "started_at": NOW,
        "completed_at": NOW,
    }
    return ConformanceStageRecord.model_validate(document)


def _envelope_line(tuple_digest: str, ordinal: int, record: ConformanceStageRecord) -> str:
    """Return one journal line as the committed store holds it."""
    envelope = Envelope(
        id=f"{tuple_digest}:{ordinal}:{record.stage}",
        kind=StoreKind.CONFORMANCE_STAGE,
        scope_id=tuple_digest,
        created_at=record.completed_at,
        summary=f"{record.stage} {record.outcome}",
        payload=record.model_dump(mode="json"),
    )
    return f"{envelope.model_dump_json()}\n"


def _write_committed(state_path: Path, *rows: tuple[str, ConformanceStageRecord]) -> Path:
    """Write *rows* to the frozen committed store file, as an older release did."""
    return _write_rows(store_path(state_path, StoreKind.CONFORMANCE_STAGE), *rows)


def _write_local(state_path: Path, *rows: tuple[str, ConformanceStageRecord]) -> Path:
    """Write *rows* to the machine-local store file, as a probe on this machine does."""
    return _write_rows(local_store_path(state_path, StoreKind.CONFORMANCE_STAGE), *rows)


def _write_rows(path: Path, *rows: tuple[str, ConformanceStageRecord]) -> Path:
    """Write *rows* to the journal file at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(_envelope_line(digest, ordinal, row) for ordinal, (digest, row) in enumerate(rows)),
        encoding="utf-8",
    )
    return path


def _git(repo: Path, *args: str) -> str:
    """Run git in *repo* and return its standard output."""
    return subprocess.run(
        [GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def _stub_claude(directory: Path) -> None:
    """Write a ``claude`` stand-in that prints a version and a help body."""
    directory.mkdir()
    path = directory / "claude"
    path.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f"  --version) echo '{CLAUDE_VERSION} (Claude Code)' ;;\n"
        f"  --help) echo '{CLAUDE_HELP}' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


# ---- the writer ---------------------------------------------------------------


def test_probe_rows_go_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A background probe appends locally and leaves the committed store clean."""
    repo = tmp_path / "repo"
    state_path = repo / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{}\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".ea/local/\n", encoding="utf-8")
    committed = _write_committed(state_path, (TUPLE_B, _record("certify")))
    frozen = committed.read_bytes()
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
    _stub_claude(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    future = RuntimeCertifier(state_path.parent).ensure("claude-code", CLAUDE_VERSION, now=NOW)
    assert future is not None
    row = future.result(timeout=30)

    assert row is not None
    local = local_store_path(state_path, StoreKind.CONFORMANCE_STAGE)
    assert local == repo / ".ea" / "local" / "conformance_stage.jsonl"
    assert len(local.read_text(encoding="utf-8").splitlines()) == 1
    assert committed.read_bytes() == frozen
    assert _git(repo, "status", "--porcelain", ".ea/store") == ""
    journal = StoreStageJournal(state_path)
    assert journal.path == local
    assert [stage.stage for stage in journal.records(tuple_digest=row.tuple_digest)] == ["probe"]


def test_journal_reads_frozen_rows_before_local_ones(tmp_path: Path) -> None:
    """A tuple's history spans both files, committed rows first, and ordinals stay unique."""
    state_path = tmp_path / ".ea" / "state.json"
    _write_committed(state_path, (TUPLE_A, _record("probe")))
    journal = StoreStageJournal(state_path)

    journal.append(tuple_digest=TUPLE_A, record=_record("canary"))

    assert [row.stage for row in journal.records(tuple_digest=TUPLE_A)] == ["probe", "canary"]
    line = journal.path.read_text(encoding="utf-8").splitlines()[0]
    assert Envelope.model_validate_json(line).id == f"{TUPLE_A}:1:canary"


def test_journal_with_no_files_has_no_records(tmp_path: Path) -> None:
    """Boundary: a tree never probed has an empty history in both tiers."""
    journal = StoreStageJournal(tmp_path / ".ea" / "state.json")

    assert journal.records(tuple_digest=TUPLE_A) == ()


def test_journal_raises_on_a_malformed_frozen_line(tmp_path: Path) -> None:
    """Error path: a broken committed row is still a broken journal."""
    state_path = tmp_path / ".ea" / "state.json"
    path = _write_committed(state_path)
    path.write_text("{not json}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="is not an envelope"):
        StoreStageJournal(state_path).records(tuple_digest=TUPLE_A)


# ---- the reader ---------------------------------------------------------------


def test_runtime_health_reads_both(tmp_path: Path) -> None:
    """A stage row in either tier is reported; one tuple split across both merges."""
    local_only = tmp_path / "local-only"
    _write_local(local_only / ".ea" / "state.json", (TUPLE_A, _record("probe")))
    committed_only = tmp_path / "committed-only"
    _write_committed(committed_only / ".ea" / "state.json", (TUPLE_B, _record("certify")))
    split = tmp_path / "split"
    _write_committed(split / ".ea" / "state.json", (TUPLE_A, _record("probe")))
    _write_local(split / ".ea" / "state.json", (TUPLE_A, _record("canary")))

    local_checks = run_runtime_tuple_health_checks(workspace=local_only)
    committed_checks = run_runtime_tuple_health_checks(workspace=committed_only)
    split_checks = run_runtime_tuple_health_checks(workspace=split)

    assert [check.name for check in local_checks] == [f"{RUNTIME_TUPLE_CHECK_PREFIX}_{'a' * 12}"]
    assert [check.name for check in committed_checks] == [
        f"{RUNTIME_TUPLE_CHECK_PREFIX}_{'b' * 12}"
    ]
    assert len(split_checks) == 1
    assert split_checks[0].provenance is not None
    assert split_checks[0].provenance.stage == "canary"
    assert "2 stage record(s)" in (split_checks[0].detail or "")


def test_runtime_health_skips_a_malformed_local_line(tmp_path: Path) -> None:
    """Error path: a broken local row is skipped like a broken committed one."""
    state_path = tmp_path / ".ea" / "state.json"
    _write_committed(state_path, (TUPLE_B, _record("certify")))
    _write_local(state_path).write_text("{not json}\n", encoding="utf-8")

    assert len(run_runtime_tuple_health_checks(workspace=tmp_path)) == 1
