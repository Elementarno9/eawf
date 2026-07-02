"""Floor-pack ↔ readiness integration tests (P28-I01-W10).

Pins the W10 sc:

* When a wave has no typed CriterionSpec rows but the active profile
  carries a non-empty :class:`VerifyBlock.floor_checks`, the
  readiness compute renders one ``CriterionView(source="floor")``
  per floor check.
* The 3 fixture profiles compile byte-DIFFERENT floor packs but
  yield byte-IDENTICAL :class:`CloseReadiness` shapes (same field
  names + ordering keys; only the values differ).
* A typed CriterionSpec on the wave wins: when both typed specs AND
  a profile floor pack are present, the floor pack does NOT render
  (typed specs are authoritative).
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from eawf.kernel.spec.common import CriterionSpec, QualityDimension
from eawf.kernel.state.enums import ProjectStatus, ScopeKind
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.kernel.store.paths import store_dir as _store_dir
from eawf.platform.profiles import load_profile
from eawf.platform.profiles.models import FloorCheck, VerifyBlock
from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec
from eawf.workflow.lifecycle.transitions import (
    LifecycleError,
    claim_wave,
    open_iter,
    open_phase,
    plan_wave,
)
from eawf.workflow.verify import readiness as readiness_mod
from tests._criteria_helpers import legacy_criteria
from tests.conftest import make_floor_waiver, make_intent

WAVE_ID = "P01-I01-W01"

FloorScope = Literal["changed", "touched", "all"]
FloorPolicy = Literal["block", "warn", "advisory"]


# ---- fixtures ---------------------------------------------------------------


def _empty_state() -> State:
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:FLR",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="FLR",
                slug="flr",
                title="FLR",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:FLR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="FLR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _seed_wave(
    state: State,
    *,
    success_criteria: list[str] | None = None,
    file_scopes: list[str] | None = None,
) -> None:
    open_phase(state, phase_id="P01", title="phase")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="iter")
    plan_wave(
        state,
        wave_id=WAVE_ID,
        iter_id="P01-I01",
        title="wave",
        file_scopes=file_scopes or ["src/"],
        success_criteria=legacy_criteria(*(success_criteria or [])),
        criteria_floor_waiver=make_floor_waiver(),
        effort_bucket="M",
        intent=make_intent(),
    )
    claim_wave(state, wave_id=WAVE_ID, session_id="SES-flr")


def _git(repo_root: Path, *args: str) -> None:
    """Run a quiet ``git -C <repo_root>`` command, raising on non-zero exit."""
    subprocess.run(["git", "-C", str(repo_root), *args], check=True, capture_output=True)


def _init_test_repo(repo_root: Path) -> None:
    """Initialise *repo_root* as a minimal git repo with one commit.

    The floor-pack live-run path invokes the W15-hardened gate runner
    which expects a real git tree.
    """
    repo_root.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "init", "-q", "-b", "main")
    _git(repo_root, "config", "user.email", "test@example.com")
    _git(repo_root, "config", "user.name", "test")
    (repo_root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo_root, "add", ".")
    _git(repo_root, "commit", "-q", "-m", "seed")


def _passing_floor_check(
    name: str, *, scope: FloorScope = "all", policy: FloorPolicy = "warn"
) -> FloorCheck:
    """Build a floor check whose argv reliably exits zero in a seeded repo."""
    return FloorCheck(
        name=name,
        cmd=["git", "status", "--porcelain"],
        scope=scope,
        cadence="every-wave",
        policy=policy,
    )


def _failing_floor_check(
    name: str, *, scope: FloorScope = "all", policy: FloorPolicy = "warn"
) -> FloorCheck:
    """Build a floor check whose argv reliably exits non-zero in any repo."""
    return FloorCheck(
        name=name,
        cmd=["git", "show", "no-such-ref-w10-floor"],
        scope=scope,
        cadence="every-wave",
        policy=policy,
    )


def _write_verify_profile(
    repo_root: Path,
    *,
    enforce: bool,
    command: list[str] | None = None,
) -> None:
    """Write a workspace profile selected by ``profiles.enabled``."""
    profile_dir = repo_root / ".ea" / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (repo_root / ".ea" / "config.yaml").write_text(
        "profiles:\n  enabled:\n    - enforcing\n",
        encoding="utf-8",
    )
    cmd = command or ["git", "show", "no-such-ref-w26-floor"]
    rendered_cmd = ", ".join(f'"{part}"' for part in cmd)
    profile_dir.joinpath("enforcing.yaml").write_text(
        "\n".join(
            [
                "name: enforcing",
                "verify:",
                f"  enforce: {'true' if enforce else 'false'}",
                "  argv_allowlist:",
                "    - git",
                "  floor_checks:",
                "    - name: fail-floor",
                f"      cmd: [{rendered_cmd}]",
                "      scope: all",
                "      cadence: every-wave",
                "      policy: block",
                "",
            ]
        ),
        encoding="utf-8",
    )


# ---- integration: floor pack -> readiness ---------------------------------


def test_no_typed_specs_and_no_floor_pack_keeps_legacy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SC: backward-compat — no floor pack means the W06 path is unchanged."""
    state = _empty_state()
    _seed_wave(state, success_criteria=["legacy a"])
    store_dir = _store_dir(tmp_path / "state.json")

    # Default _load_active_verify_block returns None; assert no
    # source="floor" view appears.
    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)
    assert all(view.source != "floor" for view in result.criteria)
    assert any(view.source == "legacy" for view in result.criteria)


