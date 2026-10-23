"""Unit tests for the opaque session-log handle registry.

Covers :mod:`eawf.runtime.daemon.session` (register / resolve). The
registry is process-local in-memory; each test resets it via
:func:`eawf.runtime.daemon.session.reset_registry` so the order of
execution does not leak.

Per AGENTS rule 16 (secrets / PII hygiene): the handle is the only
identifier serialised into ``state.json`` / ``event.jsonl``. Tests
assert the handle format follows the URN-shaped convention so the
opaque-handle invariant has a regression seat.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from eawf.runtime.daemon.session import (
    HANDLE_PREFIX,
    register_session_log,
    reset_registry,
    resolve_session_log,
)

pytestmark = pytest.mark.unit


_HANDLE_RE = re.compile(r"^urn:eawf:v1:session-log:[a-z0-9_\-]+:[0-9a-f]{32}$")


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    """Reset the process-local registry between tests."""
    reset_registry()
    yield
    reset_registry()


def test_register_resolves_round_trip(tmp_path: Path) -> None:
    raw_path = tmp_path / "session-log.json"
    handle = register_session_log("claude-code", raw_path)
    assert resolve_session_log(handle) == raw_path


def test_register_returns_urn_shaped_handle(tmp_path: Path) -> None:
    handle = register_session_log("codex", tmp_path / "log.json")
    assert handle.startswith(HANDLE_PREFIX)
    assert _HANDLE_RE.fullmatch(handle), f"unexpected handle shape: {handle!r}"


def test_handle_prefix_is_urn_eawf_v1_session_log() -> None:
    assert HANDLE_PREFIX == "urn:eawf:v1:session-log:"


def test_resolve_unknown_handle_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="unknown session-log handle"):
        resolve_session_log("urn:eawf:v1:session-log:claude-code:dead")


def test_register_rejects_empty_runtime(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="runtime must be non-empty"):
        register_session_log("", tmp_path / "log.json")


def test_distinct_runtimes_get_distinct_handles(tmp_path: Path) -> None:
    h1 = register_session_log("claude-code", tmp_path / "a.json")
    h2 = register_session_log("codex", tmp_path / "b.json")
    assert h1 != h2
    assert resolve_session_log(h1) != resolve_session_log(h2)


def test_two_handles_for_same_runtime_differ(tmp_path: Path) -> None:
    h1 = register_session_log("claude-code", tmp_path / "a.json")
    h2 = register_session_log("claude-code", tmp_path / "b.json")
    assert h1 != h2
