"""Pin: both plugin packagers emit only handler-backed hook wrappers.

Companion to ``tests/contract/test_runtime_resolution_contract.py``, which
pins the same invariant for the local installers (``eawf plugin install
<runtime>``). This module pins it for the standalone marketplace-tree
packagers (``eawf plugin package <runtime>``) that ship to npm users. The
Claude packager used to wire every ``PLUGIN_HOOK_REGISTRY`` event (eight
wrappers) even though only ``SESSION_END`` has a real runner-registered
handler, so every ``Bash`` tool call in an installed session fired seven
idle no-op hooks.

"Handler-backed" means the event has a real callable registered by
``register_runtime_capture_hooks``:

- Claude: :data:`HookSpec.has_handler` on
  :data:`eawf.surfaces.render.hooks.HOOK_REGISTRY` (today ``SESSION_START``,
  whose rule-projection staleness check runs for every runtime, and
  ``SESSION_END`` -- the Claude runtime never sets ``event.runtime ==
  "codex"``, so the Codex-lifecycle callable registered for
  SUBAGENT_START / SUBAGENT_STOP is a live no-op there).
- Codex: every event in
  :data:`eawf.runtime.runtimes.codex.hook_map.CODEX_HOOK_EVENT_TYPES` is
  genuinely handled, since the Codex packager always sets
  ``runtime="codex"`` and the registered callables act unconditionally
  for that runtime; :func:`~eawf.runtime.runtimes.codex.hook_map.
  registered_handler_event_types` derives this from the real runner
  registrations rather than a hand-kept list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.runtime.runtimes.claude.plugin_package import package_plugin as claude_package_plugin
from eawf.runtime.runtimes.codex.hook_map import (
    CODEX_HOOK_EVENT_TYPES,
    registered_handler_event_types,
)
from eawf.runtime.runtimes.codex.plugin_package import package_plugin as codex_package_plugin
from eawf.surfaces.render.hooks import HOOK_REGISTRY

pytestmark = pytest.mark.unit

_CLAUDE_HANDLER_BACKED: frozenset[str] = frozenset(
    spec.event_type.value for spec in HOOK_REGISTRY if spec.has_handler
)
_CODEX_HANDLER_BACKED: frozenset[str] = frozenset(
    event_type.value for event_type in CODEX_HOOK_EVENT_TYPES
)


def _assert_emitted_are_handler_backed(emitted: set[str], handler_backed: frozenset[str]) -> None:
    """Fail, naming the offending events, when *emitted* exceeds *handler_backed*.

    Shared by the real-packager assertions below and the gate-fire-proof
    test, so both exercise the exact same check.
    """
    unbacked = emitted - handler_backed
    assert not unbacked, f"packager emitted handler-less hook(s): {sorted(unbacked)}"


def test_claude_packager_emits_only_handler_backed_hooks(tmp_path: Path) -> None:
    """``eawf plugin package claude`` wires only SESSION_START and SESSION_END."""
    target = tmp_path / "claude-pkg"
    claude_package_plugin(target)
    emitted = {p.stem for p in (target / "hooks").iterdir()}
    _assert_emitted_are_handler_backed(emitted, _CLAUDE_HANDLER_BACKED)
    assert emitted == {"session_start", "session_end"}


def test_codex_packager_emits_only_handler_backed_hooks(tmp_path: Path) -> None:
    """``eawf plugin package codex`` wires every provider-native lifecycle hook."""
    target = tmp_path / "codex-pkg"
    codex_package_plugin(target)
    hooks_dir = target / "plugins" / "eawf" / "hooks"
    emitted = {p.stem for p in hooks_dir.glob("*.sh")}
    _assert_emitted_are_handler_backed(emitted, _CODEX_HANDLER_BACKED)
    assert emitted == _CODEX_HANDLER_BACKED


def test_codex_handler_backed_set_matches_runner_registrations() -> None:
    """CODEX_HOOK_EVENT_TYPES is a subset of the mechanically-derived registry.

    Cross-checks the packaged Codex set against
    ``registered_handler_event_types()`` -- built from a scratch
    ``HookRunner`` + ``register_runtime_capture_hooks`` rather than a
    second hand-kept list -- so a future event added to
    ``CODEX_HOOK_EVENT_NAMES`` without a registered handler is caught here
    (in addition to the import-time boot guard in ``codex.hook_map``).
    """
    registered = registered_handler_event_types()
    assert {event_type.value for event_type in registered} >= _CODEX_HANDLER_BACKED


def test_unbacked_hook_entry_reds_the_check() -> None:
    """Gate-fire proof: an event with no registered handler fails the shared check.

    Simulates the original defect directly -- a packager also emitting a
    ``subagent_stop.sh`` wrapper under the Claude runtime, where it has no
    live handler -- without mutating any production registry, proving the
    assertion helper the two tests above rely on actually has teeth.
    """
    seeded = set(_CLAUDE_HANDLER_BACKED) | {"subagent_stop"}
    with pytest.raises(AssertionError, match="subagent_stop"):
        _assert_emitted_are_handler_backed(seeded, _CLAUDE_HANDLER_BACKED)


def test_claude_handler_backed_set_is_non_empty() -> None:
    """Fixture invariant -- at least one Claude event is handler-backed."""
    assert _CLAUDE_HANDLER_BACKED


def test_codex_handler_backed_set_is_non_empty() -> None:
    """Fixture invariant -- at least one Codex event is handler-backed."""
    assert _CODEX_HANDLER_BACKED
