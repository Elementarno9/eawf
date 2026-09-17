"""The plugin bundle is byte-reproducible from the tree and the epoch alone.

Under test: :mod:`eawf.workflow.release.plugin_bundle`, which replaced
``tar -czf`` in the source-host leg. Two builds of one tree must hash
equal even when the checkout's mtimes, modes and owners differ, because
the dry run freezes the bundle digest before the tag build produces the
bundle it publishes. Normalising must not cost the payload: hooks stay
executable and the dotdirs the plugin lives in stay in the archive.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import runpy
import sys
import tarfile
from pathlib import Path

import pytest

from eawf.workflow.release import plugin_bundle
from eawf.workflow.release.plugin_bundle import (
    EXECUTABLE_MODE,
    EXIT_USAGE,
    REGULAR_MODE,
    build_bundle,
    bundle_members,
    main,
)
from eawf.workflow.release.reproducibility import SOURCE_DATE_EPOCH_ENV

pytestmark = pytest.mark.unit

EPOCH = 1_789_571_840
BUNDLE = "eawf-plugin-0.7.0.dev3.tar.gz"
HOOK = "hooks/session-start.sh"

#: Relative path -> (content, executable) of the synthetic rendered tree.
TREE: dict[str, tuple[bytes, bool]] = {
    ".claude-plugin/plugin.json": (b'{"name": "eawf"}\n', False),
    ".claude-plugin/marketplace.json": (b'{"plugins": []}\n', False),
    HOOK: (b"#!/bin/sh\nexit 0\n", True),
    "skills/review/SKILL.md": (b"# review\n", False),
    "README.md": (b"# eawf\n", False),
}


def _write_tree(root: Path, *, file_mode: int = 0o644, exec_mode: int = 0o755) -> Path:
    """Write :data:`TREE` under *root* with the given modes and return *root*."""
    for name, (content, executable) in TREE.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(exec_mode if executable else file_mode)
    (root / "agents").mkdir()
    return root


def _restamp(root: Path, when: int) -> None:
    """Set every entry under *root*, and *root* itself, to mtime *when*."""
    for path in [root, *root.rglob("*")]:
        os.utime(path, (when, when))


def _members(bundle: Path) -> list[tarfile.TarInfo]:
    with tarfile.open(bundle, mode="r:gz") as archive:
        return archive.getmembers()


def test_build_bundle_is_byte_identical_across_mtimes_and_modes(tmp_path: Path) -> None:
    """Two checkouts of one tree bundle to the same bytes.

    The second checkout differs in every input a plain tar records:
    mtimes decades apart and a group-writable umask.
    """
    first = _write_tree(tmp_path / "first")
    second = _write_tree(tmp_path / "second", file_mode=0o664, exec_mode=0o775)
    _restamp(first, 1_000_000_000)
    _restamp(second, 1_900_000_000)

    one = build_bundle(first, tmp_path / "one.tar.gz", epoch=EPOCH)
    two = build_bundle(second, tmp_path / "two.tar.gz", epoch=EPOCH)

    assert one == two
    assert (tmp_path / "one.tar.gz").read_bytes() == (tmp_path / "two.tar.gz").read_bytes()


def test_build_bundle_rebuild_of_one_tree_is_byte_identical(tmp_path: Path) -> None:
    """Building the same directory twice, touched in between, hashes equal."""
    tree = _write_tree(tmp_path / "tree")
    first = build_bundle(tree, tmp_path / "a.tar.gz", epoch=EPOCH)
    _restamp(tree, 1_234_567_890)
    second = build_bundle(tree, tmp_path / "b.tar.gz", epoch=EPOCH)
    assert first == second


def test_build_bundle_follows_source_date_epoch(tmp_path: Path) -> None:
    """The epoch is the one clock the bundle reads: a new epoch, new bytes."""
    tree = _write_tree(tmp_path / "tree")
    pinned = build_bundle(tree, tmp_path / "pinned.tar.gz", epoch=EPOCH)
    moved = build_bundle(tree, tmp_path / "moved.tar.gz", epoch=EPOCH + 1)

    assert pinned != moved
    assert {m.mtime for m in _members(tmp_path / "pinned.tar.gz")} == {EPOCH}
    assert {m.mtime for m in _members(tmp_path / "moved.tar.gz")} == {EPOCH + 1}


def test_build_bundle_pins_owners_to_numeric_root(tmp_path: Path) -> None:
    """No member carries the runner's uid, gid or account names."""
    tree = _write_tree(tmp_path / "tree")
    build_bundle(tree, tmp_path / BUNDLE, epoch=EPOCH)
    members = _members(tmp_path / BUNDLE)
    assert {(m.uid, m.gid, m.uname, m.gname) for m in members} == {(0, 0, "", "")}


