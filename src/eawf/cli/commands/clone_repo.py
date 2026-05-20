"""``eawf clone-repo`` — clone a remote git repo and run ``eawf init``.

Surface contract::

    eawf clone-repo <git-url> [--branch <ref>] [--depth <n>]
                              [--target <path>] [--project-code <CODE>]
                              [--profile <id>...]

The command shells out to ``git clone`` (auth via the operator's existing
git credentials and ``GIT_*`` environment — there is **no interactive
authentication** in v0.1) and then delegates to the ``eawf init --no-input``
pipeline against the freshly-cloned tree. Bad URLs surface as
``InvalidInput`` (exit 3); transient git failures (network, auth) surface
as ``LockConflict`` (exit 5) so caller scripts can retry.

Why a separate command? ``git clone`` + ``eawf init`` is two transactions;
combining them lets the v0.1 CLI offer a single "go from zero to a
workspace tree" entry point without forcing users to remember the order.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags
from eawf.state.ids import is_project_code, normalize_to_project_code

logger = logging.getLogger(__name__)

# Recognises https/ssh/file URIs and the ``git@host:path`` shorthand.
# Drops ``http://`` and ``git://`` — both transit cleartext and have no
# authentication story. ``file://`` stays because integration tests
# bootstrap from a local bare repo over that scheme, and locking it out
# would force every CI fixture to spin up a loopback HTTPS server.
_GIT_URL_RE = re.compile(r"^(?:(?:https|ssh|file)://[^\s]+|git@[^:\s]+:[^\s]+)$")


def _validate_git_url(url: str) -> None:
    """Reject non-URL strings before we shell out to git."""
    if not url or not _GIT_URL_RE.match(url):
        raise cli_errors.InvalidInput(
            f"not a git URL: {url!r} (expected https://, ssh://, file://, or git@host:path)"
        )


def _derive_target_dir(url: str, target: Path | None) -> Path:
    """Resolve the on-disk target directory for the clone.

    ``--target`` wins; otherwise we mirror ``git clone``'s default of
    "basename of the URL with the trailing ``.git`` stripped".
    """
    if target is not None:
        return target.resolve()
    # Strip protocol/host/path noise to recover the basename.
    last = url.rstrip("/").rsplit("/", 1)[-1]
    last = last.rsplit(":", 1)[-1]
    if last.endswith(".git"):
        last = last[: -len(".git")]
    if not last:
        raise cli_errors.InvalidInput(
            f"could not derive target directory from URL {url!r}; pass --target"
        )
    return (Path.cwd() / last).resolve()


def _derive_project_code(target_dir: Path, project_code: str | None) -> str:
    """Decide which project code the freshly-cloned tree uses for ``eawf init``.

    Precedence:

    - Explicit ``--project-code`` always wins (after regex validation).
    - Otherwise we delegate to :func:`eawf.state.ids.normalize_to_project_code`
      which uppercases the basename and collapses space/underscore into
      dash before validating against :data:`RE_PROJECT_CODE`. Single
      source of truth for the coercion rules — the wizard validator and
      the clone-repo derivation now agree byte-for-byte.

    Raises :class:`InvalidInput` when neither path produces a valid code.
    """
    if project_code is not None:
        if not is_project_code(project_code):
            raise cli_errors.InvalidInput(
                f"--project-code {project_code!r} must match ^[A-Z][A-Z0-9_-]{{1,15}}$"
            )
        return project_code
    try:
        return normalize_to_project_code(target_dir.name)
    except ValueError as exc:
        raise cli_errors.InvalidInput(f"{exc}; pass --project-code") from exc


def _git_clone(
    *,
    url: str,
    target_dir: Path,
    branch: str | None,
    depth: int | None,
) -> None:
    """Run ``git clone`` and surface errors via the canonical taxonomy.

    Exit-code mapping per the plan §W06:

    - Bad URL / argv mistake -> :class:`InvalidInput` (exit 3).
    - Network / auth / "not a git repository" / unreachable host
      -> :class:`LockConflict` (exit 5) — these are transient by nature.
    """
    if shutil.which("git") is None:
        raise cli_errors.InstrumentMissing(
            "git executable not found on PATH; install git before clone-repo"
        )
    args: list[str] = ["git", "clone"]
    if branch:
        args.extend(["--branch", branch])
    if depth is not None:
        args.extend(["--depth", str(depth)])
    args.extend([url, str(target_dir)])
    logger.info(f"_git_clone: invoking {args}")
    try:
        res = subprocess.run(args, capture_output=True, text=True, check=False, timeout=300.0)
    except subprocess.TimeoutExpired as exc:
        raise cli_errors.LockConflict(f"git clone timed out after 300s: {url}") from exc
    if res.returncode != 0:
        stderr = (res.stderr or "").strip()
        # Treat path-shaped failures as bad input; everything else is transient.
        bad_url_markers = (
            "repository not found",
            "does not appear to be a git repository",
            "not a valid object",
            "invalid",
            "Couldn't find remote ref",
        )
        lower = stderr.lower()
        if any(marker.lower() in lower for marker in bad_url_markers):
            raise cli_errors.InvalidInput(
                f"git clone failed (rc={res.returncode}): {stderr or 'unknown'}"
            )
        raise cli_errors.LockConflict(
            f"git clone failed (rc={res.returncode}): {stderr or 'unknown'}"
        )


def clone_repo_cmd(
    ctx: typer.Context,
    url: Annotated[str, typer.Argument(help="Git URL (https/ssh/git/file://, or git@host:path).")],
    branch: Annotated[
        str | None,
        typer.Option("--branch", help="Branch or tag to clone (forwarded to git --branch)."),
    ] = None,
    depth: Annotated[
        int | None,
        typer.Option("--depth", help="Shallow clone depth (forwarded to git --depth)."),
    ] = None,
    target: Annotated[
        Path | None,
        typer.Option(
            "--target",
            help="Local directory to clone into (default: basename of URL).",
        ),
    ] = None,
    project_code: Annotated[
        str | None,
        typer.Option(
            "--project-code",
            help="Project code to record in the freshly-initialised .ea/. "
            "Defaults to the uppercased target basename.",
        ),
    ] = None,
    project_title: Annotated[
        str | None,
        typer.Option("--project-title", help="Free-form project title."),
    ] = None,
    profile: Annotated[
        list[str] | None,
        typer.Option("--profile", help="Profiles to enable (repeatable; default 'core')."),
    ] = None,
    runtime: Annotated[
        str,
        typer.Option(
            "--runtime",
            help="Default runtime (claude-code|opencode|generic).",
        ),
    ] = "claude-code",
    lifecycle_depth: Annotated[
        str,
        typer.Option(
            "--lifecycle-depth",
            help="Default lifecycle depth (phase|iter|wave).",
        ),
    ] = "phase",
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing .ea/ in the cloned tree (rare).",
        ),
    ] = False,
) -> None:
    """Clone *url* and run ``eawf init --no-input`` against the result.

    Auth in v0.1 relies on the operator's existing git config (``GIT_ASKPASS``,
    SSH agent, ``~/.netrc``, etc.) — the command never prompts for
    credentials.
    """
    from eawf.cli.commands.init import init_cmd as _init_cmd

    flags: GlobalFlags = ctx.obj
    try:
        _validate_git_url(url)
        target_dir = _derive_target_dir(url, target)
        # Validate project code BEFORE cloning so a bad combo fails fast
        # without polluting disk.
        chosen_code = _derive_project_code(target_dir, project_code)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    if target_dir.exists() and any(target_dir.iterdir()):
        cli_errors.emit_error(
            cli_errors.InvalidInput(
                f"target directory not empty: {target_dir}; git clone would refuse to write here"
            ),
            flags=flags,
        )
        return

    try:
        _git_clone(url=url, target_dir=target_dir, branch=branch, depth=depth)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    # The clone succeeded; pivot to the init surface. ``init_cmd`` honours
    # ``--no-input`` semantics via the global flag — if the operator did not
    # pass --no-input the wizard would launch the questionary prompt loop,
    # which is incompatible with a freshly-cloned tree's likely use-case
    # (CI, scripts).
    # We force ``--no-input`` semantics by mutating the resolved flags object
    # for the duration of this call; the original GlobalFlags belong to the
    # parent ctx so we restore on the way out.
    original_no_input = flags.no_input
    flags.no_input = True
    try:
        _init_cmd(
            ctx,
            target=target_dir,
            state_path=Path(".ea/state.json"),
            project_code=chosen_code,
            project_title=project_title,
            profile=profile,
            runtime=runtime,
            lifecycle_depth=lifecycle_depth,
            plugin=None,
            mcp=None,
            acceptance_tests=True,
            acceptance_lint=True,
            acceptance_typecheck=True,
            force=force,
        )
    finally:
        flags.no_input = original_no_input


__all__ = [
    "clone_repo_cmd",
]
