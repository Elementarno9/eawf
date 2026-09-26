"""The post-merge release pipeline walks a merged phase to BAKED, resumably.

Every step runs its real logic against a fake daemon and a fake host:
the daemon keeps one record in memory and moves it the way the
``release.*`` methods do, and the host stands in for git, the forge and
the isolated gate proofs. What is pinned here:

- the tag push, which publishes, never happens without the operator's
  publish flag, and the verb exits non-zero before it;
- a walk interrupted after the candidate resumes at the gate receipts
  with no earlier step repeated, and the journal holds one row per step;
- against fake adapters the record bakes, the train advance row comes
  back, and the trailer repin runs after the landing;
- each lesson of the first hand walk refuses by name or is handled:
  nested artifact downloads, a HEAD off ``main``, a toolchain hidden with
  the agent CLIs, a dirty tree, a lagging registry, and a baseline that
  must take only the new evidence files.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.spec.release import ReleaseStatus, release_key, semver_equivalent
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import create as real_release_create
from eawf.runtime.daemon.methods.release import show as real_release_show
from eawf.runtime.daemon.methods.release_candidate import candidate as real_release_candidate
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import release_pipeline as cli_module
from eawf.workflow.lifecycle.wave_trailer_repin import TrailerRepin
from eawf.workflow.release import pipeline as pipeline_module
from eawf.workflow.release import pipeline_host as host_module
from eawf.workflow.release.advance import TrainAdvanceRecord
from eawf.workflow.release.pipeline import (
    PIPELINE_STEPS,
    PipelineHostError,
    PipelineJournalRow,
    PipelineLayout,
    PipelineOptions,
    PipelineRefusal,
    PipelineRefusalCode,
    PipelineRpcError,
    PipelineStep,
    discard_journal_from,
    read_journal,
    run_post_merge_pipeline,
    rung_label,
)
from eawf.workflow.release.pipeline_files import (
    PRE_COMMIT_CONFIG,
    SECRETS_BASELINE,
    baseline_evidence,
    baseline_hash,
    flatten_artifacts,
)
from eawf.workflow.release.pipeline_host import isolated_proof_path
from eawf.workflow.release.pipeline_receipts import RECEIPT_FILENAMES
from eawf.workflow.release.publication_receipt import receipt_filename
from eawf.workflow.release.records import read_release_record
from eawf.workflow.release.train import V07_TRAIN
from tests._release_helpers import stage_passing_receipts

VERSION = "0.7.0.dev4"
KEY = f"REL-{VERSION}"
NEXT_KEY = "REL-0.7.0rc1"
TARGETS = ("npm", "pypi")
#: Commit ids are derived rather than spelled, so no literal here reads
#: as a secret to the scanner the evidence step drives.
SOURCE = hashlib.sha1(b"pipeline-source").hexdigest()
OTHER = hashlib.sha1(b"pipeline-other").hexdigest()
DIGEST = f"sha256:{hashlib.sha256(b'pipeline-manifest').hexdigest()}"
WAVE_PIN = hashlib.sha1(b"pipeline-wave").hexdigest()
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


@dataclass
class FakeHost:
    """Git, the forge and the isolated proofs, recorded instead of run."""

    root: Path
    calls: list[str]
    dirty: tuple[str, ...] = ()
    head_sha: str = SOURCE
    main_sha: str = SOURCE
    missing_programs: frozenset[str] = frozenset()
    pushed_tag_sha: str | None = None
    preflight_blocker: str | None = None
    receipt_version: str = "0.7.0-dev.4"
    receipt_conclusion: str = "success"
    prove_error: BaseException | None = None
    refused_gates: tuple[str, ...] = ()
    pins: dict[str, str] = field(default_factory=lambda: {"P35-I01-W01": WAVE_PIN})
    pushes: list[str] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)

    @property
    def repo_root(self) -> Path:
        return self.root

    def dirty_paths(self) -> tuple[str, ...]:
        return self.dirty

    def head(self) -> str:
        return self.head_sha

    def remote_main(self) -> str:
        return self.main_sha

    def which(self, program: str) -> str | None:
        return None if program in self.missing_programs else f"/bin/{program}"

    def target_ids(self, version: str) -> tuple[str, ...]:
        return TARGETS

    def preflight(self, version: str, *, source: str) -> str | None:
        self.calls.append("host.preflight")
        return self.preflight_blocker

    def remote_tag(self, tag: str) -> str | None:
        return self.pushed_tag_sha

    def push_tag(self, tag: str, *, revision: str) -> None:
        self.calls.append("host.push_tag")
        self.pushes.append(f"{tag}@{revision}")
        self.pushed_tag_sha = revision

    def run_build_receipts(self, *, channel: str, source: str, dest: Path) -> None:
        self.calls.append("host.run_build_receipts")
        ci = self.root / "ci-out"
        stage_passing_receipts(ci, version=VERSION, source_sha=source)
        for name, filename in RECEIPT_FILENAMES.items():
            nested = dest / name / filename
            nested.parent.mkdir(parents=True, exist_ok=True)
            nested.write_bytes((ci / "dist" / "release-receipts" / filename).read_bytes())

    def wait_publication(self, *, tag: str, dest: Path) -> None:
        self.calls.append("host.wait_publication")
        for target in TARGETS:
            nested = dest / f"publication-receipt-{target}" / f"publication-receipt-{target}.json"
            nested.parent.mkdir(parents=True, exist_ok=True)
            version = self.receipt_version if target == "npm" else VERSION
            nested.write_text(
                json.dumps(
                    {
                        "target_id": target,
                        "version": version,
                        "artifact_digests": {},
                        "job_conclusion": self.receipt_conclusion,
                        "run_id": "101",
                    }
                ),
                encoding="utf-8",
            )

    def prove_gates(self, version: str) -> dict[str, Any]:
        self.calls.append("host.prove_gates")
        if self.prove_error is not None:
            raise self.prove_error
        return {
            "release_key": KEY,
            "source_sha": SOURCE,
            "receipts": [{"gate": "migration"}],
            "refused": [{"gate": gate, "detail": "red"} for gate in self.refused_gates],
        }

    def phase_wave_pins(self, phase_id: str) -> dict[str, str]:
        return dict(self.pins)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


@dataclass
class FakeDaemon:
    """The ``release.*`` methods over one in-memory record."""

    calls: list[str]
    record: dict[str, Any] | None = None
    open_key: str = KEY
    lagging_reads: int = 0
    params: dict[str, Mapping[str, Any]] = field(default_factory=dict)

    def _move(self, **changes: Any) -> dict[str, Any]:
        assert self.record is not None
        self.record = {**self.record, **changes, "revision": self.record["revision"] + 1}
        return self.record

    def _check_revision(self, params: Mapping[str, Any]) -> None:
        assert self.record is not None
        if params["expected_revision"] != self.record["revision"]:
            raise PipelineRpcError("validation_failed: revision_conflict")

    def __call__(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(method)
        self.params[method] = params
        handler = getattr(self, f"_{method.removeprefix('release.')}", None)
        if handler is None:
            raise AssertionError(f"unexpected method {method}")
        return handler(params)  # type: ignore[no-any-return]

    def _show(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if params["version"] is None:
            return {"checkpoint": {"release_key": self.open_key}, "record": self.record}
        return {"record": self.record}

    def _create(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self.record = {
            "key": KEY,
            "version": VERSION,
            "status": "draft",
            "revision": 0,
            "source_sha": None,
            "manifest_digest": None,
            "target_statuses": dict.fromkeys(TARGETS, "not_started"),
        }
        return {"release": self.record}

    def _candidate(self, params: Mapping[str, Any]) -> dict[str, Any]:
        receipts = sorted(path.name for path in Path(params["receipts_dir"]).iterdir())
        assert receipts == [f"publication-receipt-{target}.json" for target in TARGETS]
        record = self._move(
            status="candidate", source_sha=params["source_sha"], manifest_digest=DIGEST
        )
        return {"release": record, "manifest": {"digest": DIGEST}, "manifest_digest": DIGEST}

    def _compute_readiness(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return {"readiness": {"ready": True}, "first_red": None, "next_status": "candidate"}

    def _approve(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return {"release": self._move(status="approved", approval_ref=params["approval_ref"])}

    def _publish(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._check_revision(params)
        statuses = dict.fromkeys(TARGETS, "queued")
        return {"release": self._move(status="publishing", target_statuses=statuses)}

    def _set_target(self, target: str, status: str) -> dict[str, str]:
        assert self.record is not None
        return {**self.record["target_statuses"], target: status}

    def _reconcile(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._check_revision(params)
        assert params["receipt"]["target_id"] == params["target_id"]
        statuses = self._set_target(params["target_id"], "reported_success")
        return {"release": self._move(target_statuses=statuses)}

    def _observe_target(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._check_revision(params)
        if params["target_id"] == "npm" and self.lagging_reads > 0:
            self.lagging_reads -= 1
            raise PipelineRpcError("validation_failed: observation_inconclusive: not yet")
        statuses = self._set_target(params["target_id"], "observed_success")
        baked = all(value == "observed_success" for value in statuses.values())
        status = "baked" if baked else "verifying"
        return {"release": self._move(target_statuses=statuses, status=status)}

    def _advance_train(self, params: Mapping[str, Any]) -> dict[str, Any]:
        assert self.record is not None and self.record["status"] == "baked"
        self.open_key = NEXT_KEY
        advance = TrainAdvanceRecord(
            train_id=V07_TRAIN.train_id,
            closed_key=KEY,
            closed_revision=self.record["revision"],
            opened_key=NEXT_KEY,
            receipt_refs=("checkpoint-receipt://seed",),
            advanced_at=NOW,
            train_revision=5,
        )
        return {"advance": advance.model_dump(mode="json")}


@dataclass
class World:
    """One checkout, its fake host, its fake daemon and a shared call log."""

    root: Path
    calls: list[str]
    host: FakeHost
    daemon: FakeDaemon
    repins: list[tuple[dict[str, str], str]]

    def run(self, *, publish: bool = True, redo: PipelineStep | None = None) -> Any:
        options = PipelineOptions(
            version=VERSION, phase_id="P35", publish=publish, observe_backoff_seconds=5.0
        )
        return run_post_merge_pipeline(
            options, host=self.host, rpc=self.daemon, redo=redo, now=lambda: NOW
        )

    def journal_steps(self) -> list[PipelineStep]:
        return [row.step for row in read_journal(PipelineLayout(self.root, VERSION).journal)]


def _seed_baseline(root: Path) -> None:
    baseline = {
        "version": "1.5.0",
        "plugins_used": [{"name": "HexHighEntropyString", "limit": 3.0}],
        "filters_used": [],
        "results": {
            "kept/older.json": [
                {
                    "type": "Hex High Entropy String",
                    "filename": "kept/older.json",
                    "hashed_secret": "0" * 40,
                    "is_verified": False,
                    "line_number": 1,
                }
            ]
        },
        "generated_at": "2026-09-16T16:15:03Z",
    }
    (root / SECRETS_BASELINE).write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    (root / PRE_COMMIT_CONFIG).write_text(
        "repos:\n  - repo: https://github.com/Yelp/detect-secrets\n"
        "    # Baseline-hash: 0000000000000000 (.secrets.baseline; recompute and bump\n",
        encoding="utf-8",
    )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    calls: list[str] = []
    repins: list[tuple[dict[str, str], str]] = []

    def spy_repins(
        pins: Mapping[str, str], *, target_ref: str, repo_root: Path
    ) -> list[TrailerRepin]:
        calls.append("resolve_trailer_repins")
        repins.append((dict(pins), target_ref))
        return [
            TrailerRepin(wave_id=wave_id, old_commit=sha, new_commit=sha, outcome="unchanged")
            for wave_id, sha in sorted(pins.items())
        ]

    monkeypatch.setattr(pipeline_module, "resolve_trailer_repins", spy_repins)
    _seed_baseline(tmp_path)
    return World(
        root=tmp_path,
        calls=calls,
        host=FakeHost(root=tmp_path, calls=calls),
        daemon=FakeDaemon(calls=calls),
        repins=repins,
    )


# ---- the publish flag gate -------------------------------------------------------


def test_run_post_merge_pipeline_refuses_the_push_without_the_publish_flag(world: World) -> None:
    with pytest.raises(PipelineRefusal) as refused:
        world.run(publish=False)
    assert refused.value.step is PipelineStep.TAG
    assert refused.value.code is PipelineRefusalCode.PUBLISH_FLAG_MISSING
    assert world.host.pushes == []
    assert "host.push_tag" not in world.calls
    assert world.journal_steps() == [PipelineStep.PIN_SOURCE, PipelineStep.BUILD_RECEIPTS]
    assert [o.step for o in refused.value.outcomes] == world.journal_steps()


def test_release_pipeline_cli_exits_nonzero_before_any_push(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(world.root)
    monkeypatch.setattr(host_module, "GitHubReleaseHost", lambda *_a, **_k: world.host)
    monkeypatch.setattr(cli_module, "_daemon_rpc", world.daemon)
    result = CliRunner().invoke(app, ["release", "pipeline", VERSION, "--phase", "P35"])
    assert result.exit_code == 3, result.output
    assert "publish_flag_missing" in result.output
    assert world.host.pushes == []


def test_release_pipeline_cli_rejects_an_unknown_redo_step(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(world.root)
    result = CliRunner().invoke(
        app, ["release", "pipeline", VERSION, "--phase", "P35", "--redo", "bake"]
    )
    assert result.exit_code != 0
    assert "is not a step" in result.output


def test_run_post_merge_pipeline_adopts_a_tag_already_pushed_at_the_source(world: World) -> None:
    world.host.pushed_tag_sha = SOURCE
    world.run(publish=False)
    assert world.host.pushes == []
    assert world.journal_steps() == list(PIPELINE_STEPS)


def test_run_post_merge_pipeline_refuses_a_tag_at_another_commit(world: World) -> None:
    world.host.pushed_tag_sha = OTHER
    with pytest.raises(PipelineRefusal) as refused:
        world.run()
    assert refused.value.code is PipelineRefusalCode.TAG_CONFLICT
    assert world.host.pushes == []


def test_run_post_merge_pipeline_refuses_the_push_on_a_red_preflight(world: World) -> None:
    world.host.preflight_blocker = "changelog (missing_section): add the section"
    with pytest.raises(PipelineRefusal) as refused:
        world.run()
    assert refused.value.code is PipelineRefusalCode.RELEASE_NOT_READY
    assert world.host.pushes == []


# ---- resume ------------------------------------------------------------------------


def test_run_post_merge_pipeline_resumes_at_receipts_after_an_interrupt(world: World) -> None:
    world.host.prove_error = RuntimeError("killed mid-proof")
    with pytest.raises(RuntimeError, match="killed"):
        world.run()
    assert world.journal_steps() == list(
        PIPELINE_STEPS[: PIPELINE_STEPS.index(PipelineStep.RECEIPTS)]
    )

    world.host.prove_error = None
    world.calls.clear()
    world.run()

    assert world.journal_steps() == list(PIPELINE_STEPS)
    assert next(call for call in world.calls if call.startswith("host.")) == "host.prove_gates"
    for repeated in (
        "host.push_tag",
        "host.wait_publication",
        "release.create",
        "release.candidate",
    ):
        assert repeated not in world.calls
    assert world.host.pushes == [f"v{VERSION}@{SOURCE}"]
    assert world.calls.count("host.prove_gates") == 1


def test_run_post_merge_pipeline_reruns_nothing_once_complete(world: World) -> None:
    world.run()
    world.calls.clear()
    result = world.run()
    assert world.calls == []
    assert {o.disposition for o in result.outcomes} == {"resumed"}


def test_run_post_merge_pipeline_redo_reruns_receipts_and_skips_settled_steps(
    world: World,
) -> None:
    world.run()
    world.calls.clear()
    world.run(redo=PipelineStep.RECEIPTS)
    assert world.calls.count("host.prove_gates") == 1
    assert "release.approve" not in world.calls
    assert "release.publish" not in world.calls
    assert world.journal_steps() == list(PIPELINE_STEPS)


def test_run_post_merge_pipeline_refuses_a_moved_head_on_resume(world: World) -> None:
    world.host.prove_error = RuntimeError("killed mid-proof")
    with pytest.raises(RuntimeError, match="killed"):
        world.run()
    world.host.prove_error = None
    world.host.head_sha = OTHER
    with pytest.raises(PipelineRefusal) as refused:
        world.run()
    assert refused.value.code is PipelineRefusalCode.SOURCE_MOVED
    assert refused.value.step is PipelineStep.RECEIPTS


# ---- the whole walk ----------------------------------------------------------------


def test_run_post_merge_pipeline_bakes_advances_and_repins(world: World) -> None:
    result = world.run()

    assert world.daemon.record is not None
    assert world.daemon.record["status"] == "baked"
    assert isinstance(result.advance, TrainAdvanceRecord)
    assert (result.advance.closed_key, result.advance.opened_key) == (KEY, NEXT_KEY)
    assert world.repins == [({"P35-I01-W01": WAVE_PIN}, "refs/remotes/origin/main")]
    assert world.calls.index("resolve_trailer_repins") > world.calls.index("release.advance_train")
    publish = world.daemon.params["release.publish"]
    assert publish["proof_digest"] == publish["approved_manifest_digest"] == DIGEST
    assert world.journal_steps() == list(PIPELINE_STEPS)


def test_run_post_merge_pipeline_lands_evidence_and_baselines_only_it(world: World) -> None:
    (world.root / "unrelated.json").write_text(json.dumps({"sha": OTHER}), encoding="utf-8")
    world.run()

    evidence = world.root / ".ea/artifacts/evidence/2026-09-25-dev4-publication"
    names = sorted(path.name for path in evidence.iterdir())
    assert names == sorted(
        [
            "dev4-approved.json",
            "dev4-baked.json",
            "dev4-candidate.json",
            "dev4-manifest.json",
            "readiness-reply.json",
            "readiness.json",
            "receipts.json",
        ]
    )
    baseline_text = (world.root / SECRETS_BASELINE).read_text(encoding="utf-8")
    results = json.loads(baseline_text)["results"]
    assert "unrelated.json" not in results
    assert "kept/older.json" in results
    assert any(name.startswith(".ea/artifacts/evidence/") for name in results)
    # Rows carry the repo-relative path, never the scanner's absolute one.
    assert all(row["filename"] == name for name, rows in results.items() for row in rows)
    config = (world.root / PRE_COMMIT_CONFIG).read_text(encoding="utf-8")
    assert f"# Baseline-hash: {baseline_hash(baseline_text)} (" in config
    approval = world.daemon.params["release.approve"]["approval_ref"]
    assert approval == "repo:.ea/artifacts/evidence/2026-09-25-dev4-publication/receipts.json"


def _load_commit_prefix_lint() -> ModuleType:
    """Load ``tools/commit_prefix_lint.py``, which is a script, not a package module."""
    lint_path = Path(__file__).resolve().parents[4] / "tools" / "commit_prefix_lint.py"
    if str(lint_path.parent) not in sys.path:
        sys.path.insert(0, str(lint_path.parent))
    spec = importlib.util.spec_from_file_location("commit_prefix_lint", lint_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["commit_prefix_lint"] = module
    spec.loader.exec_module(module)
    return module


def test_commit_lint_accepts_the_evidence_step_as_one_state_commit(
    world: World, tmp_path_factory: pytest.TempPathFactory
) -> None:
    # The CLI tells the operator to land the evidence step's output in one
    # bare state commit; the commit lint must accept exactly that path set.
    world.run()
    evidence = world.root / ".ea/artifacts/evidence/2026-09-25-dev4-publication"
    staged = [
        ".ea/store/release_record.jsonl",
        *(path.relative_to(world.root).as_posix() for path in sorted(evidence.iterdir())),
        SECRETS_BASELINE,
        PRE_COMMIT_CONFIG,
    ]
    message = tmp_path_factory.mktemp("commit") / "COMMIT_EDITMSG"
    message.write_text(
        "[P35] state: land dev4 publication evidence\n\n"
        "Co-Authored-By: Claude <noreply@anthropic.com>\n",
        encoding="utf-8",
    )
    code, diagnostic = _load_commit_prefix_lint().lint(message, staged)
    assert code == 0, diagnostic


def test_run_post_merge_pipeline_waits_out_a_lagging_registry(world: World) -> None:
    world.daemon.lagging_reads = 2
    world.run()
    assert world.host.sleeps == [5.0, 5.0]
    assert world.daemon.record is not None and world.daemon.record["status"] == "baked"


def test_run_post_merge_pipeline_refuses_a_registry_that_never_reads_back(world: World) -> None:
    world.daemon.lagging_reads = 99
    with pytest.raises(PipelineRefusal) as refused:
        world.run()
    assert refused.value.code is PipelineRefusalCode.OBSERVATION_INCONCLUSIVE
    assert refused.value.step is PipelineStep.OBSERVE


# ---- lessons of the first hand walk ------------------------------------------------


def test_run_post_merge_pipeline_refuses_a_dirty_tree_first(world: World) -> None:
    world.host.dirty = (" M src/eawf/_version.py",)
    with pytest.raises(PipelineRefusal) as refused:
        world.run()
    assert (refused.value.step, refused.value.code) == (
        PipelineStep.PIN_SOURCE,
        PipelineRefusalCode.DIRTY_RELEASE_TREE,
    )
    assert world.journal_steps() == []


def test_run_post_merge_pipeline_refuses_a_head_off_main(world: World) -> None:
    world.host.head_sha = OTHER
    with pytest.raises(PipelineRefusal) as refused:
        world.run()
    assert refused.value.code is PipelineRefusalCode.SOURCE_NOT_ON_MAIN
    assert "git checkout --detach origin/main" in refused.value.remedy


def test_run_post_merge_pipeline_refuses_a_missing_uvx(world: World) -> None:
    world.host.missing_programs = frozenset({"uvx"})
    with pytest.raises(PipelineRefusal) as refused:
        world.run()
    assert refused.value.code is PipelineRefusalCode.PROOF_TOOLCHAIN_MISSING
    assert "uvx" in refused.value.detail


def test_run_post_merge_pipeline_flattens_nested_build_receipts(world: World) -> None:
    with pytest.raises(PipelineRefusal):
        world.run(publish=False)
    flat = world.root / "dist" / "release-receipts"
    assert sorted(path.name for path in flat.iterdir()) == sorted(RECEIPT_FILENAMES.values())


@pytest.mark.parametrize(
    ("version", "conclusion", "needle"),
    [
        ("0.7.0-dev.3", "success", "receipt is for 0.7.0-dev.3"),
        ("0.7.0-dev.4", "failure", "job concluded failure"),
    ],
)
def test_run_post_merge_pipeline_refuses_bad_publication_receipts(
    world: World, version: str, conclusion: str, needle: str
) -> None:
    world.host.receipt_version = version
    world.host.receipt_conclusion = conclusion
    with pytest.raises(PipelineRefusal) as refused:
        world.run()
    assert refused.value.code is PipelineRefusalCode.PUBLICATION_RECEIPT_INVALID
    assert needle in refused.value.detail
    assert "release.create" not in world.calls


def test_run_post_merge_pipeline_refuses_refused_gate_receipts(world: World) -> None:
    world.host.refused_gates = ("front_door_journey",)
    with pytest.raises(PipelineRefusal) as refused:
        world.run()
    assert refused.value.code is PipelineRefusalCode.GATE_RECEIPTS_REFUSED
    assert "front_door_journey" in refused.value.detail
    assert "release.approve" not in world.calls


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        ("unique_trailer", PipelineRefusalCode.TRAILER_REPIN_PENDING),
        ("ambiguous", PipelineRefusalCode.TRAILER_REPIN_UNDECIDED),
    ],
)
def test_run_post_merge_pipeline_refuses_unsettled_wave_pins(
    world: World, monkeypatch: pytest.MonkeyPatch, outcome: str, code: PipelineRefusalCode
) -> None:
    def moved(pins: Mapping[str, str], *, target_ref: str, repo_root: Path) -> list[TrailerRepin]:
        new = OTHER if outcome == "unique_trailer" else None
        return [TrailerRepin("P35-I01-W01", WAVE_PIN, new, outcome)]  # type: ignore[arg-type]

    monkeypatch.setattr(pipeline_module, "resolve_trailer_repins", moved)
    with pytest.raises(PipelineRefusal) as refused:
        world.run()
    assert (refused.value.step, refused.value.code) == (PipelineStep.REPIN, code)
    assert "P35-I01-W01" in refused.value.detail


def test_run_post_merge_pipeline_refuses_a_phase_with_no_wave_pins(world: World) -> None:
    """A well-formed but mistyped phase id reads no pins; verifying zero pins is no check."""
    world.host.pins = {}
    with pytest.raises(PipelineRefusal) as refused:
        world.run()
    assert (refused.value.step, refused.value.code) == (
        PipelineStep.REPIN,
        PipelineRefusalCode.PHASE_PINS_MISSING,
    )
    assert "P35" in refused.value.detail
    assert "resolve_trailer_repins" not in world.calls


def test_run_post_merge_pipeline_refuses_a_daemon_refusal_by_name(world: World) -> None:
    def refusing(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if method == "release.create":
            raise PipelineRpcError("daemon rejected release.create: invalid_membership_cardinality")
        return world.daemon(method, params)

    options = PipelineOptions(version=VERSION, phase_id="P35", publish=True)
    with pytest.raises(PipelineRefusal) as refused:
        run_post_merge_pipeline(options, host=world.host, rpc=refusing, now=lambda: NOW)
    assert (refused.value.step, refused.value.code) == (
        PipelineStep.CREATE,
        PipelineRefusalCode.DAEMON_REFUSED,
    )
    assert "invalid_membership_cardinality" in refused.value.detail


def test_run_post_merge_pipeline_rejects_a_non_train_version(world: World) -> None:
    options = PipelineOptions(version="0.7", phase_id="P35", publish=True)
    with pytest.raises(ValueError, match="not a train version"):
        run_post_merge_pipeline(options, host=world.host, rpc=world.daemon)


# ---- phase id validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "phase_id",
    [
        "P<NN>",  # the workflow's un-substituted fallback (.github/workflows/phase-release.yaml)
        "p35",  # lowercase
        "P5",  # below the two-digit floor
        "P",  # no digits at all
        "35",  # no P prefix
        "",  # empty
    ],
)
def test_pipeline_options_rejects_a_non_phase_id(phase_id: str) -> None:
    """The workflow prints the literal ``P<NN>`` when its subject has no ``[P<NN>]``
    prefix; opening the pipeline against that literal must refuse, not read
    wave pins for a phase that does not exist."""
    with pytest.raises(ValueError, match="not a phase id"):
        PipelineOptions(version=VERSION, phase_id=phase_id, publish=True)


@pytest.mark.parametrize("phase_id", ["P35", "P05", "P100"])
def test_pipeline_options_accepts_a_real_phase_id(phase_id: str) -> None:
    assert PipelineOptions(version=VERSION, phase_id=phase_id, publish=True).phase_id == phase_id


def test_release_pipeline_cli_rejects_the_unsubstituted_phase_placeholder(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(world.root)
    result = CliRunner().invoke(app, ["release", "pipeline", VERSION, "--phase", "P<NN>"])
    assert result.exit_code != 0
    assert "not a phase id" in result.output


# ---- journal -------------------------------------------------------------------------


def test_read_journal_boundaries(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    assert read_journal(journal) == []
    journal.write_text("\n", encoding="utf-8")
    assert read_journal(journal) == []


def test_read_journal_rejects_a_duplicate_step(world: World) -> None:
    with pytest.raises(PipelineRefusal):
        world.run(publish=False)
    journal = PipelineLayout(world.root, VERSION).journal
    first = journal.read_text(encoding="utf-8").splitlines()[0]
    journal.write_text(f"{first}\n{first}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="twice"):
        read_journal(journal)


def test_read_journal_rejects_a_malformed_row(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    journal.write_text('{"step": "bake"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        read_journal(journal)


def test_discard_journal_from_drops_the_step_and_later_rows(world: World) -> None:
    with pytest.raises(PipelineRefusal):
        world.run(publish=False)
    journal = PipelineLayout(world.root, VERSION).journal
    assert discard_journal_from(journal, PipelineStep.TAG) == ()
    assert discard_journal_from(journal, PipelineStep.BUILD_RECEIPTS) == (
        PipelineStep.BUILD_RECEIPTS,
    )
    assert world.journal_steps() == [PipelineStep.PIN_SOURCE]


def test_append_journal_leaves_no_temp_file_behind(world: World) -> None:
    """The atomic write renames its temp file away; nothing lingers beside it."""
    with pytest.raises(PipelineRefusal):
        world.run(publish=False)
    journal_dir = PipelineLayout(world.root, VERSION).journal.parent
    leftovers = [path.name for path in journal_dir.iterdir() if ".tmp-" in path.name]
    assert leftovers == []


def test_append_journal_serializes_through_the_journal_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent run of the same checkpoint is guarded on the journal file.

    A bare append (or a rewrite with no lock) would let two processes
    interleave their read-modify-write and lose a row; this proves the
    append path actually goes through :mod:`eawf.runtime.lock.portalock`
    rather than writing straight to the file.
    """
    journal = tmp_path / "journal.jsonl"
    real_acquire = pipeline_module.portalock.acquire
    acquired: list[Path] = []

    @contextlib.contextmanager
    def spying_acquire(target: Path, **kwargs: Any) -> Iterator[None]:
        acquired.append(Path(target))
        with real_acquire(target, **kwargs):
            yield

    monkeypatch.setattr(pipeline_module.portalock, "acquire", spying_acquire)
    row = PipelineJournalRow(version=VERSION, step=PipelineStep.PIN_SOURCE, completed_at=NOW)

    pipeline_module._append_journal(journal, row)

    assert acquired == [journal]
    assert read_journal(journal) == [row]


