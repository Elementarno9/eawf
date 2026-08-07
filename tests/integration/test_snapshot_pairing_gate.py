"""Unit tests for the CI snapshot-pairing gate (``tools/snapshot_pairing_gate.py``).

The gate enforces the C09 §5.6 contract: every commit in a single-iter PR range
that *mutates* (status ``M`` / ``D`` / ``R``) a managed golden surface MUST carry
a wave-form ``test:`` subject, so a golden change can never sneak in under an
unrelated ``feat:`` / ``fix:`` commit. Phase PRs (ranges spanning more than one
iter) defer to wholesale diff review and exit ``0``.

Coverage:

- the pure subject-grammar helpers ``is_paired`` / ``iter_key`` /
  ``range_spans_multiple_iters`` over their boundary + reject cases;
- ``_is_managed_golden`` matches the C09 §5.6 watch set and rejects siblings;
- the single-pass perf contract: ``main`` fires exactly *one* git subprocess
  per gate run (a spy on ``subprocess.run`` asserts the call count is ``1``);
- ``_parse_log`` decodes the ``git log --name-status -z`` stream, matching
  rename (``R``) / copy (``C``) records on their *destination* path;
- the NEGATIVE CONTROL the wave's criterion names: a real ephemeral git repo
  whose single-iter range carries an *unpaired* golden mutation (a ``feat:``
  subject that rewrites committed golden bytes) reds the gate -- ``find_unpaired``
  cites it and ``main`` returns exit ``1``;
- the positive control: the same mutation under a wave-form ``test:`` subject is
  paired and the gate passes;
- a rename of a golden within the managed dir is caught on its destination path;
- a pure *addition* of a golden (status ``A``) is exempt -- a new surface ships
  its fixtures with the ``feat:`` wave that introduces it;
- the phase-PR escape hatch: a range spanning multiple iters surfaces the bundled
  golden commits for review and exits ``0`` even when a commit is unpaired;
- the no-base/no-head push-build path no-ops at exit ``0``.

``tools/`` is excluded from the package, so the gate is loaded via
:mod:`importlib`. The single ``git log`` pass shells out to bare ``git`` against
the cwd, so the git-fixture tests ``chdir`` into the ephemeral fixture repo.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE_PATH = _REPO_ROOT / "tools" / "snapshot_pairing_gate.py"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git is required for snapshot pairing gate tests",
)


def _load_gate() -> ModuleType:
    """Load ``tools/snapshot_pairing_gate.py`` by path (``tools/`` is not a package)."""
    tool_dir = _GATE_PATH.parent
    if str(tool_dir) not in sys.path:
        sys.path.insert(0, str(tool_dir))
    spec = importlib.util.spec_from_file_location("snapshot_pairing_gate", _GATE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["snapshot_pairing_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


_GATE = _load_gate()

#: A managed golden directory from the C09 §5.6 watch set, used to seed fixtures.
_GOLDEN_DIR = _GATE._WATCHED_DIRS[0]


def _git(repo: Path, *args: str) -> str:
    """Run ``git <args>`` inside *repo* and return stripped stdout."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init_repo(workdir: Path) -> Path:
    """Initialise a git repo on ``main`` with one committed golden file."""
    workdir.mkdir(parents=True, exist_ok=True)
    _git(workdir, "init", "-q", "-b", "main")
    _git(workdir, "config", "user.email", "ci@example.com")
    _git(workdir, "config", "user.name", "ci")
    # Pin rename detection ON so ``git log --name-status`` emits ``R`` records
    # deterministically across git versions (the production default, but made
    # explicit here so the rename-parsing test cannot flake on a stray config).
    _git(workdir, "config", "diff.renames", "true")
    golden = workdir / _GOLDEN_DIR / "screen.txt"
    golden.parent.mkdir(parents=True, exist_ok=True)
    golden.write_text("original golden bytes\n", encoding="utf-8")
    _git(workdir, "add", ".")
    _git(workdir, "commit", "-q", "-m", "[P30-I01-W01] feat: seed golden surface")
    return workdir


def _commit_golden_mutation(repo: Path, *, subject: str) -> str:
    """Rewrite the committed golden in *repo* and commit it under *subject*.

    The new bytes embed *subject* (which every caller keeps unique) so
    back-to-back mutations always produce a diff -- a no-op rewrite would
    make ``git commit`` fail with "nothing to commit".
    """
    (repo / _GOLDEN_DIR / "screen.txt").write_text(f"rewritten: {subject}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


# --- pure subject-grammar helpers -------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "[P30-I15-W06] test: snapshot update tui",
        "[P27-W19] test: snapshot update agents_md",  # pre-I02 bare-phase form
        "[P100-I100-W100] test: 3-digit ids parse",  # widened-grammar boundary
        "test: snapshot update agent_report",  # out-of-phase bare conventional form
    ],
)
def test_is_paired_accepts_wave_form_test_subjects(subject: str) -> None:
    assert _GATE.is_paired(subject) is True


