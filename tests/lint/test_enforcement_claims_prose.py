"""Two enforcement claims in the source tree agree with the code.

A reader takes a docstring for a contract. Two of them used to promise
enforcement the tree does not perform:

* The sandbox boundary prose named an *egress proxy* as a live member. The
  proxy module exists, but ``start_egress_proxy`` has no production call site,
  so nothing constrains where a spawned agent dials.
* The research steer / broadcast prose said operator notes shaped the round
  they landed in. The loop reads the channel only when a round's findings are
  reconciled, after every researcher of that round was spawned, so a note is
  recorded against a round record and reaches no live session.

This lint pins the corrected prose against the code it describes: each check
fails both when the correction is dropped from a source file and when the code
grows the producer the old prose assumed, so a future wave that really wires
the proxy or a live steer channel is told to re-word rather than silently
re-introducing an overstatement.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "eawf"

#: The one sentence every boundary surface must carry: outbound network is not
#: constrained by anything eawf starts today.
_EGRESS_CLAIM = "outbound network is unrestricted until the provider-native sandbox lands"

#: The one sentence every steer / broadcast surface must carry.
_STEER_CLAIM = "never reach"

#: Source files whose boundary prose must carry :data:`_EGRESS_CLAIM`.
_BOUNDARY_FILES: tuple[str, ...] = (
    "runtime/daemon/dispatch_runner.py",
    "runtime/sandbox/env_scrub.py",
    "runtime/sandbox/jail.py",
)

#: Source files whose steer / broadcast prose must say notes are recorded and
#: never reach a running round, paired with the symbol each claim sits on.
_STEER_FILES: tuple[tuple[str, str], ...] = (
    ("kernel/state/models.py", "steer_notes"),
    ("kernel/store/kinds/research_round.py", "steer_notes"),
    ("runtime/daemon/methods/research.py", "broadcast"),
    ("surfaces/tui/modes/research_board.py", "broadcast"),
)

#: The function that would start the UDS egress proxy. A production caller
#: means the boundary prose is due a rewrite, not a re-pin.
_PROXY_STARTER = "start_egress_proxy"

#: Prose that reads as a live steering channel. Banned from the steer surfaces
#: because the loop folds the channel after the round's researchers are spawned.
_OVERSTATED_STEER = re.compile(
    r"shaped the round|shaped its dispatch|fans? a notice to every running round"
    r"|folded in \*?before\*? this round ran",
    re.IGNORECASE,
)


#: The pre-correction boundary prose, verbatim enough to drive the
#: proves-it-reds check: it names the proxy as a live boundary member and says
#: nothing about the un-constrained outbound path.
_OLD_BOUNDARY_PROSE = (
    "The sandbox boundary (egress proxy, env-scrub, argv-policy, cwd-guard)\n"
    "hands its SandboxEnforcementEvent here.\n"
)

#: The pre-correction steer prose: the note is said to shape the round.
_OLD_STEER_PROSE = (
    "steer_notes: The operator steer / override notes folded into the round\n"
    "    -- the between-rounds feedback that shaped its dispatch set.\n"
)


def _read(relative: str) -> str:
    return (_SRC / relative).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Collapse line wrapping so a phrase check spans a wrapped comment."""
    return " ".join(text.replace("#", " ").split())


def carries_egress_claim(text: str) -> bool:
    """Return whether *text* states the outbound path is unconstrained.

    Args:
        text: Source text of one boundary module.

    Returns:
        ``True`` when the corrected claim survives *text*'s line wrapping.
    """
    return _EGRESS_CLAIM in _flat(text)


def carries_steer_claim(text: str) -> bool:
    """Return whether *text* states a note reaches no running round.

    Args:
        text: Source text of one steer / broadcast surface.

    Returns:
        ``True`` when the corrected claim survives *text*'s line wrapping and
        no pre-correction live-steering phrasing remains.
    """
    flat = _flat(text)
    stated = (
        f"{_STEER_CLAIM} a running round" in flat or f"{_STEER_CLAIM}es a running round" in flat
    )
    return stated and _OVERSTATED_STEER.search(text) is None


def test_carries_egress_claim_rejects_the_pre_correction_prose() -> None:
    """The check reds on the exact prose it was written to replace."""
    assert carries_egress_claim(_OLD_BOUNDARY_PROSE) is False


def test_carries_steer_claim_rejects_the_pre_correction_prose() -> None:
    """The steer check reds on the exact prose it was written to replace."""
    assert carries_steer_claim(_OLD_STEER_PROSE) is False


def test_carries_steer_claim_rejects_a_half_correction() -> None:
    """Adding the honest line without dropping the old claim still reds."""
    half = _OLD_STEER_PROSE + "    The notes never reach a running round.\n"
    assert carries_steer_claim(half) is False


def test_carries_claims_accept_the_corrected_prose() -> None:
    """Both checks pass on minimal corrected prose (the positive path)."""
    assert carries_egress_claim(f"Nothing starts the proxy, so {_EGRESS_CLAIM}.") is True
    assert carries_steer_claim("The note is recorded and never reaches a running round.") is True


def test_carries_claims_reject_empty_text() -> None:
    """Empty source carries neither claim (the empty boundary case)."""
    assert carries_egress_claim("") is False
    assert carries_steer_claim("") is False


@pytest.mark.parametrize("relative", _BOUNDARY_FILES)
def test_boundary_prose_states_outbound_network_is_unrestricted(relative: str) -> None:
    """Each sandbox-boundary surface states the un-wired egress honestly."""
    assert carries_egress_claim(_read(relative))


def test_egress_proxy_has_no_production_caller() -> None:
    """No ``src/`` module starts the proxy, so the boundary prose stays correct.

    A production caller invalidates the correction this lint pins: the prose
    would then understate. Re-word the boundary docstrings in the same change
    that wires the proxy rather than deleting this check.
    """
    callers = [
        path.relative_to(_REPO_ROOT).as_posix()
        for path in _SRC.rglob("*.py")
        if path.name != "egress_proxy.py"
        and f"{_PROXY_STARTER}(" in path.read_text(encoding="utf-8")
    ]
    assert callers == []


@pytest.mark.parametrize(("relative", "symbol"), _STEER_FILES)
def test_steer_prose_states_notes_never_reach_a_running_round(relative: str, symbol: str) -> None:
    """Each steer / broadcast surface says the note reaches no running round."""
    source = _read(relative)
    assert symbol in source
    assert carries_steer_claim(source)


def test_research_round_channel_fold_runs_after_dispatch() -> None:
    """The channel fold is read during reconcile, never before a round spawns.

    The prose correction rests on call order: ``_channel_notes`` is called from
    ``_reconcile``, which runs on a round's returned findings. Were it hoisted
    ahead of the dispatch, notes WOULD reach the round and the corrected prose
    would become the overstatement.
    """
    source = _read("runtime/daemon/methods/research.py")
    tree = ast.parse(source)
    reconcilers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_reconcile"
    ]
    assert reconcilers, "_reconcile not found in research.py"
    called = {
        node.func.id
        for reconciler in reconcilers
        for node in ast.walk(reconciler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_channel_notes" in called


def test_boundary_files_all_exist() -> None:
    """A renamed boundary module fails here rather than skipping its check."""
    missing = [rel for rel in _BOUNDARY_FILES if not (_SRC / rel).is_file()]
    missing.extend(rel for rel, _ in _STEER_FILES if not (_SRC / rel).is_file())
    assert missing == []