def test_floor_pack_renders_one_criterion_view_per_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SC: each floor check becomes one ``CriterionView(source='floor')``."""
    state = _empty_state()
    _seed_wave(state)
    _init_test_repo(tmp_path)
    store_dir = _store_dir(tmp_path / "state.json")

    block = VerifyBlock(
        argv_allowlist=[],
        floor_checks=[
            _passing_floor_check("c-1"),
            _passing_floor_check("c-2"),
        ],
    )
    monkeypatch.setattr(
        readiness_mod,
        "_load_active_verify_block",
        lambda scope_id, state_arg, **kwargs: block,
    )

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    floor_views = [v for v in result.criteria if v.source == "floor"]
    assert len(floor_views) == 2
    assert [v.id for v in floor_views] == ["c-1", "c-2"]
    for view in floor_views:
        assert view.gate_results is not None
        assert len(view.gate_results) == 1
        assert view.gate_results[0].gate_id == view.id
        assert view.gate_results[0].status == "pass"


def test_floor_pack_failing_check_flips_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing ``policy: block`` floor check rolls the readiness to ``ready=False``."""
    state = _empty_state()
    _seed_wave(state)
    _init_test_repo(tmp_path)
    store_dir = _store_dir(tmp_path / "state.json")

    block = VerifyBlock(
        argv_allowlist=[],
        floor_checks=[_failing_floor_check("c-fail", policy="block")],
    )
    monkeypatch.setattr(
        readiness_mod,
        "_load_active_verify_block",
        lambda scope_id, state_arg, **kwargs: block,
    )

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    assert result.ready is False
    fail_view = next(v for v in result.criteria if v.id == "c-fail")
    assert fail_view.source == "floor"
    assert fail_view.status == "fail"
    assert fail_view.gate_results is not None
    assert fail_view.gate_results[0].status == "fail"


def test_real_profile_verify_enforce_rejects_not_ready(tmp_path: Path) -> None:
    """``profile.verify.enforce`` loaded from config turns readiness into a gate."""
    state = _empty_state()
    _seed_wave(state)
    _init_test_repo(tmp_path)
    _write_verify_profile(tmp_path, enforce=True)
    store_dir = _store_dir(tmp_path / ".ea" / "state.json")

    with pytest.raises(LifecycleError, match="readiness enforcement failed"):
        readiness_mod.compute(
            WAVE_ID,
            state=state,
            store_dir=store_dir,
            repo_root=tmp_path,
            config_root=tmp_path,
        )


def test_real_profile_without_enforce_stays_advisory(tmp_path: Path) -> None:
    """Default advisory profile mode returns ``ready=False`` without raising."""
    state = _empty_state()
    _seed_wave(state)
    _init_test_repo(tmp_path)
    _write_verify_profile(tmp_path, enforce=False)
    store_dir = _store_dir(tmp_path / ".ea" / "state.json")

    result = readiness_mod.compute(
        WAVE_ID,
        state=state,
        store_dir=store_dir,
        repo_root=tmp_path,
        config_root=tmp_path,
    )

    assert result.ready is False
    assert [view.id for view in result.criteria] == ["fail-floor"]


