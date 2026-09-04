"""Architecture test: every exception the runtime package declares is catchable.

An exception class with no catch site anywhere in ``src/eawf/`` is a *declared*
failure mode with no *handled* failure mode: it escapes whatever seam raises it
and surfaces as an unhandled traceback. The concurrent-spawn cap was exactly
that -- raised by all three vendor adapters, caught nowhere -- so this test
walks the tree and reds on any exception class the source never catches.

Reachability is resolved through the class hierarchy: a class is handled when
its own name, or the name of any ancestor other than the catch-all
``Exception`` / ``BaseException``, appears in an ``except`` clause. Catching a
declared base (``RuntimeSpawnError``, ``SandboxError``) genuinely handles its
subclasses, so the walk credits that; crediting ``except Exception`` would make
the test vacuous, so it does not.

:data:`UNHANDLED_BY_DESIGN` pins the exceptions that intentionally have no
handler, each with its reason. The baseline is checked in BOTH directions: a
new orphan reds, and an entry that has since gained a handler also reds, so the
list cannot rot into a permanent waiver.
"""

from __future__ import annotations

import ast
import asyncio
from functools import cache
from pathlib import Path

import pytest

from eawf.runtime.runtimes.adapter import (
    RUNTIME_RATE_LIMIT,
    ConcurrentSpawnCapError,
    RuntimeSpawnError,
)
from eawf.workflow.dispatch.retry import RetryExhaustedError, spawn_with_retry

#: Root of the shipped package (this file lives at ``tests/runtime/runtimes/``).
_SRC_ROOT = Path(__file__).resolve().parents[4] / "src" / "eawf"

#: The subtree whose declared exceptions must be catchable.
_RUNTIME_ROOT = _SRC_ROOT / "runtime"

#: Bases too broad to confer reachability. Crediting a class as "handled"
#: because some module somewhere catches ``Exception`` would pass every class
#: trivially and prove nothing.
_CATCH_ALL_BASES: frozenset[str] = frozenset({"Exception", "BaseException"})

#: Exceptions with no catch site, by design, each with the reason. Every entry
#: is asserted to STILL be unhandled, so a handler landing later reds this test
#: and forces the waiver to be dropped.
UNHANDLED_BY_DESIGN: dict[str, str] = {
    "SessionResumeFailedError": (
        "the V8 fall-through consumer has no production caller yet -- "
        "continue_session is declared on the Protocol and raised by the "
        "opencode adapter, but the daemon does not resume sessions"
    ),
}


def _class_defs(root: Path) -> dict[str, ast.ClassDef]:
    """Return every class defined under *root*, keyed by name."""
    defined: dict[str, ast.ClassDef] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                defined.setdefault(node.name, node)
    return defined


def _base_names(node: ast.ClassDef) -> list[str]:
    """Return the bare names of *node*'s bases (dotted bases use the attr)."""
    names: list[str] = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
    return names


def _is_exception_class(node: ast.ClassDef) -> bool:
    """Return whether *node* declares an exception (by base-name convention)."""
    return any(name.endswith(("Error", "Exception")) for name in _base_names(node))


@cache
def _caught_names(root: Path) -> frozenset[str]:
    """Return every name appearing in an ``except`` clause under *root*."""
    caught: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or node.type is None:
                continue
            handled = node.type
            items = handled.elts if isinstance(handled, ast.Tuple) else [handled]
            for item in items:
                if isinstance(item, ast.Name):
                    caught.add(item.id)
                elif isinstance(item, ast.Attribute):
                    caught.add(item.attr)
    return frozenset(caught)


def _is_handled(
    name: str,
    *,
    caught: frozenset[str] | set[str],
    defined: dict[str, ast.ClassDef],
    seen: frozenset[str] = frozenset(),
) -> bool:
    """Return whether *name* or a non-catch-all ancestor has a catch site."""
    if name in seen or name in _CATCH_ALL_BASES:
        return False
    if name in caught:
        return True
    node = defined.get(name)
    if node is None:
        return False
    return any(
        _is_handled(base, caught=caught, defined=defined, seen=seen | {name})
        for base in _base_names(node)
    )