@pytest.mark.parametrize(
    "subject",
    [
        "[P30-I15-W06] feat: not a test subject",  # wrong type
        "[P30-I15] test: missing wave suffix",  # no -W##
        "[P30-I02-CORE] test: regen goldens",  # retired -CORE alias
        "[P30-W00] test: zero wave index rejected",  # 1-based reject
        "[P30-I00-W06] test: zero iter index rejected",  # 1-based reject
        "feat: bare conventional wrong type",
        "test:",  # missing summary
    ],
)
def test_is_paired_rejects_non_wave_form_subjects(subject: str) -> None:
    assert _GATE.is_paired(subject) is False


def test_iter_key_extracts_phase_or_iter_scope() -> None:
    assert _GATE.iter_key("[P30-I15-W06] test: x") == "P30-I15"
    assert _GATE.iter_key("[P27-W19] feat: x") == "P27"  # pre-I02 bare-phase form
    assert _GATE.iter_key("no scope tag at all") is None


def test_range_spans_multiple_iters_is_false_for_single_iter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "single")
    _commit_golden_mutation(repo, subject="[P30-I15-W06] test: a")
    _commit_golden_mutation(repo, subject="[P30-I15-W07] test: b")
    base = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    # Drive the helper from the fixture repo's cwd (gate shells out to bare git).
    monkeypatch.chdir(repo)
    records = _GATE.scan_range(base, "HEAD")
    assert _GATE.range_spans_multiple_iters(records) is False


def test_is_managed_golden_matches_watch_set_and_rejects_siblings() -> None:
    assert _GATE._is_managed_golden(f"{_GOLDEN_DIR}screen.txt") is True
    # A sibling-prefix path outside the watched dir must not match.
    sibling = _GOLDEN_DIR.rstrip("/") + "_other/screen.txt"
    assert _GATE._is_managed_golden(sibling) is False
    # The unmanaged CLI help-panel tree is deliberately out of scope.
    assert _GATE._is_managed_golden("tests/golden/cli/help.txt") is False


# --- single-pass perf contract + stream parsing -----------------------------------


def test_main_invokes_exactly_one_git_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PERF CONTRACT (the criterion): the whole gate run reaches git exactly once.
    # The former implementation fanned out to `rev-list` + a per-commit `diff-tree`
    # + `log`, which is ~30s over a phase-sized range; the single `git log
    # --name-status -z` pass collapses that to one subprocess.
    repo = _init_repo(tmp_path / "onecall")
    base = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    _commit_golden_mutation(repo, subject="[P30-I15-W06] test: snapshot update state")
    monkeypatch.chdir(repo)

    # Spy is installed *after* fixture setup so only `main`'s git calls count.
    calls: list[list[str]] = []
    real_run = _GATE.subprocess.run

    def counting_run(cmd: list[str], *args: object, **kwargs: object) -> object:
        calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(_GATE.subprocess, "run", counting_run)
    rc = _GATE.main(["snapshot_pairing_gate.py", base, "HEAD"])

    assert rc == 0
    assert len(calls) == 1
    # ...and the single call is the name-status log pass, not a per-commit probe.
    (only_call,) = calls
    assert only_call[0] == "git"
    assert "log" in only_call
    assert "--name-status" in only_call
    assert "-z" in only_call


def test_parse_log_matches_rename_and_copy_on_destination_path() -> None:
    # `git log --name-status -z` renders a modify as `<status>\0<path>`, but a
    # rename/copy as the three-token `<status>\0<old-path>\0<new-path>`. The
    # parser must record the DESTINATION (new) path for R/C so the gate matches
    # a golden by where the bytes landed, not where they came from. The leading
    # `\n` on the first status is git's header/diff separator under `-z`.
    sha = "a" * 40
    raw = (
        f"COMMIT\x00{sha}\x00[P30-I15-W06] test: x\x00"
        f"\nM\x00{_GOLDEN_DIR}one.txt\x00"
        f"R100\x00{_GOLDEN_DIR}old.txt\x00{_GOLDEN_DIR}new.txt\x00"
        f"C080\x00src/orig.py\x00{_GOLDEN_DIR}copied.txt\x00"
    )
    records = _GATE._parse_log(raw)

    assert len(records) == 1
    record = records[0]
    assert record.sha == sha
    assert record.subject == "[P30-I15-W06] test: x"
    # Destination paths, never the sources, are recorded for R/C.
    assert ("M", f"{_GOLDEN_DIR}one.txt") in record.changed
    assert ("R", f"{_GOLDEN_DIR}new.txt") in record.changed
    assert ("C", f"{_GOLDEN_DIR}copied.txt") in record.changed
    assert (f"{_GOLDEN_DIR}old.txt") not in [path for _, path in record.changed]


def test_parse_log_returns_empty_for_empty_stream() -> None:
    # Boundary: an empty range (`git log` over base..base) yields no records.
    assert _GATE._parse_log("") == []