def test_floor_pack_yields_to_typed_spec_when_both_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SC: typed CriterionSpec wins — the floor pack does NOT render alongside.

    Typed specs are authoritative; the floor pack is the per-domain
    baseline for waves with NO typed criteria. When a wave has typed
    criteria the floor pack is silent.
    """
    state = _empty_state()
    _seed_wave(state)
    store_dir = _store_dir(tmp_path / "state.json")

    typed_criterion = CriterionSpec(
        id="CRIT-typed",
        text="typed criterion",
        kind="behavior",
        acceptance_style="binary",
        evidence_kind="jury",
        gate_ids=[],
        required=True,
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="the typed criterion wins and the floor pack stays silent for this wave",
    )
    monkeypatch.setattr(
        readiness_mod,
        "_load_criterion_specs",
        lambda scope_id, state_arg: [typed_criterion],
    )
    monkeypatch.setattr(readiness_mod, "_load_gate_specs", lambda scope_id, state_arg: [])

    block = VerifyBlock(
        argv_allowlist=[],
        floor_checks=[_passing_floor_check("floor-skipped")],
    )
    monkeypatch.setattr(
        readiness_mod,
        "_load_active_verify_block",
        lambda scope_id, state_arg, **kwargs: block,
    )

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    # The typed criterion renders; the floor view does NOT.
    assert any(v.id == "CRIT-typed" for v in result.criteria)
    assert all(v.source != "floor" for v in result.criteria)


def test_empty_floor_pack_does_not_render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile with an empty ``floor_checks`` list contributes nothing."""
    state = _empty_state()
    _seed_wave(state, success_criteria=[])
    store_dir = _store_dir(tmp_path / "state.json")

    block = VerifyBlock()  # default-empty floor pack
    monkeypatch.setattr(
        readiness_mod,
        "_load_active_verify_block",
        lambda scope_id, state_arg, **kwargs: block,
    )

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    # Empty wave + empty floor + empty spec -> "no criteria" warning.
    assert all(v.source != "floor" for v in result.criteria)
    assert result.warnings == ["no criteria attached to wave"]


# ---- 3 profiles -> distinct floor packs, identical readiness shape --------


def test_three_profiles_compile_distinct_floor_packs() -> None:
    """SC: python / apps / robotics yield byte-DIFFERENT compiled floor packs."""
    from eawf.workflow.verify.compile import compile_floor_pack

    packs: dict[str, list[str]] = {}
    for pid in ("python", "apps", "robotics"):
        body = load_profile(pid)
        assert body.verify is not None
        compiled = compile_floor_pack(
            body.verify.floor_checks,
            allowlist=list(body.verify.argv_allowlist),
        )
        packs[pid] = [spec.name for spec in compiled]

    # No two profile packs have the same set of check names.
    assert set(packs["python"]) != set(packs["apps"])
    assert set(packs["python"]) != set(packs["robotics"])
    assert set(packs["apps"]) != set(packs["robotics"])


