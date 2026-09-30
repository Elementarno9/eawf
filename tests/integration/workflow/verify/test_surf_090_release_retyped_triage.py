"""SURF-090: the release preflight holds every re-typed rule over the threshold to a disposition.

The tag chokepoint's realization probe counts, over the window since the previous release
tag, the instructions the operator typed again -- from the host session history and from
the tree's root Runs -- and reds the row while any subject over the configured threshold
has no disposition in the committed triage. The row names subjects by id and count only;
the operator's wording goes to the local reflection collection, never into the release
record.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from eawf.kernel.runtime.events import MessageSummaryPayload, RunEventKind
from eawf.kernel.spec.release_config import ReleaseConfig, load_release_config
from eawf.kernel.store.commit_policy import CommitPolicy, classify_path
from eawf.observability.reflect.retyped import RETYPED_TRIAGE_PATH, load_retyped_triage
from eawf.observability.telemetry.sources.session_history import claude_history_root
from eawf.workflow.release.train import DEV1_RELEASE_CONFIG_YAML, V07_TRAIN
from eawf.workflow.verify.release_probes import TagPreflightInputs, build_tag_probes
from eawf.workflow.verify.release_readiness import (
    ReleaseReadiness,
    ReleaseSignalFailureCode,
    ReleaseSignalName,
    ReleaseSignalStatus,
    compute_readiness,
)
from tests.contract.surfaces.cli._reflect_tree import event, native_tree, run_row

_NOW: Final = datetime(2027, 2, 1, 12, 0, tzinfo=UTC)
T0: Final = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
INSTRUCTION: Final = "Dispatch the waves with parallel worktree subagents."
LISTING: Final = "needs guard | dispatch_default | lens | memory_row | argued_prose"


def _config() -> ReleaseConfig:
    body: dict[str, Any] = copy.deepcopy(yaml.safe_load(DEV1_RELEASE_CONFIG_YAML))["release"]
    return load_release_config({"release": body}, train=V07_TRAIN)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text('[tool.eawf.lint]\nenabled = ["EAWF010"]\n')
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
    return root


def _history(repo: Path, texts: list[str], *, name: str = "session") -> None:
    directory = claude_history_root(repo.resolve())
    directory.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "type": "user",
            "isSidechain": False,
            "timestamp": (T0 + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z"),
            "origin": {"kind": "human"},
            "message": {"role": "user", "content": text},
        }
        for minute, text in enumerate(texts)
    ]
    body = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
    (directory / f"{name}.jsonl").write_text(body, encoding="utf-8")


def _sweep(repo: Path) -> ReleaseReadiness:
    inputs = TagPreflightInputs(
        repo_root=repo,
        version="0.7.0.dev1",
        tag="v0.7.0.dev1",
        package_version="0.7.0.dev1",
        remote="origin",
        today=date(2027, 2, 1),
    )
    return compute_readiness(
        _config(),
        probes={
            ReleaseSignalName.PERFECT_REALIZATION: build_tag_probes(inputs)[
                ReleaseSignalName.PERFECT_REALIZATION
            ]
        },
        observed_revision="deadbee",
        computed_at=_NOW,
    )


def _row(repo: Path) -> Any:
    return _sweep(repo).row(ReleaseSignalName.PERFECT_REALIZATION)


def _triage(repo: Path, dispositions: list[dict[str, str]]) -> None:
    path = repo / RETYPED_TRIAGE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"schema_version": 1, "dispositions": dispositions}))


def _git(repo: Path, *args: str, when: datetime | None = None) -> None:
    env = os.environ | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    if when is not None:
        env |= {"GIT_COMMITTER_DATE": when.isoformat(), "GIT_AUTHOR_DATE": when.isoformat()}
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        check=True,
        capture_output=True,
        env=env,
    )


def test_surf_090_an_untriaged_instruction_over_the_threshold_reds_the_release(
    repo: Path,
) -> None:
    """Gate fire: one instruction typed four times against the default threshold of three."""
    _history(repo, [INSTRUCTION] * 4)

    row = _row(repo)

    assert row.status is ReleaseSignalStatus.FAIL
    assert row.failure_code is ReleaseSignalFailureCode.PERFECT_REALIZATION_FAILED
    assert "retyped_rule_triage: 1 rule(s) re-typed more than 3 time(s)" in row.remediation
    assert f"x4: {LISTING}" in row.remediation
    assert str(RETYPED_TRIAGE_PATH) in row.remediation
    (ref,) = row.evidence_refs
    assert ref.startswith("retyped_rule_triage:typed:") and ref.endswith(":4")


def test_surf_090_the_release_row_carries_no_operator_wording(repo: Path) -> None:
    _history(repo, [INSTRUCTION] * 4)

    row = _row(repo)

    for word in ("Dispatch", "worktree", "subagents"):
        assert word not in row.remediation
        assert all(word not in ref for ref in row.evidence_refs)


def test_surf_090_the_exemplars_land_in_the_local_reflection_collection(repo: Path) -> None:
    _history(repo, [INSTRUCTION] * 4)

    row = _row(repo)

    stored = repo / ".ea" / "local" / "reflect" / "2027-02-01-reflect-retyped.json"
    assert str(stored.relative_to(repo)) in row.remediation
    (counted,) = json.loads(stored.read_text(encoding="utf-8"))
    assert counted["exemplar"] == INSTRUCTION
    assert counted["count"] == 4
    assert counted["mark"] == "non-quotable"


def test_surf_090_a_count_equal_to_the_threshold_is_not_over_it(repo: Path) -> None:
    _history(repo, [INSTRUCTION] * 3)

    row = _row(repo)

    assert row.status is ReleaseSignalStatus.UNAVAILABLE
    assert "module_length_exclusion" in row.remediation


def test_surf_090_the_configured_threshold_moves_the_boundary(repo: Path) -> None:
    _history(repo, [INSTRUCTION] * 4)
    (repo / ".ea").mkdir(exist_ok=True)
    (repo / ".ea" / "config.yaml").write_text("verify:\n  retyped_rule_threshold: 4\n")

    assert _row(repo).status is ReleaseSignalStatus.UNAVAILABLE


@pytest.mark.parametrize(
    "disposition",
    [
        {"disposition": "guard", "reference": "eawf.lint.dispatch-guard"},
        {"disposition": "dispatch_default", "reference": "dispatch.orchestration"},
        {"disposition": "lens", "reference": "coverage-lens"},
        {"disposition": "memory_row", "reference": "feedback-dispatch"},
        {"disposition": "argued_prose", "argument": "Phrased per task; no mechanism fits."},
    ],
)
def test_surf_090_a_triaged_subject_hands_the_row_on(
    repo: Path, disposition: dict[str, str]
) -> None:
    _history(repo, [INSTRUCTION] * 4)
    (subject,) = (ref.split(":", 1)[1].rsplit(":", 1)[0] for ref in _row(repo).evidence_refs)
    _triage(repo, [{"subject": subject} | disposition])

    row = _row(repo)

    assert row.status is ReleaseSignalStatus.UNAVAILABLE
    assert "retyped_rule_triage" not in row.remediation


def test_surf_090_a_malformed_triage_blocks_the_row_instead_of_passing(repo: Path) -> None:
    _history(repo, [INSTRUCTION] * 4)
    _triage(repo, [{"subject": "typed:x", "disposition": "argued_prose"}])

    assert _row(repo).status is ReleaseSignalStatus.BLOCKED


def test_surf_090_a_restated_compiled_rule_is_named_with_its_title(repo: Path) -> None:
    (repo / ".ea").mkdir(exist_ok=True)
    (repo / ".ea" / "rules.yaml").write_text("schema_version: 1\nmodules:\n  - eawf.core.vcs\n")
    _history(repo, ["Run the pre-commit hooks before the commit, never skip them."] * 4)

    row = _row(repo)

    assert row.status is ReleaseSignalStatus.FAIL
    assert (
        f"eawf.core.vcs.pre-commit (Run the pre-commit hooks before every commit) x4: {LISTING}"
        in row.remediation
    )
    assert row.evidence_refs == ("retyped_rule_triage:eawf.core.vcs.pre-commit:4",)


def test_surf_090_turns_before_the_previous_release_tag_are_outside_the_window(
    repo: Path,
) -> None:
    _history(repo, [INSTRUCTION] * 4)
    _git(repo, "init", "-q")
    _git(repo, "add", "pyproject.toml")
    _git(repo, "commit", "-q", "-m", "init", when=T0 + timedelta(minutes=1, seconds=30))
    _git(repo, "tag", "v0.6.0")

    row = _row(repo)

    assert row.status is ReleaseSignalStatus.UNAVAILABLE


def test_surf_090_the_tag_being_cut_does_not_open_the_window(repo: Path) -> None:
    _history(repo, [INSTRUCTION] * 4)
    _git(repo, "init", "-q")
    _git(repo, "add", "pyproject.toml")
    _git(repo, "commit", "-q", "-m", "init", when=T0 + timedelta(minutes=1, seconds=30))
    _git(repo, "tag", "v0.7.0.dev1")

    row = _row(repo)

    assert row.status is ReleaseSignalStatus.FAIL
    assert "since the first release" in row.remediation


def test_surf_090_a_root_runs_operator_messages_are_counted_without_a_daemon(
    repo: Path,
) -> None:
    key = "RUN-00000001"
    messages = [
        event(
            key,
            sequence,
            event_kind=RunEventKind.MESSAGE_SUMMARIZED,
            payload=MessageSummaryPayload(message_role="user", summary=INSTRUCTION),
        )
        for sequence in range(1, 5)
    ]
    native_tree(repo, [run_row(key)], messages)

    row = _row(repo)

    assert row.status is ReleaseSignalStatus.FAIL
    assert "x4" in row.remediation


def test_surf_090_a_turn_in_both_the_history_and_a_run_counts_once(repo: Path) -> None:
    key = "RUN-00000001"
    native_tree(
        repo,
        [run_row(key)],
        [
            event(
                key,
                1,
                event_kind=RunEventKind.MESSAGE_SUMMARIZED,
                payload=MessageSummaryPayload(message_role="user", summary=INSTRUCTION),
                observed_at=T0,
            )
        ],
    )
    _history(repo, [INSTRUCTION] * 3)

    assert _row(repo).status is ReleaseSignalStatus.UNAVAILABLE


_REPO_ROOT: Final = Path(__file__).resolve().parents[4]


def test_surf_090_the_repository_commits_its_triage_under_the_closed_schema() -> None:
    """The tag preflight reads this repository's own triage, so it must exist and validate."""
    assert (_REPO_ROOT / RETYPED_TRIAGE_PATH).is_file()

    document = load_retyped_triage(_REPO_ROOT)

    assert document.schema_version == 1


def test_surf_090_the_triage_is_declared_committed_in_the_ea_census() -> None:
    """A triage the commit census does not declare cannot be tracked, so no clone has it."""
    assert classify_path(RETYPED_TRIAGE_PATH.as_posix()).policy is CommitPolicy.COMMITTED
