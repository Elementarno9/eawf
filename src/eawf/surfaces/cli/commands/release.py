"""``eawf release`` Typer sub-app.

Two kinds of verb live here. ``tag``, ``changelog``, ``notes``,
``preflight`` and ``train show`` are local: they read the checkout and
the authored checkpoint and print. The rest dispatch to the daemon's
``release.*`` JSON-RPC namespace, because they read or write records the
daemon owns.

:data:`RELEASE_RPC_METHODS` is the parity map between the two -- every
registered ``release.*`` method names the subcommand that reaches it, so
a verb added to the daemon without an operator surface reds the parity
test rather than shipping as substrate nobody can call.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final

import orjson
import typer

from eawf.kernel.state.resolve import resolve_with_reason
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    from eawf.kernel.spec.release_config import ReleaseConfig
    from eawf.kernel.state.models import State
    from eawf.workflow.verify.release_readiness import ReleaseReadiness

logger = logging.getLogger(__name__)

#: ``eawf release <subcommand>`` -> the ``release.*`` JSON-RPC method it
#: dispatches to. The map is asserted total over the daemon's registered
#: release namespace, so it is the contract "no release verb is
#: unreachable", not a convenience index.
RELEASE_RPC_METHODS: Final[Mapping[str, str]] = {
    "show": "release.show",
    "readiness": "release.compute_readiness",
    "create": "release.create",
    "approve": "release.approve",
    "publish": "release.publish",
    "retry": "release.retry_target",
    "reconcile": "release.reconcile",
    "observe": "release.observe_target",
    "burn": "release.burn",
    "adopt": "release.adopt",
    "cancel": "release.cancel",
    "advance": "release.advance_train",
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


def _checkpoint_config(version: str) -> ReleaseConfig:
    """Return the authored checkpoint configuration for *version*.

    Args:
        version: Normalized checkpoint version, e.g. ``0.7.0.dev1``.

    Returns:
        The loaded, train-validated configuration.

    Raises:
        cli_errors.UserError: When no configuration is authored for
            *version* -- an unauthored version has no gates to pass, so
            it cannot be published rather than being published unchecked.
        cli_errors.ValidationError: When the authored configuration is
            rejected by the loader.
    """
    from eawf.kernel.spec.release_config import ReleaseConfigError, load_release_config
    from eawf.workflow.release.train import V07_TRAIN, checkpoint_config_yaml

    try:
        return load_release_config(checkpoint_config_yaml(version), train=V07_TRAIN)
    except KeyError as exc:
        raise cli_errors.UserError(
            f"no release configuration authored for {version!r}; author its checkpoint "
            f"before tagging or publishing it",
            kind="NotFound",
        ) from exc
    except ReleaseConfigError as exc:
        raise cli_errors.ValidationError(f"{exc.code.value}: {exc}") from exc


def _sweep_release(
    *,
    version: str,
    remote: str,
    repo_root: Path,
    source: str | None,
    waiver_count: int,
) -> ReleaseReadiness:
    """Compute the full readiness sweep for *version* over *repo_root*.

    The tag chokepoint and the ``preflight`` verb both come through
    here, so the sweep an operator inspects is the one the push is
    gated on rather than a second opinion computed elsewhere. The
    composition itself lives in
    :func:`~eawf.runtime.release.chokepoint.sweep_for_tag`; what this
    wrapper adds is the CLI's configuration resolution and its error
    vocabulary.

    Args:
        version: Checkpoint version being swept.
        remote: Remote whose branch ancestry is proven against.
        repo_root: Working copy the probes read.
        source: Revision the sweep is stamped with.
        waiver_count: Waivers recorded against the checkpoint.

    Returns:
        The total readiness object.

    Raises:
        cli_errors.CliError: When the configuration is missing or
            invalid, or the sweep's arguments are rejected.
    """
    from datetime import UTC, datetime

    from eawf import __version__
    from eawf.runtime.release import sweep_for_tag

    config = _checkpoint_config(version)
    try:
        return sweep_for_tag(
            config,
            version=version,
            repo_root=repo_root,
            remote=remote,
            package_version=__version__,
            source=source,
            waiver_count=waiver_count,
            computed_at=datetime.now(UTC),
        )
    except ValueError as exc:
        raise cli_errors.ValidationError(str(exc)) from exc


def _refuse_unready(readiness: ReleaseReadiness, *, tag: str) -> None:
    """Raise the named refusal when *readiness* does not clear the push.

    Args:
        readiness: The sweep computed for the tag.
        tag: The tag the push would create.

    Raises:
        cli_errors.StateConflict: With ``data.kind="ReleaseNotReady"``
            when a required signal is not passing or a waiver is
            outstanding.
    """
    if readiness.ready:
        logger.info(f"release_preflight_green release_key={readiness.release_key!r} tag={tag!r}")
        return
    red = readiness.first_red
    if red is None:
        detail = f"{readiness.waiver_count} outstanding waiver(s)"
    else:
        row = readiness.row(red)
        code = row.failure_code.value if row.failure_code is not None else row.status.value
        detail = f"first red signal {red.value} ({code}): {row.remediation}"
    version = readiness.version
    raise cli_errors.StateConflict(
        f"release preflight refuses {tag}: {detail} -- run "
        f"`eawf release preflight {version}` for the full sweep",
        kind="ReleaseNotReady",
    )


def _dirty_paths(repo_root: Path) -> tuple[str, ...]:
    """Return the porcelain status lines of *repo_root*.

    Args:
        repo_root: Working copy to inspect.

    Returns:
        One line per uncommitted path; empty when the tree is clean.

    Raises:
        subprocess.CalledProcessError: When git refuses to report.
    """
    import subprocess

    status = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    return tuple(line for line in status.stdout.splitlines() if line.strip())


def _head_revision(repo_root: Path) -> str | None:
    """Return the HEAD sha of *repo_root*, or ``None`` when it has none."""
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None


def _waiver_for_dirty_tree(
    *, dirty: tuple[str, ...], reason: str | None
) -> dict[str, str | int] | None:
    """Return the waiver record admitting *dirty*, or ``None`` when clean.

    A dirty release tree is refused outright. ``--force`` used to admit
    it silently, which made the tag a claim about a tree nobody has:
    the only way past it now is an explicit waiver that states why, and
    the waiver is carried into the sweep so it can never read as green.

    Args:
        dirty: Porcelain status lines of the release tree.
        reason: The operator's waiver reason, or ``None`` when no
            waiver was requested.

    Returns:
        The waiver record when a reason admits a dirty tree, otherwise
        ``None``.

    Raises:
        cli_errors.UserError: With ``data.kind="DirtyReleaseTree"`` when
            the tree is dirty and unwaived, or with
            ``data.kind="InvalidInput"`` when the waiver states no
            reason.
    """
    from eawf.workflow.verify.release_readiness import (
        ReleaseSignalFailureCode,
        ReleaseSignalName,
    )

    if not dirty:
        return None
    if reason is None:
        raise cli_errors.UserError(
            f"dirty_release_tree: {len(dirty)} uncommitted path(s) in the release tree; "
            f"commit or stash them, or record a waiver with --waive-dirty-tree '<reason>'",
            kind="DirtyReleaseTree",
        )
    if not reason.strip():
        raise cli_errors.UserError(
            "--waive-dirty-tree requires a non-empty reason; a waiver that states nothing "
            "records nothing",
            kind="InvalidInput",
        )
    waiver: dict[str, str | int] = {
        "signal": ReleaseSignalName.TREE_CLEANLINESS.value,
        "failure_code": ReleaseSignalFailureCode.DIRTY_RELEASE_TREE.value,
        "reason": reason.strip(),
        "dirty_path_count": len(dirty),
    }
    logger.warning(
        f"release_tag_waiver signal={ReleaseSignalName.TREE_CLEANLINESS.value} "
        f"dirty_paths={len(dirty)} reason={reason.strip()!r}"
    )
    return waiver


def _create_and_push_tag(*, tag: str, remote: str, push: bool, force: bool) -> bool:
    """Create the annotated *tag* and optionally push it to *remote*.

    Args:
        tag: Tag to create.
        remote: Remote to push to.
        push: Whether to push after tagging.
        force: Whether to overwrite an existing tag / force the push.

    Returns:
        Whether the tag was pushed.

    Raises:
        subprocess.CalledProcessError: When git refuses the tag or push.
    """
    import subprocess

    tag_cmd = ["git", "tag", "-a", tag, "-m", f"Release {tag}"]
    if force:
        tag_cmd.insert(2, "-f")
    subprocess.run(tag_cmd, check=True)
    if not push:
        return False
    push_cmd = ["git", "push"]
    if force:
        push_cmd.append("--force")
    push_cmd.extend([remote, tag])
    subprocess.run(push_cmd, check=True)
    return True


@release_app.command("tag")
def release_tag(
    ctx: typer.Context,
    version: Annotated[
        str | None,
        typer.Argument(help="Version to tag (default: the current package version)."),
    ] = None,
    push: Annotated[
        bool,
        typer.Option("--push", help="Push the tag to the remote, triggering the release pipeline."),
    ] = False,
    remote: Annotated[
        str,
        typer.Option("--remote", help="Remote to push the tag to."),
    ] = "origin",
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite an existing tag (never admits a dirty tree)."),
    ] = False,
    waive_dirty_tree: Annotated[
        str | None,
        typer.Option(
            "--waive-dirty-tree",
            help="Reason admitting a dirty release tree; recorded as a waiver.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the tag/push plan without running git."),
    ] = False,
) -> None:
    """Create the ``v<version>`` release tag and (with ``--push``) trigger the pipeline.

    The release workflow (``.github/workflows/release.yaml``) fires on a
    ``v0.*`` tag push and publishes to PyPI behind the tag-push
    condition, so the push *is* the publication decision. ``--push``
    therefore runs the full readiness sweep first -- version
    consistency, the changelog section, the migration outcome, ancestry
    against the remote and tree cleanliness -- and refuses to create or
    push the tag while any required signal is red. The same sweep runs
    again in the workflow before the publish job, so a hand-pushed tag
    cannot walk past it either.

    A dirty release tree is refused outright: ``--force`` overwrites an
    existing tag and nothing else. The only way to tag over uncommitted
    work is ``--waive-dirty-tree '<reason>'``, which records the reason
    as a waiver and keeps the sweep from ever reading green.
    """
    import subprocess

    from eawf import __version__

    flags: GlobalFlags = ctx.obj
    resolved = version or __version__
    tag = f"v{resolved}"
    repo_root = Path.cwd()
    try:
        waiver = _waiver_for_dirty_tree(dirty=_dirty_paths(repo_root), reason=waive_dirty_tree)
        readiness = None
        if push:
            readiness = _sweep_release(
                version=resolved,
                remote=remote,
                repo_root=repo_root,
                source=_head_revision(repo_root),
                waiver_count=1 if waiver is not None else 0,
            )
            _refuse_unready(readiness, tag=tag)
        listing = subprocess.run(
            ["git", "tag", "--list", tag],
            capture_output=True,
            text=True,
            check=True,
        )
        tag_exists = bool(listing.stdout.strip())
        plan: dict[str, object] = {
            "tag": tag,
            "version": resolved,
            "push": push,
            "remote": remote,
            "tag_exists": tag_exists,
            "dry_run": dry_run,
            "waiver": waiver,
            "preflight_ready": None if readiness is None else readiness.ready,
        }
        if dry_run:
            suffix = f" and push to {remote} (triggers pipeline)" if push else ""
            emit_json_or_text(plan, f"dry-run: would create tag {tag}{suffix}", flags=flags)
            return
        if tag_exists and not force:
            raise cli_errors.UserError(
                f"tag {tag!r} already exists; pass --force to overwrite", kind="InvalidInput"
            )
        pushed = _create_and_push_tag(tag=tag, remote=remote, push=push, force=force)
        plan["pushed"] = pushed
        waived = "" if waiver is None else f" over a waived dirty tree ({waiver['reason']})"
        text = (
            f"tagged {tag}{waived}; pushed to {remote} (pipeline triggered)"
            if pushed
            else f"tagged {tag}{waived}; run `git push {remote} {tag}` to trigger the pipeline"
        )
        emit_json_or_text(plan, text, flags=flags)
    except subprocess.CalledProcessError as exc:
        cli_errors.emit_error(
            cli_errors.UserError(f"git command failed: {exc}", kind="InvalidInput"), flags=flags
        )
        return
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return


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
    """Render the ladder, the open index and each checkpoint's status."""
    from eawf.workflow.release.advance import render_train_ladder, render_train_ladder_text
    from eawf.workflow.release.train import V07_TRAIN

    flags: GlobalFlags = ctx.obj
    if json_output:
        flags = replace(flags, json_output=True)
    emit_json_or_text(
        render_train_ladder(V07_TRAIN), render_train_ladder_text(V07_TRAIN), flags=flags
    )


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


