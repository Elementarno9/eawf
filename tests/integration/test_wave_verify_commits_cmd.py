"""Unit tests for ``eawf wave verify-commits [--repair]``.

Exercises the CLI verb end-to-end against an isolated ``state.json``
fixture under ``EA_STATE``. ``derive_wave_sha`` + ``shutil.which`` are
monkeypatched so the scan is deterministic without a real git repo
(mirroring ``tests/unit/test_drift_reconciler.py``).

Covered:

- clean repo -> exit 0, "0 drift".
- each repairable / unrepairable kind is detected + classified.
- drift without ``--repair`` exits ``VALIDATION_ERROR`` (2) AND still
  prints the per-wave report (so CI can read the detail + branch on the
  code).
- ``--repair`` re-pins a ``closed`` wave's mismatch and an unpinned
  derivable wave, persists through the canonical writer, prints a
  repaired/skipped summary, exits 0.
- an unrepairable kind is reported as skipped under ``--repair``.
- ``--repair`` is idempotent: the second run is clean.
- ``--md`` and ``--json`` render the report; ``--md --json`` is rejected.
- in a real clone of main plus tags, every pre-squash pin is classified
  as repairable, ambiguous or unresolvable.
- after ``--repair`` in that clone, acking only the ambiguous row by name
  leaves ``verify-commits`` exiting 0.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

W1 = "P28-I01-W01"
W2 = "P28-I01-W02"


def _seed_state(state_dir: Path, *, waves: dict[str, str | None]) -> Path:
    """Write a state.json whose closed waves carry the given commit pins.

    Args:
        state_dir: ``.ea`` directory to write ``state.json`` into.
        waves: ``{wave_id: commit_or_None}`` -- each entry becomes a
            CLOSED wave with that ``Wave.commit`` value.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    wave_rows = {
        wid: {
            "id": wid,
            "iter_id": "P28-I01",
            "title": f"wave {wid}",
            "status": "closed",
            "deps": [],
            "blocks": [],
            "file_scopes": [],
            "claim_session_id": None,
            "worktree_id": None,
            "token_budget": None,
            "tokens_consumed": 0,
            "outcome": "done",
            "commit": commit,
            "opened_at": "2026-05-27T00:00:00Z",
            "closed_at": "2026-05-27T00:01:00Z",
        }
        for wid, commit in waves.items()
    }
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scope_kind": "repo",
                "urn": "urn:eawf:v1:state:ZZ",
                "updated_at": "2026-05-27T00:00:00Z",
                "project": {
                    "code": "ZZ",
                    "slug": "zz",
                    "title": "ZZ",
                    "description": None,
                    "domains": [],
                    "default_branch": "main",
                    "status": "active",
                    "repo_urn": "urn:eawf:v1:repo:ZZ",
                },
                "current": {
                    "project_code": "ZZ",
                    "track_id": None,
                    "phase_id": "P28",
                    "iter_id": "P28-I01",
                    "active_wave_ids": [],
                    "active_session_ids": [],
                },
                "workspace": None,
                "phases": {
                    "P28": {
                        "id": "P28",
                        "scope_id": "ZZ",
                        "track_id": None,
                        "title": "bootstrap",
                        "status": "active",
                        "iter_ids": ["P28-I01"],
                        "outcome_ids": [],
                        "opened_at": "2026-05-27T00:00:00Z",
                        "closed_at": None,
                        "audit_id": None,
                    }
                },
                "iters": {
                    "P28-I01": {
                        "id": "P28-I01",
                        "phase_id": "P28",
                        "title": "first",
                        "status": "active",
                        "wave_ids": list(wave_rows),
                        "estimate_id": None,
                        "audit_id": None,
                        "opened_at": "2026-05-27T00:00:00Z",
                        "closed_at": None,
                    }
                },
                "waves": wave_rows,
                "artifacts": {},
                "agent_sessions": {},
                "plugins": {},
                "indexes": {},
            }
        ),
        encoding="utf-8",
    )
    return state_path


@pytest.fixture
def git_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: "/usr/bin/git")


def _patch_derive(monkeypatch: pytest.MonkeyPatch, table: dict[str, str | None]) -> None:
    reachable = {sha: {wave_id} for wave_id, sha in table.items() if sha is not None}
    candidates = {wave_id: [sha] for wave_id, sha in table.items() if sha is not None}
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha._reachable_wave_keys",
        lambda repo_root=None: reachable,
    )
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha._first_parent_wave_candidates",
        lambda repo_root=None: candidates,
    )
    monkeypatch.setattr(
        "eawf.workflow.lifecycle.wave_sha.commit_identity_digest",
        lambda commit, repo_root=None: f"sha256:{'0' * 64}",
    )


def _read_commit(state_path: Path, wave_id: str) -> str | None:
    payload = orjson.loads(state_path.read_bytes())
    return payload["waves"][wave_id]["commit"]