def test_build_bundle_keeps_hook_executable_bits(tmp_path: Path) -> None:
    """Hooks stay executable; everything else collapses to two modes."""
    tree = _write_tree(tmp_path / "tree", file_mode=0o600, exec_mode=0o700)
    build_bundle(tree, tmp_path / BUNDLE, epoch=EPOCH)
    modes = {m.name: m.mode for m in _members(tmp_path / BUNDLE)}

    assert modes[HOOK] == EXECUTABLE_MODE
    assert modes["README.md"] == REGULAR_MODE
    assert modes[".claude-plugin"] == EXECUTABLE_MODE
    assert set(modes.values()) == {EXECUTABLE_MODE, REGULAR_MODE}


def test_build_bundle_keeps_dotdirs_and_content(tmp_path: Path) -> None:
    """Extracting the bundle gives back the tree, dotdirs and empty dirs included."""
    tree = _write_tree(tmp_path / "tree")
    build_bundle(tree, tmp_path / BUNDLE, epoch=EPOCH)
    out = tmp_path / "out"
    with tarfile.open(tmp_path / BUNDLE, mode="r:gz") as archive:
        archive.extractall(out, filter="data")

    for name, (content, executable) in TREE.items():
        assert (out / name).read_bytes() == content
        assert bool((out / name).stat().st_mode & 0o111) is executable
    assert (out / "agents").is_dir()


def test_build_bundle_orders_members_like_bundle_members(tmp_path: Path) -> None:
    """The archive lists members in the walk's deterministic order."""
    tree = _write_tree(tmp_path / "tree")
    build_bundle(tree, tmp_path / BUNDLE, epoch=EPOCH)
    assert tuple(m.name for m in _members(tmp_path / BUNDLE)) == bundle_members(tree)


def test_bundle_members_orders_each_directory_before_its_children(tmp_path: Path) -> None:
    """Segment order puts ``a/b`` before ``a-b``, which plain string order would not."""
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").write_text("x", encoding="utf-8")
    (tmp_path / "a-b").write_text("x", encoding="utf-8")
    (tmp_path / ".hidden").write_text("x", encoding="utf-8")
    assert bundle_members(tmp_path) == (".hidden", "a", "a/b", "a-b")


def test_bundle_members_empty_tree_is_empty(tmp_path: Path) -> None:
    """A tree with nothing in it lists nothing."""
    assert bundle_members(tmp_path) == ()


def test_build_bundle_writes_gzip_without_name_or_timestamp(tmp_path: Path) -> None:
    """The gzip header carries no FNAME flag and a zero MTIME."""
    tree = _write_tree(tmp_path / "tree")
    build_bundle(tree, tmp_path / BUNDLE, epoch=EPOCH)
    header = (tmp_path / BUNDLE).read_bytes()[:10]

    assert header[:3] == b"\x1f\x8b\x08"
    assert header[3] == 0, "FLG must carry no FNAME, FCOMMENT or FEXTRA bit"
    assert header[4:8] == b"\x00\x00\x00\x00"
    assert BUNDLE.encode() not in (tmp_path / BUNDLE).read_bytes()
    with gzip.open(tmp_path / BUNDLE) as stream:
        assert stream.read(262)[257:262] == b"ustar"


def test_build_bundle_returns_the_written_digest(tmp_path: Path) -> None:
    """The returned digest is the sha256 of the file on disk."""
    tree = _write_tree(tmp_path / "tree")
    digest = build_bundle(tree, tmp_path / BUNDLE, epoch=EPOCH)
    assert digest == hashlib.sha256((tmp_path / BUNDLE).read_bytes()).hexdigest()