def _dispatch(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Call one ``release.*`` JSON-RPC method and return its result.

    Args:
        method: Fully-qualified method name, e.g. ``release.publish``.
        params: Already-assembled JSON-RPC params.

    Returns:
        The handler's result object.

    Raises:
        cli_errors.UserError: With ``data.kind="DaemonError"`` when the
            daemon refuses the call or cannot be reached. The refusal is
            surfaced verbatim: a release verb denied for a named reason
            is the answer, not a failure to be reworded.
    """
    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

    try:
        with DaemonClient() as client:
            return client.call(method, params)
    except DaemonRpcError as exc:
        raise cli_errors.UserError(
            f"daemon rejected {method}: code={exc.code} {exc.message}", kind="DaemonError"
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise cli_errors.UserError(
            f"daemon unavailable for {method}: {exc}", kind="DaemonError"
        ) from exc


def _record_line(result: dict[str, Any]) -> str:
    """Return the one-line summary of a reply carrying a release record."""
    record = result.get("release") or {}
    return f"{record.get('key')} {record.get('status')} revision={record.get('revision')}"


def _operation_line(result: dict[str, Any]) -> str:
    """Return the operator-facing summary of a publication-verb reply."""
    operation = result.get("operation") or {}
    rows = operation.get("publication_receipts") or ()
    legs = ", ".join(
        f"{row.get('target_id')}#{row.get('attempt')}={row.get('status')}"
        for row in rows
        if isinstance(row, dict)
    )
    replayed = " (replayed)" if result.get("replayed") else ""
    return f"{_record_line(result)}{replayed}\n  operation: {result.get('operation_ref')}\n  {legs}"


@release_app.command("observe")
def release_observe(
    ctx: typer.Context,
    release_key: Annotated[
        str, typer.Argument(help="Release key to observe, e.g. REL-0.7.0.dev1.")
    ],
    target: Annotated[
        str, typer.Option("--target", help="Publication target to read back, e.g. pypi.")
    ],
    release_file: Annotated[
        Path,
        typer.Option("--release", help="Path to the serialized Release record being observed."),
    ],
    manifest_file: Annotated[
        Path,
        typer.Option("--manifest", help="Path to the frozen manifest the release approved."),
    ],
    idempotency_key: Annotated[
        str, typer.Option("--idempotency-key", help="Replay identity of this read-back.")
    ],
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

    No HTTP client ships here yet, so ``--response`` is how the recorded
    registry answer reaches the adapter. Without it the leg's reader
    reports ``registry_unreachable`` and the verb refuses -- which is the
    honest answer for a registry nobody queried.
    """
    flags: GlobalFlags = ctx.obj
    try:
        record = _release_document(release_file, release_key)
        params: dict[str, Any] = {
            "release": record,
            "expected_revision": record.get("revision", 0),
            "idempotency_key": idempotency_key,
            "target_id": target,
            "manifest": _read_json_document(manifest_file, label="frozen manifest"),
            "effect_receipt_ref": effect_receipt_ref,
        }
        if response_file is not None:
            params["response"] = _read_json_document(response_file, label="recorded response")
        result = _dispatch(RELEASE_RPC_METHODS["observe"], params)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    observation = result.get("observation") or {}
    observed = result.get("release") or {}
    text = (
        f"{release_key} target {target}: {observation.get('result')} "
        f"({observation.get('code')}) -> release {observed.get('status')}\n"
        f"  identity: {observation.get('queried_identity')}\n"
        f"  evidence: {observation.get('evidence_ref')}\n"
        f"  {observation.get('detail')}"
    )
    emit_json_or_text(result, text, flags=flags)


@release_app.command("preflight")
def release_preflight(
    ctx: typer.Context,
    version: Annotated[str, typer.Argument(help="Checkpoint version, e.g. 0.7.0.dev1.")],
    source: Annotated[
        str | None,
        typer.Option("--source", help="Source revision the sweep is computed against."),
    ] = None,
    remote: Annotated[
        str,
        typer.Option("--remote", help="Remote the source must be reachable from."),
    ] = "origin",
    waiver_count: Annotated[
        int,
        typer.Option("--waiver-count", help="Gate waivers recorded against the checkpoint."),
    ] = 0,
) -> None:
    """Sweep every readiness signal for one checkpoint over this checkout.

    The sweep never fail-fasts: all twelve signals are reported on every
    run, so one pass shows the whole repair list rather than the first
    red row. Signals whose producer has not landed yet report
    ``unavailable`` and name the gap.

    This is the same sweep ``eawf release tag --push`` is gated on and
    the same one the release workflow runs before its publish job, so a
    tag pushed by hand meets it too. A non-ready sweep exits non-zero
    after printing every row -- the whole repair list, then the refusal.

    It runs the tag probes against the working copy, which is what makes
    it the tag chokepoint. ``eawf release readiness`` asks the daemon for
    the same sweep without them, and is the verb to reach for when the
    question is which status a candidate record would land in.
    """
    flags: GlobalFlags = ctx.obj
    try:
        readiness = _sweep_release(
            version=version,
            remote=remote,
            repo_root=Path.cwd(),
            source=source,
            waiver_count=waiver_count,
        )
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    required = set(readiness.required_signals)
    lines = [
        f"{readiness.release_key}  profile={readiness.gate_profile.value}  "
        f"ready={readiness.ready}  waivers={readiness.waiver_count}"
    ]
    for row in readiness.signals:
        flag = "REQ" if row.signal in required else "   "
        lines.append(f" {flag} {row.signal.value:<20} {row.status.value}")
    if readiness.first_red is not None:
        lines.append(f"first red: {readiness.first_red.value}")
    emit_json_or_text(readiness.model_dump(mode="json"), "\n".join(lines), flags=flags)
    if not readiness.ready:
        raise typer.Exit(exit_codes.STATE_CONFLICT)


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
    rung nobody has opened answers ``record: none`` rather than refusing.
    """
    flags: GlobalFlags = ctx.obj
    try:
        result = _dispatch(RELEASE_RPC_METHODS["show"], {"version": version})
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    checkpoint = result.get("checkpoint") or {}
    record = result.get("record")
    standing = "none (never opened)" if record is None else _record_line({"release": record})
    text = (
        f"{result.get('train_id')} -> {result.get('target_version')}  "
        f"index={result.get('current_checkpoint_index')}\n"
        f"  checkpoint: {checkpoint.get('version')} ({checkpoint.get('release_key')})\n"
        f"  record: {standing}"
    )
    emit_json_or_text(result, text, flags=flags)


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
        result = _dispatch(RELEASE_RPC_METHODS["readiness"], params)
    except KeyError as exc:
        cli_errors.emit_error(
            cli_errors.ValidationError(f"waiver document is missing the {exc} key"), flags=flags
        )
        return
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    readiness = result.get("readiness") or {}
    rows = readiness.get("signals") or ()
    lines = [
        f"{readiness.get('release_key')}  ready={readiness.get('ready')}  "
        f"waivers={readiness.get('waiver_count')}"
    ]
    lines.extend(
        f"  {row.get('signal'):<20} {row.get('status')}" for row in rows if isinstance(row, dict)
    )
    if result.get("first_red") is not None:
        lines.append(f"first red: {result['first_red']}")
    if "next_status" in result:
        lines.append(f"candidate would become: {result['next_status']}")
    emit_json_or_text(result, "\n".join(lines), flags=flags)


@release_app.command("create")
def release_create(
    ctx: typer.Context,
    version: Annotated[str, typer.Argument(help="Checkpoint version to open, e.g. 0.7.0.dev2.")],
    membership_ref: Annotated[
        list[str] | None,
        typer.Option("--membership-ref", help="Milestone acceptance bundle; repeatable."),
    ] = None,
) -> None:
    """Open one checkpoint's DRAFT record, after measured admission.

    A checkpoint the admission table covers cannot be opened until every
    measured contract backing it is promoted and resolvable. The refusal
    names the single missing contract plus the command that promotes it,
    so the next action is in the error rather than in a runbook.
    """
    flags: GlobalFlags = ctx.obj
    try:
        result = _dispatch(
            RELEASE_RPC_METHODS["create"],
            {"version": version, "membership_refs": list(membership_ref or ())},
        )
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    contracts = ", ".join(result.get("measured_contracts") or ()) or "(none required)"
    text = (
        f"{_record_line(result)}\n"
        f"  record: {result.get('release_record_id')}\n"
        f"  measured contracts: {contracts}"
    )
    emit_json_or_text(result, text, flags=flags)


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
) -> None:
    """Approve a candidate against a readiness sweep, and record it.

    The approval is the decision the whole publication path is authorised
    by, so it is written to the release-record collection before the
    reply is built. A sweep whose required signals are not all passing
    denies ``release_not_ready`` naming the first red row.
    """
    flags: GlobalFlags = ctx.obj
    try:
        result = _dispatch(
            RELEASE_RPC_METHODS["approve"],
            {
                "release": _release_document(release_file, release_key),
                "readiness": _read_json_document(readiness_file, label="readiness sweep"),
                "approval_ref": approval_ref,
            },
        )
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    text = f"{_record_line(result)}\n  record: {result.get('release_record_id')}"
    emit_json_or_text(result, text, flags=flags)


@release_app.command("publish")
def release_publish(
    ctx: typer.Context,
    release_key: Annotated[
        str, typer.Argument(help="Release key to publish, e.g. REL-0.7.0.dev1.")
    ],
    release_file: Annotated[
        Path,
        typer.Option("--release", help="Path to the serialized approved record."),
    ],
    approved_manifest_digest: Annotated[
        str, typer.Option("--approved-manifest-digest", help="Manifest digest the approval bound.")
    ],
    proof_digest: Annotated[
        str, typer.Option("--proof-digest", help="Digest binding the exact artifact set.")
    ],
    idempotency_key: Annotated[
        str, typer.Option("--idempotency-key", help="Replay identity of this publication.")
    ],
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
    the same payload returns the original receipt instead of publishing
    twice.
    """
    flags: GlobalFlags = ctx.obj
    try:
        record = _release_document(release_file, release_key)
        result = _dispatch(
            RELEASE_RPC_METHODS["publish"],
            {
                "release": record,
                "expected_revision": record.get("revision", 0),
                "idempotency_key": idempotency_key,
                "approved_manifest_digest": approved_manifest_digest,
                "proof_digest": proof_digest,
                "observed_revision": source,
                "waiver_count": waiver_count,
            },
        )
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    emit_json_or_text(result, _operation_line(result), flags=flags)


@release_app.command("retry")
def release_retry(
    ctx: typer.Context,
    release_key: Annotated[str, typer.Argument(help="Release key whose leg is re-queued.")],
    target: Annotated[str, typer.Option("--target", help="The single leg to re-queue.")],
    release_file: Annotated[
        Path,
        typer.Option("--release", help="Path to the serialized record being retried."),
    ],
    proof_digest: Annotated[
        str,
        typer.Option("--proof-digest", help="Artifact-set digest; must equal the operation's."),
    ],
    idempotency_key: Annotated[
        str, typer.Option("--idempotency-key", help="Replay identity of this retry.")
    ],
) -> None:
    """Re-queue one leg of the open episode under the idempotency proof.

    The proof digest must equal the open operation's: a retry against a
    different artifact set is a different publication wearing the same
    version, and the verb refuses it ``unsafe_release_retry`` rather than
    letting one version mean two builds.
    """
    flags: GlobalFlags = ctx.obj
    try:
        record = _release_document(release_file, release_key)
        result = _dispatch(
            RELEASE_RPC_METHODS["retry"],
            {
                "release": record,
                "expected_revision": record.get("revision", 0),
                "idempotency_key": idempotency_key,
                "target_id": target,
                "proof_digest": proof_digest,
            },
        )
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    emit_json_or_text(result, _operation_line(result), flags=flags)


@release_app.command("reconcile")
def release_reconcile(
    ctx: typer.Context,
    release_key: Annotated[str, typer.Argument(help="Release key whose leg is settled.")],
    target: Annotated[str, typer.Option("--target", help="The leg whose adapter reported late.")],
    release_file: Annotated[
        Path,
        typer.Option("--release", help="Path to the serialized record being reconciled."),
    ],
    idempotency_key: Annotated[
        str, typer.Option("--idempotency-key", help="Replay identity of this reconciliation.")
    ],
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
    """
    flags: GlobalFlags = ctx.obj
    try:
        if (status is None) == (receipt_file is None):
            raise cli_errors.UserError(
                "reconcile takes exactly one of --status (the asserted result) or --receipt "
                "(the publish job's own, which decides the status)",
                kind="InvalidInput",
            )
        record = _release_document(release_file, release_key)
        params: dict[str, Any] = {
            "release": record,
            "expected_revision": record.get("revision", 0),
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
        result = _dispatch(RELEASE_RPC_METHODS["reconcile"], params)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    emit_json_or_text(result, _operation_line(result), flags=flags)


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
        record = _release_document(release_file, release_key)
        result = _dispatch(
            RELEASE_RPC_METHODS["burn"],
            {
                "release": record,
                "expected_revision": record.get("revision", 0),
                "idempotency_key": idempotency_key,
                "reason": reason,
            },
        )
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    settled = _operation_line(result) if result.get("operation_ref") else _record_line(result)
    text = (
        f"{settled}\n  record: {result.get('release_record_id')}\n  reason: {result.get('reason')}"
    )
    emit_json_or_text(result, text, flags=flags)


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
        record = _release_document(release_file, release_key)
        result = _dispatch(
            RELEASE_RPC_METHODS["adopt"],
            {
                "release": record,
                "expected_revision": record.get("revision", 0),
                "adoption": _read_json_document(adoption_file, label="adoption"),
            },
        )
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    observed = result.get("observed_targets") or {}
    legs = ", ".join(f"{target}={status}" for target, status in sorted(observed.items()))
    unconfigured = ", ".join(result.get("unconfigured_targets") or ()) or "(none)"
    text = (
        f"{_record_line(result)}\n"
        f"  record: {result.get('release_record_id')}\n"
        f"  observed: {legs}\n"
        f"  not configured: {unconfigured}"
    )
    emit_json_or_text(result, text, flags=flags)


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
        record = _release_document(release_file, release_key)
        result = _dispatch(
            RELEASE_RPC_METHODS["cancel"],
            {
                "release": record,
                "expected_revision": record.get("revision", 0),
                "reason": reason,
            },
        )
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    text = (
        f"{_record_line(result)}\n"
        f"  record: {result.get('release_record_id')}\n"
        f"  reason: {result.get('reason')}"
    )
    emit_json_or_text(result, text, flags=flags)


@release_app.command("advance")
def release_advance(
    ctx: typer.Context,
    release_key: Annotated[str, typer.Argument(help="Release key standing at the open rung.")],
    release_file: Annotated[
        Path,
        typer.Option("--release", help="Path to the serialized record at the open checkpoint."),
    ],
    receipt_file: Annotated[
        list[Path] | None,
        typer.Option("--receipt", help="A checkpoint gate receipt, as JSON; repeatable."),
    ] = None,
    membership_ref: Annotated[
        list[str] | None,
        typer.Option("--membership-ref", help="Milestone bundle for the rung being opened."),
    ] = None,
) -> None:
    """Walk the train onto its next rung, or refuse and change nothing.

    The index moves only from a baked or released checkpoint whose every
    required gate receipt still binds its exact source and manifest. The
    closing record comes back unchanged beside the opened DRAFT, so the
    caller can assert the prior rung was not rewritten.
    """
    flags: GlobalFlags = ctx.obj
    try:
        result = _dispatch(
            RELEASE_RPC_METHODS["advance"],
            {
                "release": _release_document(release_file, release_key),
                "receipts": [
                    _read_json_document(path, label="gate receipt") for path in (receipt_file or ())
                ],
                "membership_refs": list(membership_ref or ()),
            },
        )
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    closed = result.get("closed") or {}
    opened = result.get("opened") or {}
    train = result.get("train") or {}
    text = (
        f"closed {closed.get('key')} ({closed.get('status')}) -> "
        f"opened {opened.get('key')} ({opened.get('status')})\n"
        f"  index: {train.get('current_checkpoint_index')}\n"
        f"  receipts: {', '.join(result.get('receipt_refs') or ()) or '(none)'}"
    )
    emit_json_or_text(result, text, flags=flags)


__all__ = ["RELEASE_RPC_METHODS", "release_app"]
