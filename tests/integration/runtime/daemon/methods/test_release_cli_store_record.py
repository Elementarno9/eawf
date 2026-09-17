"""The registry CLI verbs act on the record the store holds.

``eawf release publish``, ``retry``, ``reconcile`` and ``observe`` used
to require ``--release <file>``, and the file an operator held was the
record from before the previous verb -- one step stale, which the
compare-and-swap then refused. Every registry verb now records the
record it produces, so without ``--release`` the CLI reads the current
record from the store and presents that; with ``--release`` it still
presents the file.

The CLI runs through the real Typer app against a ``DaemonClient``
stand-in that dispatches into the real handlers, bound to the same dev1
checkout the CLI resolves its state root from, so the record the CLI
reads is the record the previous handler wrote. No probe is patched.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
from click.testing import Result
from typer.testing import CliRunner

from eawf.kernel.spec.release import ReleaseStatus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.release import (
    observe,
    publish,
    reconcile,
    retry_target,
    show,
)
from eawf.surfaces.cli.app import app
from eawf.workflow.release.ledger import ledger_path
from eawf.workflow.release.records import release_records_path
from tests.integration.runtime.daemon.methods.conftest import (
    DEV1_VERSION,
    PROOF_DIGEST,
    RELEASE_KEY,
    TARGET_IDS,
    approve_pinned,
    manifest_digest,
    manifest_payload,
    pinned_payload,
    publication_receipt,
    response_payload,
)

pytestmark = pytest.mark.integration

#: The handler each registry verb's RPC method reaches.
HANDLERS = {
    "release.publish": publish,
    "release.retry_target": retry_target,
    "release.reconcile": reconcile,
    "release.observe_target": observe,
}


class _HandlerClient:
    """A ``DaemonClient`` stand-in dispatching into the real handlers."""

    def __init__(self, ctx: MethodContext, calls: list[dict[str, Any]]) -> None:
        self._ctx = ctx
        self._calls = calls

    def __enter__(self) -> _HandlerClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record the call, then answer it with the real handler."""
        self._calls.append({"method": method, "params": params})

        async def answer() -> dict[str, Any]:
            return await HANDLERS[method](self._ctx, dict(params or {}))

        return asyncio.run(answer())


