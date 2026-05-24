"""Import-budget CI gate for the ``eawf`` CLI tree-build path.

Shell completion (``_EAWF_COMPLETE=complete_zsh``) imports
:mod:`eawf.surfaces.cli.app` to build the Typer command tree on *every* TAB
press. Before P26-W30 that import eagerly pulled a long tail of heavy
modules — Pydantic state models, the strict validator, the layered /
profile config stack, the daemon runtime, the MCP installer, the
sandbox policy, the store-kind registry, and (transitively, via
``eawf.runtime.runtimes.*``) ``jinja2`` and ``yaml``. None of those are needed
to render ``--help`` / completion candidates, yet they dominated the
~0.5 s cold-completion wall time.

The W30 sweep relocated each heavy import into the command-handler body
(runtime values) or an ``if TYPE_CHECKING:`` block (annotation-only
names), exploiting the module-wide ``from __future__ import
annotations`` (PEP 563 → annotations are never evaluated at runtime).

This suite has two gates:

* **PRIMARY (deterministic).** In a *fresh* subprocess, import
  :mod:`eawf.surfaces.cli.app` and assert none of :data:`FORBIDDEN_MODULES`
  ended up in ``sys.modules``. A subprocess is mandatory: the in-process
  pytest interpreter has already imported most of the tree, so an
  in-process ``sys.modules`` check would be meaningless. This gate is
  the regression guard — it is *not* skippable, because it asserts a
  structural property (import graph shape), not a timing band.
* **SECONDARY (timing, generous + skippable).** Best-of-5 cold
  ``import eawf.surfaces.cli.app`` subprocess wall time under a generous ceiling
  (~2.5x the observed post-fix best). Guarded by ``EAWF_SKIP_PERF=1``
  for local dev on a busy machine, mirroring
  ``tests/perf/tui/test_perf_budget.py``.

Two residuals are deliberately *not* in :data:`FORBIDDEN_MODULES`
because they live in shared CLI infra (out of scope for the
command-handler sweep, tracked as a Phase-2 follow-up):

* ``pydantic`` / ``pydantic_core`` — :mod:`eawf.surfaces.cli.errors` builds the
  ``ErrorEnvelope`` BaseModel at module load.
* ``eawf.kernel.config.registry`` — :mod:`eawf.surfaces.cli.help_panels` calls
  ``tabs_sorted()`` at module load to build ``PANEL_ORDER``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from time import perf_counter

import pytest

#: Heavy modules the CLI tree-build path (``import eawf.surfaces.cli.app``) must
#: NOT load. Each entry was confirmed absent from a fresh-subprocess
#: ``sys.modules`` dump after the W30 lazy-import sweep. ``pydantic`` /
#: ``pydantic_core`` / ``eawf.kernel.config.registry`` are intentionally
#: excluded — they are shared-infra residuals (see module docstring).
FORBIDDEN_MODULES: tuple[str, ...] = (
    "eawf.kernel.state.models",
    "eawf.kernel.validate.strict",
    "eawf.kernel.config.profile",
    "eawf.kernel.config.loader",
    "eawf.kernel.config.layered",
    "eawf.runtime.daemon.main",
    "eawf.runtime.mcp.installer",
    "eawf.runtime.sandbox.policy",
    "eawf.kernel.store.kinds",
    "eawf.platform.profiles.compose",
    "eawf.platform.profiles.loader",
    "jinja2",
    "yaml",
)

#: Generous CI ceiling for the cold ``import eawf.surfaces.cli.app`` subprocess
#: wall time (interpreter startup included). Post-W30 best-of-5 on the
#: reference machine was ~150 ms; the ceiling is ~2.5x that with extra
#: headroom for slower / contended CI runners. A breach means a heavy
#: import crept back onto the tree-build path, not harness jitter.
CEILING_COLD_IMPORT_MS: float = 400.0

#: Best-of-N sample count for the timing gate — small enough to stay
#: cheap, large enough to discard a single cold-cache outlier.
_COLD_IMPORT_SAMPLES: int = 5

#: Local escape hatch — ``EAWF_SKIP_PERF=1 uv run pytest`` skips the
#: timing gate (the structural gate always runs).
_skip_perf = pytest.mark.skipif(
    os.environ.get("EAWF_SKIP_PERF") == "1",
    reason="EAWF_SKIP_PERF=1 — perf timing gate skipped for local dev",
)


def _loaded_modules_after_app_import() -> frozenset[str]:
    """Return ``sys.modules`` keys after importing ``eawf.surfaces.cli.app`` fresh.

    Runs the import in a clean subprocess so the result reflects the
    real cold-completion import graph, untainted by modules the host
    pytest interpreter already loaded.

    Returns:
        The frozen set of module names present in the child's
        ``sys.modules`` immediately after ``import eawf.surfaces.cli.app``.

    Raises:
        AssertionError: When the child subprocess exits non-zero (the
            import itself failed); the child's stderr is surfaced.
    """
    code = "import eawf.surfaces.cli.app, sys; print(chr(10).join(sys.modules))"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"import eawf.surfaces.cli.app failed in subprocess:\n{proc.stderr}"
    )
    return frozenset(proc.stdout.splitlines())


def _cold_import_ms() -> float:
    """Return the wall time (ms) of one cold ``import eawf.surfaces.cli.app`` subprocess.

    Each call spawns a fresh interpreter so module caches never leak
    between samples. The measured span includes interpreter startup —
    intentional, since shell completion pays that cost on every TAB.

    Returns:
        Elapsed milliseconds for the child subprocess to import the app.

    Raises:
        AssertionError: When the child subprocess exits non-zero.
    """
    start = perf_counter()
    proc = subprocess.run(
        [sys.executable, "-c", "import eawf.surfaces.cli.app"],
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed_ms = (perf_counter() - start) * 1000.0
    assert proc.returncode == 0, (
        f"import eawf.surfaces.cli.app failed in subprocess:\n{proc.stderr}"
    )
    return elapsed_ms


def test_cli_app_import_excludes_forbidden_modules() -> None:
    """The CLI tree-build path must not load any heavy module.

    Structural regression guard: re-introducing a module-level heavy
    import in any command module (or a transitive pull such as
    ``jinja2`` via ``eawf.runtime.runtimes.*``) makes this assertion red with a
    precise list of the offenders that leaked back in.
    """
    loaded = _loaded_modules_after_app_import()
    leaked = sorted(m for m in FORBIDDEN_MODULES if m in loaded)
    assert not leaked, (
        f"import eawf.surfaces.cli.app pulled forbidden heavy module(s): {leaked}. "
        "Relocate the offending module-level import into the command-handler "
        "body (runtime value) or an `if TYPE_CHECKING:` block (annotation only)."
    )


@_skip_perf
def test_cli_app_cold_import_within_ci_ceiling() -> None:
    """Best-of-5 cold ``import eawf.surfaces.cli.app`` stays under the CI ceiling.

    Generous-band timing gate (mirrors the TUI perf budget): a breach
    signals a heavy import crept back onto the tree-build path. Uses the
    best (minimum) of five samples to discard cold-cache / scheduler
    jitter on contended CI runners.
    """
    best_ms = min(_cold_import_ms() for _ in range(_COLD_IMPORT_SAMPLES))
    assert best_ms < CEILING_COLD_IMPORT_MS, (
        f"cold import eawf.surfaces.cli.app best-of-{_COLD_IMPORT_SAMPLES}={best_ms:.0f}ms "
        f"exceeds CI ceiling {CEILING_COLD_IMPORT_MS:.0f}ms — a heavy module "
        "likely returned to the tree-build path (see FORBIDDEN_MODULES)."
    )