class _StopAfterCandidateHost:
    """Real daemon handlers drive CREATE and CANDIDATE; this stub covers the rest.

    Every other test in this module fakes the whole daemon -- exactly the
    gap the audit named: a shape drift in the real ``release.*`` handlers
    would pass every test here and only red in production. PIN_SOURCE,
    BUILD_RECEIPTS and PUBLISH_WAIT are pre-satisfied (a real commit, real
    build receipts, real publication receipts) so those steps never call
    this host at all; TAG is answered as already pushed for the same
    reason. Only RECEIPTS reaches the host, and it refuses on purpose so
    the walk stops right after CANDIDATE runs for real.
    """

    def __init__(self, root: Path, source: str) -> None:
        self.root = root
        self.source = source

    @property
    def repo_root(self) -> Path:
        return self.root

    def dirty_paths(self) -> tuple[str, ...]:
        return ()

    def head(self) -> str:
        return self.source

    def remote_main(self) -> str:
        return self.source

    def which(self, program: str) -> str | None:
        return f"/usr/bin/{program}"

    def target_ids(self, version: str) -> tuple[str, ...]:
        return ("pypi", "npm", "github")

    def preflight(self, version: str, *, source: str) -> str | None:
        raise AssertionError("preflight must not run: the tag is already pushed")

    def remote_tag(self, tag: str) -> str | None:
        return self.source

    def push_tag(self, tag: str, *, revision: str) -> None:
        raise AssertionError("push_tag must not run: the tag is already pushed")

    def run_build_receipts(self, *, channel: str, source: str, dest: Path) -> None:
        raise AssertionError("run_build_receipts must not run: receipts are pre-staged")

    def wait_publication(self, *, tag: str, dest: Path) -> None:
        raise AssertionError("wait_publication must not run: receipts are pre-staged")

    def prove_gates(self, version: str) -> dict[str, Any]:
        raise PipelineHostError("stub: gate proofs are out of scope for this test")

    def phase_wave_pins(self, phase_id: str) -> dict[str, str]:
        raise AssertionError("phase_wave_pins must not run: the walk stops at receipts")

    def sleep(self, seconds: float) -> None:
        raise AssertionError("sleep must not run: no lagging registry here")


