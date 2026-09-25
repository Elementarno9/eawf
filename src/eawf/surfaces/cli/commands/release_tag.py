"""``eawf release tag`` and ``eawf release preflight``: the local sweep.

Split out of :mod:`eawf.surfaces.cli.commands.release`, whose
:data:`~eawf.surfaces.cli.commands.release.release_app` these two verbs
attach to, the same way the train-walking and candidate verbs are. Both
read the checkout and the authored checkpoint configuration directly
rather than dispatching to the daemon: the tag push and the preflight
sweep are decisions about the working copy in front of the operator, not
about a record a store holds.

From ``dev3`` on, a rung's authored configuration renders with no
membership bundles of its own -- which bundles it accepts is a fact
about the cut being made, not about the train. ``eawf release create``
learns them from an operator-supplied ``--membership-ref``, but tag and
preflight cannot take the same option: the tag is pushed, and CI's own
preflight step (``.github/workflows/release.yaml``) runs
``eawf release preflight <version> --source <sha>`` from an automated
pipeline job with nothing to pass and no record open yet to read them
from either. Both verbs instead resolve the configuration with
:func:`~eawf.workflow.release.admission.committed_membership_refs`,
which reads the same committed canary export the ``membership`` signal
itself reads. The cardinality check still refuses a membership rung
whose checkout carries no matching, accepted export; a reference that
does not resolve is caught by the sweep's own ``membership`` signal, not
raised here, so the sweep keeps reporting every row on every run rather
than stopping at the first one.

Known limit: at tag/preflight time the ``membership`` row is checking
the committed export against itself, since the refs it resolves against
come from that same export -- it can only fail by the export naming no
accepted Milestone for this release at all. The record ``release
create`` opens still carries its own, independently supplied
``membership_refs``, checked against the export by
:func:`~eawf.workflow.release.admission.assert_membership_resolves`; that
is where a mismatch between what was cut and what was accepted is
actually caught.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.commands.release import release_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    from eawf.kernel.spec.release_config import ReleaseConfig
    from eawf.workflow.verify.release_readiness import ReleaseReadiness

logger = logging.getLogger(__name__)


def _checkpoint_config(version: str, *, repo_root: Path) -> ReleaseConfig:
    """Return the authored checkpoint configuration for *version*.

    Args:
        version: Normalized checkpoint version, e.g. ``0.7.0.dev1``.
        repo_root: Checkout the checkpoint's committed acceptance
            bundles, if any, are read from. From ``dev3`` on the
            rendered document carries no membership bundles of its own,
            so :func:`~eawf.workflow.release.admission.committed_membership_refs`
            reads them off the committed canary export instead, and the
            loader's cardinality check runs against that real list
            rather than the empty one the template renders. Read but
            unused for a rung that does not require membership.

    Returns:
        The loaded, train-validated configuration.

    Raises:
        cli_errors.UserError: When no configuration is authored for
            *version* -- an unauthored version has no gates to pass, so
            it cannot be published rather than being published unchecked.
        cli_errors.ValidationError: When the authored configuration is
            rejected by the loader -- including
            ``invalid_membership_cardinality`` when a rung that requires
            membership finds no matching export to resolve against.
    """
    from eawf.kernel.release.checkpoint_template import with_membership_refs
    from eawf.kernel.spec.release import release_key
    from eawf.kernel.spec.release_config import ReleaseConfigError, load_release_config
    from eawf.workflow.release.admission import committed_membership_refs
    from eawf.workflow.release.train import V07_TRAIN, checkpoint_config_yaml

    try:
        source = checkpoint_config_yaml(version)
    except KeyError as exc:
        raise cli_errors.UserError(
            f"no release configuration authored for {version!r}; author its checkpoint "
            f"before tagging or publishing it",
            kind="NotFound",
        ) from exc
    try:
        membership_refs = committed_membership_refs(repo_root, release_key(version))
    except ValueError as exc:
        raise cli_errors.ValidationError(f"committed canary evidence: {exc}") from exc
    document: str | Mapping[str, Any] = source
    if membership_refs:
        document = with_membership_refs(source, membership_refs=membership_refs)
    try:
        return load_release_config(document, train=V07_TRAIN)
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

    config = _checkpoint_config(version, repo_root=repo_root)
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
    """Return the porcelain status lines of *repo_root*, minus the release stores.

    The tree check shares its git query with the ``tree_cleanliness``
    probe, so a release store the release verbs wrote refuses neither.

    Args:
        repo_root: Working copy to inspect.

    Returns:
        One line per uncommitted path; empty when the tree is clean.

    Raises:
        subprocess.CalledProcessError: When git refuses to report.
    """
    from eawf.workflow.verify.release_probes import release_tree_status

    status = release_tree_status(repo_root)
    status.check_returncode()
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

    From ``dev3`` on, the checkpoint requires acceptance bundles its
    rendered configuration cannot carry on its own; they are read off
    the committed canary export rather than taken as an option here (see
    the module docstring). Swept with no matching, accepted export, a
    membership rung refuses ``invalid_membership_cardinality`` before
    the tag plan is even printed.
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
    the same one the release workflow runs before its publish job (with
    no options of its own -- see the module docstring), so a tag pushed
    by hand meets it too. A non-ready sweep exits non-zero after printing
    every row -- the whole repair list, then the refusal.

    It runs the tag probes against the working copy, which is what makes
    it the tag chokepoint. ``eawf release readiness`` asks the daemon for
    the same sweep at the commit a candidate record pins, and is the verb
    to reach for when the question is which status that record would
    land in.

    From ``dev3`` on, the checkpoint's acceptance bundles are read off
    the committed canary export: swept over a checkout with no matching,
    accepted export, a membership rung refuses
    ``invalid_membership_cardinality`` before a single signal runs. A
    reference the export does not accept still shows up as the
    ``membership`` row going red, alongside every other signal.
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


__all__ = ["release_preflight", "release_tag"]
