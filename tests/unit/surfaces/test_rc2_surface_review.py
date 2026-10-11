"""The rc2 review's surface findings: status, the ledger-history gate, runtime and campaign verbs.

Each test reds on the defect the review found: a torn decision ledger that
aborted ``eawf status``, an exit-code list that named codes the CLI never
exits with, a gate that raised on a snapshot folded back into its ledger, a
canary walk sent with no principal to record it against, and a Campaign
request that minted a fresh retry key on every attempt. The verbs are called
as functions with a stand-in context, so no CLI runner or git process runs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import typer

from eawf.kernel.store.ledger import SNAPSHOT_DIRNAME, snapshot_name
from eawf.platform.lint import ledger_history
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.commands import domain, research, runtime, status
from eawf.surfaces.cli.flags import GlobalFlags
from tests._epoch2_helpers import lay_epoch2_tree
from tests.integration.test_cli_status import _VALID_STATE, _decision_ledger, _stub_no_git


def _ctx(*, json_output: bool = True, workspace: Path | None = None) -> typer.Context:
    return cast(
        typer.Context,
        SimpleNamespace(obj=GlobalFlags(json_output=json_output, workspace=workspace)),
    )


# ---------- eawf status ----------


def _status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, torn: bool, json_output: bool
) -> None:
    state_path = lay_epoch2_tree(tmp_path, state=_VALID_STATE)
    if torn:
        ledger = _decision_ledger(state_path)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text('{"schema_version":"2.0"')
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    status.status(_ctx(json_output=json_output), workspace=None, json_output=json_output)


def test_status_prints_the_unreadable_decision_ledger_as_a_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _status(tmp_path, monkeypatch, torn=True, json_output=False)
    out = capsys.readouterr().out
    assert "recent decisions: none" in out
    assert "blockers: decision ledger unreadable" in out


def test_status_with_a_readable_empty_ledger_has_no_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _status(tmp_path, monkeypatch, torn=False, json_output=True)
    assert json.loads(capsys.readouterr().out)["blockers"] == []


def test_status_documents_only_exit_codes_the_cli_defines() -> None:
    assert status.__doc__ is not None
    documented = re.findall(r"``(\d)`` \(``([A-Z_]+)``\)", status.__doc__)
    assert documented, "the exit-code list is missing"
    assert [(int(code), name) for code, name in documented] == [
        (int(code), exit_codes.name_for(int(code))) for code, _ in documented
    ]


# ---------- the ledger-history gate ----------

_LIVE = ".ea/generations/gen-x/ledger/task.jsonl"
_SEALED, _TAIL = b'{"a":1}\n', b'{"b":2}\n'
_SNAPSHOT = f".ea/generations/gen-x/ledger/{SNAPSHOT_DIRNAME}/{snapshot_name('task', _SEALED)}"


def _over(
    monkeypatch: pytest.MonkeyPatch, *, before: dict[str, bytes], after: dict[str, bytes]
) -> tuple[list[Any], Any]:
    """Run the gate's one-ledger check over two in-memory trees, ``old`` and ``tip``."""
    trees = {"old": before, "tip": after}

    def blob(_repo: Path, rev: str | None, path: str) -> bytes:
        return trees[str(rev)].get(path, b"")

    def snapshots(_repo: Path, rev: str | None, live: str) -> dict[str, bytes]:
        return {p: b for p, b in trees[str(rev)].items() if p != live}

    monkeypatch.setattr(ledger_history, "_blob", blob)
    monkeypatch.setattr(ledger_history, "_snapshots_at", snapshots)
    return ledger_history._append_only_over(Path("."), "old", "tip", _LIVE)


_SEALED_TREE = {_SNAPSHOT: _SEALED, _LIVE: _TAIL}


def test_gate_reads_a_snapshot_folded_back_into_its_ledger_as_no_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folded = {_LIVE: _SEALED + _TAIL + b'{"c":3}\n'}
    assert _over(monkeypatch, before=_SEALED_TREE, after=folded) == ([], None)