def _run_git(repo: Path, *args: str) -> str:
    """Run one git command inside *repo* and return its stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


def _real_release_rpc(ctx: MethodContext) -> pipeline_module.ReleaseRpc:
    """Return an RPC callable that runs the real ``release.*`` handlers in process."""
    handlers = {
        "release.show": real_release_show,
        "release.create": real_release_create,
        "release.candidate": real_release_candidate,
    }

    def _call(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        handler = handlers.get(method)
        if handler is None:
            raise AssertionError(f"unexpected method {method!r} for this stub daemon")
        try:
            return asyncio.run(handler(ctx, dict(params)))
        except DaemonValidationError as exc:
            raise PipelineRpcError(str(exc)) from exc

    return _call


def _publication_receipt(target_id: str, version: str, artifact_digests: dict[str, str]) -> str:
    return json.dumps(
        {
            "target_id": target_id,
            "version": version,
            "artifact_digests": artifact_digests,
            "job_conclusion": "success",
            "run_id": "1",
        }
    )


def test_run_post_merge_pipeline_drives_the_real_daemon_through_create_and_candidate(
    tmp_path: Path,
) -> None:
    """CREATE and CANDIDATE run the real ``release.*`` handlers, not a fake daemon.

    A ``MethodContext`` is bound to a throwaway git checkout carrying the
    dev1 checkpoint's empty-repo state; the real ``show`` / ``create`` /
    ``candidate`` handlers persist to and read back its actual
    ``state.json`` and resolve the source tree with real ``git``. The
    walk is stopped right after CANDIDATE by a host that refuses to
    prove gates, since only the daemon boundary is under test here.
    """
    version = "0.7.0.dev1"
    repo = tmp_path / "checkout"
    repo.mkdir()
    _run_git(repo, "init", "--quiet", "--initial-branch", "main")
    _run_git(repo, "config", "user.name", "EAWF Test")
    _run_git(repo, "config", "user.email", "test@example.invalid")
    _run_git(repo, "config", "commit.gpgSign", "false")
    _run_git(repo, "config", "core.hooksPath", ".git/hooks")
    state_path = repo / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    empty_state = Path(__file__).resolve().parents[3] / "fixtures" / "states" / "valid"
    state_path.write_text(
        (empty_state / "01-empty-repo.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    _run_git(repo, "add", "--all")
    _run_git(repo, "commit", "--quiet", "--message", "chore: seed the throwaway checkout")
    source = _run_git(repo, "rev-parse", "HEAD")

    stage_passing_receipts(repo, version=version, source_sha=source)
    receipts_dir = repo / "dist" / "publication-receipts" / version
    receipts_dir.mkdir(parents=True)
    npm_version = semver_equivalent(version)
    receipts = {
        "pypi": (
            version,
            {
                f"eawf-{version}-py3-none-any.whl": f"sha256:{'1' * 64}",
                f"eawf-{version}.tar.gz": f"sha256:{'2' * 64}",
            },
        ),
        "npm": (npm_version, {f"elementarno-eawf-{version}.tgz": f"sha256:{'3' * 64}"}),
        "github": (
            version,
            {
                "RELEASE_NOTES.md": f"sha256:{'4' * 64}",
                "SHA256SUMS": f"sha256:{'5' * 64}",
                f"eawf-plugin-{version}.tar.gz": f"sha256:{'6' * 64}",
            },
        ),
    }
    for target_id, (leg_version, artifact_digests) in receipts.items():
        (receipts_dir / receipt_filename(target_id)).write_text(
            _publication_receipt(target_id, leg_version, artifact_digests), encoding="utf-8"
        )

    ctx = MethodContext(
        started_at=datetime.now(UTC).isoformat(),
        pid=4242,
        protocol_version=PROTOCOL_VERSION,
        version="test",
        state_path=state_path,
    )
    host = _StopAfterCandidateHost(repo, source)
    options = PipelineOptions(version=version, phase_id="P35", publish=False)

    with pytest.raises(PipelineRefusal) as refused:
        run_post_merge_pipeline(options, host=host, rpc=_real_release_rpc(ctx), now=lambda: NOW)

    assert refused.value.step is PipelineStep.RECEIPTS
    assert refused.value.code is PipelineRefusalCode.GATE_RECEIPTS_REFUSED
    journal = PipelineLayout(repo, version).journal
    assert [row.step for row in read_journal(journal)] == [
        PipelineStep.PIN_SOURCE,
        PipelineStep.BUILD_RECEIPTS,
        PipelineStep.TAG,
        PipelineStep.PUBLISH_WAIT,
        PipelineStep.CREATE,
        PipelineStep.CANDIDATE,
    ]

    stored = read_release_record(state_path, release_key(version))
    assert stored is not None
    assert stored.status is ReleaseStatus.CANDIDATE
    assert stored.source_sha == source


@pytest.mark.parametrize(
    ("version", "label"),
    [("0.7.0.dev3", "dev3"), ("0.7.0rc1", "rc1"), ("0.7.0", "0.7.0"), ("1.0.0.dev10", "dev10")],
)
def test_rung_label(version: str, label: str) -> None:
    assert rung_label(version) == label


# ---- file chores ---------------------------------------------------------------------


def test_flatten_artifacts_flattens_nested_and_names_missing(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    (raw / "a").mkdir(parents=True)
    (raw / "a" / "a.json").write_text("{}", encoding="utf-8")
    (raw / "b.json").write_text("{}", encoding="utf-8")
    missing = flatten_artifacts(raw, tmp_path / "flat", ["a.json", "b.json", "c.json"])
    assert missing == ("c.json",)
    assert sorted(p.name for p in (tmp_path / "flat").iterdir()) == ["a.json", "b.json"]


def test_flatten_artifacts_boundaries(tmp_path: Path) -> None:
    assert flatten_artifacts(tmp_path / "absent", tmp_path / "flat", []) == ()
    assert flatten_artifacts(tmp_path / "absent", tmp_path / "flat", ["a.json"]) == ("a.json",)


def test_flatten_artifacts_rejects_an_ambiguous_download(tmp_path: Path) -> None:
    for run in ("one", "two"):
        (tmp_path / "raw" / run).mkdir(parents=True)
        (tmp_path / "raw" / run / "a.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="expected one"):
        flatten_artifacts(tmp_path / "raw", tmp_path / "flat", ["a.json"])


def test_baseline_evidence_leaves_a_clean_file_alone(tmp_path: Path) -> None:
    _seed_baseline(tmp_path)
    (tmp_path / "clean.json").write_text('{"status": "baked"}\n', encoding="utf-8")
    before = (tmp_path / PRE_COMMIT_CONFIG).read_text(encoding="utf-8")
    assert baseline_evidence(tmp_path, ["clean.json"]) == 0
    assert (tmp_path / PRE_COMMIT_CONFIG).read_text(encoding="utf-8") == before


def test_baseline_evidence_requires_one_hash_comment(tmp_path: Path) -> None:
    _seed_baseline(tmp_path)
    (tmp_path / PRE_COMMIT_CONFIG).write_text("repos: []\n", encoding="utf-8")
    (tmp_path / "pin.json").write_text(json.dumps({"sha": SOURCE}), encoding="utf-8")
    with pytest.raises(ValueError, match="0 Baseline-hash"):
        baseline_evidence(tmp_path, ["pin.json"])


def test_baseline_evidence_rejects_a_baseline_without_results(tmp_path: Path) -> None:
    (tmp_path / SECRETS_BASELINE).write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no results map"):
        baseline_evidence(tmp_path, [])


def test_isolated_proof_path_hides_codex_and_keeps_the_toolchain(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    for program in ("codex", "uv", "uvx", "git"):
        tool = shared / program
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        tool.chmod(0o755)
    plain = tmp_path / "plain"
    plain.mkdir()
    shim = tmp_path / "shim"
    path = isolated_proof_path(f"{shared}:{plain}", shim_dir=shim)
    assert path.split(":") == [str(shim), str(plain)]
    assert sorted(p.name for p in shim.iterdir()) == ["git", "uv", "uvx"]


def test_isolated_proof_path_refuses_a_missing_tool(tmp_path: Path) -> None:
    with pytest.raises(PipelineHostError, match="not on PATH"):
        isolated_proof_path(str(tmp_path), shim_dir=tmp_path / "shim")
