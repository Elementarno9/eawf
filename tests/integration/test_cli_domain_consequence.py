"""CON-060: a lifecycle command prints its consequence before anything is sent.

The command line keeps the console card's ordering. Every per-entity lifecycle command
prints the consequence block -- the target at the exact revision it names, the effects
and the non-effects -- before the daemon is called; ``--dry-run`` prints only that block
and never calls the daemon, so nothing canonical or external is recorded. The daemon is a
recording stand-in, so a call that should not happen is seen not to.
"""

from __future__ import annotations

from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain as domain_cmd
from eawf.surfaces.cli.commands.domain_consequence import block_text, consequence_block
from tests.integration.test_cli_domain_lifecycle import (
    _BATCH_URN,
    _TASK_URN,
    _VERB_ROWS,
    _accepted,
    _base_args,
    _FakeClient,
    _install,
    _no_escalate,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_daemon_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the escalation gate from spawning a real daemon, and start with no calls."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", _no_escalate)
    _FakeClient.calls = []


@pytest.mark.parametrize(("verb", "method", "revision_flag", "urn"), _VERB_ROWS)
def test_con_060_every_lifecycle_command_dry_runs_without_calling_the_daemon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verb: list[str],
    method: str,
    revision_flag: str,
    urn: str,
) -> None:
    _install(monkeypatch, result=_accepted(method, urn))
    args = ["--workspace", str(tmp_path), *_base_args(verb, revision_flag, urn), "--dry-run"]
    if method == domain_cmd.TASK_COMPLETE:
        pytest.skip("task complete reads an assessment file before it can be addressed")
    result = runner.invoke(app, args)
    assert result.exit_code == exit_codes.OK, result.output
    assert _FakeClient.calls == []
    assert f"consequence: {method} {urn} at revision 3 · exact" in result.stdout
    assert "dry run · nothing was sent" in result.stdout


def test_con_060_the_consequence_prints_before_the_daemons_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, result=_accepted(domain_cmd.BATCH_ACTIVATE, _BATCH_URN))
    args = [
        "--workspace",
        str(tmp_path),
        *_base_args(["batch", "activate"], "--expected-batch-revision", _BATCH_URN),
        "--yes",
    ]
    result = runner.invoke(app, args)
    assert result.exit_code == exit_codes.OK, result.output
    printed = result.output.index("consequence: domain.batch.activate")
    answered = result.output.index(f"{domain_cmd.BATCH_ACTIVATE} ok {_BATCH_URN}")
    assert printed < answered
    assert "  not: no task is claimed or dispatched" in result.output
    assert len(_FakeClient.calls) == 1


def test_con_060_a_json_dry_run_answers_the_block_and_sent_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, result=_accepted(domain_cmd.TASK_CLAIM, _TASK_URN))
    args = [
        "--json",
        "--workspace",
        str(tmp_path),
        *_base_args(["task", "claim"], "--expected-task-revision", _TASK_URN),
        "--dry-run",
    ]
    result = runner.invoke(app, args)
    assert result.exit_code == exit_codes.OK, result.output
    payload = orjson.loads(result.stdout)["result"]
    assert payload["sent"] is False
    assert payload["consequence"]["target"] == _TASK_URN
    assert payload["consequence"]["revision"] == 3
    assert payload["consequence"]["not"] == ["no run is started", "the batch does not move"]
    assert _FakeClient.calls == []


def test_con_060_the_block_names_target_revision_effects_and_non_effects() -> None:
    text = block_text(consequence_block(domain_cmd.TASK_CLAIM, _TASK_URN, 3))
    assert (
        text.splitlines()[0] == f"consequence: domain.task.claim {_TASK_URN} at revision 3 · exact"
    )
    assert "  effect: task " in text
    assert "  not: no run is started" in text
    assert "  if stale: if revision 3 moves before you confirm" in text


