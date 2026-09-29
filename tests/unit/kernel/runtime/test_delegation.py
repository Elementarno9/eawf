"""A delegation subtree is bounded by its ceilings, and a child's grant by its parent's.

RUN-022: the child count is counted across the whole subtree under every Run above the
child, never per parent, so a grandchild spends the root's allowance too.

RUN-023: a child is granted only what both its own policy and its parent's sealed
capsule grant, tool by tool and authority axis by axis.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.delegation import (
    MAX_CHILD_RUNS,
    ChildCeilingBreach,
    SubtreeOverrun,
    child_grant,
    resolved_child_runs,
    subtree_overrun,
)
from eawf.kernel.runtime.provider import AuthorityGrant
from eawf.kernel.state.epoch2.run import RepositoryScope, RunPurpose, TaskScope
from tests.unit.kernel.runtime.test_authority_capsule import capsule_fields, review_fields

pytestmark = pytest.mark.unit

REPOSITORY: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
TASK: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"
AT: Final = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def run(key: int) -> str:
    return f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-{key:08d}"


def ceilings(table: dict[str, int], default: int = MAX_CHILD_RUNS) -> Any:
    return lambda urn: table.get(urn, default)


# ---- RUN-022: the resolved ceiling ------------------------------------------------


def test_run_022_task_scope_resolves_no_children() -> None:
    scope = TaskScope(
        scope_kind="task",
        task_ref=parse_qualified_urn(TASK),
        purpose=RunPurpose.IMPLEMENT,
        write_set=("src",),
    )

    assert resolved_child_runs(scope) == 0


def test_run_022_read_only_scope_resolves_the_policy_maximum() -> None:
    scope = RepositoryScope(
        scope_kind="repository",
        repository_ref=parse_qualified_urn(REPOSITORY),
        purpose=RunPurpose.OBSERVE,
    )

    assert resolved_child_runs(scope) == MAX_CHILD_RUNS == 32


# ---- RUN-022: counting across the subtree ------------------------------------------


def test_run_022_root_without_a_parent_is_never_over() -> None:
    assert subtree_overrun({run(1): None}, child=run(1), ceiling_of=ceilings({})) is None


def test_run_022_single_child_within_ceiling_of_one() -> None:
    parents = {run(1): None, run(2): run(1)}

    assert subtree_overrun(parents, child=run(2), ceiling_of=ceilings({run(1): 1})) is None


def test_run_022_second_child_passes_ceiling_of_one() -> None:
    parents = {run(1): None, run(2): run(1), run(3): run(1)}

    overrun = subtree_overrun(parents, child=run(3), ceiling_of=ceilings({run(1): 1}))

    assert overrun == SubtreeOverrun(ancestor=run(1), ceiling=1, descendants=2)


def test_run_022_ceiling_of_zero_refuses_the_first_child() -> None:
    parents = {run(1): None, run(2): run(1)}

    overrun = subtree_overrun(parents, child=run(2), ceiling_of=ceilings({run(1): 0}))

    assert overrun == SubtreeOverrun(ancestor=run(1), ceiling=0, descendants=1)


def test_run_022_grandchild_counts_against_the_root_not_only_its_parent() -> None:
    # The root admits two Runs in its whole subtree; its child admits many. A child and
    # a grandchild fill the root's allowance, so a second grandchild passes it even
    # though its own parent is far within its ceiling.
    parents = {run(1): None, run(2): run(1), run(3): run(2), run(4): run(2)}

    overrun = subtree_overrun(parents, child=run(4), ceiling_of=ceilings({run(1): 2}))

    assert overrun == SubtreeOverrun(ancestor=run(1), ceiling=2, descendants=3)


def test_run_022_nearest_passed_ancestor_is_reported() -> None:
    parents = {run(1): None, run(2): run(1), run(3): run(2)}

    overrun = subtree_overrun(parents, child=run(3), ceiling_of=ceilings({run(2): 0, run(1): 0}))

    assert overrun is not None
    assert overrun.ancestor == run(2)


def test_run_022_other_roots_do_not_count() -> None:
    parents = {run(1): None, run(2): run(1), run(3): None, run(4): run(3), run(5): run(3)}

    assert subtree_overrun(parents, child=run(2), ceiling_of=ceilings({run(1): 1})) is None


def test_run_022_full_ceiling_is_reached_exactly_then_passed() -> None:
    # Children are keyed 2 onward, so the child keyed MAX + 1 is the MAX-th and the
    # one keyed MAX + 2 is the first past the ceiling.
    last, past = run(MAX_CHILD_RUNS + 1), run(MAX_CHILD_RUNS + 2)
    parents: dict[str, str | None] = {run(1): None}
    for key in range(2, MAX_CHILD_RUNS + 3):
        parents[run(key)] = run(1)
    at_ceiling = {key: value for key, value in parents.items() if key != past}

    assert subtree_overrun(at_ceiling, child=last, ceiling_of=ceilings({})) is None
    overrun = subtree_overrun(parents, child=past, ceiling_of=ceilings({}))
    assert overrun == SubtreeOverrun(
        ancestor=run(1), ceiling=MAX_CHILD_RUNS, descendants=MAX_CHILD_RUNS + 1
    )


def test_run_022_unknown_child_is_a_key_error() -> None:
    with pytest.raises(KeyError):
        subtree_overrun({run(1): None}, child=run(9), ceiling_of=ceilings({}))


# ---- RUN-022: the recorded breach ----------------------------------------------------


def breach(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "child_run_ref": run(3),
        "ancestor_run_ref": run(1),
        "ceiling": 1,
        "descendants": 2,
        "recorded_at": AT,
    }
    fields.update(overrides)
    return fields


def test_run_022_breach_records_a_subtree_over_its_ceiling() -> None:
    record = ChildCeilingBreach.model_validate(breach())

    assert record.payload_kind == "child_ceiling_breach"
    assert record.ancestor_run_ref.entity_key == "RUN-00000001"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"descendants": 1}, id="at-ceiling-is-no-breach"),
        pytest.param({"descendants": 0}, id="empty-subtree"),
        pytest.param({"ceiling": MAX_CHILD_RUNS + 1, "descendants": 40}, id="ceiling-past-max"),
        pytest.param({"ceiling": -1}, id="negative-ceiling"),
        pytest.param({"ancestor_run_ref": TASK}, id="ancestor-not-a-run"),
        pytest.param({"extra": 1}, id="unknown-key"),
    ],
)
def test_run_022_malformed_breach_is_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ChildCeilingBreach.model_validate(breach(**overrides))


def test_run_022_breach_without_a_child_is_refused() -> None:
    fields = breach()
    del fields["child_run_ref"]

    with pytest.raises(ValidationError):
        ChildCeilingBreach.model_validate(fields)


# ---- RUN-023: the child's grant is cut to the parent's -----------------------------


def parent_capsule(**overrides: Any) -> AuthorityCapsule:
    return AuthorityCapsule.seal(review_fields(run_ref=run(1), **overrides))


def test_run_023_child_keeps_only_tools_the_parent_holds() -> None:
    parent = parent_capsule(tool_grants=["repo_read", "submit_report"])

    tools, _ = child_grant(
        tool_grants=("repo_search", "repo_read", "submit_report"),
        authority=AuthorityGrant(state="read_only", workspace="read_only"),
        parent=parent,
    )

    assert tools == ("repo_read", "submit_report")


def test_run_023_parent_denial_is_not_handed_down() -> None:
    parent = parent_capsule(tool_grants=["repo_read", "submit_report"], tool_denials=["repo_read"])

    tools, _ = child_grant(
        tool_grants=("repo_read", "submit_report"), authority=AuthorityGrant(), parent=parent
    )

    assert tools == ("submit_report",)


def test_run_023_empty_request_stays_empty() -> None:
    tools, _ = child_grant(tool_grants=(), authority=AuthorityGrant(), parent=parent_capsule())

    assert tools == ()


def test_run_023_each_authority_axis_takes_the_lower_level() -> None:
    parent = AuthorityCapsule.seal(capsule_fields(run_ref=run(1)))
    wider = AuthorityGrant(
        state="proposal_only",
        workspace="scoped_write",
        git="read_metadata",
        credentials="reference_only",
        config="read_effective",
    )

    _, authority = child_grant(tool_grants=(), authority=wider, parent=parent)

    assert authority == AuthorityGrant(
        state="proposal_only", workspace="scoped_write", git="read_metadata"
    )


def test_run_023_child_never_rises_above_a_read_only_parent() -> None:
    parent = parent_capsule()

    _, authority = child_grant(
        tool_grants=(),
        authority=AuthorityGrant(state="proposal_only", workspace="scoped_write"),
        parent=parent,
    )

    assert authority.state == "read_only"
    assert authority.workspace == "read_only"


def test_run_023_unknown_tool_in_the_request_is_dropped_not_granted() -> None:
    tools, _ = child_grant(
        tool_grants=("not_a_tool",), authority=AuthorityGrant(), parent=parent_capsule()
    )

    assert tools == ()
