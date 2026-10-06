"""Every ``src/`` module that opens the v1 ``.ea/state.json`` is accounted for.

An epoch-2 tree reads its selected generation, and the v1 document leaves
git once nothing live needs it. A module opens the document through one of
its loaders, validators or writers (:data:`V1_DOCUMENT_CALLS`), so a call to
one of them is what this lint counts. Each module that makes such a call is
either on the migration keep closure -- the v1 chain to schema 1.20, the
epoch-2 cutover, the v1 ``State`` model and the pre-cutover ``session`` and
``worktree`` verbs -- or names the epoch-1 sweep wave (Batch B7, waves W01
to W06) that deletes or rewrites it. A module that starts opening the
document fails the lint until it is classified, and a module that stops
must leave the table, so the table never claims a reader that is gone.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.unit

SRC: Final = Path(__file__).resolve().parents[2] / "src"

#: The calls that open the v1 document: its loaders, its validators and its
#: writers. ``State.model_validate`` is matched only on the ``State`` name.
V1_DOCUMENT_CALLS: Final = frozenset(
    {
        "atomic_write_state",
        "commit_mutation",
        "load_state",
        "read_state",
        "state_transaction",
        "validate_state",
        "write_state_unlocked",
    }
)

_KEEP: Final = "keep"
_UNOWNED: Final = "B7 W06: an epoch-1 fallback no planned Task removes; EAWF-0388 assigns it"

#: Every module that opens the v1 document, and why it may.
V1_READERS: Final[dict[str, str]] = {
    "eawf/kernel/config/profile.py": _UNOWNED,
    "eawf/kernel/migrations/_base.py": f"{_KEEP}: the v1 migration chain to schema 1.20",
    "eawf/kernel/state/io.py": f"{_KEEP}: the marker fence and the daemonless fallback writer",
    "eawf/kernel/validate/strict.py": f"{_KEEP}: the v1 State validator the migration chain runs",
    "eawf/observability/doctor/checks.py": "B7 W05: EAWF-0387 deletes the v1 doctor checks",
    "eawf/platform/install/wizard.py": _UNOWNED,
    "eawf/runtime/daemon/dispatch_runner.py": "B7 W04: EAWF-0383 cuts the v1 fleet dispatch",
    "eawf/runtime/daemon/methods/agent.py": "B7 W04: EAWF-0383 deletes the module",
    "eawf/runtime/daemon/methods/close_evidence.py": "B7 W04: EAWF-0383 deletes the module",
    "eawf/runtime/daemon/methods/doctor.py": "B7 W04: EAWF-0384 deletes the module",
    "eawf/runtime/daemon/methods/fleet.py": "B7 W04: EAWF-0383 deletes the module",
    "eawf/runtime/daemon/methods/release.py": _UNOWNED,
    "eawf/runtime/daemon/methods/state.py": f"{_KEEP}: serves the exempt session verbs",
    "eawf/runtime/daemon/methods/state_close.py": "B7 W04: EAWF-0384 deletes the module",
    "eawf/runtime/daemon/methods/state_codex.py": "B7 W04: EAWF-0384 deletes the module",
    "eawf/runtime/daemon/methods/state_context.py": f"{_KEEP}: anchors the kept state methods",
    "eawf/runtime/daemon/methods/state_jury.py": "B7 W04: EAWF-0384 deletes the module",
    "eawf/runtime/daemon/methods/state_runtime.py": "B7 W04: EAWF-0384 deletes the module",
    "eawf/runtime/daemon/methods/state_worktree.py": f"{_KEEP}: serves the quiescence remediations",
    "eawf/runtime/daemon/session_ttl.py": _UNOWNED,
    "eawf/runtime/daemon/stale_wave.py": _UNOWNED,
    "eawf/runtime/session/store.py": f"{_KEEP}: the pre-cutover session verbs",
    "eawf/runtime/worktree/reconcile.py": f"{_KEEP}: the exempt worktree reconcile verb",
    "eawf/surfaces/cli/_mutation.py": f"{_KEEP}: the transaction behind the exempt verbs",
    "eawf/surfaces/cli/commands/bench.py": _UNOWNED,
    "eawf/surfaces/cli/commands/doc.py": _UNOWNED,
    "eawf/surfaces/cli/commands/doctor.py": "B7 W05: EAWF-0387 deletes the v1 drift check",
    "eawf/surfaces/cli/commands/hook.py": _UNOWNED,
    "eawf/surfaces/cli/commands/impact.py": _UNOWNED,
    "eawf/surfaces/cli/commands/lifecycle.py": _UNOWNED,
    "eawf/surfaces/cli/commands/memory.py": _UNOWNED,
    "eawf/surfaces/cli/commands/metrics.py": _UNOWNED,
    "eawf/surfaces/cli/commands/plan.py": "B7 W05: EAWF-0386 deletes plan view",
    "eawf/surfaces/cli/commands/pr.py": "B7 W05: EAWF-0386 deletes the module",
    "eawf/surfaces/cli/commands/release.py": _UNOWNED,
    "eawf/surfaces/cli/commands/session.py": f"{_KEEP}: the pre-cutover session verbs",
    "eawf/surfaces/cli/commands/status.py": "B7 W05: EAWF-0387 deletes the v1 status read",
    "eawf/surfaces/cli/commands/sync.py": _UNOWNED,
    "eawf/surfaces/cli/commands/why.py": _UNOWNED,
    "eawf/surfaces/cli/commands/wiki.py": "B7 W05: EAWF-0386 deletes the module",
    "eawf/surfaces/cli/commands/worktree.py": f"{_KEEP}: the pre-cutover worktree verbs",
    "eawf/surfaces/tui/chassis/state_binding.py": _UNOWNED,
    "eawf/workflow/evidence/_io.py": (
        "B7 W06: atomic_write_state has no production caller left; EAWF-0388 deletes it"
    ),
    "eawf/workflow/skills/research.py": _UNOWNED,
    "eawf/workflow/skills/roadmap.py": "B7 W03: EAWF-0382 deletes the module",
}

#: A disposition is the keep closure with its reason, or the sweep wave that
#: removes the reader.
_DISPOSITION: Final = re.compile(r"^(keep|B7 W0[1-6]): \S.*$")


def v1_document_calls(source: str) -> frozenset[str]:
    """Return the v1-document calls *source* makes.

    Args:
        source: A module's text.

    Returns:
        The called names drawn from :data:`V1_DOCUMENT_CALLS`, plus
        ``State.model_validate``; empty for a module that makes none.

    Raises:
        SyntaxError: *source* is not Python.
    """
    calls: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in V1_DOCUMENT_CALLS:
            calls.add(func.id)
        elif isinstance(func, ast.Attribute) and func.attr in V1_DOCUMENT_CALLS:
            calls.add(func.attr)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "model_validate"
            and isinstance(func.value, ast.Name)
            and func.value.id == "State"
        ):
            calls.add("State.model_validate")
    return frozenset(calls)


def v1_readers(root: Path) -> frozenset[str]:
    """Return every module under *root* that opens the v1 document, root-relative."""
    return frozenset(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if v1_document_calls(path.read_text(encoding="utf-8"))
    )


def test_every_v1_reader_in_src_is_accounted_for() -> None:
    unaccounted = sorted(v1_readers(SRC) - set(V1_READERS))
    assert not unaccounted, (
        f"modules newly open .ea/state.json: {unaccounted}; read the selected generation "
        "instead, or classify them in V1_READERS"
    )


def test_no_listed_module_has_stopped_reading() -> None:
    gone = sorted(set(V1_READERS) - v1_readers(SRC))
    assert not gone, f"modules no longer open .ea/state.json; drop them from V1_READERS: {gone}"


@pytest.mark.parametrize("module", sorted(V1_READERS))
def test_each_disposition_names_the_keep_closure_or_a_sweep_wave(module: str) -> None:
    assert _DISPOSITION.match(V1_READERS[module]), V1_READERS[module]


@pytest.mark.parametrize(
    "module",
    [
        "eawf/runtime/daemon/semantic_handlers.py",
        "eawf/workflow/planning/apply.py",
        "eawf/platform/rules/render.py",
        "eawf/workflow/verify/release_probes.py",
        "eawf/platform/lint/exclusion_expiry.py",
    ],
)
def test_the_moved_paths_read_the_generation(module: str) -> None:
    assert not v1_document_calls((SRC / module).read_text(encoding="utf-8"))
    assert module not in V1_READERS


def test_a_new_reader_fails_the_lint(tmp_path: Path) -> None:
    """Gate-fire proof: the shape submit_evidence had, loading then writing the document."""
    module = tmp_path / "eawf" / "fresh_reader.py"
    module.parent.mkdir()
    module.write_text(
        "from eawf.workflow.evidence._io import atomic_write_state, load_state\n"
        "def promote(path):\n"
        "    state = load_state(path)\n"
        "    atomic_write_state(path, state)\n",
        encoding="utf-8",
    )
    found = v1_readers(tmp_path)
    assert found == {"eawf/fresh_reader.py"}
    assert found - set(V1_READERS)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", frozenset()),
        ("x = 1\n", frozenset()),
        ("from m import load_state\nref = load_state\n", frozenset()),
        ("import m\nm.load_state(p)\n", frozenset({"load_state"})),
        ("State.model_validate(d)\n", frozenset({"State.model_validate"})),
        ("Project.model_validate(d)\n", frozenset()),
        (
            "read_state(p)\nwrite_state_unlocked(p, d)\n",
            frozenset({"read_state", "write_state_unlocked"}),
        ),
    ],
    ids=[
        "empty",
        "no-call",
        "bare-reference",
        "attribute-call",
        "state-model",
        "other-model",
        "two",
    ],
)
def test_v1_document_calls_boundaries(source: str, expected: frozenset[str]) -> None:
    assert v1_document_calls(source) == expected


def test_v1_document_calls_refuses_a_module_that_is_not_python() -> None:
    with pytest.raises(SyntaxError):
        v1_document_calls("def broken(:\n")