@pytest.fixture
def calls(walk_ctx: MethodContext, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Route every CLI daemon call into the handlers bound to the checkout."""
    recorded: list[dict[str, Any]] = []
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.setattr(
        "eawf.surfaces.cli._daemon_client.DaemonClient",
        lambda *args, **kwargs: _HandlerClient(walk_ctx, recorded),
    )
    return recorded


@pytest.fixture
def approved(walk_ctx: MethodContext, dev1_checkout: Path) -> dict[str, Any]:
    """Return the dev1 record approved and recorded by the daemon verbs."""
    return asyncio.run(approve_pinned(walk_ctx, dev1_checkout))


def cli(repo: Path, *argv: str) -> Result:
    """Run ``eawf --workspace <repo> release <argv>``."""
    return CliRunner().invoke(app, ["--workspace", str(repo), "release", *argv])


def document(repo: Path, name: str, payload: dict[str, Any]) -> str:
    """Write *payload* beside (not inside) *repo* and return its path.

    Outside the checkout, so no document the operator hands the CLI can
    dirty the tree the next sweep reads.
    """
    path = repo.parent / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def stored(ctx: MethodContext) -> dict[str, Any]:
    """Return the record ``release.show`` reports for the dev1 rung."""

    async def ask() -> dict[str, Any]:
        return await show(ctx, {"version": DEV1_VERSION})

    record: dict[str, Any] = asyncio.run(ask())["record"]
    return record


def publish_argv(*extra: str, key: str = "publish-cli-01") -> list[str]:
    """Return the ``release publish`` argv, minus the record source."""
    return [
        "publish",
        RELEASE_KEY,
        "--approved-manifest-digest",
        manifest_digest(),
        "--proof-digest",
        PROOF_DIGEST,
        "--idempotency-key",
        key,
        *extra,
    ]


def reconcile_argv(repo: Path, target_id: str, conclusion: str = "success") -> list[str]:
    """Return a receipt-bearing ``release reconcile`` argv reading the store."""
    receipt = publication_receipt(target_id, job_conclusion=conclusion)
    return [
        "reconcile",
        RELEASE_KEY,
        "--target",
        target_id,
        "--idempotency-key",
        f"reconcile-cli-{target_id}",
        "--receipt",
        document(repo, f"publication-receipt-{target_id}.json", receipt),
    ]


def observe_argv(repo: Path, target_id: str, case: str = "match") -> list[str]:
    """Return a ``release observe`` argv reading the store."""
    return [
        "observe",
        RELEASE_KEY,
        "--target",
        target_id,
        "--manifest",
        document(repo, "manifest.json", manifest_payload()),
        "--idempotency-key",
        f"observe-cli-{target_id}-{case}",
        "--response",
        document(repo, f"response-{target_id}.json", response_payload(target_id, case)),
    ]


# --- the store read -----------------------------------------------------------


def test_publish_reads_the_approved_record_from_the_store(
    walk_ctx: MethodContext,
    dev1_checkout: Path,
    approved: dict[str, Any],
    calls: list[dict[str, Any]],
) -> None:
    """The record the approval recorded is the one the publish presents."""
    result = cli(dev1_checkout, *publish_argv())

    assert result.exit_code == 0, result.output
    assert calls[0]["params"]["release"] == approved
    assert calls[0]["params"]["expected_revision"] == approved["revision"]
    assert stored(walk_ctx)["status"] == ReleaseStatus.PUBLISHING.value
    assert f"{RELEASE_KEY} publishing" in result.output


def test_reconcile_and_observe_walk_to_baked_reading_the_store(
    walk_ctx: MethodContext,
    dev1_checkout: Path,
    approved: dict[str, Any],
    calls: list[dict[str, Any]],
) -> None:
    """Each verb presents the record the verb before it recorded."""
    assert cli(dev1_checkout, *publish_argv()).exit_code == 0
    argvs = [reconcile_argv(dev1_checkout, target_id) for target_id in TARGET_IDS]
    argvs += [observe_argv(dev1_checkout, target_id) for target_id in TARGET_IDS]

    for argv in argvs:
        before = stored(walk_ctx)
        result = cli(dev1_checkout, *argv)
        assert result.exit_code == 0, result.output
        assert calls[-1]["params"]["release"] == before
        assert calls[-1]["params"]["expected_revision"] == before["revision"]

    assert stored(walk_ctx)["status"] == ReleaseStatus.BAKED.value
    assert [call["method"] for call in calls] == [
        "release.publish",
        *["release.reconcile"] * 3,
        *["release.observe_target"] * 3,
    ]


def test_retry_reads_the_recovering_record_from_the_store(
    walk_ctx: MethodContext,
    dev1_checkout: Path,
    approved: dict[str, Any],
    calls: list[dict[str, Any]],
) -> None:
    """A contradicted read-back leaves RECOVERING in the store; retry acts on it."""
    assert cli(dev1_checkout, *publish_argv()).exit_code == 0
    assert cli(dev1_checkout, *reconcile_argv(dev1_checkout, "npm", "failure")).exit_code == 0
    assert cli(dev1_checkout, *reconcile_argv(dev1_checkout, "pypi")).exit_code == 0
    assert cli(dev1_checkout, *observe_argv(dev1_checkout, "pypi", "mismatch")).exit_code == 0
    assert stored(walk_ctx)["status"] == ReleaseStatus.RECOVERING.value

    result = cli(
        dev1_checkout,
        "retry",
        RELEASE_KEY,
        "--target",
        "npm",
        "--proof-digest",
        PROOF_DIGEST,
        "--idempotency-key",
        "retry-cli-npm",
    )

    assert result.exit_code == 0, result.output
    assert calls[-1]["params"]["release"]["status"] == ReleaseStatus.RECOVERING.value
    assert stored(walk_ctx)["status"] == ReleaseStatus.PUBLISHING.value
    assert "npm#2=queued" in result.output


def test_the_store_read_honours_the_state_environment_variable(
    walk_ctx: MethodContext,
    dev1_checkout: Path,
    approved: dict[str, Any],
    calls: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``EA_STATE`` names the state root when no workspace flag is given."""
    monkeypatch.setenv("EA_STATE", str(dev1_checkout / ".ea" / "state.json"))

    result = CliRunner().invoke(app, ["release", *publish_argv()])

    assert result.exit_code == 0, result.output
    assert calls[0]["params"]["release"] == approved


def test_rerunning_a_command_after_the_record_moved_replays(
    walk_ctx: MethodContext,
    dev1_checkout: Path,
    approved: dict[str, Any],
    calls: list[dict[str, Any]],
) -> None:
    """The rerun reads the moved record and still answers the first receipt."""
    assert cli(dev1_checkout, *publish_argv()).exit_code == 0
    state_path = dev1_checkout / ".ea" / "state.json"
    before = (
        ledger_path(state_path).read_bytes(),
        release_records_path(state_path).read_bytes(),
    )

    result = cli(dev1_checkout, *publish_argv())

    assert result.exit_code == 0, result.output
    assert "(replayed)" in result.output
    assert calls[1]["params"]["release"]["status"] == ReleaseStatus.PUBLISHING.value
    assert (
        ledger_path(state_path).read_bytes(),
        release_records_path(state_path).read_bytes(),
    ) == before


# --- the explicit --release -----------------------------------------------------


def test_an_explicit_release_file_publishes_with_nothing_stored(
    walk_ctx: MethodContext, dev1_checkout: Path, calls: list[dict[str, Any]]
) -> None:
    """``--release`` needs no record in the store."""
    payload = pinned_payload(dev1_checkout)

    result = cli(
        dev1_checkout,
        *publish_argv("--release", document(dev1_checkout, "release.json", payload)),
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["params"]["release"] == payload
    assert stored(walk_ctx)["status"] == ReleaseStatus.PUBLISHING.value


def test_an_explicit_release_file_wins_over_the_store(
    dev1_checkout: Path, approved: dict[str, Any], calls: list[dict[str, Any]]
) -> None:
    """The file is presented as given, even beside a stored record."""
    payload = pinned_payload(dev1_checkout)
    assert payload["revision"] != approved["revision"]

    result = cli(
        dev1_checkout,
        *publish_argv("--release", document(dev1_checkout, "release.json", payload)),
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["params"]["release"] == payload
    assert calls[0]["params"]["expected_revision"] == payload["revision"]


def test_an_explicit_release_file_for_another_key_is_refused(
    dev1_checkout: Path, approved: dict[str, Any], calls: list[dict[str, Any]]
) -> None:
    """A file keyed to another checkpoint never reaches the daemon."""
    payload = {**pinned_payload(dev1_checkout), "key": "REL-0.7.0.dev2"}

    result = cli(
        dev1_checkout,
        *publish_argv("--release", document(dev1_checkout, "release.json", payload)),
    )

    assert result.exit_code != 0
    assert f"not '{RELEASE_KEY}'" in result.output
    assert calls == []


# --- refusals -----------------------------------------------------------------


@pytest.mark.parametrize("verb", ["publish", "retry", "reconcile", "observe"])
def test_a_key_with_no_stored_record_is_refused_before_dispatch(
    dev1_checkout: Path, calls: list[dict[str, Any]], verb: str
) -> None:
    """Nothing recorded and no file is a NotFound naming both ways forward."""
    argv = {
        "publish": publish_argv(),
        "retry": [
            "retry",
            RELEASE_KEY,
            "--target",
            "pypi",
            "--proof-digest",
            PROOF_DIGEST,
            "--idempotency-key",
            "retry-cli-none",
        ],
        "reconcile": reconcile_argv(dev1_checkout, "pypi"),
        "observe": observe_argv(dev1_checkout, "pypi"),
    }[verb]

    result = cli(dev1_checkout, *argv)

    assert result.exit_code != 0
    assert f"no release record is stored for '{RELEASE_KEY}'" in result.output
    assert "--release" in result.output
    assert calls == []


def test_a_corrupt_record_collection_is_refused_before_dispatch(
    dev1_checkout: Path, calls: list[dict[str, Any]]
) -> None:
    """A collection that cannot be read is never guessed around."""
    path = release_records_path(dev1_checkout / ".ea" / "state.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not an envelope\n", encoding="utf-8")

    result = cli(dev1_checkout, *publish_argv())

    assert result.exit_code != 0
    assert "release record collection is corrupt" in result.output
    assert calls == []
