"""Unit tests for :mod:`eawf.spec.writer` + :mod:`eawf.spec.cache`.

Exercises the daemon-internal writer helpers without spinning up the
daemon. Covers:

* Scope classification + path resolution (phase / iter / wave).
* URN construction (round-trips through :mod:`eawf.state.urn`).
* Scaffolded body shape + blob SHA.
* Cache document upsert + read-back.
* ``git rm`` helper against a tmp git repo.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from eawf.spec import cache as spec_cache
from eawf.spec import writer as spec_writer

pytestmark = pytest.mark.unit


# ---- Scope classification ----------------------------------------------


def test_classify_scope_phase() -> None:
    assert spec_writer.classify_scope("P25") == "phase"


def test_classify_scope_iter() -> None:
    assert spec_writer.classify_scope("P25-I01") == "iter"


def test_classify_scope_wave() -> None:
    assert spec_writer.classify_scope("P25-I01-W03") == "wave"


def test_classify_scope_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown spec scope id"):
        spec_writer.classify_scope("X99")


def test_phase_of_wave() -> None:
    assert spec_writer.phase_of("P25-I01-W03") == "P25"


# ---- Path resolution ----------------------------------------------------


def test_spec_file_path_phase(tmp_path: Path) -> None:
    path = spec_writer.spec_file_path("P25", repo_root=tmp_path)
    assert path == tmp_path / ".ea" / "specs" / "P25" / "spec.md"


def test_spec_file_path_iter(tmp_path: Path) -> None:
    path = spec_writer.spec_file_path("P25-I01", repo_root=tmp_path)
    assert path == tmp_path / ".ea" / "specs" / "P25" / "P25-I01" / "spec.md"


def test_spec_file_path_wave(tmp_path: Path) -> None:
    path = spec_writer.spec_file_path("P25-I01-W03", repo_root=tmp_path)
    expected = tmp_path / ".ea" / "specs" / "P25" / "P25-I01" / "P25-I01-W03.md"
    assert path == expected


# ---- URN construction ---------------------------------------------------


def test_build_spec_urn_phase() -> None:
    urn = spec_writer.build_spec_urn("P25", repo_code="EAWF")
    assert urn == "urn:eawf:v1:spec:EAWF/P25"


def test_build_spec_urn_iter() -> None:
    urn = spec_writer.build_spec_urn("P25-I01", repo_code="EAWF")
    assert urn == "urn:eawf:v1:spec:EAWF/P25/P25-I01"


def test_build_spec_urn_wave_round_trips() -> None:
    """Wave URN round-trips through :func:`eawf.state.urn.parse`."""
    from eawf.state.urn import parse as parse_urn

    urn = spec_writer.build_spec_urn("P25-I01-W03", repo_code="EAWF")
    parsed = parse_urn(urn)
    assert parsed.kind == "spec"
    assert parsed.owner == "EAWF"
    assert parsed.id == "P25/P25-I01/P25-I01-W03"


# ---- Scaffold body ------------------------------------------------------


def test_scaffold_body_includes_sentinel_and_status() -> None:
    body = spec_writer.scaffold_body(
        scope_id="P25-I01-W03",
        title="Spec writer + cache",
        spec_urn="urn:eawf:v1:spec:EAWF/P25/P25-I01/P25-I01-W03",
    )
    assert "eawf-template: spec-wave" in body
    assert "Spec writer + cache" in body
    assert "**Status:** DRAFT" in body
    assert "P25-I01-W03" in body


def test_blob_sha_matches_git_hash_object(tmp_path: Path) -> None:
    """Hand-rolled blob SHA matches ``git hash-object``'s output."""
    sample = tmp_path / "blob.txt"
    sample.write_bytes(b"hello world\n")
    sha = spec_writer.blob_sha_for(b"hello world\n")
    expected_header = b"blob 12\x00hello world\n"
    assert sha == hashlib.sha1(expected_header).hexdigest()


# ---- Disk + cache roundtrip --------------------------------------------


def test_write_spec_file_writes_body_and_returns_sha(tmp_path: Path) -> None:
    path = tmp_path / ".ea" / "specs" / "P25" / "spec.md"
    body = "# Hello\n"
    sha = spec_writer.write_spec_file(path, body)
    assert path.read_text(encoding="utf-8") == body
    assert sha == spec_writer.blob_sha_for(body.encode("utf-8"))


def test_write_cache_entry_upsert_creates_then_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First entry creates the file; second entry with same URN replaces it."""
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("EAWF_SPEC_CACHE_DIR", str(cache_dir))

    entry1 = spec_writer.build_entry(
        spec_urn="urn:eawf:v1:spec:EAWF/P25/P25-I01/P25-I01-W03",
        file_sha="aaaa",
        file_path=tmp_path / ".ea" / "specs" / "P25" / "P25-I01" / "P25-I01-W03.md",
        repo_root=tmp_path,
        status="DRAFT",
    )
    spec_writer.write_cache_entry(phase_id="P25", entry=entry1)
    loaded1 = spec_cache.read_phase_cache("P25")
    assert len(loaded1.entries) == 1
    assert loaded1.entries[0].file_sha == "aaaa"
    assert loaded1.entries[0].status == "DRAFT"

    entry2 = spec_writer.build_entry(
        spec_urn="urn:eawf:v1:spec:EAWF/P25/P25-I01/P25-I01-W03",
        file_sha="bbbb",
        file_path=tmp_path / ".ea" / "specs" / "P25" / "P25-I01" / "P25-I01-W03.md",
        repo_root=tmp_path,
        status="READY",
    )
    spec_writer.write_cache_entry(phase_id="P25", entry=entry2)
    loaded2 = spec_cache.read_phase_cache("P25")
    assert len(loaded2.entries) == 1
    assert loaded2.entries[0].file_sha == "bbbb"
    assert loaded2.entries[0].status == "READY"


def test_find_cached_entry_returns_none_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("EAWF_SPEC_CACHE_DIR", str(cache_dir))
    result = spec_cache.find_cached_entry(
        "urn:eawf:v1:spec:EAWF/P25",
        phase_id="P25",
    )
    assert result is None


# ---- git rm helper ------------------------------------------------------


def _init_git_repo(repo_root: Path) -> None:
    """Initialise a git repo at *repo_root* with one tracked spec file."""
    subprocess.run(["git", "init", "--quiet", "-b", "main"], cwd=repo_root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "ci@example.invalid"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "ci"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=repo_root,
        check=True,
    )


def test_git_rm_spec_removes_tracked_file(tmp_path: Path) -> None:
    """``git_rm_spec`` stages the deletion via ``git rm``."""
    _init_git_repo(tmp_path)
    spec_path = tmp_path / ".ea" / "specs" / "P25" / "spec.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("# spec\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", str(spec_path.relative_to(tmp_path))],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "seed"],
        cwd=tmp_path,
        check=True,
    )

    spec_writer.git_rm_spec(
        repo_root=tmp_path,
        repo_relative_path=Path(".ea/specs/P25/spec.md"),
    )
    assert not spec_path.exists()
    # git index now records the deletion.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "D  .ea/specs/P25/spec.md" in status.stdout


def test_git_rm_spec_raises_when_path_missing(tmp_path: Path) -> None:
    """``git rm`` failure (path absent) surfaces as ValueError."""
    _init_git_repo(tmp_path)
    with pytest.raises(ValueError, match="git rm"):
        spec_writer.git_rm_spec(
            repo_root=tmp_path,
            repo_relative_path=Path(".ea/specs/P25/never-existed.md"),
        )