def test_con_060_a_create_states_its_consequence_at_the_tree_cursor() -> None:
    text = block_text(consequence_block("domain.task.create", _TASK_URN, 0))
    assert (
        text.splitlines()[0] == f"consequence: domain.task.create {_TASK_URN} at revision 0 · exact"
    )
    assert "  effect: a new task record is admitted from the create document" in text
    assert "  not: no existing record moves" in text


def test_con_060_an_unknown_write_has_no_block() -> None:
    with pytest.raises(KeyError):
        consequence_block("domain.task.teleport", _TASK_URN, 3)


_ACTION_URN = "eawf://WS-CANARY/PRJ-CANARY/REP-CANARY/pending-action/ACT-0001"
_PERMISSION_URN = "eawf://WS-CANARY/PRJ-CANARY/REP-CANARY/permission/PERM-0001"
_RUN_URN = "eawf://WS-CANARY/PRJ-CANARY/REP-CANARY/run/RUN-00000001"
_QUESTION_URN = "eawf://WS-CANARY/PRJ-CANARY/REP-CANARY/question/QST-0001"
_ANCHORED = ["--expected-revision", "3", "--actor", "OPERATOR"]

#: Every Attention write and Run control the console sends, as its command line spells it.
_ATTENTION_ROWS = [
    (["action", "snooze", _ACTION_URN, *_ANCHORED, "--idempotency-key", "k-1"], "snooze"),
    (
        ["action", "assign", _ACTION_URN, *_ANCHORED, "--idempotency-key", "k-1", "--to", "OP-2"],
        "assign",
    ),
    (["action", "decide-permission", _PERMISSION_URN, *_ANCHORED, "--verb", "deny"], "decide"),
    (["action", "notice", "NTC-1", *_ANCHORED, "--disposition", "acknowledge"], "dispose"),
    (["question", "reply", _QUESTION_URN, *_ANCHORED, "--option-key", "yes"], "answer"),
    (
        ["run", "interrupt", _RUN_URN, "--actor", "OPERATOR", "--idempotency-key", "CTL-1"],
        "request",
    ),
    (["run", "cancel", _RUN_URN, "--actor", "OPERATOR", "--idempotency-key", "CTL-1"], "request"),
    (
        ["run", "reconcile", _RUN_URN, "--actor", "OPERATOR", "--idempotency-key", "CTL-1"],
        "request",
    ),
    *(
        (
            ["run", f"{verb}-dispatch", "--actor", "OPERATOR", "--idempotency-key", "DSP-1"],
            "request",
        )
        for verb in ("pause", "drain", "resume")
    ),
]


@pytest.mark.parametrize(("args", "verb"), _ATTENTION_ROWS)
def test_con_060_every_attention_write_dry_runs_its_consequence_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args: list[str], verb: str
) -> None:
    _install(monkeypatch, result={})
    result = runner.invoke(app, ["--workspace", str(tmp_path), *args, "--dry-run"])
    assert result.exit_code == exit_codes.OK, result.output
    assert _FakeClient.calls == []
    assert result.stdout.startswith("consequence: ")
    assert f".{verb} " in result.stdout.splitlines()[0]
    assert "dry run · nothing was sent" in result.stdout


def test_con_060_a_snooze_prints_its_consequence_before_it_is_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answer = {"action_ref": _ACTION_URN, "status": "WAITING", "revision": 3, "created": True}
    _install(monkeypatch, result={**answer, "reason": "hidden for OPERATOR only"})
    args = ["--workspace", str(tmp_path), *_ATTENTION_ROWS[0][0], "--yes"]
    result = runner.invoke(app, args)
    assert result.exit_code == exit_codes.OK, result.output
    [(method, params)] = _FakeClient.calls
    assert method == "runtime.pending_action.snooze"
    assert (params["expected_revision"], params["actor"]) == (3, "OPERATOR")
    assert result.output.index("consequence: runtime.pending_action.snooze") < result.output.index(
        "hidden for OPERATOR only"
    )