def test_three_profiles_yield_identical_readiness_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SC: the 3 profiles render byte-IDENTICAL CloseReadiness *shape* — only values differ.

    "Same shape" = the model field names + the per-criterion view field
    names are identical across the 3 packs. The per-floor-check
    *values* (ids, statuses) differ because the cmd argvs differ.
    """
    state = _empty_state()
    _seed_wave(state)
    _init_test_repo(tmp_path)
    store_dir = _store_dir(tmp_path / "state.json")

    # Synthesise a deterministic "always pass" floor pack per profile
    # so the test does not depend on hil-smoke / sphinx-build being
    # installed. The shape is what we pin, not the value.
    def _faked_block(profile_id: str, check_count: int) -> VerifyBlock:
        return VerifyBlock(
            argv_allowlist=[],
            floor_checks=[
                FloorCheck(
                    name=f"{profile_id}-fc-{i}",
                    cmd=["git", "status", "--porcelain"],
                    scope="all",
                    cadence="every-wave",
                    policy="warn",
                )
                for i in range(check_count)
            ],
        )

    fake_blocks = {
        "python": _faked_block("python", 3),
        "apps": _faked_block("apps", 4),
        "robotics": _faked_block("robotics", 1),
    }

    rendered: dict[str, dict] = {}
    for pid, block in fake_blocks.items():
        monkeypatch.setattr(
            readiness_mod,
            "_load_active_verify_block",
            lambda scope_id, state_arg, _b=block, **kwargs: _b,
        )
        result = readiness_mod.compute(
            WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path
        )
        rendered[pid] = result.model_dump(mode="json")

    # Shape pin: the top-level CloseReadiness keys are identical.
    keys_per_pid = {pid: sorted(payload.keys()) for pid, payload in rendered.items()}
    assert keys_per_pid["python"] == keys_per_pid["apps"] == keys_per_pid["robotics"]

    # Shape pin: each criterion view in each pack has the same set of
    # field names.
    def _view_field_set(payload: dict) -> set[str]:
        keys: set[str] = set()
        for view in payload["criteria"]:
            keys.update(view.keys())
        return keys

    apps_keys = _view_field_set(rendered["apps"])
    python_keys = _view_field_set(rendered["python"])
    robotics_keys = _view_field_set(rendered["robotics"])
    assert apps_keys == python_keys == robotics_keys

    # Value pin: the criteria counts differ across profiles (the floor
    # packs are distinct).
    assert len(rendered["python"]["criteria"]) == 3
    assert len(rendered["apps"]["criteria"]) == 4
    assert len(rendered["robotics"]["criteria"]) == 1


# ---- W13: scope the floor pack to the wave's file_scopes ------------------


def test_mirror_src_to_test_dir_maps_src_scope_to_test_package() -> None:
    """A ``src/<pkg>/<sub>/<file>`` scope mirrors to its ``tests/<sub>/`` dir."""
    assert (
        readiness_mod._mirror_src_to_test_dir("src/eawf/workflow/verify/readiness.py")
        == "tests/workflow/verify/"
    )
    # A directory scope mirrors to the same test package.
    assert (
        readiness_mod._mirror_src_to_test_dir("src/eawf/workflow/verify/")
        == "tests/workflow/verify/"
    )
    # A top-level module (no dir below the package) has no test package to map.
    assert readiness_mod._mirror_src_to_test_dir("src/eawf/foo.py") is None
    # A non-src scope is not mappable.
    assert readiness_mod._mirror_src_to_test_dir("docs/guide.md") is None


def test_scope_floor_argv_per_tool_narrowing(tmp_path: Path) -> None:
    """``_scope_floor_argv`` narrows each supported tool to the scope set."""
    scope_files = ["tests/workflow/verify/"]
    # pytest -> append the declared test targets.
    assert readiness_mod._scope_floor_argv(
        ["uv", "run", "pytest", "-q"],
        scope="touched",
        file_scopes=scope_files,
        runner_cwd=tmp_path,
    ) == ["uv", "run", "pytest", "-q", "tests/workflow/verify"]
    # pre-commit -> drop --all-files, pass --files over the scope set.
    assert readiness_mod._scope_floor_argv(
        ["uv", "run", "pre-commit", "run", "--all-files"],
        scope="touched",
        file_scopes=["a.py", "b.py"],
        runner_cwd=tmp_path,
    ) == ["uv", "run", "pre-commit", "run", "--files", "a.py", "b.py"]
    # mypy -> replace the whole-tree positional with the src scopes.
    assert readiness_mod._scope_floor_argv(
        ["uv", "run", "mypy", "src/"],
        scope="touched",
        file_scopes=["src/eawf/workflow/verify/readiness.py"],
        runner_cwd=tmp_path,
    ) == ["uv", "run", "mypy", "src/eawf/workflow/verify/readiness.py"]
    # generic file-consuming tool -> append the scope set as positionals.
    assert readiness_mod._scope_floor_argv(
        ["git", "diff", "--exit-code"],
        scope="touched",
        file_scopes=["pkg/mod.py"],
        runner_cwd=tmp_path,
    ) == ["git", "diff", "--exit-code", "pkg/mod.py"]


def test_scope_floor_argv_whole_tree_escapes(tmp_path: Path) -> None:
    """``scope: all``, empty scopes, and unmappable scopes keep the whole tree."""
    whole_tree = ["uv", "run", "pytest", "-q"]
    # scope: all keeps the schema / migration whole-tree gauntlet intact.
    assert (
        readiness_mod._scope_floor_argv(
            whole_tree, scope="all", file_scopes=["tests/x/"], runner_cwd=tmp_path
        )
        == whole_tree
    )
    # No declared file_scopes -> nothing to narrow to.
    assert (
        readiness_mod._scope_floor_argv(
            whole_tree, scope="touched", file_scopes=[], runner_cwd=tmp_path
        )
        == whole_tree
    )
    # mypy scopes that cross out of src -> the documented whole-tree escape.
    assert readiness_mod._scope_floor_argv(
        ["uv", "run", "mypy", "src/"],
        scope="touched",
        file_scopes=["docs/guide.md"],
        runner_cwd=tmp_path,
    ) == ["uv", "run", "mypy", "src/"]
    # pytest scope with no mappable / existing test package -> whole-tree escape.
    assert (
        readiness_mod._scope_floor_argv(
            whole_tree,
            scope="touched",
            file_scopes=["src/eawf/nowhere/absent.py"],
            runner_cwd=tmp_path,
        )
        == whole_tree
    )


def test_floor_required_only_blocks_on_block_policy() -> None:
    """A floor row gates the close only when both required AND policy=block."""
    block_row = FloorCheck(
        name="f", cmd=["git", "status"], scope="all", cadence="every-wave", policy="block"
    )
    warn_row = FloorCheck(
        name="f", cmd=["git", "status"], scope="all", cadence="every-wave", policy="warn"
    )
    not_required_block = FloorCheck(
        name="f",
        cmd=["git", "status"],
        scope="all",
        cadence="every-wave",
        policy="block",
        required=False,
    )
    assert readiness_mod._floor_required(block_row) is True
    assert readiness_mod._floor_required(warn_row) is False
    assert readiness_mod._floor_required(not_required_block) is False
    # An unmatched row defaults to blocking so a spec never silently down-grades.
    assert readiness_mod._floor_required(None) is True


def test_scoped_pytest_floor_carries_scoped_targets_not_whole_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-01: a scope:touched pytest floor carries the wave's test targets.

    A module-scoped close must NOT invoke the whole-tree ``pytest -q``; the
    invocation carries only the scoped target set. The wave id + file_scopes
    are also threaded onto the spec so the runner's diff-base + touched union
    stay wave-anchored.
    """
    state = _empty_state()
    _seed_wave(state, file_scopes=["tests/workflow/verify/"])
    store_dir = _store_dir(tmp_path / "state.json")

    captured: list[CheckSpec] = []

    def _fake_run(specs: list[CheckSpec], *, cwd: Path) -> list[CheckResult]:
        captured.extend(specs)
        return [CheckResult(name=s.name, kind=s.kind, passed=True) for s in specs]

    monkeypatch.setattr(readiness_mod, "run_checks", _fake_run)

    block = VerifyBlock(
        argv_allowlist=[],
        floor_checks=[
            FloorCheck(
                name="pytest",
                cmd=["uv", "run", "pytest", "-q"],
                scope="touched",
                cadence="every-wave",
                policy="warn",
            )
        ],
    )
    monkeypatch.setattr(
        readiness_mod,
        "_load_active_verify_block",
        lambda scope_id, state_arg, **kwargs: block,
    )

    readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    pytest_spec = next(s for s in captured if s.name == "pytest")
    assert pytest_spec.args["argv"] == ["uv", "run", "pytest", "-q", "tests/workflow/verify"]
    assert pytest_spec.args["argv"] != ["uv", "run", "pytest", "-q"]  # not the whole tree
    assert pytest_spec.args["wave_id"] == WAVE_ID
    assert pytest_spec.args["wave_file_scopes"] == ["tests/workflow/verify/"]