@cache
def _orphans() -> dict[str, str]:
    """Return every unhandled runtime exception, keyed by name, valued by site."""
    defined = _class_defs(_SRC_ROOT)
    caught = _caught_names(_SRC_ROOT)
    orphans: dict[str, str] = {}
    for path in sorted(_RUNTIME_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not _is_exception_class(node):
                continue
            if _is_handled(node.name, caught=caught, defined=defined):
                continue
            orphans[node.name] = f"{path.relative_to(_SRC_ROOT)}:{node.lineno}"
    return orphans


# ---------------------------------------------------------------------------
# The architecture gate
# ---------------------------------------------------------------------------


def test_every_runtime_exception_has_a_catch_site() -> None:
    """No exception in the runtime package lacks a handler off the baseline."""
    unexpected = {
        name: site for name, site in _orphans().items() if name not in UNHANDLED_BY_DESIGN
    }
    assert not unexpected, (
        "runtime exceptions with no catch site anywhere in src/eawf/: "
        f"{unexpected}. Add a handler, or pin it in UNHANDLED_BY_DESIGN "
        "with the reason it must escape."
    )


def test_unhandled_by_design_baseline_has_not_rotted() -> None:
    """Every pinned waiver is still genuinely unhandled."""
    orphans = _orphans()
    stale = sorted(name for name in UNHANDLED_BY_DESIGN if name not in orphans)
    assert not stale, (
        f"UNHANDLED_BY_DESIGN entries that now HAVE a handler: {stale}. "
        "Drop them from the baseline."
    )


def test_baseline_reasons_are_non_empty() -> None:
    """A waiver without a stated reason is not a waiver."""
    blank = sorted(name for name, reason in UNHANDLED_BY_DESIGN.items() if not reason.strip())
    assert not blank


def test_gate_reds_on_a_synthetic_orphan() -> None:
    """The walk actually flags an exception with no catch site.

    Proves the gate can fail: a class whose only base is an uncaught,
    non-catch-all name resolves as unhandled.
    """
    module = ast.parse("class NeverCaughtWidgetError(RuntimeSpawnError): pass")
    defined = {"NeverCaughtWidgetError": module.body[0]}
    assert isinstance(defined["NeverCaughtWidgetError"], ast.ClassDef)
    assert not _is_handled("NeverCaughtWidgetError", caught=set(), defined=defined)
    assert _is_handled("NeverCaughtWidgetError", caught={"RuntimeSpawnError"}, defined=defined)


def test_catch_all_bases_never_confer_reachability() -> None:
    """``except Exception`` does not count as handling a declared class."""
    module = ast.parse("class BareError(Exception): pass")
    node = module.body[0]
    assert isinstance(node, ast.ClassDef)
    assert not _is_handled("BareError", caught={"Exception"}, defined={"BareError": node})


# ---------------------------------------------------------------------------
# The concurrency-cap error is caught by the retry ladder
# ---------------------------------------------------------------------------


def test_concurrent_spawn_cap_error_is_a_runtime_spawn_error() -> None:
    """The cap error rides the same seam the ladder already catches."""
    assert issubclass(ConcurrentSpawnCapError, RuntimeSpawnError)


def test_concurrent_spawn_cap_error_has_a_catch_site_in_the_ladder() -> None:
    """The cap error is named in an ``except`` clause under ``src/eawf/``."""
    assert "ConcurrentSpawnCapError" in _caught_names(_SRC_ROOT)


def test_retry_ladder_catches_the_cap_error_rather_than_letting_it_escape() -> None:
    """A saturated cap surfaces as a typed exhaustion, not as the cap error."""

    async def _spawn(_runtime: str) -> object:
        raise ConcurrentSpawnCapError(inflight=16, cap=16)

    def _never_called(_exc: RuntimeSpawnError, _runtime: str) -> str:
        raise AssertionError("the cap error must not reach the vendor classifier")

    with pytest.raises(RetryExhaustedError) as caught:
        asyncio.run(
            spawn_with_retry(
                runtime="claude-code",
                preference=["claude-code", "codex"],
                spawn=_spawn,
                classify=_never_called,  # type: ignore[arg-type]
                max_attempts=2,
            )
        )
    assert caught.value.notice.error_class == RUNTIME_RATE_LIMIT
    assert caught.value.notice.retry_after_seconds == pytest.approx(5.0)
