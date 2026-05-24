"""Tests for ``cache_path_for`` session-id sanitization (P04-CORE security).

Claude's stdin payload is untrusted: a malicious or buggy session_id
could traverse out of the cache root via ``..`` segments or absolute
paths. The sanitizer collapses everything outside ``[A-Za-z0-9_.-]`` to
``_`` and pins all-traversal inputs to ``unknown``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.runtime.runtimes.claude.statusline import cache_path_for


@pytest.fixture(autouse=True)
def _cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path / "cache"))
    return tmp_path / "cache"


def test_cache_path_simple_session_id_resolves_under_root(_cache_root: Path) -> None:
    path = cache_path_for("ses-001")
    assert path.parent == _cache_root
    assert path.name == "ses-001.json"


def test_cache_path_traversal_does_not_escape_root(_cache_root: Path) -> None:
    """A ``../`` segment must not escape the cache root."""
    path = cache_path_for("../../tmp/escape")
    # Slash characters replaced with underscore — single-component
    # filename so the parent stays anchored at the cache root.
    assert "/" not in path.name
    assert "\\" not in path.name
    assert path.parent == _cache_root
    # Final resolved path stays inside the cache root regardless of
    # how many ``..`` literals appear in the basename (they don't act
    # as traversal once they're not separated by a path separator).
    resolved = path.resolve()
    assert str(resolved).startswith(str(_cache_root.resolve()))


def test_cache_path_absolute_path_session_id_collapses(_cache_root: Path) -> None:
    """An absolute-path-shaped session_id must be sanitized."""
    path = cache_path_for("/etc/passwd")
    assert path.parent == _cache_root
    # No leading slash bypassed the cache root anchor.
    resolved = path.resolve()
    assert str(resolved).startswith(str(_cache_root.resolve()))


def test_cache_path_empty_session_id_falls_through_to_unknown(_cache_root: Path) -> None:
    assert cache_path_for("").name == "unknown.json"


def test_cache_path_dot_session_id_falls_through_to_unknown(_cache_root: Path) -> None:
    """A bare ``.`` or ``..`` collapses to ``unknown.json`` rather than
    naming the cache directory itself."""
    assert cache_path_for(".").name == "unknown.json"
    assert cache_path_for("..").name == "unknown.json"


def test_cache_path_long_session_id_truncated(_cache_root: Path) -> None:
    """A pathological 4 KiB session_id must not produce a 4 KiB file name."""
    huge = "a" * 4096
    path = cache_path_for(huge)
    # 128-char cap from the sanitizer + ``.json`` suffix.
    assert len(path.name) <= 128 + len(".json")


def test_cache_path_null_byte_replaced(_cache_root: Path) -> None:
    """NUL bytes are replaced; the file system does not see them."""
    path = cache_path_for("ses\x00001")
    assert "\x00" not in path.name
    assert path.parent == _cache_root
