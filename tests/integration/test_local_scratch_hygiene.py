"""Heavy spike artefacts stay out of ``.ea/local`` and what lands there is reclaimable.

A spike that installs a virtual environment or downloads a model into
``.ea/local/research/`` grows the machine-local store without bound, since
nothing ever reads it back. Four claims are pinned here: the rendered spike
rule sends heavy artefacts to the configured scratch root, doctor warns once
``.ea/local`` outgrows ``local.size_warn_mb``, ``eawf local gc`` removes only
entries whose ``.retention`` date has passed, and ``worktrees.remove_when_clean``
decides whether a released clean lease keeps its worktree.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from eawf.kernel.config.registry.leaf_catalog import DEPRECATED_LEAF_KEYS, LEAF_KEY_REGISTRY
from eawf.platform.install.instrument_probe import PROBE_VERSION, ProbeReport, ProbeResult
from eawf.surfaces.cli.app import app

runner = CliRunner()

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPIKE_PAGE = _REPO_ROOT / "docs" / "rules" / "spike-workflow.md"
#: An absolute or home-relative path: a POSIX root path, a ``~/`` home path or a drive letter.
_ABSOLUTE_PATH = re.compile(r"(?:^|[\s(`'\"])(?:/[\w.-]+/|~/|[A-Za-z]:[\\/])")
_MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def _sandbox_host_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the operator's global config and live daemons out of every test."""
    from eawf.runtime.daemon.service_install import SupervisedAgentReport

    report = SupervisedAgentReport(
        supervisor="none",
        label="",
        installed=False,
        loaded=False,
        program=None,
        drift=False,
        rival_pid=None,
    )
    monkeypatch.setattr(
        "eawf.runtime.daemon.service_install.detect_supervised_agent",
        lambda *_a, **_k: report,
    )
    monkeypatch.setattr(
        "eawf.kernel.config.layered.global_config_path",
        lambda: tmp_path / "global-config.yaml",
    )
    monkeypatch.setattr(
        "eawf.observability.doctor.checks._probe_running_daemon_version", lambda: None
    )
    monkeypatch.setattr("eawf.observability.doctor.daemon_strays.find_stray_daemons", lambda: [])


def _write_layer(path: Path, body: dict[str, Any]) -> None:
    """Merge *body* into the config layer file at *path*, creating it when absent."""
    current = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    merged: dict[str, Any] = current if isinstance(current, dict) else {}
    for section, leaves in body.items():
        merged.setdefault(section, {}).update(leaves)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(merged), encoding="utf-8")


# ---- CR-0483-1: the rendered spike rule -------------------------------------


def test_spike_rule_sends_heavy_artefacts_to_scratch() -> None:
    page = _SPIKE_PAGE.read_text(encoding="utf-8").replace("``", "`")
    assert re.search(r"`\.ea/local/research/` holds only text evidence", page), page
    assert "`local.scratch_root`" in page
    assert "`.retention`" in page
    assert "`eawf local gc`" in page
    assert _ABSOLUTE_PATH.findall(page) == []


@pytest.mark.parametrize(
    ("text", "hits"),
    [
        ("", 0),
        ("keep it under `.ea/local/research/`", 0),
        ("run /plan propose", 0),
        ("put it in /srv/scratch/models", 1),
        ("put it in ~/scratch", 1),
        ("put it in `C:\\scratch`", 1),
    ],
)
def test_the_absolute_path_probe_reds_on_a_machine_path(text: str, hits: int) -> None:
    assert len(_ABSOLUTE_PATH.findall(text)) == hits


# ---- CR-0483-2: doctor's local_size check -----------------------------------


def _green_probe(*_args: object, **_kwargs: object) -> ProbeReport:
    return ProbeReport(
        probe_version=PROBE_VERSION,
        profile_ids=["core"],
        results=[
            ProbeResult(name="git", kind="hard", status="ok", path="git"),
            ProbeResult(name="python", kind="hard", status="ok", path="python"),
            ProbeResult(name="uv", kind="hard", status="ok", path="uv"),
        ],
    )


