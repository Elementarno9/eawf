"""The daemon-down ``state.json`` writers refuse a leaking payload too.

The daemon's canonical mutators (``state.mutate``, ``state.commit_worktree``,
``runtime_capture``, ``codex_lifecycle``) and the CLI's ``state_transaction``
chokepoint already run
:func:`eawf.observability.logging.state_leak.state_leak_refusal` between
validating a mutated payload and persisting it. The fallback writer that
runs when the daemon is unavailable --
:func:`eawf.kernel.state.io.commit_mutation` -- did not, and neither did the
scattered direct read-modify-write helpers (CLI project/workspace init,
``config profile enable``, the migration chain's canonical write, a doctor
repair, dispatch-runner token accrual, session-store reconcile/exit-stamp)
that persist ``state.json`` without going through either chokepoint.

The fix routes all of them through one place:
:func:`eawf.kernel.state.io.write_state_unlocked` now reads the on-disk
payload itself and refuses before writing, so every caller that already
holds the sibling lock and calls it -- directly or via ``commit_mutation``
-- inherits the refusal for free. This suite exercises that chokepoint
directly (covering every writer routed through it in one pass), the
headline ``commit_mutation`` path end to end, the migration writer's
carry-forward-unchanged-value exemption, and an AST census that reds when a
function writes ``state.json`` via the raw primitive without also calling
the refusal -- the regression net for a future writer that reintroduces the
bypass.

The home path, email and token are assembled from parts at runtime so this
file carries no literal the leak lints themselves would flag.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.migrations._base import write_canonical
from eawf.kernel.state.io import (
    StateValidationError,
    commit_mutation,
    fallback_wal_dir,
    state_version,
    write_state_unlocked,
)
from eawf.kernel.state.models import State

pytestmark = pytest.mark.integration

MACOS_HOME = "/" + "Users" + "/" + "alice"
LEAKY_PATH = f"{MACOS_HOME}/work/repo"
LEAKY_EMAIL = "alice.eng" + "@" + "personal-mail.dev"
LEAKY_TOKEN = "ghp_" + "a" * 40

_T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
_WAVE = "P41-I01-W01"


def _state_payload(*, title: str = "wave one") -> dict[str, Any]:
    """A minimal valid State with one CLAIMED wave under one active iter."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:XYZ",
        "updated_at": _T0.isoformat(),
        "project": {
            "code": "XYZ",
            "slug": "xyz",
            "title": "XYZ",
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:XYZ",
        },
        "current": {"project_code": "XYZ"},
        "workspace": None,
        "phases": {
            "P41": {
                "id": "P41",
                "scope_id": "XYZ",
                "track_id": None,
                "title": "P41",
                "status": "active",
                "iter_ids": ["P41-I01"],
                "outcome_ids": [],
                "opened_at": _T0.isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P41-I01": {
                "id": "P41-I01",
                "phase_id": "P41",
                "title": "I01",
                "status": "active",
                "wave_ids": [_WAVE],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _T0.isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE: {
                "id": _WAVE,
                "iter_id": "P41-I01",
                "title": title,
                "status": "claimed",
                "file_scopes": ["src/x.py"],
                "success_criteria": [],
                "gates": [],
                "effort_bucket": "S",
                "agent_role": "executor",
                "opened_at": _T0.isoformat(),
                "sessions": {},
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(state_path: Path, *, title: str = "wave one") -> bytes:
    state = State.model_validate(_state_payload(title=title))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path.read_bytes()


# ---- write_state_unlocked: the chokepoint every routed writer shares ------
# commit_mutation, the CLI project/workspace-init and repo-link writers, the
# migration chain's write_canonical, config profile's key materialiser, the
# doctor pin repair, dispatch-runner's token accrual and in-progress flip,
# and session-store's reconcile/exit-stamp all persist through this one
# function, so exercising it directly proves the refusal for all of them at
# once without re-deriving each caller's setup.


@pytest.mark.parametrize(
    ("leak", "field_kind"),
    [
        (LEAKY_PATH, "home_path"),
        (LEAKY_EMAIL, "email"),
        (LEAKY_TOKEN, "token"),
    ],
)
def test_write_state_unlocked_refuses_each_leak_shape(
    tmp_path: Path, leak: str, field_kind: str
) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    before = _write_state(state_path)
    old = orjson.loads(before)
    new = orjson.loads(before)
    new["waves"][_WAVE]["title"] = f"synced from {leak}"

    with pytest.raises(StateValidationError) as excinfo:
        write_state_unlocked(state_path, new)

    message = str(excinfo.value)
    assert message.startswith("state_leak_refused: ")
    assert f"waves.{_WAVE}.title ({field_kind})" in message
    assert leak not in message
    assert state_path.read_bytes() == before
    assert old != new  # sanity: the candidate really did differ from disk


def test_write_state_unlocked_first_write_scans_every_string(tmp_path: Path) -> None:
    """A brand-new ``state.json`` (no on-disk ``old``) is scanned too."""
    state_path = tmp_path / ".ea" / "state.json"
    payload = _state_payload(title=f"synced from {LEAKY_PATH}")

    with pytest.raises(StateValidationError) as excinfo:
        write_state_unlocked(state_path, payload)

    assert str(excinfo.value).startswith("state_leak_refused: ")
    assert not state_path.exists()


def test_write_state_unlocked_clean_write_persists(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    payload = orjson.loads(state_path.read_bytes())
    payload["waves"][_WAVE]["title"] = "a clean retitle"

    write_state_unlocked(state_path, payload)

    assert orjson.loads(state_path.read_bytes())["waves"][_WAVE]["title"] == "a clean retitle"


# ---- commit_mutation: the headline audit finding ---------------------------


def _commit_leaky_title(state_path: Path, *, leak: str) -> None:
    payload = orjson.loads(state_path.read_bytes())
    before_version = state_version(payload)
    state = State.model_validate(payload)
    state.waves[_WAVE].title = f"synced from {leak}"
    commit_mutation(
        state_path,
        candidate=state,
        before_version=before_version,
        command="test.leak",
        args={},
        scope_id=_WAVE,
        summary="test",
    )


def test_commit_mutation_refuses_leaking_wave_title(tmp_path: Path) -> None:
    """The daemon-down WAL-backed fallback refuses like the daemon does."""
    state_path = tmp_path / ".ea" / "state.json"
    before = _write_state(state_path)

    with pytest.raises(StateValidationError) as excinfo:
        _commit_leaky_title(state_path, leak=LEAKY_PATH)

    message = str(excinfo.value)
    assert message.startswith("state_leak_refused: ")
    assert f"waves.{_WAVE}.title (home_path)" in message
    assert LEAKY_PATH not in message
    assert state_path.read_bytes() == before
    # Refused before the WAL-pending record lands, mirroring the daemon's own
    # ordering -- no orphaned record for the next replay to skip over.
    wal_dir = fallback_wal_dir(state_path)
    assert not wal_dir.exists() or list(wal_dir.iterdir()) == []


def test_commit_mutation_clean_write_lands(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)

    _commit_leaky_title(state_path, leak="no leak here")

    landed = orjson.loads(state_path.read_bytes())
    assert landed["waves"][_WAVE]["title"] == "synced from no leak here"


# ---- migration writer: carries forward unchanged legacy values -----------


def test_write_canonical_refuses_a_newly_added_leak(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    before = _write_state(state_path)
    payload = orjson.loads(before)
    payload["waves"][_WAVE]["title"] = f"migrated from {LEAKY_PATH}"

    with pytest.raises(StateValidationError) as excinfo:
        write_canonical(state_path, payload)

    assert str(excinfo.value).startswith("state_leak_refused: ")
    assert state_path.read_bytes() == before


def test_write_canonical_carries_forward_a_preexisting_leak_unchanged(tmp_path: Path) -> None:
    """A migration that leaves an already-leaking field untouched still lands.

    Only a string a write *adds or changes* is scanned (mirrors every other
    canonical mutator); a value a migration step carries forward verbatim
    from a pre-migration payload that already had it is not re-flagged.
    """
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path, title=f"legacy note {LEAKY_PATH}")
    payload = orjson.loads(state_path.read_bytes())
    # Simulate a migration step that touches an unrelated field and leaves
    # the pre-existing leaky title byte-for-byte the same.
    payload["updated_at"] = datetime.now(UTC).isoformat()

    write_canonical(state_path, payload)

    landed = orjson.loads(state_path.read_bytes())
    assert landed["waves"][_WAVE]["title"] == f"legacy note {LEAKY_PATH}"


# ---- AST census: a new bypass writer must red -----------------------------


def _census_violations(src_root: Path, *, allowlist: frozenset[str] = frozenset()) -> list[str]:
    """Return ``path:function`` for each function that bypasses the chokepoint.

    Flags a function that calls the raw ``atomic_write_json_locked`` /
    ``atomic_write_json`` primitive without also calling
    ``state_leak_refusal`` in the same function body, unless its file is in
    *allowlist*.
    """
    writer_module = "eawf.kernel.state.writer"
    refusal_module = "eawf.observability.logging.state_leak"
    violations: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        rel = path.relative_to(src_root).as_posix()
        if rel in allowlist:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        writer_names = _imported_local_names(
            tree, writer_module, "atomic_write_json_locked"
        ) | _imported_local_names(tree, writer_module, "atomic_write_json")
        if not writer_names:
            continue
        refusal_names = _imported_local_names(tree, refusal_module, "state_leak_refusal")
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            calls = [call for call in ast.walk(node) if isinstance(call, ast.Call)]
            calls_writer = any(
                isinstance(call.func, ast.Name) and call.func.id in writer_names for call in calls
            )
            if not calls_writer:
                continue
            calls_refusal = any(
                isinstance(call.func, ast.Name) and call.func.id in refusal_names for call in calls
            )
            if not calls_refusal:
                violations.append(f"{rel}:{node.name}")
    return violations


def _imported_local_names(tree: ast.Module, module: str, name: str) -> set[str]:
    """Local bindings *module.name* is imported under, direct or aliased."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            names.update(alias.asname or alias.name for alias in node.names if alias.name == name)
    return names


def _src_root() -> Path:
    return Path(__file__).resolve().parents[4] / "src" / "eawf"


# Files that persist a document OTHER than a project/workspace `state.json`
# (registry.json, a lease file, a gate claim/receipt, the spec cache, the
# budget notice ledger...) --
# out of this gate's "state.json writer" scope entirely.
_NON_STATE_WRITERS = frozenset(
    {
        "kernel/state/writer.py",
        "platform/install/canary.py",
        "runtime/workspace/lease.py",
        "runtime/verification/progress.py",
        "runtime/budget/notices.py",
        "runtime/daemon/gate_execution.py",
        "runtime/daemon/methods/registry.py",
        "runtime/daemon/methods/registry_workspace.py",
        "surfaces/cli/commands/repo.py",
        "surfaces/cli/commands/workspace.py",
        "kernel/spec/writer.py",
    }
)


def test_ast_census_finds_no_unlisted_bypass_writer() -> None:
    """Every state.json writer in src/eawf routes through the chokepoint.

    No ``_KNOWN_GAPS`` carve-out: the daemon-method writers this census
    first found uncovered (fleet.py, agent.py, research.py, spec.py,
    spec_convert.py, spec_repoint.py, state_jury.py) plus the first-run
    wizard (platform/install/wizard.py,
    surfaces/tui/screens/overlays/init_wizard_render.py) and the test-fixture
    helper ``workflow.evidence._io.atomic_write_state`` are all routed now,
    so ``_NON_STATE_WRITERS`` (a different document family entirely) is the
    only allowlist left.
    """
    violations = _census_violations(_src_root(), allowlist=_NON_STATE_WRITERS)
    assert violations == []


def test_ast_census_flags_a_bypass_writer(tmp_path: Path) -> None:
    """Gate-fire proof: a synthetic new writer that skips the refusal reds."""
    bad = tmp_path / "bad_writer.py"
    bad.write_text(
        "from eawf.kernel.state.writer import atomic_write_json_locked\n"
        "\n"
        "def write_it(path, data):\n"
        "    atomic_write_json_locked(path, data)\n",
        encoding="utf-8",
    )

    violations = _census_violations(tmp_path)

    assert violations == ["bad_writer.py:write_it"]


def test_ast_census_accepts_a_writer_with_an_inline_refusal(tmp_path: Path) -> None:
    good = tmp_path / "good_writer.py"
    good.write_text(
        "from eawf.kernel.state.writer import atomic_write_json_locked\n"
        "from eawf.observability.logging.state_leak import state_leak_refusal\n"
        "\n"
        "def write_it(path, old, new):\n"
        "    if (refusal := state_leak_refusal(old, new)) is not None:\n"
        "        raise ValueError(refusal)\n"
        "    atomic_write_json_locked(path, new)\n",
        encoding="utf-8",
    )

    assert _census_violations(tmp_path) == []


def test_ast_census_ignores_files_with_no_writer_call(tmp_path: Path) -> None:
    (tmp_path / "unrelated.py").write_text("x = 1\n", encoding="utf-8")

    assert _census_violations(tmp_path) == []
