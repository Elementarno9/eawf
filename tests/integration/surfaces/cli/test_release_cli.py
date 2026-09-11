"""Every registered ``release.*`` RPC verb is reachable from the CLI.

Nine verbs were registered on the daemon and exactly one of them --
``release.observe_target`` -- had a caller. The rest were substrate: code
with tests and no producer, which is the failure mode this project keeps
rediscovering. A verb an operator cannot invoke is a verb whose params
have never been assembled by anything but a test, so its shape has never
been checked against the way it is actually reached.

The tenth verb, ``release.burn``, arrived the same way from the other
end: the library implemented the terminal burn and exported it, and no
verb anywhere reached it, so the one transition that honestly describes
a spent version was unreachable at the moment it was needed.

Two things are pinned here. The parity test asserts the registered set
and the reachable set are the same set, so an eleventh verb lands broken
rather than lands unreachable. The dispatch tests drive each subcommand
through the real Typer app with a recording client in place of the
daemon, so what is proven is that the argv the operator types produces
the method and params the handler expects -- not that a mapping table
happens to name the right string.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
from typer.testing import CliRunner

import eawf.runtime.daemon.server  # noqa: F401  -- importing it is what registers the methods
from eawf.runtime.daemon.methods import registered_methods
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands.release import RELEASE_RPC_METHODS, release_app

pytestmark = pytest.mark.integration

RELEASE_KEY = "REL-0.7.0.dev1"

PROOF_DIGEST = f"sha256:{'1' * 64}"

MANIFEST_DIGEST = f"sha256:{'c' * 64}"

#: A reply carrying every key any release verb's renderer reads. One
#: shape for all ten keeps the dispatch tests about dispatch: a renderer
#: that reached for a key the handler does not return would still be
#: caught, because the render runs.
FAKE_REPLY: dict[str, Any] = {
    "train_id": "v0.7.0",
    "target_version": "0.7.0",
    "current_checkpoint_index": 0,
    "checkpoint": {"version": "0.7.0.dev1", "release_key": RELEASE_KEY},
    "record": None,
    "readiness": {
        "release_key": RELEASE_KEY,
        "ready": True,
        "waiver_count": 0,
        "signals": [{"signal": "tree_cleanliness", "status": "pass"}],
    },
    "first_red": None,
    "release": {"key": RELEASE_KEY, "status": "approved", "revision": 4},
    "release_record_id": "release_record:0001",
    "measured_contracts": ["MC-01"],
    "operation_ref": "operation://REL-0.7.0.dev1/1",
    "operation": {
        "publication_receipts": [{"target_id": "pypi", "attempt": 1, "status": "in_flight"}]
    },
    "replayed": False,
    "observation": {
        "result": "match",
        "code": "matched",
        "queried_identity": "eawf",
        "evidence_ref": "observation://package_index/pypi/eawf@0.7.0.dev1",
        "detail": "every frozen artifact is exposed at its frozen digest",
    },
    "closed": {"key": RELEASE_KEY, "status": "baked"},
    "opened": {"key": "REL-0.7.0.dev2", "status": "draft"},
    "train": {"current_checkpoint_index": 1},
    "receipt_refs": ["receipt://gate/dev1"],
    "next_status": "candidate",
}


class _RecordingClient:
    """A ``DaemonClient`` stand-in that records the call and replies."""

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    def __enter__(self) -> _RecordingClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record *method* and *params*, then answer the omni-reply."""
        self._calls.append({"method": method, "params": params})
        return FAKE_REPLY


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Route every CLI daemon call into a recording stand-in."""
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "eawf.surfaces.cli._daemon_client.DaemonClient",
        lambda *args, **kwargs: _RecordingClient(recorded),
    )
    return recorded


def _write(path: Path, payload: dict[str, Any]) -> str:
    """Write *payload* as JSON to *path* and return its string path."""
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _record_file(tmp_path: Path, **overrides: Any) -> str:
    """Return the path of a serialized release record for the dev1 key."""
    payload: dict[str, Any] = {"key": RELEASE_KEY, "revision": 4, "status": "approved"}
    payload.update(overrides)
    return _write(tmp_path / "release.json", payload)


def _argv(subcommand: str, tmp_path: Path) -> list[str]:
    """Return a well-formed argv reaching *subcommand*.

    Args:
        subcommand: The ``eawf release`` subcommand to build argv for.
        tmp_path: Directory the JSON documents are written into.

    Returns:
        The full argv, ``eawf`` prefix excluded.

    Raises:
        KeyError: When the subcommand has no argv here, which is how a
            newly wired verb without dispatch coverage reds this module.
    """
    record = _record_file(tmp_path)
    keyed = ["--release", record, "--idempotency-key", f"{subcommand}-01"]
    table: dict[str, list[str]] = {
        "show": ["show", "0.7.0.dev1"],
        "readiness": ["readiness", "0.7.0.dev1", "--release", record],
        "create": ["create", "0.7.0.dev2", "--membership-ref", "bundle://dev2"],
        "approve": [
            "approve",
            RELEASE_KEY,
            "--release",
            record,
            "--readiness",
            _write(tmp_path / "readiness.json", {"release_key": RELEASE_KEY}),
            "--approval-ref",
            "receipt://approval/dev1",
        ],
        "publish": [
            "publish",
            RELEASE_KEY,
            *keyed,
            "--approved-manifest-digest",
            MANIFEST_DIGEST,
            "--proof-digest",
            PROOF_DIGEST,
        ],
        "retry": ["retry", RELEASE_KEY, "--target", "pypi", *keyed, "--proof-digest", PROOF_DIGEST],
        "reconcile": [
            "reconcile",
            RELEASE_KEY,
            "--target",
            "pypi",
            *keyed,
            "--status",
            "reported_success",
        ],
        "observe": [
            "observe",
            RELEASE_KEY,
            "--target",
            "pypi",
            *keyed,
            "--manifest",
            _write(tmp_path / "manifest.json", {"version": "0.7.0.dev1"}),
        ],
        "burn": [
            "burn",
            RELEASE_KEY,
            "--release",
            record,
            "--idempotency-key",
            "burn-01",
            "--reason",
            "the version is spent",
        ],
        "advance": [
            "advance",
            RELEASE_KEY,
            "--release",
            record,
            "--receipt",
            _write(tmp_path / "receipt.json", {"gate": "tests"}),
        ],
    }
    return ["release", *table[subcommand]]


# --- parity ---------------------------------------------------------------


def test_every_release_verb_has_cli_subcommand() -> None:
    """The registered release namespace and the reachable set are one set."""
    registered = {name for name in registered_methods() if name.startswith("release.")}
    assert registered == set(RELEASE_RPC_METHODS.values())


def test_every_mapped_subcommand_is_registered_on_the_release_app() -> None:
    """Every mapping key names a real ``eawf release`` subcommand."""
    declared = {command.name for command in release_app.registered_commands}
    assert set(RELEASE_RPC_METHODS) <= declared


def test_the_release_namespace_is_not_empty() -> None:
    """The parity assertion is over a non-empty set, not two empty ones."""
    assert len(RELEASE_RPC_METHODS) == 10


# --- dispatch -------------------------------------------------------------


@pytest.mark.parametrize("subcommand", sorted(RELEASE_RPC_METHODS))
def test_the_cli_dispatches_each_verb_to_its_rpc_method(
    subcommand: str, tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    """Each subcommand reaches the daemon method its mapping names."""
    result = CliRunner().invoke(app, _argv(subcommand, tmp_path))
    assert result.exit_code == 0, result.output
    assert [call["method"] for call in calls] == [RELEASE_RPC_METHODS[subcommand]]


@pytest.mark.parametrize("subcommand", ("publish", "retry", "reconcile", "observe"))
def test_the_external_effect_verbs_carry_both_keying_fields(
    subcommand: str, tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    """Every registry-touching verb sends a revision and a replay key."""
    result = CliRunner().invoke(app, _argv(subcommand, tmp_path))
    assert result.exit_code == 0, result.output
    params = calls[0]["params"]
    assert params["expected_revision"] == 4
    assert params["idempotency_key"] == f"{subcommand}-01"


def test_show_sends_the_requested_version(tmp_path: Path, calls: list[dict[str, Any]]) -> None:
    """The positional version reaches the handler as the ``version`` param."""
    result = CliRunner().invoke(app, _argv("show", tmp_path))
    assert result.exit_code == 0, result.output
    assert calls[0]["params"] == {"version": "0.7.0.dev1"}
    assert "never opened" in result.output


def test_show_without_a_version_asks_for_the_open_rung(calls: list[dict[str, Any]]) -> None:
    """An omitted version is sent as ``None`` (the argument's empty case)."""
    result = CliRunner().invoke(app, ["release", "show"])
    assert result.exit_code == 0, result.output
    assert calls[0]["params"] == {"version": None}


def test_create_sends_every_repeated_membership_ref(
    tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    """A repeated option arrives as a list, not as its last value."""
    result = CliRunner().invoke(
        app,
        ["release", "create", "0.7.0.dev2", "--membership-ref", "a", "--membership-ref", "b"],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["params"]["membership_refs"] == ["a", "b"]


def test_create_without_a_membership_ref_sends_an_empty_list(
    calls: list[dict[str, Any]],
) -> None:
    """No membership refs is an empty list, never ``None`` (empty boundary)."""
    result = CliRunner().invoke(app, ["release", "create", "0.7.0.dev2"])
    assert result.exit_code == 0, result.output
    assert calls[0]["params"]["membership_refs"] == []


def test_reconcile_sends_the_downloaded_receipt(
    tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    """``--receipt`` is forwarded as the decoded document, not as a path."""
    receipt = _write(tmp_path / "publication-receipt-pypi.json", {"job_conclusion": "success"})
    result = CliRunner().invoke(
        app,
        [
            "release",
            "reconcile",
            RELEASE_KEY,
            "--target",
            "pypi",
            "--release",
            _record_file(tmp_path),
            "--idempotency-key",
            "reconcile-01",
            "--receipt",
            receipt,
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["params"]["receipt"] == {"job_conclusion": "success"}
    assert "status" not in calls[0]["params"]


# --- refusals -------------------------------------------------------------


def test_reconcile_refuses_both_result_sources(tmp_path: Path, calls: list[dict[str, Any]]) -> None:
    """Naming a status and a receipt at once never reaches the daemon."""
    result = CliRunner().invoke(
        app,
        [
            "release",
            "reconcile",
            RELEASE_KEY,
            "--target",
            "pypi",
            "--release",
            _record_file(tmp_path),
            "--idempotency-key",
            "reconcile-01",
            "--status",
            "unknown",
            "--receipt",
            _write(tmp_path / "receipt.json", {"job_conclusion": "success"}),
        ],
    )
    assert result.exit_code != 0
    assert "exactly one of --status" in result.output
    assert calls == []


def test_reconcile_refuses_neither_result_source(
    tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    """Naming neither is refused too -- the other side of the same rule."""
    result = CliRunner().invoke(
        app,
        [
            "release",
            "reconcile",
            RELEASE_KEY,
            "--target",
            "pypi",
            "--release",
            _record_file(tmp_path),
            "--idempotency-key",
            "reconcile-01",
        ],
    )
    assert result.exit_code != 0
    assert "exactly one of --status" in result.output
    assert calls == []


@pytest.mark.parametrize("subcommand", ("approve", "publish", "retry", "reconcile", "advance"))
def test_a_record_for_another_release_is_refused(
    subcommand: str, tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    """A record file keyed to another checkpoint never reaches the daemon."""
    argv = _argv(subcommand, tmp_path)
    _record_file(tmp_path, key="REL-0.7.0.dev2")
    result = CliRunner().invoke(app, argv)
    assert result.exit_code != 0
    assert "not 'REL-0.7.0.dev1'" in result.output
    assert calls == []


def test_a_missing_record_file_is_refused(tmp_path: Path, calls: list[dict[str, Any]]) -> None:
    """An absent record is a NotFound refusal, not a daemon round trip."""
    result = CliRunner().invoke(
        app,
        [
            "release",
            "approve",
            RELEASE_KEY,
            "--release",
            str(tmp_path / "absent.json"),
            "--readiness",
            _write(tmp_path / "readiness.json", {}),
            "--approval-ref",
            "receipt://approval/dev1",
        ],
    )
    assert result.exit_code != 0
    assert "cannot read release record" in result.output
    assert calls == []


def test_an_unparseable_record_file_is_refused(tmp_path: Path, calls: list[dict[str, Any]]) -> None:
    """Malformed JSON is a validation refusal naming the document."""
    broken = tmp_path / "release.json"
    broken.write_text("{not json", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["release", "advance", RELEASE_KEY, "--release", str(broken)],
    )
    assert result.exit_code != 0
    assert "release record is not valid JSON" in result.output
    assert calls == []


def test_a_record_file_holding_a_json_array_is_refused(
    tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    """A JSON document of the wrong shape is refused before dispatch."""
    listed = tmp_path / "release.json"
    listed.write_text("[]", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["release", "advance", RELEASE_KEY, "--release", str(listed)],
    )
    assert result.exit_code != 0
    assert "must be a JSON object" in result.output
    assert calls == []


def test_readiness_refuses_a_waiver_document_with_no_rows(
    tmp_path: Path, calls: list[dict[str, Any]]
) -> None:
    """A waiver file missing its own key names the key rather than crashing."""
    result = CliRunner().invoke(
        app,
        [
            "release",
            "readiness",
            "0.7.0.dev1",
            "--waivers",
            _write(tmp_path / "waivers.json", {"rows": []}),
        ],
    )
    assert result.exit_code != 0
    assert "missing the 'waivers' key" in result.output
    assert calls == []


def test_an_unreachable_daemon_is_reported_per_method(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport failure names the method that could not be reached."""

    def refuse(*_args: object, **_kwargs: object) -> _RecordingClient:
        raise OSError("no socket")

    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", refuse)
    result = CliRunner().invoke(app, _argv("publish", tmp_path))
    assert result.exit_code != 0
    assert "daemon unavailable for release.publish" in result.output
