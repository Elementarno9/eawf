"""CLI integration tests for ``eawf roadmap`` (P19-W06)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()


_GIT_AVAILABLE = shutil.which("git") is not None


def _commit_state_changes(root: Path, *, msg: str) -> None:
    """Stage + commit any pending changes under *root* (no-op when clean)."""
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    if not status.stdout.strip():
        return
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=root, check=True)


def _make_local_repo(root: Path) -> None:
    """Initialise *root* as a clean git repo with one commit on ``main``.

    Any pre-existing files in *root* (e.g. the workspace fixture's
    ``.ea/state.json`` from ``project init``) are committed together
    with the seed ``README.md`` so the porcelain check sees a clean
    tree at the start of every gate test.
    """
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "ci"], cwd=root, check=True)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)


def _make_origin_remote(local: Path, *, remote: Path) -> None:
    """Create a sibling bare repo at *remote* and wire ``origin`` to it.

    Pushes the current ``main`` branch so ``origin/main`` exists for the
    behind-count gate to inspect.
    """
    remote.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main"], cwd=remote, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=local,
        check=True,
    )
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=local, check=True)


def _bare_commit_on_origin_main(local: Path, *, remote: Path) -> None:
    """Push an extra commit to ``origin/main`` from a throwaway clone.

    Leaves *local* behind by one commit on its tracking base. The
    throwaway clone lives under *remote*'s parent so the temp dir
    fixture cleans it up automatically.
    """
    work = remote.parent / "_advance"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "ci"], cwd=work, check=True)
    (work / "NEW.txt").write_text("advance\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=work, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "advance"], cwd=work, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=work, check=True)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Yield a temp workspace dir with EA_STATE pointing inside it + project init."""
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    res = runner.invoke(
        app,
        [
            "project",
            "init",
            "QR",
            "--title",
            "Quant Research",
            "--domains",
            "quant",
        ],
    )
    assert res.exit_code == 0, res.output
    yield tmp_path


def _read_state(workspace: Path) -> dict:
    return orjson.loads((workspace / ".ea" / "state.json").read_bytes())


def test_roadmap_propose_creates_planned_phase(workspace: Path) -> None:
    """propose persists a PLANNED phase + P##-I01 iter and returns needs_user."""
    res = runner.invoke(
        app,
        ["--json", "roadmap", "propose", "--phase", "P21", "--title", "Test phase"],
    )
    assert res.exit_code == 0, res.output
    body = orjson.loads(res.stdout)
    assert body["status"] == "needs_user"
    assert body["phase_id"] == "P21"
    assert body["iter_id"] == "P21-I01"
    state = _read_state(workspace)
    assert state["phases"]["P21"]["status"] == "planned"
    assert state["iters"]["P21-I01"]["status"] == "planned"