def _doctor_local_size(repo: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run ``eawf --json doctor`` on *repo* and return its ``local_size`` check."""
    monkeypatch.setattr("eawf.observability.doctor.checks.probe", _green_probe)
    monkeypatch.chdir(repo)
    result = runner.invoke(app, ["--json", "-w", str(repo), "doctor"])
    checks: list[dict[str, Any]] = json.loads(result.stdout)["checks"]
    (check,) = [c for c in checks if c["name"] == "local_size"]
    return check


def _sparse_file(path: Path, size: int) -> None:
    """Write a file of apparent *size* bytes without spending the disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(size)


def test_doctor_warns_on_a_large_local_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    _write_layer(repo / ".ea" / "config.yaml", {"local": {"size_warn_mb": 2}})
    _sparse_file(repo / ".ea" / "local" / "research" / "venv" / "lib.so", 3 * _MIB)

    over = _doctor_local_size(repo, monkeypatch)
    assert over["status"] == "warn"
    assert "3.0 MB" in over["detail"]
    assert "local.scratch_root" in over["detail"]

    _write_layer(repo / ".ea" / "config.yaml", {"local": {"size_warn_mb": 4}})
    under = _doctor_local_size(repo, monkeypatch)
    assert under["status"] == "ok"
    assert "3.0 MB" in under["detail"]


def test_local_size_is_ok_without_a_local_dir(tmp_path: Path) -> None:
    from eawf.observability.doctor.checks_local_size import check_local_size

    (tmp_path / ".ea").mkdir()
    result = check_local_size(workspace=tmp_path)
    assert (result.status, result.detail) == ("ok", ".ea/local is 0.0 MB (warns at 1024 MB)")


def test_local_size_is_ok_without_an_anchor() -> None:
    from eawf.observability.doctor.checks_local_size import check_local_size

    assert check_local_size(workspace=None).status == "ok"


def test_local_size_warns_at_exactly_the_threshold(tmp_path: Path) -> None:
    from eawf.observability.doctor.checks_local_size import check_local_size

    _write_layer(tmp_path / ".ea" / "config.yaml", {"local": {"size_warn_mb": 1}})
    _sparse_file(tmp_path / ".ea" / "local" / "blob", _MIB - 1)
    assert check_local_size(workspace=tmp_path).status == "ok"
    _sparse_file(tmp_path / ".ea" / "local" / "blob", _MIB)
    assert check_local_size(workspace=tmp_path).status == "warn"


@pytest.mark.parametrize("raw", ["big", 0, -1, True])
def test_an_unusable_size_threshold_is_a_warning(tmp_path: Path, raw: object) -> None:
    from eawf.observability.doctor.checks_local_size import check_local_size

    _write_layer(tmp_path / ".ea" / "config.yaml", {"local": {"size_warn_mb": raw}})
    result = check_local_size(workspace=tmp_path)
    assert result.status == "warn"
    assert "local.size_warn_mb" in (result.detail or "")


def test_a_malformed_config_layer_is_a_warning(tmp_path: Path) -> None:
    from eawf.observability.doctor.checks_local_size import check_local_size

    layer = tmp_path / ".ea" / "config.yaml"
    layer.parent.mkdir(parents=True)
    layer.write_text("verify: [unclosed\n", encoding="utf-8")
    result = check_local_size(workspace=tmp_path)
    assert result.status == "warn"
    assert "config merge failed" in (result.detail or "")


# ---- CR-0483-3: eawf local gc -----------------------------------------------


def _seed_local(repo: Path) -> Path:
    """Seed ``.ea/local`` with briefs and retained entries; return the research dir."""
    research = repo / ".ea" / "local" / "research"
    research.mkdir(parents=True)
    (research / "2026-10-01-tts.md").write_text("# brief\n", encoding="utf-8")
    (research / "2026-10-02-models.md").write_text("# brief\n", encoding="utf-8")
    for name, expiry in (("tts-venv", "2000-01-01"), ("model-cache", "2999-12-31")):
        (research / name / "bin").mkdir(parents=True)
        (research / name / "bin" / "python").write_text("x", encoding="utf-8")
        (research / name / ".retention").write_text(f"{expiry}\n", encoding="utf-8")
    (research / "unmarked").mkdir()
    (research / "unmarked" / "data.bin").write_text("x", encoding="utf-8")
    return research


def _gc(repo: Path, *flags: str) -> dict[str, Any]:
    result = runner.invoke(app, ["--json", "-w", str(repo), "local", "gc", *flags])
    assert result.exit_code == 0, result.output
    body: dict[str, Any] = json.loads(result.stdout)
    return body


def test_local_gc_removes_only_expired_entries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    research = _seed_local(repo)
    before = sorted(str(p.relative_to(repo)) for p in repo.rglob("*"))

    preview = _gc(repo, "--dry-run")
    assert preview["dry_run"] is True
    assert preview["expired"] == [".ea/local/research/tts-venv"]
    assert sorted(str(p.relative_to(repo)) for p in repo.rglob("*")) == before

    applied = _gc(repo)
    assert applied["dry_run"] is False
    assert applied["expired"] == [".ea/local/research/tts-venv"]
    assert not (research / "tts-venv").exists()
    assert (research / "model-cache" / "bin" / "python").is_file()
    assert (research / "unmarked" / "data.bin").is_file()
    assert sorted(p.name for p in research.glob("*.md")) == [
        "2026-10-01-tts.md",
        "2026-10-02-models.md",
    ]


def test_local_gc_on_a_tree_without_a_local_dir_removes_nothing(tmp_path: Path) -> None:
    assert _gc(tmp_path)["expired"] == []


def test_an_entry_expiring_today_is_kept(tmp_path: Path) -> None:
    from eawf.platform.local_gc import plan_local_gc

    entry = tmp_path / "local" / "probe"
    entry.mkdir(parents=True)
    (entry / ".retention").write_text("2026-10-23", encoding="utf-8")
    assert plan_local_gc(tmp_path / "local", today=date(2026, 10, 23)).expired == ()
    assert plan_local_gc(tmp_path / "local", today=date(2026, 10, 24)).expired == (entry,)


@pytest.mark.parametrize("body", ["", "soon", "2026-13-01", "2000-01-01 extra"])
def test_an_unreadable_retention_never_deletes(tmp_path: Path, body: str) -> None:
    from eawf.platform.local_gc import apply_local_gc, plan_local_gc

    entry = tmp_path / "local" / "probe"
    entry.mkdir(parents=True)
    (entry / ".retention").write_text(body, encoding="utf-8")
    plan = plan_local_gc(tmp_path / "local", today=date(2026, 10, 23))
    assert (plan.expired, plan.unreadable) == ((), (entry,))
    apply_local_gc(plan)
    assert entry.is_dir()


def test_a_retention_on_the_local_root_itself_is_ignored(tmp_path: Path) -> None:
    from eawf.platform.local_gc import plan_local_gc

    local = tmp_path / "local"
    local.mkdir()
    (local / ".retention").write_text("2000-01-01", encoding="utf-8")
    assert plan_local_gc(local, today=date(2026, 10, 23)).expired == ()


def test_a_nested_expired_entry_is_removed_with_its_parent(tmp_path: Path) -> None:
    from eawf.platform.local_gc import plan_local_gc

    outer = tmp_path / "local" / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    for entry in (outer, inner):
        (entry / ".retention").write_text("2000-01-01", encoding="utf-8")
    assert plan_local_gc(tmp_path / "local", today=date(2026, 10, 23)).expired == (outer,)


def test_local_gc_never_follows_a_link_out_of_the_local_dir(tmp_path: Path) -> None:
    from eawf.platform.local_gc import apply_local_gc, plan_local_gc

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".retention").write_text("2000-01-01", encoding="utf-8")
    local = tmp_path / "local"
    local.mkdir()
    (local / "link").symlink_to(outside, target_is_directory=True)
    plan = plan_local_gc(local, today=date(2026, 10, 23))
    assert plan.expired == ()
    apply_local_gc(plan)
    assert (outside / ".retention").is_file()