# ---- read-only (no --repair) -----------------------------------------------


def test_verify_commits_clean_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_available: None
) -> None:
    """All pins in sync -> exit 0, honest no-drift line."""
    state_path = _seed_state(tmp_path / ".ea", waves={W1: "a" * 40})
    monkeypatch.setenv("EA_STATE", str(state_path))
    _patch_derive(monkeypatch, {W1: "a" * 40})

    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["wave", "verify-commits"])
    assert res.exit_code == 0, res.output
    assert "0 drift" in res.output


def test_verify_commits_drift_exits_validation_error_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_available: None
) -> None:
    """A mismatch without --repair exits 2 AND prints the per-wave row."""
    from eawf.surfaces.cli import exit_codes

    state_path = _seed_state(tmp_path / ".ea", waves={W1: "a" * 40})
    monkeypatch.setenv("EA_STATE", str(state_path))
    _patch_derive(monkeypatch, {W1: "b" * 40})

    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["wave", "verify-commits"])
    assert res.exit_code == exit_codes.VALIDATION_ERROR, res.output
    # The full report is printed BEFORE the non-zero exit.
    assert W1 in res.output
    assert "pinned_mismatch" in res.output
    # Read-only: the on-disk pin is unchanged.
    assert _read_commit(state_path, W1) == "a" * 40