def test_golden_rename_within_managed_dir_is_caught_on_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A rename of a committed golden lands as an `R` record whose destination is
    # still under the managed dir, so it is a mutation the gate must catch. Under
    # a `feat:` subject the single-iter range must red.
    repo = _init_repo(tmp_path / "rename")
    base = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    _git(repo, "mv", f"{_GOLDEN_DIR}screen.txt", f"{_GOLDEN_DIR}renamed.txt")
    _git(repo, "commit", "-q", "-m", "[P30-I15-W06] feat: rename a golden fixture")
    monkeypatch.chdir(repo)

    records = _GATE.scan_range(base, "HEAD")
    assert len(records) == 1
    codes = {code for code, _ in records[0].changed}
    assert "R" in codes
    rename_paths = [path for code, path in records[0].changed if code == "R"]
    assert rename_paths == [f"{_GOLDEN_DIR}renamed.txt"]  # destination, not source
    assert _GATE.commit_mutates_golden(records[0]) is True

    rc = _GATE.main(["snapshot_pairing_gate.py", base, "HEAD"])
    assert rc == 1


# --- the wave's named negative + positive controls (real git fixture) -------------


def test_unpaired_golden_mutation_reds_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # NEGATIVE CONTROL (the criterion): a single-iter range with a golden
    # mutation under a `feat:` subject must red -- find_unpaired cites it and
    # `main` returns exit 1.
    repo = _init_repo(tmp_path / "unpaired")
    base = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    bad_sha = _commit_golden_mutation(repo, subject="[P30-I15-W06] feat: sneak golden rewrite")
    monkeypatch.chdir(repo)

    offenders = _GATE.find_unpaired(base, "HEAD")
    assert len(offenders) == 1
    short_sha, subject = offenders[0]
    assert bad_sha.startswith(short_sha)
    assert "feat:" in subject

    rc = _GATE.main(["snapshot_pairing_gate.py", base, "HEAD"])
    assert rc == 1


def test_paired_golden_mutation_passes_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # POSITIVE CONTROL: the same mutation under a wave-form `test:` subject is
    # paired -- no offenders and `main` exits 0.
    repo = _init_repo(tmp_path / "paired")
    base = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    _commit_golden_mutation(repo, subject="[P30-I15-W06] test: snapshot update agent_report")
    monkeypatch.chdir(repo)

    assert _GATE.find_unpaired(base, "HEAD") == []
    rc = _GATE.main(["snapshot_pairing_gate.py", base, "HEAD"])
    assert rc == 0


def test_bare_paired_golden_mutation_passes_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Out-of-phase bare ``test:`` commits satisfy the pairing contract."""
    repo = _init_repo(tmp_path / "bare-paired")
    base = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    _commit_golden_mutation(repo, subject="test: snapshot update agent_report")
    monkeypatch.chdir(repo)

    assert _GATE.find_unpaired(base, "HEAD") == []
    assert _GATE.main(["snapshot_pairing_gate.py", base, "HEAD"]) == 0


def test_pure_golden_addition_is_exempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A brand-new golden (status A) under a `feat:` subject is exempt: a new
    # surface ships its fixtures with the wave that introduces it.
    repo = _init_repo(tmp_path / "addition")
    base = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    new_golden = repo / _GOLDEN_DIR / "brand_new_screen.txt"
    new_golden.write_text("fresh\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "[P30-I15-W06] feat: add a new golden surface")
    monkeypatch.chdir(repo)

    assert _GATE.find_unpaired(base, "HEAD") == []
    rc = _GATE.main(["snapshot_pairing_gate.py", base, "HEAD"])
    assert rc == 0


def test_phase_pr_multi_iter_range_surfaces_but_does_not_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Phase-PR escape hatch: a range spanning >1 iter surfaces the bundled
    # golden commits for review and exits 0 even with an unpaired mutation.
    repo = _init_repo(tmp_path / "phase_pr")
    base = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    _commit_golden_mutation(repo, subject="[P30-I14-W01] feat: golden rewrite in iter 14")
    _commit_golden_mutation(repo, subject="[P30-I15-W06] feat: golden rewrite in iter 15")
    monkeypatch.chdir(repo)

    # Both are unpaired by the per-commit grammar...
    assert len(_GATE.find_unpaired(base, "HEAD")) == 2
    # ...but the multi-iter (phase-PR) range defers to wholesale review.
    rc = _GATE.main(["snapshot_pairing_gate.py", base, "HEAD"])
    assert rc == 0


def test_no_base_or_head_is_a_push_build_noop() -> None:
    # Push build (no PR context): the pairing contract is a PR-review gate, so
    # an empty base/head no-ops at exit 0.
    assert _GATE.main(["snapshot_pairing_gate.py", "", ""]) == 0


def test_main_usage_error_on_too_few_args() -> None:
    # Boundary: fewer than two positional SHAs prints usage and returns 1.
    assert _GATE.main(["snapshot_pairing_gate.py"]) == 1