def test_local_gc_refuses_a_planned_entry_outside_the_local_dir(tmp_path: Path) -> None:
    from eawf.platform.local_gc import LocalGcPlan, apply_local_gc

    stray = tmp_path / "stray"
    stray.mkdir()
    with pytest.raises(ValueError, match="outside"):
        apply_local_gc(LocalGcPlan(local_root=tmp_path / "local", expired=(stray,)))
    assert stray.is_dir()


# ---- CR-0483-4: worktrees.remove_when_clean ---------------------------------


@pytest.mark.parametrize(("remove", "kept"), [(None, False), (True, False), (False, True)])
def test_remove_when_clean_governs_lease_cleanup(
    tmp_path: Path, remove: bool | None, kept: bool
) -> None:
    from eawf.kernel.runtime.lease import LeaseStatus
    from eawf.runtime.workspace.lease import read_lease, reconcile_root, workspace_path
    from tests.integration.runtime.daemon.test_worklease_recovery import (
        JUDGED,
        build_canary,
        lease_the_repo,
    )

    assert "worktrees.remove_when_clean" in LEAF_KEY_REGISTRY
    assert "worktrees.remove_when_clean" not in DEPRECATED_LEAF_KEYS
    assert LEAF_KEY_REGISTRY["worktrees.remove_when_clean"].default is True
    provisioned, context = build_canary(tmp_path / "repo", code="LSH")
    if remove is not None:
        _write_layer(
            provisioned.root / ".ea" / "config.yaml", {"worktrees": {"remove_when_clean": remove}}
        )
    lease_id = lease_the_repo(context)
    issued = read_lease(context, lease_id=lease_id)
    assert issued is not None
    target = workspace_path(context, handle=issued.workspace_handle)

    report = reconcile_root(context, now=JUDGED)

    released = read_lease(context, lease_id=lease_id)
    assert report.released == 1
    assert released is not None
    assert released.status is LeaseStatus.RELEASED
    assert target.is_dir() is kept