def test_verify_commits_acknowledged_residue_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_available: None
) -> None:
    """Acked unrepairable rows are accepted residue, so the gate exits clean."""
    (tmp_path / ".git").mkdir()
    _seed_state(tmp_path / ".ea", waves={W1: "a" * 40})
    ack_dir = tmp_path / ".eawf"
    ack_dir.mkdir()
    (ack_dir / "drift-acks.json").write_text(
        json.dumps({"acked_wave_ids": [W1]}),
        encoding="utf-8",
    )
    monkeypatch.delenv("EA_STATE", raising=False)
    _patch_derive(monkeypatch, {})  # pinned_but_missing, then filtered by ack

    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["--json", "-w", str(tmp_path), "wave", "verify-commits"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["drift_count"] == 0
    assert payload["drifts"] == []


def test_verify_commits_detects_every_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_available: None
) -> None:
    """All four hard kinds + the soft unpinned_derivable kind classify in --json."""
    state_path = _seed_state(
        tmp_path / ".ea",
        waves={
            "P28-I01-W01": "a" * 40,  # mismatch (derive -> b)
            "P28-I01-W02": "c" * 40,  # pinned_but_missing (derive -> None)
            "P28-I01-W03": None,  # closed_no_pin (derive -> None)
            "P28-I01-W04": None,  # unpinned_derivable (derive -> e)
        },
    )
    monkeypatch.setenv("EA_STATE", str(state_path))
    _patch_derive(
        monkeypatch,
        {"P28-I01-W01": "b" * 40, "P28-I01-W04": "e" * 40},
    )

    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["--json", "wave", "verify-commits"])
    assert res.exit_code != 0, res.output  # drift -> non-zero
    payload = json.loads(res.stdout)
    kinds = {row["wave"]: row["kind"] for row in payload["drifts"]}
    assert kinds == {
        "P28-I01-W01": "pinned_mismatch",
        "P28-I01-W02": "pinned_but_missing",
        "P28-I01-W03": "closed_no_pin",
        "P28-I01-W04": "unpinned_derivable",
    }
    repairable = {row["wave"]: row["repairable"] for row in payload["drifts"]}
    assert repairable == {
        "P28-I01-W01": True,
        "P28-I01-W02": False,
        "P28-I01-W03": False,
        "P28-I01-W04": True,
    }


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit_wave(root: Path, *, name: str, wave_id: str) -> str:
    (root / name).write_text(f"{name}\n", encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", f"feat: add {name}\n\nEawf-Wave: {wave_id}\n")
    return _git(root, "rev-parse", "HEAD")


def _main_plus_tags_clone(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Clone only ``main`` and its tags from an origin whose waves ran on side branches.

    Returns the clone and the origin-side SHAs the state pins: ``w01`` is a
    pre-cherry-pick commit whose copy landed on main, ``w06`` is the same with
    no stored identity digest, ``w02`` has two landed copies, ``w03`` never
    landed, and ``w05`` lives only on a release tag.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.name", "Test")
    _git(origin, "config", "user.email", "test@example.invalid")
    _git(origin, "config", "commit.gpgsign", "false")
    (origin / "root.txt").write_text("root\n", encoding="utf-8")
    _git(origin, "add", "root.txt")
    _git(origin, "commit", "-q", "-m", "chore: root")
    pins: dict[str, str] = {}
    for wave in ("W01", "W02", "W03", "W05", "W06"):
        _git(origin, "switch", "-q", "-c", f"feature/zz-v1.0-p28-{wave.lower()}", "main")
        pins[wave] = _commit_wave(origin, name=f"{wave}.txt", wave_id=f"P28-I01-{wave}")
    _git(origin, "tag", "v1.0.0", pins["W05"])
    _git(origin, "switch", "-q", "main")
    # W04 lands first so the cherry-pick gets a new parent and a new SHA.
    _commit_wave(origin, name="W04.txt", wave_id="P28-I01-W04")
    _git(origin, "cherry-pick", pins["W01"], pins["W06"])
    _commit_wave(origin, name="W02-first.txt", wave_id="P28-I01-W02")
    _commit_wave(origin, name="W02-second.txt", wave_id="P28-I01-W02")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--single-branch", "--branch", "main", origin.as_uri(), str(clone)],
        check=True,
    )
    _git(clone, "fetch", "-q", "--tags", "origin")
    return clone, pins


def test_verify_commits_classifies_every_mismatch_in_main_plus_tags_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-squash pins absent from the clone resolve as repairable, ambiguous or not."""
    from eawf.workflow.lifecycle.wave_sha import commit_identity_digest

    global_config = tmp_path / "gitconfig"
    global_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    clone, pins = _main_plus_tags_clone(tmp_path)
    w01_digest = commit_identity_digest(pins["W01"], repo_root=tmp_path / "origin")
    assert _git(clone, "cat-file", "-t", pins["W05"]) == "commit"
    for wave in ("W01", "W02", "W03", "W06"):
        assert (
            subprocess.run(
                ["git", "-C", str(clone), "cat-file", "-e", pins[wave]], capture_output=True
            ).returncode
            != 0
        )
    state_path = _seed_state(
        clone / ".ea",
        waves={
            "P28-I01-W01": pins["W01"],
            "P28-I01-W02": pins["W02"],
            "P28-I01-W03": pins["W03"],
            "P28-I01-W04": None,
            "P28-I01-W05": pins["W05"],
            "P28-I01-W06": pins["W06"],
        },
    )
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["waves"]["P28-I01-W01"]["commit_identity_digest"] = w01_digest
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("EA_STATE", str(state_path))

    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["--json", "wave", "verify-commits"])
    rows = {
        row["wave"]: (row["kind"], row["resolution"]) for row in json.loads(res.stdout)["drifts"]
    }
    assert rows == {
        "P28-I01-W01": ("pinned_mismatch", "repairable"),
        "P28-I01-W02": ("ambiguous_successor", "ambiguous"),
        "P28-I01-W03": ("pinned_but_missing", "unresolvable"),
        "P28-I01-W04": ("unpinned_derivable", "repairable"),
        "P28-I01-W06": ("pinned_mismatch", "repairable"),
    }
    text = CliRunner().invoke(app, ["wave", "verify-commits"])
    assert "5 drift(s) (3 repairable, 1 ambiguous, 1 unresolvable)" in text.output
    md = CliRunner().invoke(app, ["wave", "verify-commits", "--md"])
    assert "| resolution |" in md.output
    assert "| P28-I01-W02 | ambiguous_successor | " in md.output


def test_verify_commits_clean_after_repair_with_named_ambiguous_acks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repair the repairable pins, ack the ambiguous row by name, and the gate exits 0."""
    global_config = tmp_path / "gitconfig"
    global_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    clone, pins = _main_plus_tags_clone(tmp_path)
    state_path = _seed_state(
        clone / ".ea",
        waves={
            "P28-I01-W01": pins["W01"],
            "P28-I01-W02": pins["W02"],
            "P28-I01-W04": None,
            "P28-I01-W05": pins["W05"],
            "P28-I01-W06": pins["W06"],
        },
    )
    monkeypatch.setenv("EA_STATE", str(state_path))

    from eawf.surfaces.cli import exit_codes
    from eawf.surfaces.cli.app import app

    runner = CliRunner()
    repair = runner.invoke(app, ["--json", "wave", "verify-commits", "--repair"])
    assert repair.exit_code == 0, repair.output
    repaired = json.loads(repair.stdout)
    assert sorted(row["wave"] for row in repaired["repaired"]) == [
        "P28-I01-W01",
        "P28-I01-W04",
        "P28-I01-W06",
    ]
    assert [row["wave"] for row in repaired["skipped"]] == ["P28-I01-W02"]

    # The ambiguous row still counts as drift until it is acknowledged by name.
    residue = runner.invoke(app, ["--json", "wave", "verify-commits"])
    assert residue.exit_code == exit_codes.VALIDATION_ERROR, residue.output
    assert [row["resolution"] for row in json.loads(residue.stdout)["drifts"]] == ["ambiguous"]

    ack = runner.invoke(app, ["wave", "ack-drift", "P28-I01-W02"])
    assert ack.exit_code == 0, ack.output
    acks = json.loads((clone / ".eawf" / "drift-acks.json").read_text(encoding="utf-8"))
    assert acks == {"acked_wave_ids": ["P28-I01-W02"]}

    clean = runner.invoke(app, ["--json", "wave", "verify-commits"])
    assert clean.exit_code == 0, clean.output
    assert json.loads(clean.stdout)["drift_count"] == 0


def test_verify_commits_closed_unfindable_when_git_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git off PATH classifies an unpinned closed wave as closed_unfindable."""
    state_path = _seed_state(tmp_path / ".ea", waves={W1: None})
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setattr("eawf.workflow.lifecycle.wave_sha.shutil.which", lambda _: None)

    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["--json", "wave", "verify-commits"])
    payload = json.loads(res.stdout)
    assert payload["drifts"][0]["kind"] == "closed_unfindable"


def test_verify_commits_md_renders_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_available: None
) -> None:
    state_path = _seed_state(tmp_path / ".ea", waves={W1: "a" * 40})
    monkeypatch.setenv("EA_STATE", str(state_path))
    _patch_derive(monkeypatch, {W1: "b" * 40})

    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["wave", "verify-commits", "--md"])
    assert "| wave | kind |" in res.output
    assert "pinned_mismatch" in res.output


def test_verify_commits_md_and_json_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_available: None
) -> None:
    from eawf.surfaces.cli import exit_codes

    state_path = _seed_state(tmp_path / ".ea", waves={W1: "a" * 40})
    monkeypatch.setenv("EA_STATE", str(state_path))
    _patch_derive(monkeypatch, {W1: "a" * 40})

    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["--json", "wave", "verify-commits", "--md"])
    assert res.exit_code == exit_codes.USER_ERROR, res.output


# ---- --repair --------------------------------------------------------------


def test_verify_commits_repair_repins_mismatch_and_unpinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_available: None
) -> None:
    """--repair pins both repairable kinds through the canonical writer."""
    state_path = _seed_state(
        tmp_path / ".ea",
        waves={W1: "a" * 40, W2: None},  # mismatch + unpinned_derivable
    )
    monkeypatch.setenv("EA_STATE", str(state_path))
    _patch_derive(monkeypatch, {W1: "b" * 40, W2: "c" * 40})

    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["wave", "verify-commits", "--repair"])
    assert res.exit_code == 0, res.output
    assert "2 re-pinned" in res.output
    # The on-disk state now carries the git-derived SHAs.
    assert _read_commit(state_path, W1) == "b" * 40
    assert _read_commit(state_path, W2) == "c" * 40


def test_verify_commits_repair_reports_unrepairable_as_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_available: None
) -> None:
    """--repair skips kinds with no derivable SHA and names them."""
    state_path = _seed_state(
        tmp_path / ".ea",
        waves={W1: "a" * 40, W2: None},
    )
    monkeypatch.setenv("EA_STATE", str(state_path))
    # W1 pinned_but_missing (derive None); W2 unpinned_derivable (derive -> c).
    _patch_derive(monkeypatch, {W2: "c" * 40})

    from eawf.surfaces.cli.app import app

    res = CliRunner().invoke(app, ["--json", "wave", "verify-commits", "--repair"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["repaired_count"] == 1
    assert payload["skipped_count"] == 1
    assert payload["repaired"][0]["wave"] == W2
    assert payload["skipped"][0]["wave"] == W1
    assert payload["skipped"][0]["kind"] == "pinned_but_missing"
    # W1's unreachable pin is left intact; W2 hardened.
    assert _read_commit(state_path, W1) == "a" * 40
    assert _read_commit(state_path, W2) == "c" * 40


def test_verify_commits_repair_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_available: None
) -> None:
    """A second --repair (then a read-only verify) finds nothing to do."""
    state_path = _seed_state(tmp_path / ".ea", waves={W1: "a" * 40})
    monkeypatch.setenv("EA_STATE", str(state_path))
    _patch_derive(monkeypatch, {W1: "b" * 40})

    from eawf.surfaces.cli.app import app

    runner = CliRunner()
    first = runner.invoke(app, ["wave", "verify-commits", "--repair"])
    assert first.exit_code == 0, first.output
    assert _read_commit(state_path, W1) == "b" * 40

    # Second repair: pin now equals derived -> nothing repaired.
    second = runner.invoke(app, ["--json", "wave", "verify-commits", "--repair"])
    assert second.exit_code == 0, second.output
    payload = json.loads(second.stdout)
    assert payload["repaired_count"] == 0
    assert payload["skipped_count"] == 0

    # And a read-only verify is now clean (exit 0).
    verify = runner.invoke(app, ["wave", "verify-commits"])
    assert verify.exit_code == 0, verify.output
    assert "0 drift" in verify.output
