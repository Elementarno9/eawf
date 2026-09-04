"""``eawf release`` Typer sub-app."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

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

release_app = typer.Typer(
    name="release",
    help="Tag releases and render release notes / changelog reports.",
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
    gated on rather than a second opinion computed elsewhere.

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
    from eawf.workflow.verify.release_probes import TagPreflightInputs, build_tag_probes
    from eawf.workflow.verify.release_readiness import compute_readiness

    config = _checkpoint_config(version)
    try:
        probes = build_tag_probes(
            TagPreflightInputs(
                repo_root=repo_root,
                version=version,
                tag=f"v{version}",
                package_version=__version__,
                remote=remote,
            )
        )
        return compute_readiness(
            config,
            probes=probes,
            observed_revision=source,
            computed_at=datetime.now(UTC),
            waiver_count=waiver_count,
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


@release_app.command("train")
def release_train(ctx: typer.Context) -> None:
    """Render the release-train ladder and the checkpoint currently open."""
    from eawf.workflow.release.train import V07_TRAIN

    flags: GlobalFlags = ctx.obj
    payload = {
        "train_id": V07_TRAIN.train_id,
        "target_version": V07_TRAIN.target_version,
        "current_checkpoint_index": V07_TRAIN.current_checkpoint_index,
        "checkpoints": [rung.model_dump(mode="json") for rung in V07_TRAIN.checkpoints],
    }
    lines = [f"{V07_TRAIN.train_id} -> {V07_TRAIN.target_version}"]
    for index, rung in enumerate(V07_TRAIN.checkpoints):
        marker = "*" if index == V07_TRAIN.current_checkpoint_index else " "
        lines.append(
            f" {marker} {rung.release_key}  epoch={rung.authority_epoch} "
            f"profile={rung.gate_profile.value} "
            f"membership={'required' if rung.requires_membership else 'forbidden'}"
        )
    emit_json_or_text(payload, "\n".join(lines), flags=flags)


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
    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

    flags: GlobalFlags = ctx.obj
    try:
        record = _read_json_document(release_file, label="release record")
        if record.get("key") != release_key:
            raise cli_errors.UserError(
                f"{release_file} holds release {record.get('key')!r}, not {release_key!r}",
                kind="InvalidInput",
            )
        params: dict[str, object] = {
            "release": record,
            "expected_revision": record.get("revision", 0),
            "idempotency_key": idempotency_key,
            "target_id": target,
            "manifest": _read_json_document(manifest_file, label="frozen manifest"),
            "effect_receipt_ref": effect_receipt_ref,
        }
        if response_file is not None:
            params["response"] = _read_json_document(response_file, label="recorded response")
        with DaemonClient() as client:
            result = client.call("release.observe_target", params)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    except DaemonRpcError as exc:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"daemon rejected release.observe_target: code={exc.code} {exc.message}",
                kind="DaemonError",
            ),
            flags=flags,
        )
        return
    except (OSError, RuntimeError) as exc:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"daemon unavailable for release.observe_target: {exc}", kind="DaemonError"
            ),
            flags=flags,
        )
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
    """Compute every release readiness signal for one checkpoint.

    The sweep never fail-fasts: all twelve signals are reported on every
    run, so one pass shows the whole repair list rather than the first
    red row. Signals whose producer has not landed yet report
    ``unavailable`` and name the gap.

    This is the same sweep ``eawf release tag --push`` is gated on and
    the same one the release workflow runs before its publish job, so a
    tag pushed by hand meets it too. A non-ready sweep exits non-zero
    after printing every row -- the whole repair list, then the refusal.
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


__all__ = ["release_app"]
