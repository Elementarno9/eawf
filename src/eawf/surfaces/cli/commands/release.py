"""``eawf release`` Typer sub-app.

Two kinds of verb live here. ``changelog``, ``notes`` and ``train show``
are local: they read the checkout and print. The rest dispatch to the
daemon's ``release.*`` JSON-RPC namespace, because they read or write
records the daemon owns, and answer with the one machine envelope of
:mod:`eawf.surfaces.cli.verb_contract`: a refusal is an ``error`` envelope
carrying the daemon's code, and exits with the envelope's typed status. A
verb that mutates a record names the revision the caller read it at with
``--expected-revision``; the anchor is never read off the record it sends.

:data:`RELEASE_RPC_METHODS` is the parity map between the dispatching
verbs and the daemon -- every registered ``release.*`` method names the
subcommand that reaches it, so a verb added to the daemon without an
operator surface reds the parity test rather than shipping as substrate
nobody can call.

The two train-walking verbs, ``receipts`` and ``advance``, live in
:mod:`eawf.surfaces.cli.commands.release_train`; the manifest pin,
``candidate``, lives in
:mod:`eawf.surfaces.cli.commands.release_candidate`; the local sweep
verbs, ``tag`` and ``preflight``, live in
:mod:`eawf.surfaces.cli.commands.release_tag`; and the post-merge walk,
``pipeline``, lives in :mod:`eawf.surfaces.cli.commands.release_pipeline`.
All four attach their verbs to :data:`release_app` when this module is
imported.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final

import orjson
import typer

from eawf.kernel.state.resolve import resolve_with_reason
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.verb_contract import (
    answer_envelope,
    emit_envelope,
    refusal_envelope,
    request_document,
)

if TYPE_CHECKING:
    from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)

#: ``eawf release <subcommand>`` -> the ``release.*`` JSON-RPC method it
#: dispatches to. The map is asserted total over the daemon's registered
#: release namespace, so it is the contract "no release verb is
#: unreachable", not a convenience index.
RELEASE_RPC_METHODS: Final[Mapping[str, str]] = {
    "show": "release.show",
    "readiness": "release.compute_readiness",
    "create": "release.create",
    "candidate": "release.candidate",
    "approve": "release.approve",
    "publish": "release.publish",
    "retry": "release.retry_target",
    "reconcile": "release.reconcile",
    "observe": "release.observe_target",
    "burn": "release.burn",
    "adopt": "release.adopt",
    "cancel": "release.cancel",
    "advance": "release.advance_train",
    "receipts": "release.produce_receipts",
}

release_app = typer.Typer(
    name="release",
    help="Tag releases and drive the release train's checkpoint records.",
    no_args_is_help=True,
    add_completion=False,
)


def _load_state(state_path: Path) -> State:
    from eawf.kernel.validate.strict import validate_state

    if not state_path.exists():
        raise cli_errors.UserError(f"state file not found: {state_path}", kind="NotFound")
    payload = orjson.loads(state_path.read_bytes())
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise cli_errors.ValidationError(
            f"state schema invalid: {'; '.join(report.schema_errors[:3])}"
        )
    return report.state


@release_app.command("changelog")
def release_changelog(ctx: typer.Context) -> None:
    """Mine the current ``CHANGELOG.md`` unreleased section."""
    from eawf.surfaces.render.release_notes import mine_unreleased_changelog

    flags: GlobalFlags = ctx.obj
    path = Path("CHANGELOG.md")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        cli_errors.emit_error(
            cli_errors.UserError(f"cannot read CHANGELOG.md: {exc}", kind="NotFound"), flags=flags
        )
        return
    lines = mine_unreleased_changelog(text)
    payload = {"entries": lines, "count": len(lines)}
    out = "\n".join(lines) if lines else "(no unreleased changelog entries)"
    emit_json_or_text(payload, out, flags=flags)


@release_app.command("notes")
def release_notes(
    ctx: typer.Context,
    version: Annotated[str, typer.Argument(help="Release version label.")],
    from_phase: Annotated[
        str | None,
        typer.Option("--from-phase", help="First phase to include, e.g. P14."),
    ] = None,
    to_phase: Annotated[
        str | None,
        typer.Option("--to-phase", help="Last phase to include, e.g. P17."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Optional output path for the draft."),
    ] = None,
) -> None:
    """Render a scrubbed release-notes draft."""
    from eawf.surfaces.render.release_notes import (
        ReleaseNotesValidationError,
        build_release_notes,
        release_slug,
    )

    flags: GlobalFlags = ctx.obj
    try:
        state_path, _reason = resolve_with_reason(flags.workspace)
        state = _load_state(state_path)
        changelog_text = None
        changelog_path = Path("CHANGELOG.md")
        if changelog_path.exists():
            changelog_text = changelog_path.read_text(encoding="utf-8")
        body = build_release_notes(
            state,
            from_phase=from_phase,
            to_phase=to_phase,
            changelog_text=changelog_text,
        )
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(body, encoding="utf-8")
    except ReleaseNotesValidationError as exc:
        cli_errors.emit_error(cli_errors.ValidationError(str(exc)), flags=flags)
        return
    except OSError as exc:
        cli_errors.emit_error(
            cli_errors.UserError(f"cannot write release notes: {exc}", kind="InvalidInput"),
            flags=flags,
        )
        return
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    payload = {
        "version": version,
        "slug": release_slug(version),
        "body": body,
        "output": str(output) if output is not None else None,
    }
    emit_json_or_text(payload, body, flags=flags)


train_app = typer.Typer(
    name="train",
    help="Inspect the release-train ladder and the checkpoint it has open.",
    no_args_is_help=True,
    add_completion=False,
)
release_app.add_typer(train_app)


@train_app.command("show")
def release_train_show(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Shorthand for the global --json flag."),
    ] = False,
) -> None:
    """Render the ladder, the open index and each checkpoint's status.

    The open index is derived from the release-record and train-advance
    stores (:func:`~eawf.workflow.release.advance.derive_train`), not read
    off the source-declared :data:`~eawf.workflow.release.train.V07_TRAIN`
    constant -- that constant always starts at rung zero, so rendering it
    unread would report ``dev1`` open long after the train walked past it.
    """
    from eawf.workflow.release.advance import (
        derive_train,
        render_train_ladder,
        render_train_ladder_text,
    )
    from eawf.workflow.release.records import read_release_records
    from eawf.workflow.release.train import V07_TRAIN
    from eawf.workflow.release.train_store import read_train_advances

    flags: GlobalFlags = ctx.obj
    if json_output:
        flags = replace(flags, json_output=True)
    state_path, _reason = resolve_with_reason(flags.workspace)
    train = derive_train(
        V07_TRAIN,
        recorded_keys=read_release_records(state_path).keys(),
        advances=read_train_advances(state_path),
    )
    emit_json_or_text(render_train_ladder(train), render_train_ladder_text(train), flags=flags)


def _read_json_document(path: Path, *, label: str) -> dict[str, object]:
    """Return the JSON object at *path*.

    Args:
        path: File to read.
        label: What the document is, for the error message.

    Returns:
        The decoded object.

    Raises:
        cli_errors.UserError: When the file cannot be read.
        cli_errors.ValidationError: When it is not a JSON object.
    """
    try:
        decoded = orjson.loads(path.read_bytes())
    except OSError as exc:
        raise cli_errors.UserError(f"cannot read {label}: {exc}", kind="NotFound") from exc
    except orjson.JSONDecodeError as exc:
        raise cli_errors.ValidationError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise cli_errors.ValidationError(
            f"{label} must be a JSON object, got {type(decoded).__name__}"
        )
    return decoded


def _release_document(path: Path, release_key: str) -> dict[str, Any]:
    """Return the serialized release record at *path*, keyed as expected.

    Args:
        path: File holding the serialized record.
        release_key: The key the operator named on the command line.

    Returns:
        The decoded record.

    Raises:
        cli_errors.UserError: When the file cannot be read, or holds a
            record for a different release -- acting on the wrong
            checkpoint is the mistake worth a refusal here.
        cli_errors.ValidationError: When the file is not a JSON object.
    """
    record = _read_json_document(path, label="release record")
    if record.get("key") != release_key:
        raise cli_errors.UserError(
            f"{path} holds release {record.get('key')!r}, not {release_key!r}",
            kind="InvalidInput",
        )
    return record


def _current_record(
    release_file: Path | None,
    release_key: str,
    *,
    workspace: Path | None,
) -> dict[str, Any]:
    """Return the record a registry verb acts on: the file, else the stored one.

    Every registry verb records the record it produces, so the store
    holds the revision the next verb must present, whereas a file saved
    one step early is a stale revision.

    Args:
        release_file: The ``--release`` path, or ``None`` for the store.
        release_key: The key the operator named on the command line.
        workspace: The ``--workspace`` root, or ``None``.

    Returns:
        The serialized record.

    Raises:
        cli_errors.UserError: When the file is unreadable or holds
            another release, or nothing is stored for the key.
        cli_errors.ValidationError: When the file is not a JSON object
            or the record collection is corrupt.
    """
    from eawf.workflow.release.records import read_release_record

    if release_file is not None:
        return _release_document(release_file, release_key)
    state_path, _reason = resolve_with_reason(workspace)
    try:
        record = read_release_record(state_path, release_key)
    except ValueError as exc:
        raise cli_errors.ValidationError(f"release record collection is corrupt: {exc}") from exc
    if record is None:
        raise cli_errors.UserError(
            f"no release record is stored for {release_key!r}; open the checkpoint with "
            f"`eawf release create`, or pass --release <file>",
            kind="NotFound",
        )
    return record.model_dump(mode="json")


def _dispatch(
    method: str,
    params: dict[str, Any],
    *,
    call_timeout_seconds: float | None = None,
    spawn: bool = True,
) -> dict[str, Any]:
    """Call one ``release.*`` JSON-RPC method and return its result.

    Args:
        method: Fully-qualified method name, e.g. ``release.publish``.
        params: Already-assembled JSON-RPC params.
        call_timeout_seconds: How long to wait for the reply; ``None``
            keeps the client's default, which suits every verb that does
            not run proof commands.
        spawn: Whether a missing daemon is started; a read verb passes
            ``False`` so inspecting a release never starts one.

    Returns:
        The handler's result object.

    Raises:
        DaemonRpcError: The daemon refused the call, left for
            :func:`_answer` to render as a refusal envelope.
        cli_errors.UserError: With ``data.kind="DaemonError"`` when the
            daemon cannot be reached.
    """
    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

    try:
        client = (
            DaemonClient(spawn=spawn)
            if call_timeout_seconds is None
            else DaemonClient(call_timeout_seconds=call_timeout_seconds, spawn=spawn)
        )
        with client:
            return client.call(method, params)
    except DaemonRpcError:
        raise
    except (OSError, RuntimeError) as exc:
        raise cli_errors.UserError(
            f"daemon unavailable for {method}: {exc}", kind="DaemonError"
        ) from exc


def _answer(
    ctx: typer.Context,
    method: str,
    params: dict[str, Any],
    *,
    subject: str,
    revision: int | None = None,
    call_timeout_seconds: float | None = None,
    failed_guard: Callable[[dict[str, Any]], str | None] | None = None,
    links: Callable[[dict[str, Any]], dict[str, str]] | None = None,
    spawn: bool = True,
) -> dict[str, Any] | None:
    """Send one release verb and print its answer as the machine envelope.

    Args:
        ctx: Typer context carrying the resolved global flags.
        method: Fully-qualified method name.
        params: Already-assembled JSON-RPC params.
        subject: The release key or version the request addresses.
        revision: The record revision the caller anchored the request at,
            or ``None`` for a verb that takes no anchor.
        call_timeout_seconds: How long to wait for the reply, or ``None``.
        failed_guard: Names the check the answer did not pass, if any,
            so an answer the daemon gave but that refused part of the work
            exits with the refusal status.
        links: Derives the commands a caller may follow next from the answer.
        spawn: Whether a missing daemon is started for the call.

    Returns:
        The answer when it stood; ``None`` after a refusal or an error was
        printed (both exit, so ``None`` is reached only under test doubles).
    """
    from eawf.surfaces.cli._daemon_client import DaemonRpcError

    flags: GlobalFlags = ctx.obj
    try:
        result = _dispatch(method, params, call_timeout_seconds=call_timeout_seconds, spawn=spawn)
    except DaemonRpcError as exc:
        if exc.code != cli_errors.RPC_VALIDATION_FAILED:
            cli_errors.emit_error(
                cli_errors.UserError(
                    f"daemon rejected {method}: code={exc.code} {exc.message}", kind="DaemonError"
                ),
                flags=flags,
            )
            return None
        emit_envelope(
            refusal_envelope(exc.message, operation=method, urn=subject), urn=subject, flags=flags
        )
        return None
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return None
    record = result.get("release")
    envelope = answer_envelope(
        result,
        operation=method,
        urn=subject,
        revision_before=revision,
        revision_after=record.get("revision") if isinstance(record, dict) else None,
        failed_guard=None if failed_guard is None else failed_guard(result),
        links=None if links is None else links(result),
    )
    emit_envelope(envelope, urn=subject, flags=flags)
    return result


_ExpectedRevision = Annotated[
    int,
    typer.Option("--expected-revision", help="Record revision the caller read (compare-and-swap)."),
]


@release_app.command("observe")
def release_observe(
    ctx: typer.Context,
    release_key: Annotated[
        str, typer.Argument(help="Release key to observe, e.g. REL-0.7.0.dev1.")
    ],
    target: Annotated[
        str, typer.Option("--target", help="Publication target to read back, e.g. pypi.")
    ],
    manifest_file: Annotated[
        Path,
        typer.Option("--manifest", help="Path to the frozen manifest the release approved."),
    ],
    expected_revision: _ExpectedRevision,
    idempotency_key: Annotated[
        str, typer.Option("--idempotency-key", help="Replay identity of this read-back.")
    ],
    release_file: Annotated[
        Path | None,
        typer.Option("--release", help="Record file (default: the stored record)."),
    ] = None,
    response_file: Annotated[
        Path | None,
        typer.Option("--response", help="Recorded registry answer to judge, as JSON."),
    ] = None,
    effect_receipt_ref: Annotated[
        str | None,
        typer.Option("--effect-receipt", help="Adapter receipt for a leg that timed out."),
    ] = None,
) -> None:
    """Read one publication target back and settle it against the manifest.

    This is the only verb that can write ``observed_success`` or
    ``observed_mismatch``. The adapter is the one the checkpoint's target
    declares -- there is no fallback -- and the manifest passed here must
    recompute to the digest the release approved, so an observation can
    never be collected against artifacts nobody signed off.

    A contradicted or missing read-back moves the release to
    ``RECOVERING``; the matched read-back that completes the required
    target set bakes it. A read-back that settled nothing refuses with
    ``observation_inconclusive`` rather than guessing.

    Without ``--response`` the daemon reads the registry live; a version
    still missing inside the propagation window of the leg's reported
    success refuses as ``observation_inconclusive``. A ``--response``
    must carry what the reader adds (npm tarball digest, repository).

    Without ``--release`` it observes the record the store holds, at the
    revision the caller names.
    """
    flags: GlobalFlags = ctx.obj
    try:
        params: dict[str, Any] = {
            "release": _current_record(release_file, release_key, workspace=flags.workspace),
            "expected_revision": expected_revision,
            "idempotency_key": idempotency_key,
            "target_id": target,
            "manifest": _read_json_document(manifest_file, label="frozen manifest"),
            "effect_receipt_ref": effect_receipt_ref,
        }
        if response_file is not None:
            params["response"] = _read_json_document(response_file, label="recorded response")
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    _answer(
        ctx,
        RELEASE_RPC_METHODS["observe"],
        params,
        subject=release_key,
        revision=expected_revision,
    )


@release_app.command("show")
def release_show(
    ctx: typer.Context,
    version: Annotated[
        str | None,
        typer.Argument(help="Checkpoint version to describe (default: the open rung)."),
    ] = None,
) -> None:
    """Describe the train ladder, one checkpoint rung, and its record.

    "Which rung exists" and "has that rung been cut" are different
    questions. The ladder answers the first from source; the second is
    the recorded release, which the daemon reads back -- so an operator
    asking where a checkpoint stands never has to open a store file. A
    rung nobody has opened answers ``record: null`` rather than refusing.
    """
    _answer(
        ctx,
        RELEASE_RPC_METHODS["show"],
        {"version": version},
        subject=version or "open rung",
        spawn=False,
    )


@release_app.command("readiness")
def release_readiness(
    ctx: typer.Context,
    version: Annotated[str, typer.Argument(help="Checkpoint version, e.g. 0.7.0.dev1.")],
    release_file: Annotated[
        Path | None,
        typer.Option("--release", help="Candidate record the sweep result is applied to."),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option("--source", help="Source revision the sweep is computed against."),
    ] = None,
    waiver_count: Annotated[
        int,
        typer.Option("--waiver-count", help="Gate waivers recorded against the checkpoint."),
    ] = 0,
    waivers_file: Annotated[
        Path | None,
        typer.Option("--waivers", help="JSON object with a 'waivers' list explaining the count."),
    ] = None,
    acknowledgements_file: Annotated[
        Path | None,
        typer.Option(
            "--acknowledgements",
            help="JSON object with an 'acknowledgements' list accepting the waivers.",
        ),
    ] = None,
) -> None:
    """Ask the daemon for a checkpoint's readiness sweep.

    Pass ``--release`` to also learn which status the sweep result moves
    that candidate into, which is the question worth asking before
    approving it.

    A counted waiver with no rows behind it classifies ``unexplained``
    and no acknowledgement can clear it, so ``--waivers`` and
    ``--acknowledgements`` carry the rows rather than leaving the count
    unexplained. Both files are JSON objects holding the list under the
    option's own name.
    """
    flags: GlobalFlags = ctx.obj
    try:
        params: dict[str, Any] = {
            "version": version,
            "observed_revision": source,
            "waiver_count": waiver_count,
        }
        if waivers_file is not None:
            params["waivers"] = _read_json_document(waivers_file, label="waiver rows")["waivers"]
        if acknowledgements_file is not None:
            params["acknowledgements"] = _read_json_document(
                acknowledgements_file, label="acknowledgement rows"
            )["acknowledgements"]
        if release_file is not None:
            params["release"] = _read_json_document(release_file, label="release record")
    except KeyError as exc:
        cli_errors.emit_error(
            cli_errors.ValidationError(f"waiver document is missing the {exc} key"), flags=flags
        )
        return
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    _answer(ctx, RELEASE_RPC_METHODS["readiness"], params, subject=version, spawn=False)


@release_app.command("create")
def release_create(
    ctx: typer.Context,
    version: Annotated[
        str | None, typer.Argument(help="Checkpoint version to open, e.g. 0.7.0.dev2.")
    ] = None,
    membership_ref: Annotated[
        list[str] | None,
        typer.Option("--membership-ref", help="Milestone acceptance bundle; repeatable."),
    ] = None,
    from_spec: Annotated[
        Path | None,
        typer.Option(
            "--from-spec",
            help="JSON file (or - for stdin) carrying the whole request, in place of the flags.",
        ),
    ] = None,
) -> None:
    """Open one checkpoint's DRAFT record, after measured admission.

    A checkpoint the admission table covers cannot be opened until every
    measured contract backing it is promoted and resolvable. The refusal
    names the single missing contract plus the command that promotes it,
    so the next action is in the error rather than in a runbook. The
    version and membership refs, or the ``--from-spec`` document, parse
    through the RPC's own closed params model before anything is sent.
    """
    from eawf.runtime.daemon.methods.release import CreateParams

    flags: GlobalFlags = ctx.obj
    try:
        request = request_document(
            CreateParams,
            from_spec,
            {"version": version, "membership_refs": list(membership_ref or ()) or None},
        )
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    _answer(
        ctx,
        RELEASE_RPC_METHODS["create"],
        request.model_dump(mode="json"),
        subject=request.version,
    )


@release_app.command("approve")
def release_approve(
    ctx: typer.Context,
    release_key: Annotated[
        str, typer.Argument(help="Release key to approve, e.g. REL-0.7.0.dev1.")
    ],
    release_file: Annotated[
        Path,
        typer.Option("--release", help="Path to the serialized candidate record."),
    ],
    readiness_file: Annotated[
        Path,
        typer.Option("--readiness", help="Path to the readiness sweep the approval binds."),
    ],
    approval_ref: Annotated[
        str, typer.Option("--approval-ref", help="Reference to the approval receipt.")
    ],
    proof_digest: Annotated[
        str, typer.Option("--proof-digest", help="Digest of the exact artifact set approved.")
    ],
) -> None:
    """Approve a candidate against a readiness sweep, and record it.

    The approval is the decision the whole publication path is authorised
    by, so it is written to the release-record collection before the
    reply is built. A sweep whose required signals are not all passing
    denies ``release_not_ready`` naming the first red row.
    """
    flags: GlobalFlags = ctx.obj
    try:
        params = {
            "release": _release_document(release_file, release_key),
            "readiness": _read_json_document(readiness_file, label="readiness sweep"),
            "approval_ref": approval_ref,
            "proof_digest": proof_digest,
        }
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    _answer(ctx, RELEASE_RPC_METHODS["approve"], params, subject=release_key)


@release_app.command("publish")
def release_publish(
    ctx: typer.Context,
    release_key: Annotated[
        str, typer.Argument(help="Release key to publish, e.g. REL-0.7.0.dev1.")
    ],
    approved_manifest_digest: Annotated[
        str, typer.Option("--approved-manifest-digest", help="Manifest digest the approval bound.")
    ],
    proof_digest: Annotated[
        str, typer.Option("--proof-digest", help="Digest binding the exact artifact set.")
    ],
    expected_revision: _ExpectedRevision,
    idempotency_key: Annotated[
        str, typer.Option("--idempotency-key", help="Replay identity of this publication.")
    ],
    release_file: Annotated[
        Path | None,
        typer.Option("--release", help="Record file (default: the stored record)."),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option("--source", help="Source revision the chokepoint sweep runs at."),
    ] = None,
    waiver_count: Annotated[
        int,
        typer.Option("--waiver-count", help="Gate waivers recorded against the checkpoint."),
    ] = 0,
) -> None:
    """Open the publication episode and return its reference at once.

    The chokepoint sweep is recomputed here rather than trusted from the
    approval, because the last thing to run before external effect has to
    be a fresh preflight. The legs are queued, not awaited, so the verb
    returns the operation reference immediately and a slow registry
    cannot hold the call open. Replaying the same idempotency key with
    the same request returns the original receipt instead of publishing
    twice, even after the record has moved on.

    Without ``--release`` it publishes the record the store holds, at the
    revision the caller names.
    """
    flags: GlobalFlags = ctx.obj
    try:
        params = {
            "release": _current_record(release_file, release_key, workspace=flags.workspace),
            "expected_revision": expected_revision,
            "idempotency_key": idempotency_key,
            "approved_manifest_digest": approved_manifest_digest,
            "proof_digest": proof_digest,
            "observed_revision": source,
            "waiver_count": waiver_count,
        }
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    _answer(
        ctx,
        RELEASE_RPC_METHODS["publish"],
        params,
        subject=release_key,
        revision=expected_revision,
        links=lambda result: {"follow": f"eawf follow {result['operation_ref']}"},
    )


@release_app.command("retry")
def release_retry(
    ctx: typer.Context,
    release_key: Annotated[str, typer.Argument(help="Release key whose leg is re-queued.")],
    target: Annotated[str, typer.Option("--target", help="The single leg to re-queue.")],
    proof_digest: Annotated[
        str,
        typer.Option("--proof-digest", help="Artifact-set digest; must equal the operation's."),
    ],
    expected_revision: _ExpectedRevision,
    idempotency_key: Annotated[
        str, typer.Option("--idempotency-key", help="Replay identity of this retry.")
    ],
    release_file: Annotated[
        Path | None,
        typer.Option("--release", help="Record file (default: the stored record)."),
    ] = None,
) -> None:
    """Re-queue one leg of the open episode under the idempotency proof.

    The proof digest must equal the open operation's: a retry against a
    different artifact set is a different publication wearing the same
    version, and the verb refuses it ``unsafe_release_retry`` rather than
    letting one version mean two builds.

    Without ``--release`` it retries the record the store holds, at the
    revision the caller names.
    """
    flags: GlobalFlags = ctx.obj
    try:
        params = {
            "release": _current_record(release_file, release_key, workspace=flags.workspace),
            "expected_revision": expected_revision,
            "idempotency_key": idempotency_key,
            "target_id": target,
            "proof_digest": proof_digest,
        }
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    _answer(
        ctx,
        RELEASE_RPC_METHODS["retry"],
        params,
        subject=release_key,
        revision=expected_revision,
    )


@release_app.command("reconcile")
def release_reconcile(
    ctx: typer.Context,
    release_key: Annotated[str, typer.Argument(help="Release key whose leg is settled.")],
    target: Annotated[str, typer.Option("--target", help="The leg whose adapter reported late.")],
    expected_revision: _ExpectedRevision,
    idempotency_key: Annotated[
        str, typer.Option("--idempotency-key", help="Replay identity of this reconciliation.")
    ],
    release_file: Annotated[
        Path | None,
        typer.Option("--release", help="Record file (default: the stored record)."),
    ] = None,
    status: Annotated[
        str | None,
        typer.Option(
            "--status", help="Asserted result: reported_success/reported_failure/unknown."
        ),
    ] = None,
    receipt_file: Annotated[
        Path | None,
        typer.Option("--receipt", help="The publish job's own receipt, which decides the status."),
    ] = None,
    effect_receipt_ref: Annotated[
        str | None,
        typer.Option("--effect-receipt", help="Adapter receipt the reported status points at."),
    ] = None,
) -> None:
    """Settle one leg against what its publish job finally reported.

    The word arrives either as an operator-asserted ``--status`` or as
    the job's downloaded ``--receipt``, never both: attaching a green
    receipt to a failure claim would leave the ledger holding the claim.
    Neither door reaches an ``observed_*`` status -- confirming an
    artifact is really on the registry is ``eawf release observe``.

    A ``--receipt`` names the run that published the leg, so it also
    settles a leg nothing marked as dispatched. Without ``--release`` it
    reconciles the record the store holds, at the revision the caller names.
    """
    flags: GlobalFlags = ctx.obj
    try:
        if (status is None) == (receipt_file is None):
            raise cli_errors.UserError(
                "reconcile takes exactly one of --status (the asserted result) or --receipt "
                "(the publish job's own, which decides the status)",
                kind="InvalidInput",
            )
        params: dict[str, Any] = {
            "release": _current_record(release_file, release_key, workspace=flags.workspace),
            "expected_revision": expected_revision,
            "idempotency_key": idempotency_key,
            "target_id": target,
            "effect_receipt_ref": effect_receipt_ref,
        }
        if status is not None:
            params["status"] = status
        else:
            params["receipt"] = _read_json_document(
                Path(str(receipt_file)), label="publication receipt"
            )
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    _answer(
        ctx,
        RELEASE_RPC_METHODS["reconcile"],
        params,
        subject=release_key,
        revision=expected_revision,
    )


@release_app.command("burn")
def release_burn(
    ctx: typer.Context,
    release_key: Annotated[str, typer.Argument(help="Release key to burn, e.g. REL-0.7.0.dev1.")],
    release_file: Annotated[
        Path,
        typer.Option("--release", help="Path to the serialized recovering record."),
    ],
    reason: Annotated[
        str, typer.Option("--reason", help="Why the version is spent; recorded with the burn.")
    ],
    expected_revision: _ExpectedRevision,
    idempotency_key: Annotated[
        str, typer.Option("--idempotency-key", help="Replay identity of this burn.")
    ],
) -> None:
    """Burn the version: record the spent checkpoint as partially released.

    The burn is terminal. It freezes the pinned source, tree and manifest
    exactly as recovery found them, abandons the open publication
    operation, and leaves a record that can never return to draft or
    cancelled -- a burned version is corrected by the next version, never
    by reopening this one. ``--reason`` is mandatory and is written
    beside the burned record: a terminal status with no stated cause
    reads as an outcome rather than as an abandonment.

    The verb refuses ``recovery_budget_available`` while any configured
    leg still has a retry left, so a version cannot be declared spent
    while recovery could still succeed.
    """
    flags: GlobalFlags = ctx.obj
    try:
        params = {
            "release": _release_document(release_file, release_key),
            "expected_revision": expected_revision,
            "idempotency_key": idempotency_key,
            "reason": reason,
        }
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    _answer(
        ctx,
        RELEASE_RPC_METHODS["burn"],
        params,
        subject=release_key,
        revision=expected_revision,
    )


@release_app.command("adopt")
def release_adopt(
    ctx: typer.Context,
    release_key: Annotated[
        str, typer.Argument(help="Release key to adopt into, e.g. REL-0.7.0.dev1.")
    ],
    release_file: Annotated[
        Path,
        typer.Option("--release", help="Path to the serialized draft record."),
    ],
    adoption_file: Annotated[
        Path,
        typer.Option("--adoption", help="Path to the observed per-target facts, as JSON."),
    ],
    expected_revision: _ExpectedRevision,
) -> None:
    """Adopt a publication that ran without a release record.

    The verb writes observed facts and nothing else: one independent
    read-back per target, the incident they belong to, and why the
    version is being written up this way. It asserts no approval, runs
    no readiness sweep and pins no manifest, because none of the three
    happened -- and the record model forbids an adoption beside an
    approval reference, so the adopted checkpoint can never be mistaken
    for one that earned its approval.

    Every configured target must carry a read-back or the call is
    refused; a target the checkpoint never declared is recorded and
    named back, because an uncontrolled publication can reach somewhere
    the configuration does not know about.

    The record stays a draft. ``eawf release burn`` ends it, and
    ``eawf release cancel`` is refused on it from here on.
    """
    flags: GlobalFlags = ctx.obj
    try:
        params = {
            "release": _release_document(release_file, release_key),
            "expected_revision": expected_revision,
            "adoption": _read_json_document(adoption_file, label="adoption"),
        }
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    _answer(
        ctx,
        RELEASE_RPC_METHODS["adopt"],
        params,
        subject=release_key,
        revision=expected_revision,
    )


@release_app.command("cancel")
def release_cancel(
    ctx: typer.Context,
    release_key: Annotated[str, typer.Argument(help="Release key to cancel, e.g. REL-0.7.0.dev1.")],
    release_file: Annotated[
        Path,
        typer.Option("--release", help="Path to the serialized record being abandoned."),
    ],
    reason: Annotated[
        str, typer.Option("--reason", help="Why the checkpoint is abandoned; recorded with it.")
    ],
    expected_revision: _ExpectedRevision,
) -> None:
    """Abandon a checkpoint that never touched a registry, or refuse.

    A cancellation asserts that nothing was published under this
    version, so the assertion is checked against the record itself: an
    adopted publication, an opened publication operation or any leg past
    ``not_started`` denies ``release_effect_already_started``. A spent
    version is burned, never cancelled.

    ``--reason`` is mandatory for the same reason the burn's is:
    ``cancelled`` is terminal, so no later transition can explain it.
    """
    flags: GlobalFlags = ctx.obj
    try:
        params = {
            "release": _release_document(release_file, release_key),
            "expected_revision": expected_revision,
            "reason": reason,
        }
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    _answer(
        ctx,
        RELEASE_RPC_METHODS["cancel"],
        params,
        subject=release_key,
        revision=expected_revision,
    )


# ---- command registration ---------------------------------------------------
# Importing the siblings runs their ``@release_app.command(...)`` decorators so
# the app carries every verb the parity map names. The imports sit after
# every shared symbol is defined, so the siblings can import them from here.
from eawf.surfaces.cli.commands import release_candidate as _release_candidate  # noqa: E402, F401
from eawf.surfaces.cli.commands import release_pipeline as _release_pipeline  # noqa: E402, F401
from eawf.surfaces.cli.commands import release_tag as _release_tag  # noqa: E402, F401
from eawf.surfaces.cli.commands import release_train as _release_train  # noqa: E402, F401

__all__ = ["RELEASE_RPC_METHODS", "release_app"]
