"""Every epoch-1 noun left in ``src/`` sits in a reasoned, ratcheting allowlist.

The lifecycle is Track, Milestone, Batch, Task and Run. A ``src/`` file
outside a migration or upgrade path that still says phase, iter or wave,
names a retired skill or spells a ``P<NN>-I<NN>-W<NN>`` id is either code
that must keep the epoch-1 word for now or prose that went stale. The
census runs :data:`~eawf.workflow.skills.census.RETIRED_NOUN_PATTERN` over
every git-tracked ``src/`` file, skipping the :data:`HOMONYMS` source code
uses, and holds each file that matches to a row
of :data:`SRC_EPOCH1_ALLOWLIST`: a ceiling, the match count when the row
was written, and the reason the words stay. A file that gains a match, or
a new file that holds one, fails; so does a file that sheds one until its
ceiling is lowered to the new count, and a row whose file reaches zero
until it is deleted.

The reasons:

* ``keep-closure``: the module is inside the epoch-1 keep closure
  (``tests/fixtures/epoch1-keep-closure.json``), the reader and importer
  code that still loads epoch-1 state.
* ``historic-store-kind``: the file names an epoch-1 entity, store kind,
  serialized field, config leaf, URN kind, enum member or hook event that
  live code still reads or writes, or prose about one. Renaming it is a
  separate change with its own migration.
* ``retired-key-message``: the file names a retired skill, verb or key to
  register, refuse or redirect it.
* ``legacy-import-mapping``: the file maps an epoch-1 record onto its
  native form at a read or import boundary.
* ``flag-day-retirement``: the flag-day refusal surface, or sample console
  content that shows a retirement.
* ``native-phase``: every match is the bare word phase or phases naming a
  native stage, such as the phase of a Run control, a tool or host call, a
  transcript event or an integration lock. The noun pattern skips the
  compound names of those stages but cannot tell the bare word from the
  lifecycle noun, so the gate checks that such a row holds nothing else.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, Literal, get_args

import pytest

from eawf.workflow.skills.census import RETIRED_NOUN_PATTERN

pytestmark = pytest.mark.unit

REPO: Final = Path(__file__).resolve().parents[2]

#: The epoch-1 lifecycle id shape: a phase and iter, with an optional wave.
EPOCH1_ID: Final = re.compile(r"\bP\d{2,}-I\d{2,}(?:-W\d{2,})?\b")

#: Identifiers built on an epoch-1 noun that name something else, matched
#: whole and case-blind. Python's iteration protocol names its generators
#: this way; a Run control, a tool or host call, a transcript event and an
#: integration lock each move through typed stages, and a release version
#: carries a pre-release segment. None of them is an epoch-1 lifecycle
#: entity, so the src census skips them, and a call of Python's ``iter()``,
#: instead of reporting noise. A published page is held to the full
#: pattern: it is read as prose, where these are the epoch-1 nouns.
HOMONYMS: Final[tuple[str, ...]] = (
    "iter_agent_reports",
    "iter_child_nodes",
    "iter_modules",
    "iter_rows",
    "iter_subscriber_pushes",
    "_declared_phase_dispositions",
    "_disposition_belongs_to_the_phase",
    "_fields_match_the_phase",
    "_host_tool_phases",
    "_outcome_belongs_to_the_result_phase",
    "_payload_and_phase_match_the_kind",
    "_payload_phases",
    "_state_phase",
    "_status_belongs_to_the_terminal_phase",
    "_summary_belongs_to_the_summarized_phase",
    "compile_phase_dispositions",
    "compile_phase_locks",
    "gate_phase",
    "locked_phase",
    "missing_phase_rows",
    "missing_phases",
    "phase_dispositions",
    "phase_locks",
    "phase_order",
    "pre_phase",
    "state_tool_phase",
    "tool_phase_kinds",
    "unlocked_phases",
)

#: The page census pattern with every :data:`HOMONYMS` identifier and a call
#: of ``iter()`` left out.
SRC_NOUN_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"(?!(?:{'|'.join(HOMONYMS)})\b)(?!iter\()(?:{RETIRED_NOUN_PATTERN.pattern})",
    re.IGNORECASE,
)


def src_nouns(text: str) -> tuple[str, ...]:
    """Return every distinct src census noun in *text*, in first-seen order."""
    return tuple(dict.fromkeys(match.group(0) for match in SRC_NOUN_PATTERN.finditer(text)))


#: The only nouns a ``native-phase`` row may hold.
NATIVE_PHASE_NOUNS: Final[frozenset[str]] = frozenset({"phase", "phases"})

Reason = Literal[
    "keep-closure",
    "historic-store-kind",
    "retired-key-message",
    "legacy-import-mapping",
    "flag-day-retirement",
    "native-phase",
]

REASONS: Final[frozenset[str]] = frozenset(get_args(Reason))

#: File -> (ceiling, reason). The ceiling is the measured match count.
SRC_EPOCH1_ALLOWLIST: Final[dict[str, tuple[int, Reason]]] = {
    "src/eawf/kernel/__init__.py": (3, "keep-closure"),
    "src/eawf/kernel/config/defaults.py": (4, "historic-store-kind"),
    "src/eawf/kernel/config/layered.py": (7, "historic-store-kind"),
    "src/eawf/kernel/config/registry/config_keys.py": (3, "historic-store-kind"),
    "src/eawf/kernel/config/registry/leaf_catalog.py": (12, "historic-store-kind"),
    "src/eawf/kernel/config/registry/leaf_keys.py": (2, "historic-store-kind"),
    "src/eawf/kernel/economics/notice_policy.py": (2, "historic-store-kind"),
    "src/eawf/kernel/errors/lifecycle.py": (10, "historic-store-kind"),
    "src/eawf/kernel/identity/alias.py": (1, "legacy-import-mapping"),
    "src/eawf/kernel/projection/settings.py": (3, "historic-store-kind"),
    "src/eawf/kernel/projection/transcript.py": (17, "native-phase"),
    "src/eawf/kernel/runtime/control.py": (36, "native-phase"),
    "src/eawf/kernel/runtime/events.py": (97, "native-phase"),
    "src/eawf/kernel/runtime/usage.py": (7, "native-phase"),
    "src/eawf/kernel/spec/__init__.py": (17, "keep-closure"),
    "src/eawf/kernel/spec/cadence.py": (6, "keep-closure"),
    "src/eawf/kernel/spec/common.py": (29, "keep-closure"),
    "src/eawf/kernel/spec/eval_report.py": (9, "historic-store-kind"),
    "src/eawf/kernel/spec/intent.py": (10, "keep-closure"),
    "src/eawf/kernel/spec/iter.py": (30, "historic-store-kind"),
    "src/eawf/kernel/spec/phase.py": (10, "historic-store-kind"),
    "src/eawf/kernel/spec/validators.py": (12, "historic-store-kind"),
    "src/eawf/kernel/state/enums.py": (37, "keep-closure"),
    "src/eawf/kernel/state/epoch2/measurement.py": (15, "keep-closure"),
    "src/eawf/kernel/state/ids.py": (10, "keep-closure"),
    "src/eawf/kernel/state/models.py": (80, "keep-closure"),
    "src/eawf/kernel/state/urn.py": (3, "keep-closure"),
    "src/eawf/kernel/store/commit_policy.py": (2, "keep-closure"),
    "src/eawf/kernel/store/kinds/__init__.py": (1, "keep-closure"),
    "src/eawf/kernel/store/kinds/agent_report.py": (7, "keep-closure"),
    "src/eawf/kernel/store/kinds/conduct_deviation.py": (1, "historic-store-kind"),
    "src/eawf/kernel/store/kinds/event.py": (10, "keep-closure"),
    "src/eawf/kernel/store/kinds/events/__init__.py": (1, "keep-closure"),
    "src/eawf/kernel/store/kinds/events/agent_output_chunk.py": (4, "keep-closure"),
    "src/eawf/kernel/store/kinds/events/base.py": (9, "keep-closure"),
    "src/eawf/kernel/store/kinds/events/dispatch_cost.py": (5, "keep-closure"),
    "src/eawf/kernel/store/kinds/events/runtime_switched.py": (4, "keep-closure"),
    "src/eawf/kernel/store/kinds/events/session_closed.py": (4, "keep-closure"),
    "src/eawf/kernel/store/kinds/events/session_continued.py": (4, "keep-closure"),
    "src/eawf/kernel/store/kinds/events/session_failover.py": (3, "keep-closure"),
    "src/eawf/kernel/store/kinds/evidence.py": (3, "historic-store-kind"),
    "src/eawf/kernel/store/kinds/gate_rereceipt.py": (11, "historic-store-kind"),
    "src/eawf/kernel/store/kinds/jury_ballot.py": (1, "historic-store-kind"),
    "src/eawf/kernel/store/kinds/legacy_audit_disposition.py": (2, "historic-store-kind"),
    "src/eawf/kernel/store/kinds/spec_updated.py": (1, "historic-store-kind"),
    "src/eawf/observability/bench/seed.py": (44, "historic-store-kind"),
    "src/eawf/observability/bench/turn_cost.py": (32, "historic-store-kind"),
    "src/eawf/observability/doctor/checks.py": (4, "retired-key-message"),
    "src/eawf/observability/eval/__init__.py": (4, "historic-store-kind"),
    "src/eawf/observability/eval/jury_validation.py": (67, "historic-store-kind"),
    "src/eawf/observability/eval/reputation.py": (45, "historic-store-kind"),
    "src/eawf/observability/eval/rule_adherence.py": (10, "historic-store-kind"),
    "src/eawf/observability/eval/score.py": (4, "historic-store-kind"),
    "src/eawf/observability/eval/self_eval.py": (2, "historic-store-kind"),
    "src/eawf/observability/logging/state_leak.py": (2, "keep-closure"),
    "src/eawf/observability/measurement/transcript_spans.py": (3, "native-phase"),
    "src/eawf/observability/metrics/__init__.py": (1, "historic-store-kind"),
    "src/eawf/observability/metrics/odr.py": (6, "historic-store-kind"),
    "src/eawf/observability/reflect/tasks.py": (1, "legacy-import-mapping"),
    "src/eawf/observability/telemetry/_VENDOR_PROVENANCE.txt": (1, "historic-store-kind"),
    "src/eawf/observability/telemetry/__init__.py": (2, "keep-closure"),
    "src/eawf/observability/telemetry/aggregator.py": (7, "historic-store-kind"),
    "src/eawf/observability/telemetry/cost_class.py": (2, "historic-store-kind"),
    "src/eawf/observability/telemetry/models.py": (13, "keep-closure"),
    "src/eawf/observability/telemetry/sources/claude_session.py": (1, "historic-store-kind"),
    "src/eawf/observability/telemetry/sources/codex_session.py": (1, "historic-store-kind"),
    "src/eawf/observability/telemetry/sources/dispatch_cost.py": (4, "historic-store-kind"),
    "src/eawf/observability/telemetry/sources/opencode_session.py": (1, "historic-store-kind"),
    "src/eawf/observability/telemetry/store/base.py": (1, "historic-store-kind"),
    "src/eawf/observability/telemetry/turn_cost.py": (44, "historic-store-kind"),
    "src/eawf/platform/install/dist_tag.py": (1, "native-phase"),
    "src/eawf/platform/install/gitignore_writer.py": (1, "keep-closure"),
    "src/eawf/platform/install/steps.py": (4, "historic-store-kind"),
    "src/eawf/platform/lint/clarity_anchors.py": (2, "historic-store-kind"),
    "src/eawf/platform/lint/dispositions.py": (16, "historic-store-kind"),
    "src/eawf/platform/lint/duration_budget.py": (2, "historic-store-kind"),
    "src/eawf/platform/lint/eawf016_title_clarity.py": (2, "historic-store-kind"),
    "src/eawf/platform/lint/eawf024_test_tier_contract.py": (2, "historic-store-kind"),
    "src/eawf/platform/lint/kind_taxonomy.py": (25, "historic-store-kind"),
    "src/eawf/platform/memory/digest.py": (28, "historic-store-kind"),
    "src/eawf/platform/profiles/clarity.py": (7, "historic-store-kind"),
    "src/eawf/platform/profiles/data/python.yaml": (3, "historic-store-kind"),
    "src/eawf/platform/profiles/models.py": (28, "historic-store-kind"),
    "src/eawf/platform/registry/staleness.py": (2, "keep-closure"),
    "src/eawf/platform/render_block.py": (1, "historic-store-kind"),
    "src/eawf/platform/templates/init/__init__.py": (1, "historic-store-kind"),
    "src/eawf/runtime/budget/legacy_notices.py": (1, "legacy-import-mapping"),
    "src/eawf/runtime/budget/notices.py": (1, "historic-store-kind"),
    "src/eawf/runtime/budget/policy.py": (12, "historic-store-kind"),
    "src/eawf/runtime/budget/service.py": (2, "historic-store-kind"),
    "src/eawf/runtime/control/reducer.py": (2, "native-phase"),
    "src/eawf/runtime/daemon/batch_close.py": (1, "historic-store-kind"),
    "src/eawf/runtime/daemon/budget_interlock.py": (9, "historic-store-kind"),
    "src/eawf/runtime/daemon/bus.py": (1, "native-phase"),
    "src/eawf/runtime/daemon/dispatch_runner.py": (5, "historic-store-kind"),
    "src/eawf/runtime/daemon/dispatch_stream.py": (4, "native-phase"),
    "src/eawf/runtime/daemon/gate_execution.py": (2, "historic-store-kind"),
    "src/eawf/runtime/daemon/host_calls.py": (4, "native-phase"),
    "src/eawf/runtime/daemon/limits.py": (1, "historic-store-kind"),
    "src/eawf/runtime/daemon/main.py": (1, "historic-store-kind"),
    "src/eawf/runtime/daemon/methods/__init__.py": (3, "keep-closure"),
    "src/eawf/runtime/daemon/methods/config.py": (5, "historic-store-kind"),
    "src/eawf/runtime/daemon/methods/delivery_proof.py": (2, "historic-store-kind"),
    "src/eawf/runtime/daemon/methods/event.py": (1, "native-phase"),
    "src/eawf/runtime/daemon/methods/host_question.py": (8, "native-phase"),
    "src/eawf/runtime/daemon/methods/host_subagent.py": (10, "native-phase"),
    "src/eawf/runtime/daemon/methods/host_tool.py": (3, "native-phase"),
    "src/eawf/runtime/daemon/methods/permission.py": (1, "native-phase"),
    "src/eawf/runtime/daemon/methods/run.py": (14, "native-phase"),
    "src/eawf/runtime/daemon/methods/run_budget.py": (15, "native-phase"),
    "src/eawf/runtime/daemon/semantic_gateway.py": (10, "native-phase"),
    "src/eawf/runtime/daemon/session.py": (12, "historic-store-kind"),
    "src/eawf/runtime/daemon/spend.py": (1, "native-phase"),
    "src/eawf/runtime/daemon/tool_events.py": (12, "native-phase"),
    "src/eawf/runtime/hooks/event.py": (20, "historic-store-kind"),
    "src/eawf/runtime/hooks/host_calls.py": (6, "native-phase"),
    "src/eawf/runtime/integration/recovery.py": (36, "native-phase"),
    "src/eawf/runtime/runtimes/adapter.py": (8, "historic-store-kind"),
    "src/eawf/runtime/runtimes/claude/runtime_counters.py": (1, "historic-store-kind"),
    "src/eawf/runtime/runtimes/claude/transcript_counters.py": (6, "historic-store-kind"),
    "src/eawf/runtime/runtimes/codex/adapter.py": (2, "historic-store-kind"),
    "src/eawf/runtime/runtimes/codex/app_server.py": (2, "native-phase"),
    "src/eawf/runtime/runtimes/fallback.py": (4, "historic-store-kind"),
    "src/eawf/runtime/runtimes/host_transcript.py": (2, "native-phase"),
    "src/eawf/runtime/runtimes/metering.py": (14, "historic-store-kind"),
    "src/eawf/runtime/runtimes/opencode/adapter.py": (2, "historic-store-kind"),
    "src/eawf/runtime/runtimes/transcript_items.py": (7, "native-phase"),
    "src/eawf/runtime/sandbox/policy.py": (5, "keep-closure"),
    "src/eawf/runtime/vcs/checkpoint.py": (7, "historic-store-kind"),
    "src/eawf/schemas/state.schema.json": (16, "historic-store-kind"),
    "src/eawf/surfaces/cli/commands/clone_repo.py": (4, "historic-store-kind"),
    "src/eawf/surfaces/cli/commands/hook.py": (19, "historic-store-kind"),
    "src/eawf/surfaces/cli/commands/init.py": (4, "historic-store-kind"),
    "src/eawf/surfaces/cli/commands/lifecycle.py": (19, "historic-store-kind"),
    "src/eawf/surfaces/cli/commands/mcp_grants.py": (1, "historic-store-kind"),
    "src/eawf/surfaces/cli/commands/memory_read.py": (6, "historic-store-kind"),
    "src/eawf/surfaces/cli/commands/metrics.py": (2, "historic-store-kind"),
    "src/eawf/surfaces/cli/commands/repo.py": (4, "historic-store-kind"),
    "src/eawf/surfaces/cli/errors.py": (1, "keep-closure"),
    "src/eawf/surfaces/cli/help_panels.py": (5, "historic-store-kind"),
    "src/eawf/surfaces/render/agents_md.py": (3, "historic-store-kind"),
    "src/eawf/surfaces/render/envelope.py": (4, "keep-closure"),
    "src/eawf/surfaces/render/hooks.py": (18, "historic-store-kind"),
    "src/eawf/surfaces/render/link_wrap.py": (20, "historic-store-kind"),
    "src/eawf/surfaces/render/snapshot_normalize.py": (7, "native-phase"),
    "src/eawf/surfaces/tui/chassis/offline.py": (39, "historic-store-kind"),
    "src/eawf/surfaces/tui/chassis/sigils.py": (15, "historic-store-kind"),
    "src/eawf/surfaces/tui/chassis/state_binding.py": (2, "historic-store-kind"),
    "src/eawf/surfaces/tui/chassis/status_tint.py": (9, "historic-store-kind"),
    "src/eawf/surfaces/tui/console/coverage_grid.py": (15, "historic-store-kind"),
    "src/eawf/surfaces/tui/console/data/chrome.json": (4, "historic-store-kind"),
    "src/eawf/surfaces/tui/console/normalisation.py": (2, "native-phase"),
    "src/eawf/surfaces/tui/console/overlays/mutation_card.py": (2, "native-phase"),
    "src/eawf/surfaces/tui/console/overlays/question.py": (2, "flag-day-retirement"),
    "src/eawf/surfaces/tui/console/renderers/history.py": (1, "legacy-import-mapping"),
    "src/eawf/surfaces/tui/console/renderers/run_detail.py": (4, "native-phase"),
    "src/eawf/surfaces/tui/console/renderers/task_detail.py": (1, "flag-day-retirement"),
    "src/eawf/surfaces/tui/console/renderers/track.py": (1, "flag-day-retirement"),
    "src/eawf/workflow/__init__.py": (1, "historic-store-kind"),
    "src/eawf/workflow/audit_dsl/kinds/criterion_in_diff.py": (5, "historic-store-kind"),
    "src/eawf/workflow/audit_dsl/kinds/jury_calibrated.py": (10, "historic-store-kind"),
    "src/eawf/workflow/audit_dsl/kinds/transition_coverage.py": (57, "historic-store-kind"),
    "src/eawf/workflow/audit_dsl/models.py": (14, "historic-store-kind"),
    "src/eawf/workflow/audit_dsl/registry.py": (6, "historic-store-kind"),
    "src/eawf/workflow/audit_dsl/transition_tables.py": (25, "historic-store-kind"),
    "src/eawf/workflow/dispatch/retry.py": (2, "historic-store-kind"),
    "src/eawf/workflow/dispatch/routing.py": (11, "historic-store-kind"),
    "src/eawf/workflow/dispatch/seed.py": (1, "historic-store-kind"),
    "src/eawf/workflow/estimation/calibration.py": (3, "historic-store-kind"),
    "src/eawf/workflow/estimation/mapping.py": (7, "historic-store-kind"),
    "src/eawf/workflow/estimation/metrics.py": (3, "historic-store-kind"),
    "src/eawf/workflow/evidence/evibound.py": (5, "historic-store-kind"),
    "src/eawf/workflow/evidence/measured_contract.py": (4, "historic-store-kind"),
    "src/eawf/workflow/evidence/resolve.py": (9, "historic-store-kind"),
    "src/eawf/workflow/evidence/rung2.py": (1, "historic-store-kind"),
    "src/eawf/workflow/evidence/rung4.py": (3, "historic-store-kind"),
    "src/eawf/workflow/propose/generator.py": (1, "historic-store-kind"),
    "src/eawf/workflow/release/lifecycle.py": (3, "historic-store-kind"),
    "src/eawf/workflow/skills/bodies/__init__.py": (4, "retired-key-message"),
    "src/eawf/workflow/skills/bodies/audit.py": (6, "retired-key-message"),
    "src/eawf/workflow/skills/bodies/flow.py": (3, "retired-key-message"),
    "src/eawf/workflow/skills/bodies/review.py": (3, "retired-key-message"),
    "src/eawf/workflow/skills/bodies/ship.py": (3, "retired-key-message"),
    "src/eawf/workflow/skills/catalog.py": (1, "retired-key-message"),
    "src/eawf/workflow/skills/census.py": (11, "retired-key-message"),
    "src/eawf/workflow/skills/engine.py": (6, "retired-key-message"),
    "src/eawf/workflow/verify/compile.py": (10, "historic-store-kind"),
    "src/eawf/workflow/verify/models.py": (6, "historic-store-kind"),
    "src/eawf/workflow/verify/readiness.py": (4, "historic-store-kind"),
}


def census_paths(root: Path) -> list[str]:
    """Return the git-tracked ``src/`` files outside migration and upgrade paths.

    Args:
        root: The repository root.

    Returns:
        Repository-relative POSIX paths, in ``git ls-files`` order.

    Raises:
        subprocess.CalledProcessError: *root* is not a git work tree.
    """
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "src"],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    return [
        path
        for path in listed.split("\0")
        if path and "migration" not in path and "upgrade" not in path
    ]


def census(root: Path, paths: list[str], pattern: re.Pattern[str]) -> dict[str, int]:
    """Return the *pattern* match count of each file in *paths* that has one.

    Args:
        root: The repository root the paths are relative to.
        paths: Repository-relative files to scan.
        pattern: The pattern to count.

    Returns:
        Path -> match count, for the files with at least one match.
    """
    counts = {
        path: len(pattern.findall((root / path).read_text(encoding="utf-8"))) for path in paths
    }
    return {path: count for path, count in counts.items() if count}


def unallowed(counts: Mapping[str, int], allowlist: Mapping[str, tuple[int, str]]) -> list[str]:
    """Return one line per file the allowlist does not cover.

    Args:
        counts: Path -> match count, from :func:`census`.
        allowlist: Path -> (ceiling, reason).

    Returns:
        A sorted description of each unlisted file, file over or under its
        ceiling and row whose reason is outside :data:`REASONS`.
    """
    problems: list[str] = []
    for path, count in sorted(counts.items()):
        row = allowlist.get(path)
        if row is None:
            problems.append(f"{path}: {count} matches, no allowlist row")
            continue
        ceiling, reason = row
        if count > ceiling:
            problems.append(f"{path}: {count} matches over the ceiling {ceiling}")
        elif count < ceiling:
            problems.append(
                f"{path}: {count} matches under the ceiling {ceiling}; lower the ceiling"
            )
        if reason not in REASONS:
            problems.append(f"{path}: reason {reason!r} is not one of {sorted(REASONS)}")
    return problems


def stray_native_phase_nouns(root: Path, allowlist: Mapping[str, tuple[int, str]]) -> list[str]:
    """Return one line per ``native-phase`` row that holds another noun.

    Args:
        root: The repository root the paths are relative to.
        allowlist: Path -> (ceiling, reason).

    Returns:
        A sorted description of each such row and the nouns it should not hold.
    """
    problems: list[str] = []
    for path, (_, reason) in sorted(allowlist.items()):
        if reason != "native-phase" or not (root / path).is_file():
            continue
        text = (root / path).read_text(encoding="utf-8")
        stray = sorted({noun for noun in src_nouns(text) if noun.lower() not in NATIVE_PHASE_NOUNS})
        if stray:
            problems.append(f"{path}: native-phase row holds {stray}")
    return problems


def stale_rows(
    allowlist: Mapping[str, tuple[int, str]],
    counts: Mapping[str, int],
    exists: Callable[[str], bool],
) -> list[str]:
    """Return the allowlist rows whose file is gone or holds no match.

    Args:
        allowlist: Path -> (ceiling, reason).
        counts: Path -> match count, from :func:`census`.
        exists: Whether a repository-relative path is a file.

    Returns:
        A sorted description of each stale row.
    """
    problems: list[str] = []
    for path in sorted(allowlist):
        if not exists(path):
            problems.append(f"{path}: listed but missing")
        elif path not in counts:
            problems.append(f"{path}: listed but holds no match")
    return problems


@pytest.fixture(scope="module")
def paths() -> list[str]:
    return census_paths(REPO)


@pytest.fixture(scope="module")
def counts(paths: list[str]) -> dict[str, int]:
    return census(REPO, paths, SRC_NOUN_PATTERN)


# ---- the gates ---------------------------------------------------------------


def test_every_src_epoch1_match_is_allowlisted(counts: dict[str, int]) -> None:
    assert unallowed(counts, SRC_EPOCH1_ALLOWLIST) == []
    assert stray_native_phase_nouns(REPO, SRC_EPOCH1_ALLOWLIST) == []


def test_allowlist_has_no_stale_row(counts: dict[str, int]) -> None:
    assert stale_rows(SRC_EPOCH1_ALLOWLIST, counts, lambda path: (REPO / path).is_file()) == []


def test_no_src_file_spells_an_epoch1_id(paths: list[str]) -> None:
    assert census(REPO, paths, EPOCH1_ID) == {}


# ---- the census is real ------------------------------------------------------


def test_the_census_scans_src_and_skips_migration_paths(paths: list[str]) -> None:
    assert "src/eawf/workflow/skills/census.py" in paths
    assert all(path.startswith("src/") for path in paths)
    assert not [path for path in paths if "migration" in path or "upgrade" in path]


def test_every_ceiling_is_positive() -> None:
    assert [path for path, (ceiling, _) in SRC_EPOCH1_ALLOWLIST.items() if ceiling < 1] == []


# ---- red: each gate fires on a planted defect --------------------------------


def test_an_unlisted_file_with_a_match_reds() -> None:
    assert unallowed({"src/eawf/new.py": 1}, {}) == ["src/eawf/new.py: 1 matches, no allowlist row"]


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (2, ["src/eawf/a.py: 2 matches under the ceiling 3; lower the ceiling"]),
        (3, []),
        (4, ["src/eawf/a.py: 4 matches over the ceiling 3"]),
    ],
    ids=["below", "at", "over"],
)
def test_the_ceiling_is_exact(count: int, expected: list[str]) -> None:
    allowlist = {"src/eawf/a.py": (3, "native-phase")}
    assert unallowed({"src/eawf/a.py": count}, allowlist) == expected


def test_a_reason_outside_the_set_reds() -> None:
    assert unallowed({"src/eawf/a.py": 1}, {"src/eawf/a.py": (1, "because")}) == [
        f"src/eawf/a.py: reason 'because' is not one of {sorted(REASONS)}"
    ]


def test_an_empty_census_needs_no_row() -> None:
    assert unallowed({}, {}) == []
    assert stale_rows({}, {}, lambda _: True) == []


def test_a_row_for_a_missing_file_is_stale() -> None:
    assert stale_rows({"src/eawf/gone.py": (1, "native-phase")}, {}, lambda _: False) == [
        "src/eawf/gone.py: listed but missing"
    ]


def test_a_row_for_a_clean_file_is_stale() -> None:
    assert stale_rows({"src/eawf/clean.py": (1, "native-phase")}, {}, lambda _: True) == [
        "src/eawf/clean.py: listed but holds no match"
    ]


def test_a_planted_epoch1_id_reds(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "planted.py").write_text(
        'EXAMPLE = {"wave_id": "P00-I01-W01"}\n', encoding="utf-8"
    )
    (tmp_path / "src" / "migration_row.py").write_text('ROW = "P01-I02"\n', encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "src"], check=True)

    assert census(tmp_path, census_paths(tmp_path), EPOCH1_ID) == {"src/planted.py": 1}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("P01", 0),
        ("P01-I02", 1),
        ("P01-I02-W03", 1),
        ("P100-I100-W100", 1),
        ("P1-I02", 0),
        ("P<NN>-I<NN>-W<NN>", 0),
        ("EAWF-0461", 0),
    ],
    ids=["empty", "phase", "iter", "wave", "three-digit", "one-digit", "placeholder", "native"],
)
def test_the_id_pattern_boundaries(text: str, expected: int) -> None:
    assert len(EPOCH1_ID.findall(text)) == expected


def test_a_native_phase_row_holding_a_lifecycle_noun_reds(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "# one phase of a control\n# the wave_id\n", encoding="utf-8"
    )
    (tmp_path / "src" / "b.py").write_text("# the phases of a tool call\n", encoding="utf-8")
    allowlist = {"src/a.py": (2, "native-phase"), "src/b.py": (1, "native-phase")}

    assert stray_native_phase_nouns(tmp_path, allowlist) == [
        "src/a.py: native-phase row holds ['wave_id']"
    ]


@pytest.mark.parametrize(
    "text",
    [
        "next(iter(rows))",
        "for row in source.iter_rows(path)",
        "async for env in bus.iter_subscriber_pushes(sub)",
        "ast.iter_child_nodes(node)",
        "pkgutil.iter_modules(path)",
        "iter_agent_reports(root)",
        "policy(gate_phase=True)",
        "release, pre_phase, pre_number",
        "PHASE_ORDER and PHASE_LOCKS",
        "compile_phase_dispositions()",
        "_HOST_TOOL_PHASES",
        "_outcome_belongs_to_the_result_phase",
    ],
    ids=[
        "iter-call",
        "iter-rows",
        "iter-pushes",
        "iter-child-nodes",
        "iter-modules",
        "iter-agent-reports",
        "gate-phase",
        "pre-phase",
        "phase-order-locks",
        "phase-dispositions",
        "host-tool-phases",
        "phase-helper",
    ],
)
def test_the_src_pattern_skips_a_homonym(text: str) -> None:
    assert src_nouns(text) == ()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("next(iter(rows)) for the iter", ("iter",)),
        ("gate_phase=True on the phase_close", ("phase_close",)),
        ("iter_rows then iter_close", ("iter_close",)),
        ("iter_rowsx", ("iter_rowsx",)),
        ("PHASE_ORDER of the waves", ("waves",)),
        ("the iter_record and iter_id", ("iter_record", "iter_id")),
    ],
    ids=["bare-iter", "joined-id", "joined-iter", "longer-name", "plural", "lifecycle-iter-names"],
)
def test_a_lifecycle_noun_beside_a_homonym_still_matches(
    text: str, expected: tuple[str, ...]
) -> None:
    assert src_nouns(text) == expected


def test_census_paths_refuses_a_directory_outside_git(tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        census_paths(tmp_path)
