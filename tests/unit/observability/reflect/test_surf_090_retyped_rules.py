"""The re-typed rule count: which operator turns count, what "the same rule" means, and the triage.

SURF-090: every rule the operator re-types more than a configured threshold within a
release is triaged into a guard, a dispatch default, a lens or a memory row, and staying
in prose is a disposition that must be argued. These tests pin the three pieces the
release preflight stands on: the corpus (a turn is a human-marked prompt or a root Run's
``user`` message, scrubbed), the count (a deterministic token match against the rule
graph and against earlier turns, once per turn), and the triage record's closed shape.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

import pytest
import yaml
from pydantic import ValidationError

from eawf.kernel.runtime.events import MessageSummaryPayload, QuarantineReason, RunEventKind
from eawf.kernel.state.epoch2.run import Run
from eawf.observability.reflect.report import NON_QUOTABLE_MARK
from eawf.observability.reflect.retyped import (
    EXEMPLAR_MAX_CHARS,
    RETYPED_TRIAGE_PATH,
    OperatorTurn,
    RetypedDisposition,
    RetypedRule,
    RetypedTriage,
    RetypedTriageDocument,
    RuleVocabulary,
    count_retyped_rules,
    instruction_tokens,
    load_retyped_triage,
    operator_turns_from_history,
    operator_turns_from_runs,
    resolve_retyped_rule_threshold,
    store_retyped_rows,
)
from eawf.observability.reflect.runs import RunReading
from eawf.observability.reflect.titles import REFLECTION_MARKER
from tests.contract.surfaces.cli._reflect_tree import RUN_PREFIX, event, run_row

T0: Final = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)

PRE_COMMIT: Final = RuleVocabulary(
    subject="eawf.core.vcs.pre-commit",
    title="Run the pre-commit hooks before every commit",
    tokens=instruction_tokens(
        "Run the pre-commit hooks before every commit. Run the pre-commit hooks over the "
        "change before every commit and root-cause each failure; never skip them with "
        "--no-verify."
    ),
)
UV_RUN: Final = RuleVocabulary(
    subject="eawf.craft.python.uv-run",
    title="Invoke Python through the project runner",
    tokens=instruction_tokens(
        "Invoke Python through the project runner. Invoke every Python command through "
        "uv run, never through a bare interpreter or a path into the virtual environment."
    ),
)


def _turn(minutes: int, text: str) -> OperatorTurn:
    return OperatorTurn(at=T0 + timedelta(minutes=minutes), text=text)


def _prompt(text: Any, *, minutes: int = 0, **fields: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": "user",
        "isSidechain": False,
        "timestamp": (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
        "origin": {"kind": "human"},
        "promptSource": "typed",
        "message": {"role": "user", "content": text},
    }
    return record | fields


def _history(tmp_path: Path, records: list[dict[str, Any] | str]) -> Path:
    path = tmp_path / "session.jsonl"
    lines = [
        row if isinstance(row, str) else json.dumps(row, separators=(",", ":")) for row in records
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _message(
    key: str,
    sequence: int,
    text: str,
    *,
    role: Literal["user", "assistant"] = "user",
    **fields: Any,
) -> Any:
    return event(
        key,
        sequence,
        event_kind=RunEventKind.MESSAGE_SUMMARIZED,
        payload=MessageSummaryPayload(message_role=role, summary=text),
        **fields,
    )


def _reading(key: str, events: list[Any], *, parent: str | None = None) -> RunReading:
    row = run_row(key)
    if parent is not None:
        row["parent_run_ref"] = f"{RUN_PREFIX}/{parent}"
    return RunReading(run=Run.model_validate(row), events=tuple(events), canonical_sequence=1)


# ---- the tokens a sentence is compared in ----------------------------------------------


def test_surf_090_tokens_of_empty_text_are_empty() -> None:
    assert instruction_tokens("") == frozenset()


def test_surf_090_tokens_drop_stop_words_short_words_and_redaction_placeholders() -> None:
    assert instruction_tokens("Do it in <local-path> and the <host> now") == frozenset({"now"})


def test_surf_090_tokens_fold_case_plural_and_tense() -> None:
    assert instruction_tokens("Verified CLAIMS, verifying claim") == frozenset({"verify", "claim"})


def test_surf_090_a_short_word_keeps_its_suffix() -> None:
    assert instruction_tokens("uses runs hooks") == frozenset({"uses", "runs", "hook"})


# ---- which history records are operator turns ------------------------------------------


def test_surf_090_a_human_marked_prompt_is_a_turn(tmp_path: Path) -> None:
    turns = operator_turns_from_history(_history(tmp_path, [_prompt("Run the hooks first.")]))

    assert turns == (OperatorTurn(at=T0, text="Run the hooks first."),)


@pytest.mark.parametrize(
    ("label", "record"),
    [
        ("peer message without origin", _prompt("Another session sent this", origin=None)),
        ("task notification", _prompt("Done", origin={"kind": "task-notification"})),
        ("sidechain", _prompt("A dispatched prompt", isSidechain=True)),
        ("harness metadata", _prompt("Base directory for this skill", isMeta=True)),
        ("harness-wrapped command", _prompt("<command-name>/clear</command-name>")),
        ("interruption marker", _prompt("[Request interrupted by user]")),
        ("reflection helper", _prompt(f"{REFLECTION_MARKER} write a title")),
        ("tool result", _prompt([{"type": "tool_result", "content": "ok"}])),
        ("assistant", _prompt("Sure", type="assistant")),
        ("no timestamp", _prompt("Run the hooks first.", timestamp=None)),
        ("naive timestamp", _prompt("Run the hooks first.", timestamp="2026-09-20T09:00:00")),
        ("bad timestamp", _prompt("Run the hooks first.", timestamp="yesterday")),
        ("non-text content", _prompt({"unexpected": "shape"})),
    ],
)
def test_surf_090_a_record_the_operator_did_not_type_is_no_turn(
    tmp_path: Path, label: str, record: dict[str, Any]
) -> None:
    assert operator_turns_from_history(_history(tmp_path, [record])) == (), label


def test_surf_090_malformed_lines_are_skipped_not_raised(tmp_path: Path) -> None:
    path = _history(
        tmp_path,
        ['{"type":"user", broken', '["type":"user"]', _prompt("Keep this one.", minutes=1)],
    )

    assert [turn.text for turn in operator_turns_from_history(path)] == ["Keep this one."]


def test_surf_090_a_missing_history_file_reads_as_no_turns(tmp_path: Path) -> None:
    assert operator_turns_from_history(tmp_path / "absent.jsonl") == ()


def test_surf_090_text_blocks_join_and_images_drop(tmp_path: Path) -> None:
    content = [
        {"type": "text", "text": "First line."},
        {"type": "image", "source": {}},
        {"type": "text", "text": "Second line."},
    ]

    turns = operator_turns_from_history(_history(tmp_path, [_prompt(content)]))

    assert [turn.text for turn in turns] == ["First line.\nSecond line."]


def test_surf_090_a_turn_is_scrubbed_before_it_is_kept(tmp_path: Path) -> None:
    private = Path.home() / "private" / "notes.txt"
    raw = f"Never write {private} or mail someone@corp.example."

    (turn,) = operator_turns_from_history(_history(tmp_path, [_prompt(raw)]))

    assert str(Path.home()) not in turn.text
    assert "someone@corp.example" not in turn.text
    assert "<local-path>" in turn.text


# ---- which Run messages are operator turns ---------------------------------------------


def test_surf_090_a_root_runs_user_message_is_a_turn() -> None:
    reading = _reading("RUN-00000001", [_message("RUN-00000001", 1, "Run the hooks first.")])

    (turn,) = operator_turns_from_runs([reading])

    assert turn.text == "Run the hooks first."
    assert turn.at == T0 + timedelta(seconds=1)


def test_surf_090_the_observed_time_wins_over_the_recorded_time() -> None:
    observed = T0 - timedelta(hours=1)
    reading = _reading(
        "RUN-00000001",
        [_message("RUN-00000001", 1, "Run the hooks first.", observed_at=observed)],
    )

    assert operator_turns_from_runs([reading])[0].at == observed


def test_surf_090_run_messages_the_operator_did_not_type_are_no_turns() -> None:
    root = "RUN-00000001"
    reading = _reading(
        root,
        [
            _message(root, 1, "I will run the hooks.", role="assistant"),
            _message(root, 2, "Late line.", quarantine=QuarantineReason.LATE_AFTER_TERMINAL),
            _message(root, 3, f"{REFLECTION_MARKER} title this"),
            event(root, 4),
        ],
    )
    child = _reading("RUN-00000002", [_message("RUN-00000002", 1, "Parent's prompt.")], parent=root)

    assert operator_turns_from_runs([reading, child]) == ()


def test_surf_090_no_runs_read_as_no_turns() -> None:
    assert operator_turns_from_runs([]) == ()


# ---- what "the same rule, re-typed" means ----------------------------------------------


def test_surf_090_no_turns_count_nothing() -> None:
    assert count_retyped_rules([], [PRE_COMMIT]) == ()


def test_surf_090_a_restated_rule_is_counted_once_per_turn() -> None:
    turns = [
        _turn(0, "Never skip the pre-commit hooks. Run the pre-commit hooks, never skip them."),
        _turn(5, "Please run pre-commit hooks before the commit."),
        _turn(9, "Unrelated: rename the dashboard widget."),
    ]

    rows = count_retyped_rules(turns, [PRE_COMMIT, UV_RUN])

    rule_rows = [row for row in rows if row.rule_title is not None]
    assert [(row.subject, row.count) for row in rule_rows] == [("eawf.core.vcs.pre-commit", 2)]
    assert rule_rows[0].first_at == T0
    assert rule_rows[0].last_at == T0 + timedelta(minutes=5)
    assert rule_rows[0].exemplar == "Never skip the pre-commit hooks."


def test_surf_090_a_question_is_consultation_not_a_restatement() -> None:
    turns = [_turn(0, "Should we run the pre-commit hooks before every commit?")]

    assert count_retyped_rules(turns, [PRE_COMMIT]) == ()


def test_surf_090_a_sentence_below_the_shared_token_floor_matches_nothing() -> None:
    turns = [_turn(0, "Run hooks."), _turn(1, "Run hooks.")]

    assert count_retyped_rules(turns, [PRE_COMMIT]) == ()


def test_surf_090_a_sentence_mostly_off_the_rules_vocabulary_is_no_restatement() -> None:
    turns = [_turn(0, "Run the hooks, then deploy the staging cluster and rotate dashboards.")]

    rows = count_retyped_rules(turns, [PRE_COMMIT])

    assert all(row.rule_title is None for row in rows)


def test_surf_090_an_instruction_no_rule_states_clusters_with_its_earlier_turns() -> None:
    turns = [
        _turn(0, "Dispatch the waves with parallel worktree subagents."),
        _turn(3, "Dispatch these waves via parallel worktree subagents!"),
        _turn(7, "Dispatch the waves with parallel worktree subagents, please."),
        _turn(8, "Rename the dashboard widget colour palette."),
    ]

    rows = count_retyped_rules(turns, [PRE_COMMIT])

    assert [row.count for row in rows] == [3, 1]
    assert rows[0].subject.startswith("typed:")
    assert rows[0].rule_title is None
    assert rows[0].exemplar == "Dispatch the waves with parallel worktree subagents."


def test_surf_090_a_typed_subject_id_is_stable_across_counts() -> None:
    turns = [_turn(0, "Dispatch the waves with parallel worktree subagents.")]

    first = count_retyped_rules(turns, [])
    second = count_retyped_rules(list(turns), [])

    assert first == second
    assert first[0].subject == second[0].subject


def test_surf_090_a_tie_between_rules_resolves_to_the_lower_rule_id() -> None:
    twin = RuleVocabulary(subject="aaa.twin", title="Twin", tokens=PRE_COMMIT.tokens)
    turns = [_turn(0, "Run the pre-commit hooks, never skip them.")]

    rows = count_retyped_rules(turns, sorted([PRE_COMMIT, twin], key=lambda v: v.subject))

    assert [row.subject for row in rows] == ["aaa.twin"]


def test_surf_090_rows_order_by_count_then_subject() -> None:
    turns = [
        _turn(0, "Invoke python through uv run, never a bare interpreter."),
        _turn(1, "Run the pre-commit hooks, never skip them."),
        _turn(2, "Run the pre-commit hooks, never skip them."),
    ]

    rows = count_retyped_rules(turns, [PRE_COMMIT, UV_RUN])

    assert [row.subject for row in rows] == ["eawf.core.vcs.pre-commit", "eawf.craft.python.uv-run"]


def test_surf_090_an_exemplar_is_cut_to_the_maximum_and_marked_non_quotable() -> None:
    long_sentence = "Dispatch " + " ".join(f"worker{index}" for index in range(80)) + "."

    (row,) = count_retyped_rules([_turn(0, long_sentence)], [])

    assert len(row.exemplar) == EXEMPLAR_MAX_CHARS
    assert row.mark == NON_QUOTABLE_MARK


# ---- the triage record -----------------------------------------------------------------


def test_surf_090_argued_prose_carries_its_argument() -> None:
    row = RetypedDisposition(
        subject="typed:0123456789ab", disposition="argued_prose", argument="Context only."
    )

    assert row.argument == "Context only."


@pytest.mark.parametrize(
    "fields",
    [
        {"disposition": "argued_prose"},
        {"disposition": "argued_prose", "argument": "   "},
        {"disposition": "argued_prose", "argument": "Why.", "reference": "x"},
        {"disposition": "guard"},
        {"disposition": "memory_row", "reference": "note", "argument": "Why."},
        {"disposition": "prose", "argument": "Why."},
        {"disposition": "lens", "reference": "lens-a", "owner": "someone"},
        {"disposition": "guard", "reference": 7},
    ],
)
def test_surf_090_a_disposition_with_the_wrong_backing_is_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RetypedDisposition.model_validate({"subject": "eawf.core.vcs.pre-commit"} | fields)


def test_surf_090_a_subject_triaged_twice_is_refused() -> None:
    row = {"subject": "s", "disposition": "lens", "reference": "lens-a"}

    with pytest.raises(ValidationError, match="more than once"):
        RetypedTriageDocument.model_validate({"schema_version": 1, "dispositions": [row, row]})


def test_surf_090_an_unknown_triage_schema_version_is_refused() -> None:
    with pytest.raises(ValidationError):
        RetypedTriageDocument.model_validate({"schema_version": 2})


def test_surf_090_a_repository_without_a_triage_has_triaged_nothing(tmp_path: Path) -> None:
    assert load_retyped_triage(tmp_path).dispositions == ()


def test_surf_090_the_committed_triage_loads_through_the_closed_model(tmp_path: Path) -> None:
    path = tmp_path / RETYPED_TRIAGE_PATH
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "dispositions": [
                    {
                        "subject": "eawf.core.vcs.pre-commit",
                        "disposition": "guard",
                        "reference": "hook",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    (row,) = load_retyped_triage(tmp_path).dispositions

    assert (row.disposition, row.reference) == ("guard", "hook")


def test_surf_090_a_malformed_committed_triage_is_refused(tmp_path: Path) -> None:
    path = tmp_path / RETYPED_TRIAGE_PATH
    path.parent.mkdir(parents=True)
    path.write_text("schema_version: 1\nextra: true\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_retyped_triage(tmp_path)


def test_surf_090_untriaged_is_the_over_threshold_rows_without_a_disposition() -> None:
    rows = tuple(
        RetypedRule(
            subject=subject, rule_title=None, count=4, first_at=T0, last_at=T0, exemplar="x"
        )
        for subject in ("a", "b")
    )
    disposition = RetypedDisposition(subject="a", disposition="lens", reference="lens-a")

    triage = RetypedTriage(threshold=3, since=None, over=rows, dispositions={"a": disposition})

    assert [row.subject for row in triage.untriaged] == ["b"]


# ---- the configured threshold ----------------------------------------------------------


def test_surf_090_the_threshold_defaults_to_three(tmp_path: Path) -> None:
    assert resolve_retyped_rule_threshold(tmp_path) == 3


def test_surf_090_the_repository_layer_sets_the_threshold(tmp_path: Path) -> None:
    (tmp_path / ".ea").mkdir()
    (tmp_path / ".ea" / "config.yaml").write_text(
        "verify:\n  retyped_rule_threshold: 5\n", encoding="utf-8"
    )

    assert resolve_retyped_rule_threshold(tmp_path) == 5


@pytest.mark.parametrize("value", ["0", "-1", "many"])
def test_surf_090_a_threshold_below_one_or_not_an_integer_is_refused(
    tmp_path: Path, value: str
) -> None:
    (tmp_path / ".ea").mkdir()
    (tmp_path / ".ea" / "config.yaml").write_text(
        f"verify:\n  retyped_rule_threshold: {value}\n", encoding="utf-8"
    )

    with pytest.raises(ValidationError):
        resolve_retyped_rule_threshold(tmp_path)


# ---- the local counted rows ------------------------------------------------------------


def test_surf_090_counted_rows_are_written_only_to_the_local_reflection_collection(
    tmp_path: Path,
) -> None:
    row = RetypedRule(
        subject="typed:0123456789ab",
        rule_title=None,
        count=4,
        first_at=T0,
        last_at=T0,
        exemplar="Dispatch the waves.",
    )

    path = store_retyped_rows(tmp_path / ".ea", [row], today=date(2026, 9, 29))

    assert path == tmp_path / ".ea" / "local" / "reflect" / "2026-09-29-reflect-retyped.json"
    (stored,) = json.loads(path.read_text(encoding="utf-8"))
    assert stored["mark"] == NON_QUOTABLE_MARK
    assert RetypedRule.model_validate(stored) == row


def test_surf_090_no_counted_rows_write_an_empty_list(tmp_path: Path) -> None:
    path = store_retyped_rows(tmp_path / ".ea", [], today=date(2026, 9, 29))

    assert json.loads(path.read_text(encoding="utf-8")) == []