def test_gate_still_reds_on_a_fold_back_that_drops_a_sealed_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    findings, sealed = _over(monkeypatch, before=_SEALED_TREE, after={_LIVE: _TAIL})
    assert len(findings) == 1
    assert sealed is None


def test_gate_still_reports_a_seal_with_its_one_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    findings, sealed = _over(monkeypatch, before={_LIVE: _SEALED + _TAIL}, after=_SEALED_TREE)
    assert findings == []
    assert (sealed.snapshot, sealed.sealed_bytes) == (_SNAPSHOT, len(_SEALED))


# ---------- eawf runtime certify ----------


@pytest.fixture
def certify_sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def send(_ctx: Any, method: str, params: dict[str, Any], **_options: Any) -> None:
        assert method == runtime.RUNTIME_CERTIFY
        calls.append(params)

    monkeypatch.setattr(runtime, "_send", send)
    return calls


def test_certify_refuses_a_task_without_an_actor_before_the_daemon(
    certify_sent: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(typer.Exit) as exited:
        runtime.runtime_certify_cmd(_ctx(), "claude-code", task="EAWF-0001", actor=None)
    assert exited.value.exit_code == exit_codes.USER_ERROR
    assert "--task needs --actor" in json.loads(capsys.readouterr().out)["message"]
    assert certify_sent == []


@pytest.mark.parametrize(
    ("task", "actor"), [(None, None), (None, "OP-0001"), ("EAWF-0001", "OP-0001")]
)
def test_certify_sends_every_accepted_combination(
    certify_sent: list[dict[str, Any]], task: str | None, actor: str | None
) -> None:
    runtime.runtime_certify_cmd(_ctx(), "claude-code", task=task, actor=actor)
    expected = {"runtime": "claude-code", "task": task, "actor": actor}
    assert certify_sent == [{k: v for k, v in expected.items() if v is not None}]


# ---------- eawf campaign new ----------

_TRACK = "eawf://WSP-MAIN/PRJ-QR/REP-QR/track/TRK-0001"


@pytest.fixture
def campaign_sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def answer(_method: str, params: dict[str, Any], **_options: Any) -> dict[str, Any]:
        calls.append(params)
        return {"record": {"key": "CAM-0001", "plan_steps": [{}]}, "driving": False}

    monkeypatch.setattr(domain, "_native_answer", answer)
    return calls


def _key(sent: list[dict[str, Any]], tmp_path: Path, **options: Any) -> str:
    fields: dict[str, Any] = {"question": ["Is the WAL fsynced?"]} | options
    research.campaign_new(
        _ctx(workspace=tmp_path), "Establish replay order", "OP-0001", _TRACK, **fields
    )
    key: str = sent[-1]["idempotency_key"]
    return key


def test_campaign_new_retried_unchanged_sends_one_key(
    campaign_sent: list[dict[str, Any]], tmp_path: Path
) -> None:
    first = _key(campaign_sent, tmp_path)
    assert first == _key(campaign_sent, tmp_path)
    assert first.startswith("campaign-new-")


@pytest.mark.parametrize(
    "changed",
    [
        {"question": ["Is the WAL fsynced twice?"]},
        {"question": ["Is the WAL fsynced?", "Which restarts reorder?"]},
        {"depth": "deep"},
        {"budget_rounds": 1},
        {"run": True},
    ],
)
def test_campaign_new_keys_a_different_request_apart(
    campaign_sent: list[dict[str, Any]], tmp_path: Path, changed: dict[str, Any]
) -> None:
    assert _key(campaign_sent, tmp_path, **changed) != _key(campaign_sent, tmp_path)


def test_campaign_new_sends_a_named_key_verbatim(
    campaign_sent: list[dict[str, Any]], tmp_path: Path
) -> None:
    assert _key(campaign_sent, tmp_path, idempotency_key="mine-1") == "mine-1"