def test_build_bundle_single_file_tree(tmp_path: Path) -> None:
    """A one-file tree bundles to exactly one member."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "plugin.json").write_bytes(b"{}")
    build_bundle(tree, tmp_path / BUNDLE, epoch=0)
    assert [(m.name, m.size, m.mtime) for m in _members(tmp_path / BUNDLE)] == [
        ("plugin.json", 2, 0)
    ]


def test_build_bundle_empty_tree_raises(tmp_path: Path) -> None:
    """An empty tree would publish a bundle of nothing."""
    tree = tmp_path / "tree"
    tree.mkdir()
    with pytest.raises(ValueError, match="is empty"):
        build_bundle(tree, tmp_path / BUNDLE, epoch=EPOCH)
    assert not (tmp_path / BUNDLE).exists()


def test_build_bundle_negative_epoch_raises(tmp_path: Path) -> None:
    """A negative epoch is not a timestamp any commit carries."""
    tree = _write_tree(tmp_path / "tree")
    with pytest.raises(ValueError, match="non-negative"):
        build_bundle(tree, tmp_path / BUNDLE, epoch=-1)


def test_build_bundle_missing_source_raises(tmp_path: Path) -> None:
    """A tree that was never rendered is refused, not bundled as empty."""
    with pytest.raises(ValueError, match="is not a directory"):
        build_bundle(tmp_path / "absent", tmp_path / BUNDLE, epoch=EPOCH)


def test_build_bundle_destination_inside_source_raises(tmp_path: Path) -> None:
    """Writing into the archived tree would archive the half-written bundle."""
    tree = _write_tree(tmp_path / "tree")
    with pytest.raises(ValueError, match="outside the tree"):
        build_bundle(tree, tree / BUNDLE, epoch=EPOCH)


def test_build_bundle_symlink_raises(tmp_path: Path) -> None:
    """A symlink would publish a pointer into the builder's filesystem."""
    tree = _write_tree(tmp_path / "tree")
    (tree / "hooks" / "link.sh").symlink_to(tree / HOOK)
    with pytest.raises(ValueError, match=r"hooks/link\.sh"):
        build_bundle(tree, tmp_path / BUNDLE, epoch=EPOCH)


def test_main_prints_the_sha256sum_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The module entry point bundles under the environment's epoch."""
    tree = _write_tree(tmp_path / "tree")
    monkeypatch.setenv(SOURCE_DATE_EPOCH_ENV, str(EPOCH))

    assert main([str(tree), str(tmp_path / BUNDLE)]) == 0

    digest = hashlib.sha256((tmp_path / BUNDLE).read_bytes()).hexdigest()
    assert capsys.readouterr().out == f"{digest}  {BUNDLE}\n"
    assert {m.mtime for m in _members(tmp_path / BUNDLE)} == {EPOCH}


def test_main_without_source_date_epoch_exits_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No epoch is a usage error; the wall clock is never the fallback."""
    tree = _write_tree(tmp_path / "tree")
    monkeypatch.delenv(SOURCE_DATE_EPOCH_ENV, raising=False)

    assert main([str(tree), str(tmp_path / BUNDLE)]) == EXIT_USAGE

    assert "SOURCE_DATE_EPOCH" in capsys.readouterr().err
    assert not (tmp_path / BUNDLE).exists()


def test_main_non_integer_source_date_epoch_exits_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An epoch that is not an integer is refused before anything is written."""
    tree = _write_tree(tmp_path / "tree")
    monkeypatch.setenv(SOURCE_DATE_EPOCH_ENV, "2026-09-17")

    assert main([str(tree), str(tmp_path / BUNDLE)]) == EXIT_USAGE

    assert "'2026-09-17'" in capsys.readouterr().err


def test_main_unbundlable_tree_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tree the builder refuses exits 1 with the reason."""
    monkeypatch.setenv(SOURCE_DATE_EPOCH_ENV, str(EPOCH))

    assert main([str(tmp_path / "absent"), str(tmp_path / BUNDLE)]) == 1

    assert "plugin bundle failed" in capsys.readouterr().err


def test_plugin_bundle_runs_as_a_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``python -m eawf.workflow.release.plugin_bundle`` is the workflow's call."""
    tree = _write_tree(tmp_path / "tree")
    monkeypatch.setenv(SOURCE_DATE_EPOCH_ENV, str(EPOCH))
    monkeypatch.setattr(sys, "argv", ["plugin_bundle", str(tree), str(tmp_path / BUNDLE)])
    monkeypatch.delitem(sys.modules, plugin_bundle.__name__)

    with pytest.raises(SystemExit) as exited:
        runpy.run_module(plugin_bundle.__name__, run_name="__main__")

    assert exited.value.code == 0
    assert (tmp_path / BUNDLE).is_file()