def test_scoped_floor_ignores_out_of_scope_break(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-01: a scoped floor passes even when an OUT-of-scope file is broken.

    Proof the narrowing is real, not cosmetic: the repo carries a broken file
    a whole-tree ``git diff --exit-code`` would fail on, but the floor scoped
    to the clean in-scope file exits zero.
    """
    state = _empty_state()
    _init_test_repo(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "in_scope.py").write_text("clean = 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "out_scope.py").write_text("clean = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "pkg")
    # Break the OUT-of-scope file (uncommitted diff).
    (tmp_path / "pkg" / "out_scope.py").write_text("still broken\n", encoding="utf-8")

    _seed_wave(state, file_scopes=["pkg/in_scope.py"])
    store_dir = _store_dir(tmp_path / "state.json")

    block = VerifyBlock(
        argv_allowlist=[],
        floor_checks=[
            FloorCheck(
                name="git-diff",
                cmd=["git", "diff", "--exit-code"],
                scope="touched",
                cadence="every-wave",
                policy="warn",
            )
        ],
    )
    monkeypatch.setattr(
        readiness_mod,
        "_load_active_verify_block",
        lambda scope_id, state_arg, **kwargs: block,
    )

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    view = next(v for v in result.criteria if v.id == "git-diff")
    assert view.status == "pass"  # scoped to the clean in-scope file


def test_broken_file_in_scope_still_fails_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-02: a break INSIDE file_scopes still fails the floor, warn stays non-blocking.

    The falsifier survives scoping (an in-scope break is still detected) AND a
    ``policy: warn`` floor row does not flip ``ready`` -- the two halves of the
    W13 close-floor contract.
    """
    state = _empty_state()
    _init_test_repo(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("clean = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "pkg")
    # Break the IN-scope file (uncommitted diff).
    (tmp_path / "pkg" / "mod.py").write_text("still broken\n", encoding="utf-8")

    _seed_wave(state, file_scopes=["pkg/mod.py"])
    store_dir = _store_dir(tmp_path / "state.json")

    block = VerifyBlock(
        argv_allowlist=[],
        floor_checks=[
            FloorCheck(
                name="git-diff",
                cmd=["git", "diff", "--exit-code"],
                scope="touched",
                cadence="every-wave",
                policy="warn",
            )
        ],
    )
    monkeypatch.setattr(
        readiness_mod,
        "_load_active_verify_block",
        lambda scope_id, state_arg, **kwargs: block,
    )

    result = readiness_mod.compute(WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path)

    view = next(v for v in result.criteria if v.id == "git-diff")
    assert view.status == "fail"  # the falsifier survives scoping
    assert view.required is False  # policy: warn -> non-blocking
    assert result.ready is True  # a warn fail does not gate the close


def test_warn_floor_non_blocking_but_block_floor_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-02: policy governs gating -- a failing warn floor keeps ready True; block flips it."""
    state = _empty_state()
    _seed_wave(state)
    _init_test_repo(tmp_path)
    store_dir = _store_dir(tmp_path / "state.json")

    warn_block = VerifyBlock(
        argv_allowlist=[],
        floor_checks=[_failing_floor_check("c-warn", policy="warn")],
    )
    monkeypatch.setattr(
        readiness_mod,
        "_load_active_verify_block",
        lambda scope_id, state_arg, **kwargs: warn_block,
    )
    warn_result = readiness_mod.compute(
        WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path
    )
    warn_view = next(v for v in warn_result.criteria if v.id == "c-warn")
    assert warn_view.status == "fail"
    assert warn_view.required is False
    assert warn_result.ready is True  # warn fail is non-blocking

    block_block = VerifyBlock(
        argv_allowlist=[],
        floor_checks=[_failing_floor_check("c-block", policy="block")],
    )
    monkeypatch.setattr(
        readiness_mod,
        "_load_active_verify_block",
        lambda scope_id, state_arg, **kwargs: block_block,
    )
    block_result = readiness_mod.compute(
        WAVE_ID, state=state, store_dir=store_dir, repo_root=tmp_path
    )
    block_view = next(v for v in block_result.criteria if v.id == "c-block")
    assert block_view.status == "fail"
    assert block_view.required is True
    assert block_result.ready is False  # block fail gates the close