def test_roadmap_propose_with_source_briefs_and_deps(workspace: Path) -> None:
    res = runner.invoke(
        app,
        [
            "roadmap",
            "propose",
            "--phase",
            "P21",
            "--title",
            "Test phase",
            "--from-briefs",
            "RES-2026-05-14-001,RES-2026-05-14-002",
        ],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["phases"]["P21"]["source_brief_ids"] == [
        "RES-2026-05-14-001",
        "RES-2026-05-14-002",
    ]


def test_roadmap_propose_from_plan_stages_phase_iters_waves(workspace: Path) -> None:
    plan_path = workspace / "roadmap-plan.yaml"
    plan_path.write_text(
        """
schema_version: "1.0"
kind: RoadmapPlan
phase:
  id: P22
  title: Plan import
  description: phase narrative
  source_brief_ids:
    - RES-2026-05-14-001
iters:
  - id: P22-I01
    title: First iter
    description: iter narrative
    waves:
      - id: P22-I01-W02
        title: "Second wave"
        file_scopes:
          - src/b
        deps:
          - P22-I01-W01
        success_criteria:
          - id: CR-01
            text: second criterion
            kind: legacy
            acceptance_style: binary
            evidence_kind: attested
            quality_dimension: functional_suitability
            measurable_signal: grandfathered legacy criterion
        effort_bucket: S
        intent:
          problem: second wave needs staging
          desired_outcome: second wave is planned
          priority_rationale: stage after its dep wave
      - id: P22-I01-W01
        title: "First wave"
        file_scopes:
          - src/a
        agent_role: executor
        effort_bucket: XS
        intent:
          problem: first wave needs staging
          desired_outcome: first wave is planned
          priority_rationale: stage the leaf wave first
""".lstrip(),
        encoding="utf-8",
    )

    res = runner.invoke(app, ["--json", "roadmap", "propose", "--from-plan", str(plan_path)])

    assert res.exit_code == 0, res.output
    body = orjson.loads(res.stdout)
    assert body["status"] == "needs_user"
    assert body["phase_id"] == "P22"
    assert body["iter_ids"] == ["P22-I01"]
    assert body["wave_count"] == 2
    state = _read_state(workspace)
    assert state["phases"]["P22"]["status"] == "planned"
    assert state["phases"]["P22"]["source_brief_ids"] == ["RES-2026-05-14-001"]
    assert state["iters"]["P22-I01"]["description"] == "iter narrative"
    assert state["iters"]["P22-I01"]["wave_ids"] == ["P22-I01-W01", "P22-I01-W02"]
    assert state["waves"]["P22-I01-W01"]["blocks"] == ["P22-I01-W02"]
    assert state["waves"]["P22-I01-W02"]["deps"] == ["P22-I01-W01"]
    stored_criteria = state["waves"]["P22-I01-W02"]["success_criteria"]
    assert [c["text"] for c in stored_criteria] == ["second criterion"]
    assert stored_criteria[0]["kind"] == "legacy"


def test_roadmap_propose_from_plan_validation_failure_is_all_or_nothing(
    workspace: Path,
) -> None:
    plan_path = workspace / "bad-roadmap-plan.yaml"
    plan_path.write_text(
        """
schema_version: "1.0"
kind: RoadmapPlan
phase:
  id: P22
  title: Bad plan
  unexpected: no
iters:
  - id: P22-I01
    title: First iter
""".lstrip(),
        encoding="utf-8",
    )

    res = runner.invoke(app, ["roadmap", "propose", "--from-plan", str(plan_path)])

    assert res.exit_code != 0
    combined = res.output + (res.stderr or "")
    assert "invalid roadmap plan" in combined
    state = _read_state(workspace)
    assert "P22" not in state["phases"]
    assert "P22-I01" not in state["iters"]


def test_roadmap_propose_duplicate_phase_rejected(workspace: Path) -> None:
    runner.invoke(
        app,
        ["roadmap", "propose", "--phase", "P21", "--title", "X"],
    )
    res = runner.invoke(
        app,
        ["roadmap", "propose", "--phase", "P21", "--title", "Y"],
    )
    assert res.exit_code != 0
    assert "already exists" in res.stderr or "already exists" in res.output


def test_roadmap_revise_add_wave(workspace: Path) -> None:
    runner.invoke(
        app,
        ["roadmap", "propose", "--phase", "P21", "--title", "X"],
    )
    res = runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--success",
            "criterion1,criterion2",
            "--criteria-floor-waiver",
            "test fixture models legacy success strings",
            "--agent-role",
            "executor",
            "--effort-bucket",
            "S",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert "P21-I01-W01" in state["waves"]
    assert state["waves"]["P21-I01-W01"]["title"] == "Foo handling"
    stored_criteria = state["waves"]["P21-I01-W01"]["success_criteria"]
    assert [c["text"] for c in stored_criteria] == ["criterion1", "criterion2"]
    assert all(c["kind"] == "legacy" for c in stored_criteria)


def test_roadmap_revise_set_deps(workspace: Path) -> None:
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    for wid in ("W01", "W02"):
        runner.invoke(
            app,
            [
                "roadmap",
                "revise",
                "P21",
                "--add-wave",
                wid,
                "--title",
                f"Wave {wid}",
                "--files",
                "src/",
                "--effort-bucket",
                "M",
                "--intent-problem",
                "auto intent problem",
                "--intent-desired-outcome",
                "auto intent outcome",
                "--intent-priority-rationale",
                "auto intent rationale",
            ],
        )
    res = runner.invoke(
        app,
        ["roadmap", "revise", "P21", "--set-deps", "W02=W01"],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["waves"]["P21-I01-W02"]["deps"] == ["P21-I01-W01"]


def test_roadmap_revise_remove_wave(workspace: Path) -> None:
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "First wave",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    res = runner.invoke(app, ["roadmap", "revise", "P21", "--remove-wave", "W01"])
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert "P21-I01-W01" not in state["waves"]


def test_roadmap_revise_requires_exactly_one_action(workspace: Path) -> None:
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    res = runner.invoke(app, ["roadmap", "revise", "P21"])
    assert res.exit_code != 0


def test_roadmap_revise_rejects_non_planned(workspace: Path) -> None:
    # P21 doesn't exist; revise should reject as unknown phase.
    res = runner.invoke(
        app,
        ["roadmap", "revise", "P21", "--remove-wave", "W01"],
    )
    assert res.exit_code != 0


def _set_phase_status(workspace: Path, phase_id: str, status: str) -> None:
    """Test helper: rewrite ``.ea/state.json`` to flip *phase_id*'s status.

    Used by the P19-W12 revise-on-ACTIVE tests so the integration suite
    doesn't have to drive the full activate/close lifecycle just to reach
    a particular phase status. The file is the only persistent state so
    a direct rewrite is equivalent for the read-side behaviour the CLI
    gate is testing.
    """
    state_path = workspace / ".ea" / "state.json"
    state_blob = orjson.loads(state_path.read_bytes())
    state_blob["phases"][phase_id]["status"] = status
    state_path.write_bytes(orjson.dumps(state_blob))


def _propose_with_wave(workspace: Path) -> None:
    """Reusable fixture: propose P21 + W01 then leave PLANNED."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )


def test_roadmap_revise_planned_phase_still_allows_pending_wave(workspace: Path) -> None:
    """P19-W12: PLANNED parent + PENDING wave path is unchanged."""
    _propose_with_wave(workspace)
    res = runner.invoke(
        app,
        ["roadmap", "revise", "P21", "--retitle", "W01=Updated wave"],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["waves"]["P21-I01-W01"]["title"] == "Updated wave"
    assert state["phases"]["P21"]["status"] == "planned"


def test_roadmap_revise_retitle_iter_routes_to_iter(workspace: Path) -> None:
    """--retitle P##-I## routes to the iter title, not a wave."""
    _propose_with_wave(workspace)
    res = runner.invoke(
        app,
        ["roadmap", "revise", "P21", "--retitle", "P21-I01=TUI richer views"],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["iters"]["P21-I01"]["title"] == "TUI richer views"
    # the wave title is untouched — the retitle hit the iter, not the wave
    assert state["waves"]["P21-I01-W01"]["title"] == "Foo handling"


def test_roadmap_revise_retitle_iter_status_agnostic_on_active(workspace: Path) -> None:
    """--retitle on an iter works under an ACTIVE phase (cosmetic, no gate)."""
    _propose_with_wave(workspace)
    _set_phase_status(workspace, "P21", "active")
    res = runner.invoke(
        app,
        ["roadmap", "revise", "P21", "--retitle", "P21-I01=Normalised iter title"],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["iters"]["P21-I01"]["title"] == "Normalised iter title"


def test_roadmap_revise_retitle_iter_on_closed_phase(workspace: Path) -> None:
    """--retitle on an iter works under a CLOSED phase (cosmetic, skips gate).

    Wave edits keep the PLANNED/ACTIVE gate, but an iter retitle is
    status-agnostic (id preserved, no lifecycle change), so a CLOSED-phase
    iter title can still be normalized.
    """
    _propose_with_wave(workspace)
    # Properly close the phase + iter + wave (status + closed_at) so the
    # post-mutation closure invariants hold; a bare status flip would leave a
    # structurally invalid CLOSED phase. Exercises the iter-retitle gate-skip.
    state_path = workspace / ".ea" / "state.json"
    blob = orjson.loads(state_path.read_bytes())
    ts = "2026-05-22T00:00:00Z"
    blob["phases"]["P21"].update(status="closed", closed_at=ts)
    blob["iters"]["P21-I01"].update(status="closed", closed_at=ts)
    blob["waves"]["P21-I01-W01"].update(status="closed", closed_at=ts)
    state_path.write_bytes(orjson.dumps(blob))
    res = runner.invoke(
        app,
        ["roadmap", "revise", "P21", "--retitle", "P21-I01=Closed-phase normalised"],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["iters"]["P21-I01"]["title"] == "Closed-phase normalised"


def test_roadmap_revise_retitle_wave_on_closed_phase_rejected(workspace: Path) -> None:
    """A wave retitle under a CLOSED phase stays gated — only iter retitle skips."""
    _propose_with_wave(workspace)
    _set_phase_status(workspace, "P21", "closed")
    res = runner.invoke(
        app,
        ["roadmap", "revise", "P21", "--retitle", "W01=feat: nope"],
    )
    assert res.exit_code != 0
    assert "closed" in res.output.lower()


def test_roadmap_revise_active_phase_allows_pending_wave(workspace: Path) -> None:
    """P19-W12: ACTIVE parent + PENDING wave is the new escape hatch."""
    _propose_with_wave(workspace)
    _set_phase_status(workspace, "P21", "active")
    res = runner.invoke(
        app,
        ["roadmap", "revise", "P21", "--retitle", "W01=Revised in flight"],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["waves"]["P21-I01-W01"]["title"] == "Revised in flight"
    assert state["phases"]["P21"]["status"] == "active"


def test_roadmap_revise_active_phase_add_wave_allowed(workspace: Path) -> None:
    """P19-W12: --add-wave under an ACTIVE phase plans a new PENDING wave."""
    _propose_with_wave(workspace)
    _set_phase_status(workspace, "P21", "active")
    res = runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W02",
            "--title",
            "Extra wave",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert "P21-I01-W02" in state["waves"]
    assert state["waves"]["P21-I01-W02"]["status"] == "pending"


def test_roadmap_revise_active_phase_rejects_closed_wave(workspace: Path) -> None:
    """P19-W12: a CLOSED wave under an ACTIVE phase stays immutable."""
    _propose_with_wave(workspace)
    # Bypass the full activate/claim/close pipeline by flipping statuses
    # directly. The CLI gate routes through the lifecycle transitions
    # which check wave.status; we only need the wave's status field to
    # be CLOSED to exercise that guard.
    state_path = workspace / ".ea" / "state.json"
    state_blob = orjson.loads(state_path.read_bytes())
    state_blob["phases"]["P21"]["status"] = "active"
    state_blob["waves"]["P21-I01-W01"]["status"] = "closed"
    state_path.write_bytes(orjson.dumps(state_blob))
    res = runner.invoke(
        app,
        ["roadmap", "revise", "P21", "--retitle", "W01=feat: too late"],
    )
    assert res.exit_code != 0
    combined = f"{res.stdout}{res.stderr}"
    assert "not pending" in combined


def test_roadmap_revise_closed_phase_rejected(workspace: Path) -> None:
    """P19-W12: CLOSED phase is immutable via revise."""
    _propose_with_wave(workspace)
    _set_phase_status(workspace, "P21", "closed")
    res = runner.invoke(
        app,
        ["roadmap", "revise", "P21", "--retitle", "W01=feat: no"],
    )
    assert res.exit_code != 0
    combined = f"{res.stdout}{res.stderr}"
    assert "closed" in combined


def test_roadmap_apply_requires_wave(workspace: Path) -> None:
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    res = runner.invoke(app, ["roadmap", "apply", "P21"])
    assert res.exit_code != 0
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "First wave",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    res = runner.invoke(app, ["roadmap", "apply", "P21"])
    assert res.exit_code == 0, res.output


def test_roadmap_apply_renders_wave_dag_and_gates_with_needs_user(workspace: Path) -> None:
    """Bare apply renders the full wave DAG and emits a needs_user AUQ gate."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "First wave",
            "--files",
            "src/a",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W02",
            "--title",
            "Second wave",
            "--files",
            "src/b",
            "--deps",
            "W01",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    res = runner.invoke(app, ["--json", "roadmap", "apply", "P21"])
    assert res.exit_code == 0, res.output
    body = orjson.loads(res.stdout)
    assert body["status"] == "needs_user"
    assert body["decision_kind"] == "approve_plan"
    assert body["wave_count"] == 2
    rendered_ids = [w["id"] for w in body["waves"]]
    assert rendered_ids == ["P21-I01-W01", "P21-I01-W02"]
    # W02's dep on W01 is surfaced in the rendered DAG.
    w02 = next(w for w in body["waves"] if w["id"] == "P21-I01-W02")
    assert w02["deps"] == ["P21-I01-W01"]
    assert [opt["label"] for opt in body["options"]] == ["approve", "revise", "cancel"]


def test_roadmap_apply_dag_text_lists_pending_waves(workspace: Path) -> None:
    """The text render of apply names every PENDING wave id."""
    _propose_with_wave(workspace)
    res = runner.invoke(app, ["roadmap", "apply", "P21"])
    assert res.exit_code == 0, res.output
    assert "Wave DAG" in res.output
    assert "P21-I01-W01" in res.output


def test_roadmap_apply_approve_finalises_to_ok(workspace: Path) -> None:
    """`--approve` confirms the DAG and emits an ok envelope for /prep."""
    _propose_with_wave(workspace)
    res = runner.invoke(app, ["--json", "roadmap", "apply", "P21", "--approve"])
    assert res.exit_code == 0, res.output
    body = orjson.loads(res.stdout)
    assert body["status"] == "ok"
    assert body["wave_count"] == 1
    assert body["next"] == "eawf prep P21"


def test_roadmap_apply_approve_emits_event(workspace: Path) -> None:
    """Only the --approve path appends a roadmap apply EVENT row."""
    _propose_with_wave(workspace)
    # Bare apply (needs_user) must not append the apply event.
    runner.invoke(app, ["roadmap", "apply", "P21"])
    before = [e for e in _read_events(workspace) if e["payload"]["command"] == "roadmap apply"]
    assert not before
    runner.invoke(app, ["roadmap", "apply", "P21", "--approve"])
    after = [e for e in _read_events(workspace) if e["payload"]["command"] == "roadmap apply"]
    assert len(after) == 1


# ---- EAWF022 propose/apply coverage binding ---------------------------------


def _add_wave_with_uncovered_step(workspace: Path) -> None:
    """Add a P21-W01 whose criterion covers one planned step but drops another.

    The intent carries two planned steps; the single success criterion shares
    significant tokens with the first step only, so the second step is an
    uncovered EAWF022 span. ``--effort-bucket`` and ``--files`` satisfy the
    add-wave required-flag gate.
    """
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--success",
            "implement parser tokeniser module returning tokens",
            "--criteria-floor-waiver",
            "test fixture models legacy success strings",
            "--intent-problem",
            "parser drift",
            "--intent-desired-outcome",
            "parser covered",
            "--intent-planned-steps",
            "implement parser tokeniser module,wire telemetry dashboard exporter",
        ],
    )


def test_roadmap_apply_propose_render_surfaces_coverage_gap_advisory(
    workspace: Path,
) -> None:
    """The bare apply needs_user envelope surfaces the EAWF022 gap as advisory."""
    _add_wave_with_uncovered_step(workspace)
    res = runner.invoke(app, ["--json", "roadmap", "apply", "P21"])
    assert res.exit_code == 0, res.output
    body = orjson.loads(res.stdout)
    assert body["status"] == "needs_user"
    assert body["coverage_advisory"] is False
    gaps = body["coverage_gaps"]
    assert len(gaps) == 1
    assert gaps[0]["wave_id"] == "P21-I01-W01"
    # The dropped "telemetry dashboard exporter" step is the uncovered span.
    assert gaps[0]["uncovered_spans"]


def test_roadmap_apply_approve_blocks_on_coverage_gap(workspace: Path) -> None:
    """`--approve` refuses when a planned step is silently dropped (EAWF022)."""
    _add_wave_with_uncovered_step(workspace)
    res = runner.invoke(app, ["roadmap", "apply", "P21", "--approve"])
    assert res.exit_code != 0
    combined = f"{res.stdout}{res.stderr}"
    assert "EAWF022" in combined
    assert "uncovered planned steps" in combined
    # No apply EVENT lands when the blocking gate refuses.
    events = [e for e in _read_events(workspace) if e["payload"]["command"] == "roadmap apply"]
    assert not events


def test_roadmap_apply_approve_passes_when_every_step_covered(workspace: Path) -> None:
    """A wave whose criteria cover every planned step applies cleanly."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--success",
            "implement parser tokeniser module,wire telemetry dashboard exporter",
            "--criteria-floor-waiver",
            "test fixture models legacy success strings",
            "--intent-problem",
            "parser drift",
            "--intent-desired-outcome",
            "parser covered",
            "--intent-planned-steps",
            "implement parser tokeniser module,wire telemetry dashboard exporter",
        ],
    )
    res = runner.invoke(app, ["--json", "roadmap", "apply", "P21", "--approve"])
    assert res.exit_code == 0, res.output
    body = orjson.loads(res.stdout)
    assert body["status"] == "ok"


def test_roadmap_apply_advisory_clean_when_wave_has_no_intent(workspace: Path) -> None:
    """A wave with no planned steps contributes no coverage gap (clean no-op)."""
    _propose_with_wave(workspace)
    res = runner.invoke(app, ["--json", "roadmap", "apply", "P21"])
    assert res.exit_code == 0, res.output
    body = orjson.loads(res.stdout)
    assert body["coverage_gaps"] == []


def _write_source_brief(workspace: Path, *, rel: str, body: str) -> None:
    """Write a source-brief document under *workspace* at the repo-relative path."""
    brief = workspace / rel
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text(body, encoding="utf-8")


def _add_wave_with_uncovered_source_brief(workspace: Path, *, rel: str) -> None:
    """Add a P21-W01 with empty planned steps + a source brief it never covers.

    The wave is required-intent (its ``--intent-source-brief-ids`` names the
    on-disk brief) but carries no planned steps, so only the source-brief
    coverage leg can see the dropped ``telemetry dashboard exporter``
    deliverable enumerated in the brief.
    """
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--success",
            "implement parser tokeniser module returning tokens",
            "--criteria-floor-waiver",
            "test fixture models legacy success strings",
            "--intent-problem",
            "parser drift",
            "--intent-desired-outcome",
            "parser covered",
            "--intent-source-brief-ids",
            rel,
        ],
    )


def test_roadmap_apply_approve_blocks_on_source_brief_gap(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--approve` refuses an uncovered source-brief unit (empty planned_steps)."""
    monkeypatch.chdir(workspace)
    rel = ".ea/local/research/2026-06-09-brief.md"
    _write_source_brief(
        workspace,
        rel=rel,
        body=(
            "Implement the parser tokeniser module returning tokens.\n"
            "Wire the telemetry dashboard exporter for live metrics.\n"
        ),
    )
    _add_wave_with_uncovered_source_brief(workspace, rel=rel)
    res = runner.invoke(app, ["roadmap", "apply", "P21", "--approve"])
    assert res.exit_code != 0
    combined = f"{res.stdout}{res.stderr}"
    assert "EAWF022" in combined
    # No apply EVENT lands when the blocking gate refuses.
    events = [e for e in _read_events(workspace) if e["payload"]["command"] == "roadmap apply"]
    assert not events


def test_roadmap_apply_approve_passes_when_source_brief_fully_covered(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wave whose criteria cover every source-brief deliverable applies cleanly."""
    monkeypatch.chdir(workspace)
    rel = ".ea/local/research/2026-06-09-brief.md"
    _write_source_brief(
        workspace,
        rel=rel,
        body="Implement the parser tokeniser module returning tokens.\n",
    )
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--success",
            "implement parser tokeniser module returning tokens",
            "--criteria-floor-waiver",
            "test fixture models legacy success strings",
            "--intent-problem",
            "parser drift",
            "--intent-desired-outcome",
            "parser covered",
            "--intent-source-brief-ids",
            rel,
        ],
    )
    res = runner.invoke(app, ["--json", "roadmap", "apply", "P21", "--approve"])
    assert res.exit_code == 0, res.output
    body = orjson.loads(res.stdout)
    assert body["status"] == "ok"


def test_roadmap_apply_non_planned_phase_rejected(workspace: Path) -> None:
    """Apply on a non-PLANNED phase is rejected (only PLANNED can apply)."""
    _propose_with_wave(workspace)
    _set_phase_status(workspace, "P21", "active")
    res = runner.invoke(app, ["roadmap", "apply", "P21"])
    assert res.exit_code != 0
    combined = f"{res.stdout}{res.stderr}"
    assert "only PLANNED" in combined


def test_roadmap_drop_archives_planned(workspace: Path) -> None:
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    res = runner.invoke(app, ["roadmap", "drop", "P21"])
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["phases"]["P21"]["status"] == "archived"


def test_roadmap_drop_cascades_pending_waves_to_abandoned(workspace: Path) -> None:
    """Dropping a planned phase abandons its PENDING child waves + iter."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "First wave",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    res = runner.invoke(app, ["roadmap", "drop", "P21"])
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["phases"]["P21"]["status"] == "archived"
    assert state["waves"]["P21-I01-W01"]["status"] == "abandoned"
    assert state["waves"]["P21-I01-W01"]["closed_at"] is not None
    assert state["iters"]["P21-I01"]["status"] == "abandoned"


def test_roadmap_show_renders_planned_queue(workspace: Path) -> None:
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "Test"])
    res = runner.invoke(app, ["roadmap", "show"])
    assert res.exit_code == 0
    assert "P21" in res.output
    assert "planned" in res.output


def test_roadmap_show_json_envelope(workspace: Path) -> None:
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "Test"])
    res = runner.invoke(app, ["--json", "roadmap", "show"])
    assert res.exit_code == 0
    body = orjson.loads(res.stdout)
    assert any(row["id"] == "P21" for row in body["phases"])


def test_roadmap_show_md_consumes_eu_view_config(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI ``--md`` threads merged ``tui.eu_view`` config into the renderer."""
    _propose_with_wave(workspace)
    monkeypatch.setenv("EAWF_TUI__EU_VIEW__DENSITY", "compact")
    monkeypatch.setenv("EAWF_TUI__EU_VIEW__FIELDS", "realistic")

    res = runner.invoke(app, ["roadmap", "show", "--phase", "P21", "--md"])

    assert res.exit_code == 0, res.output
    assert "| Phase | Metric | EU | Hours |" in res.output
    assert "| `P21` | realistic |" in res.output
    assert "work-sum" not in res.output


def test_roadmap_show_rich_renders_phases_iters_waves(workspace: Path) -> None:
    """P20-W01: rich-table renderer surfaces phases, iters, and waves."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "Test phase"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Alpha wave",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    res = runner.invoke(app, ["roadmap", "show"])
    assert res.exit_code == 0, res.output
    # All three levels render in the table body.
    assert "P21" in res.output
    assert "P21-I01" in res.output
    assert "P21-I01-W01" in res.output
    # Row-kind markers present (Rich strips bold styling in non-TTY mode
    # but the literal kind text remains).
    assert "phase" in res.output
    assert "iter" in res.output
    assert "wave" in res.output


def test_roadmap_show_plain_fallback(workspace: Path) -> None:
    """``--plain`` bypasses Rich markup and emits the deterministic ASCII grid."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "Test"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Alpha wave",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    res = runner.invoke(app, ["--plain", "roadmap", "show"])
    assert res.exit_code == 0, res.output
    # Header line is from the plain renderer (no Rich box-drawing chars).
    assert "kind" in res.output
    assert "P21" in res.output
    assert "P21-I01-W01" in res.output


def test_roadmap_show_stale_planned_phase_marked(workspace: Path) -> None:
    """A PLANNED phase opened > freshness window ago renders as muted."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "Test"])
    # Hand-roll an older opened_at on the phase so the freshness gate trips.
    state_path = workspace / ".ea" / "state.json"
    blob = orjson.loads(state_path.read_bytes())
    blob["phases"]["P21"]["opened_at"] = "2024-01-01T00:00:00Z"
    blob["iters"]["P21-I01"]["opened_at"] = "2024-01-01T00:00:00Z"
    state_path.write_bytes(orjson.dumps(blob))
    res = runner.invoke(app, ["--plain", "roadmap", "show"])
    assert res.exit_code == 0, res.output
    # Plain renderer marks stale rows with a trailing "(stale)" tag.
    assert "(stale)" in res.output


def test_roadmap_show_active_phase_not_stale(workspace: Path) -> None:
    """ACTIVE phases never read as stale regardless of opened_at age."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "Test"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Alpha wave",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    # Backdate + flip to ACTIVE so the freshness check would trip if it
    # weren't gated on PhaseStatus.PLANNED.
    state_path = workspace / ".ea" / "state.json"
    blob = orjson.loads(state_path.read_bytes())
    blob["phases"]["P21"]["opened_at"] = "2024-01-01T00:00:00Z"
    blob["phases"]["P21"]["status"] = "active"
    blob["iters"]["P21-I01"]["status"] = "active"
    state_path.write_bytes(orjson.dumps(blob))
    res = runner.invoke(app, ["--plain", "roadmap", "show"])
    assert res.exit_code == 0, res.output
    # The phase line itself must not carry the (stale) tag.
    phase_line = next(line for line in res.output.splitlines() if line.startswith("phase"))
    assert "(stale)" not in phase_line


def test_roadmap_show_dormant_iter_marked(workspace: Path) -> None:
    """ACTIVE iter older than the window with only PENDING waves is dormant."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "Test"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Alpha wave",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    state_path = workspace / ".ea" / "state.json"
    blob = orjson.loads(state_path.read_bytes())
    blob["phases"]["P21"]["status"] = "active"
    blob["iters"]["P21-I01"]["status"] = "active"
    blob["iters"]["P21-I01"]["opened_at"] = "2024-01-01T00:00:00Z"
    state_path.write_bytes(orjson.dumps(blob))
    res = runner.invoke(app, ["--plain", "roadmap", "show"])
    assert res.exit_code == 0, res.output
    iter_line = next(line for line in res.output.splitlines() if line.lstrip().startswith("iter"))
    assert "(stale)" in iter_line


def test_roadmap_show_empty_state(workspace: Path) -> None:
    """No phases proposed -> renderer returns the empty-state literal."""
    res = runner.invoke(app, ["roadmap", "show"])
    assert res.exit_code == 0, res.output
    assert "no phases" in res.output


def test_roadmap_show_md_branch_unchanged(workspace: Path) -> None:
    """``--md`` still emits the markdown table (regression guard)."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "Test"])
    res = runner.invoke(app, ["roadmap", "show", "--md"])
    assert res.exit_code == 0, res.output
    assert "| Phase |" in res.output
    assert "P21" in res.output


def test_roadmap_revise_sets_phase_release(workspace: Path) -> None:
    """``revise --retitle P## --release vX.Y.Z`` persists the release band."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "Test"])
    res = runner.invoke(
        app,
        ["roadmap", "revise", "P21", "--retitle", "P21", "--release", "v0.5.0"],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["phases"]["P21"]["release"] == "v0.5.0"


def test_roadmap_revise_rejects_invalid_release(workspace: Path) -> None:
    """An invalid release string is surfaced as a clean InvalidInput error."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "Test"])
    res = runner.invoke(
        app,
        ["roadmap", "revise", "P21", "--retitle", "P21", "--release", "0.5.0"],
    )
    assert res.exit_code != 0, res.output
    state = _read_state(workspace)
    assert state["phases"]["P21"].get("release") is None


def test_roadmap_show_md_bands_by_release(workspace: Path) -> None:
    """``roadmap show --md`` bands phases once a release is set."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "Banded"])
    runner.invoke(
        app,
        ["roadmap", "revise", "P21", "--retitle", "P21", "--release", "v0.5.0"],
    )
    res = runner.invoke(app, ["roadmap", "show", "--md"])
    assert res.exit_code == 0, res.output
    assert "### v0.5.0" in res.output
    assert "`P21`" in res.output


def test_phase_activate_planned_phase(workspace: Path) -> None:
    """P19-W07: ``eawf phase activate`` flips PLANNED -> ACTIVE."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    res = runner.invoke(app, ["phase", "activate", "P21"])
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["phases"]["P21"]["status"] == "active"
    assert state["current"]["phase_id"] == "P21"


def test_phase_activate_without_waves_rejected(workspace: Path) -> None:
    """activate_phase requires at least one wave."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    res = runner.invoke(app, ["phase", "activate", "P21"])
    assert res.exit_code != 0


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git is required for phase-activate gate tests")
def test_phase_activate_dirty_worktree_rejected(workspace: Path) -> None:
    """P19-W11 gate 3: ``git status --porcelain`` non-empty blocks activation."""
    _make_local_repo(workspace)
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    # Leave an untracked file in the worktree so the porcelain check trips.
    (workspace / "dirty.txt").write_text("scratch\n", encoding="utf-8")
    res = runner.invoke(app, ["phase", "activate", "P21"])
    assert res.exit_code != 0
    combined = res.output + (res.stderr or "")
    assert "dirty" in combined
    state = _read_state(workspace)
    assert state["phases"]["P21"]["status"] == "planned"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git is required for phase-activate gate tests")
def test_phase_activate_behind_upstream_rejected(workspace: Path, tmp_path: Path) -> None:
    """P19-W11 gate 2: HEAD behind ``origin/<default_branch>`` blocks activation."""
    _make_local_repo(workspace)
    remote = tmp_path / "origin.git"
    _make_origin_remote(workspace, remote=remote)
    _bare_commit_on_origin_main(workspace, remote=remote)
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    # Commit roadmap mutations so the dirty gate does not mask the currency gate.
    _commit_state_changes(workspace, msg="seed roadmap")
    res = runner.invoke(app, ["phase", "activate", "P21"])
    assert res.exit_code != 0
    combined = res.output + (res.stderr or "")
    assert "rebase first" in combined
    state = _read_state(workspace)
    assert state["phases"]["P21"]["status"] == "planned"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git is required for phase-activate gate tests")
def test_phase_activate_allow_stale_bypasses_currency_gate(workspace: Path, tmp_path: Path) -> None:
    """P19-W13: ``--allow-stale`` bypasses currency gate even when HEAD is behind."""
    _make_local_repo(workspace)
    remote = tmp_path / "origin.git"
    _make_origin_remote(workspace, remote=remote)
    _bare_commit_on_origin_main(workspace, remote=remote)
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    _commit_state_changes(workspace, msg="seed roadmap")
    res = runner.invoke(app, ["phase", "activate", "P21", "--allow-stale"])
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["phases"]["P21"]["status"] == "active"


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git is required for phase-activate gate tests")
def test_phase_activate_local_only_branch_skips_currency_check(workspace: Path) -> None:
    """No ``origin`` remote skips the currency gate (clean tree still activates)."""
    _make_local_repo(workspace)
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    _commit_state_changes(workspace, msg="seed roadmap")
    res = runner.invoke(app, ["phase", "activate", "P21"])
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["phases"]["P21"]["status"] == "active"


def _read_events(workspace: Path) -> list[dict]:
    path = workspace / ".ea" / "store" / "event.jsonl"
    if not path.exists():
        return []
    return [orjson.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_roadmap_propose_emits_event(workspace: Path) -> None:
    """P19-W06: propose appends an EVENT envelope to event.jsonl."""
    before = len(_read_events(workspace))
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "Test"])
    after = _read_events(workspace)
    assert len(after) > before
    propose_event = next(
        (e for e in after if e["payload"]["command"] == "roadmap propose"),
        None,
    )
    assert propose_event is not None
    assert propose_event["scope_id"] == "P21"


def test_roadmap_revise_emits_event(workspace: Path) -> None:
    """revise --add-wave emits its own EVENT envelope."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    events = _read_events(workspace)
    revise_events = [e for e in events if e["payload"]["command"] == "roadmap revise"]
    assert revise_events, "expected at least one roadmap revise event"


def test_roadmap_drop_emits_event(workspace: Path) -> None:
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(app, ["roadmap", "drop", "P21"])
    events = _read_events(workspace)
    assert any(e["payload"]["command"] == "roadmap drop" for e in events)


def test_wave_show_commit_returns_sha_when_present(workspace: Path) -> None:
    """``eawf wave show --commit`` exits 0; empty stdout when no match."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    res = runner.invoke(app, ["wave", "show", "P21-I01-W01", "--commit"])
    # Test repo has no [P21-W01] commit so output is empty; exit is still 0.
    assert res.exit_code == 0, res.output


def test_wave_show_without_commit_rejected(workspace: Path) -> None:
    res = runner.invoke(app, ["wave", "show", "P21-I01-W01"])
    assert res.exit_code != 0


def test_wave_show_dispatch_prompt_returns_prompt(workspace: Path) -> None:
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )

    res = runner.invoke(app, ["wave", "show", "P21-I01-W01", "--dispatch-prompt"])

    assert res.exit_code == 0, res.output
    assert "P21-I01-W01" in res.stdout
    assert "Foo handling" in res.stdout
    assert "## Wave tags" in res.stdout


def test_wave_claim_out_of_order_flag_overrides_monotonic_gate(workspace: Path) -> None:
    """CLI flag plumbs through to ``claim_wave``'s out_of_order escape hatch."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    for wid in ("W01", "W02"):
        runner.invoke(
            app,
            [
                "roadmap",
                "revise",
                "P21",
                "--add-wave",
                wid,
                "--title",
                f"Wave {wid}",
                "--files",
                "src/",
                "--effort-bucket",
                "M",
                "--intent-problem",
                "auto intent problem",
                "--intent-desired-outcome",
                "auto intent outcome",
                "--intent-priority-rationale",
                "auto intent rationale",
            ],
        )
    # Default claim of W02 is rejected because W01 is still PENDING + ready.
    blocked = runner.invoke(app, ["wave", "claim", "P21-I01-W02", "--session", "S"])
    assert blocked.exit_code != 0
    # --out-of-order escape hatch must succeed.
    ok = runner.invoke(
        app,
        ["wave", "claim", "P21-I01-W02", "--session", "S", "--out-of-order"],
    )
    assert ok.exit_code == 0, ok.output


def test_iter_activate_planned_iter(workspace: Path) -> None:
    """``eawf iter activate`` flips PLANNED -> ACTIVE on the iter."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    runner.invoke(app, ["phase", "activate", "P21"])
    res = runner.invoke(app, ["iter", "activate", "P21-I01"])
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["iters"]["P21-I01"]["status"] == "active"


# ---- --description round-trip (P28-W02) ------------------------------------


def test_roadmap_propose_persists_description(workspace: Path) -> None:
    """roadmap propose --description lands on the Phase.description field."""
    res = runner.invoke(
        app,
        [
            "roadmap",
            "propose",
            "--phase",
            "P21",
            "--title",
            "Plan import",
            "--description",
            "long-form phase narrative captured at propose time",
        ],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["phases"]["P21"]["description"] == (
        "long-form phase narrative captured at propose time"
    )


def test_roadmap_propose_iter_description(workspace: Path) -> None:
    """roadmap propose --iter-description lands on the auto-created I01 iter."""
    res = runner.invoke(
        app,
        [
            "roadmap",
            "propose",
            "--phase",
            "P21",
            "--title",
            "Plan import",
            "--description",
            "phase scope summary",
            "--iter-description",
            "iter scope summary",
        ],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["iters"]["P21-I01"]["description"] == "iter scope summary"


def test_roadmap_propose_description_over_cap_rejected(workspace: Path) -> None:
    """--description > 500 chars surfaces a clean error, not a Pydantic stack trace."""
    res = runner.invoke(
        app,
        [
            "roadmap",
            "propose",
            "--phase",
            "P21",
            "--title",
            "Plan import",
            "--description",
            "z" * 501,
        ],
    )
    assert res.exit_code != 0
    combined = res.output + (res.stderr or "")
    # Clean validation message (not a raw traceback).
    assert "500" in combined or "string_too_long" in combined


def test_roadmap_revise_add_wave_persists_description(workspace: Path) -> None:
    """roadmap revise --add-wave --description lands on Wave.description."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    res = runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Worker wave",
            "--files",
            "src/",
            "--description",
            "wave-level narrative for the add",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["waves"]["P21-I01-W01"]["description"] == "wave-level narrative for the add"


def test_roadmap_revise_retitle_wave_with_description(workspace: Path) -> None:
    """roadmap revise --retitle WAVE --description updates wave description."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Worker wave",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    res = runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--retitle",
            "W01=Renamed wave",
            "--description",
            "annotation added post-plan",
        ],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["waves"]["P21-I01-W01"]["title"] == "Renamed wave"
    assert state["waves"]["P21-I01-W01"]["description"] == "annotation added post-plan"


def test_roadmap_revise_retitle_iter_with_description(workspace: Path) -> None:
    """roadmap revise --retitle ITER --description updates iter description."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    res = runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--retitle",
            "P21-I01=renamed iter",
            "--description",
            "iter narrative added post-plan",
        ],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["iters"]["P21-I01"]["title"] == "renamed iter"
    assert state["iters"]["P21-I01"]["description"] == "iter narrative added post-plan"


def test_phase_open_persists_description(workspace: Path) -> None:
    """phase open --description lands on Phase.description."""
    res = runner.invoke(
        app,
        [
            "phase",
            "open",
            "P22",
            "--title",
            "P22 phase",
            "--description",
            "open-time phase description",
        ],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["phases"]["P22"]["description"] == "open-time phase description"


def test_iter_plan_persists_description(workspace: Path) -> None:
    """iter plan --description lands on Iter.description."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    res = runner.invoke(
        app,
        [
            "iter",
            "plan",
            "P21-I02",
            "--title",
            "second iter",
            "--description",
            "second-iter narrative",
        ],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["iters"]["P21-I02"]["description"] == "second-iter narrative"


def test_wave_plan_persists_description(workspace: Path) -> None:
    """wave plan --description lands on Wave.description.

    Exercises the daemon JSON-RPC ROADMAP_REVISE path: ``wave plan`` carries
    ``mutation_kind=ROADMAP_REVISE`` so the description flows through
    ``state.mutate`` (when the daemon is up) or the in-process fallback
    (always exercised in these tests since the test fixture doesn't spawn
    the daemon).
    """
    runner.invoke(
        app,
        [
            "phase",
            "open",
            "P22",
            "--title",
            "X",
        ],
    )
    runner.invoke(
        app,
        [
            "iter",
            "open",
            "P22-I01",
            "--title",
            "i",
        ],
    )
    res = runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P22-I01",
            "--id",
            "P22-I01-W01",
            "--title",
            "Worker wave",
            "--files",
            "src/",
            "--description",
            "wave-plan description landing through the planner",
            "--effort-bucket",
            "M",
        ],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["waves"]["P22-I01-W01"]["description"] == (
        "wave-plan description landing through the planner"
    )


def test_roadmap_revise_add_wave_description_over_cap_rejected(workspace: Path) -> None:
    """An over-cap (>500 chars) --description on add-wave surfaces a clean error."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    res = runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Worker wave",
            "--files",
            "src/",
            "--description",
            "z" * 501,
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    assert res.exit_code != 0
    combined = res.output + (res.stderr or "")
    assert "500" in combined or "string_too_long" in combined


def _propose_with_second_iter(workspace: Path) -> None:
    """Propose P21 (auto-creates I01) then plan a PLANNED P21-I02 iter."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    res = runner.invoke(
        app,
        ["iter", "plan", "P21-I02", "--title", "second iter"],
    )
    assert res.exit_code == 0, res.output


def test_roadmap_revise_add_wave_iter_targets_named_iter(workspace: Path) -> None:
    """--iter aims --add-wave at the named iter instead of the I01 default."""
    _propose_with_second_iter(workspace)
    res = runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--iter",
            "P21-I02",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    # The wave lands under I02, not I01.
    assert "P21-I02-W01" in state["waves"]
    assert state["waves"]["P21-I02-W01"]["iter_id"] == "P21-I02"
    assert state["iters"]["P21-I02"]["wave_ids"] == ["P21-I02-W01"]


def test_roadmap_revise_add_wave_bare_iter_suffix_accepted(workspace: Path) -> None:
    """--iter accepts the bare I## suffix, expanded against the phase."""
    _propose_with_second_iter(workspace)
    res = runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--iter",
            "I02",
            "--add-wave",
            "W03",
            "--title",
            "Bar handling",
            "--files",
            "src/",
            "--effort-bucket",
            "S",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert "P21-I02-W03" in state["waves"]


def test_roadmap_revise_omitted_iter_keeps_i01_default(workspace: Path) -> None:
    """Without --iter the add-wave still lands under I01 (back-compat)."""
    _propose_with_second_iter(workspace)
    res = runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Default wave",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert "P21-I01-W01" in state["waves"]
    assert "P21-I02-W01" not in state["waves"]


def test_roadmap_revise_unknown_iter_rejected(workspace: Path) -> None:
    """--iter naming an iter absent from state is rejected (NotFound)."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    res = runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--iter",
            "P21-I09",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    assert res.exit_code != 0
    combined = res.output + (res.stderr or "")
    assert "P21-I09" in combined


def test_roadmap_revise_iter_under_other_phase_rejected(workspace: Path) -> None:
    """--iter naming an iter under a different phase is rejected."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    # A second phase with its own I01.
    runner.invoke(app, ["roadmap", "propose", "--phase", "P22", "--title", "Y"])
    res = runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--iter",
            "P22-I01",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    assert res.exit_code != 0
    combined = res.output + (res.stderr or "")
    assert "P22-I01" in combined


def test_roadmap_revise_invalid_iter_id_rejected(workspace: Path) -> None:
    """A malformed --iter value surfaces a clean InvalidInput error."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    res = runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--iter",
            "not-an-iter",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    assert res.exit_code != 0


def test_roadmap_revise_retitle_phase_rewrites_title(workspace: Path) -> None:
    """--retitle against a phase id edits the phase title via edit_phase_plan."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    res = runner.invoke(
        app,
        ["roadmap", "revise", "P21", "--retitle", "P21=Renamed phase"],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["phases"]["P21"]["title"] == "Renamed phase"


def test_roadmap_revise_phase_description_only_edit(workspace: Path) -> None:
    """--retitle PHASE with no title but --description edits only the description."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "Keep me"])
    res = runner.invoke(
        app,
        [
            "roadmap",
            "revise",
            "P21",
            "--retitle",
            "P21",
            "--description",
            "phase narrative",
        ],
    )
    assert res.exit_code == 0, res.output
    state = _read_state(workspace)
    assert state["phases"]["P21"]["title"] == "Keep me"
    assert state["phases"]["P21"]["description"] == "phase narrative"


def test_roadmap_revise_retitle_phase_over_cap_title_rejected(workspace: Path) -> None:
    """An over-cap (>72 chars) phase title via --retitle surfaces a clean error."""
    runner.invoke(app, ["roadmap", "propose", "--phase", "P21", "--title", "X"])
    res = runner.invoke(
        app,
        ["roadmap", "revise", "P21", "--retitle", f"P21={'z' * 73}"],
    )
    assert res.exit_code != 0
