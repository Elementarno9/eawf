"""Decide whether a CI run must execute the heavy test jobs.

A push that changes only state bookkeeping (``.ea/state.json``, the
``.ea/store/`` ledgers and ``.secrets.baseline``) leaves the code tree
exactly as it was when CI last went green, so re-running the test matrix
and the twice-green job on it spends half an hour proving nothing new.
This tool compares the checked-out tree with the last green ``ci.yaml``
run on the same ref and writes ``code=false`` to ``$GITHUB_OUTPUT`` only
when every changed path is bookkeeping (an identical tree changes no path
at all). Any other path writes ``code=true``, and the workflow gates the
heavy jobs on that output. The ``green`` output names the green commit
the tree was compared with, so a skipped run can record which green tree
it inherits; it is empty when no baseline was established.

The baseline is the last *successful* run, never ``github.event.before``:
a push whose predecessor was cancelled or red must not inherit a green it
never earned. A run that skipped the heavy jobs is still a sound baseline,
because its code tree equals the tree of the green run it was compared
against.

A ``pull_request`` run tests the merge of the head into the base branch,
so a base-branch code change reaches the tested tree even when the head
did not move. Such a run skips only when the head diff since the green run
AND the base-branch commits since it are bookkeeping-only. No run records
the base tip it merged, so the base side is every path touched by a base
commit the green head does not contain: a superset of the real base diff,
which can only turn a skip into a run.

Paths are listed with ``--no-renames``: rename detection reports only the
destination, so a code file moved under ``.ea/store/`` would otherwise
read as a bookkeeping-only change.

A ``schedule`` or ``workflow_dispatch`` run has no push or pull request to
diff: it exists to re-prove the whole tree, so it writes ``code=true``
without looking for a baseline.

Every failure to establish the baseline (no green run, an API or git
error, a fork pull request, any other event) writes ``code=true``: the
tool fails open to the full matrix instead of failing the run.

Invocation (GitHub Actions, ``changes`` job)::

    python3 tools/ci_changes.py

Inputs are the standard Actions variables ``GITHUB_EVENT_NAME``,
``GITHUB_REPOSITORY``, ``GITHUB_REF_NAME`` (push), ``GITHUB_HEAD_REF`` and
``GITHUB_EVENT_PATH`` (pull request), ``GITHUB_API_URL`` and
``GITHUB_OUTPUT``, plus ``GITHUB_TOKEN`` for the runs lookup. The tool is
stdlib-only because the job runs it with the runner's system Python,
before any dependency sync.

Exit codes:

- ``0`` -- a verdict was written, including every fail-open verdict.
- ``2`` -- ``GITHUB_OUTPUT`` is unset, so no verdict can be delivered.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Paths whose change never alters what the heavy test jobs exercise.
BOOKKEEPING_FILES: frozenset[str] = frozenset({".ea/state.json", ".secrets.baseline"})

#: Directories whose every file is bookkeeping.
BOOKKEEPING_PREFIXES: tuple[str, ...] = (".ea/store/",)

#: Events that run the heavy jobs unconditionally: a nightly or hand-started
#: run carries no diff and exists to re-prove the whole tree.
FULL_RUN_EVENTS: frozenset[str] = frozenset({"schedule", "workflow_dispatch"})

#: The workflow whose green runs anchor the comparison.
WORKFLOW_FILE = "ci.yaml"

_DEFAULT_API_URL = "https://api.github.com"

#: Recent green runs scanned for one pushed from this repository.
_RUNS_PER_PAGE = 30

_API_TIMEOUT_SECONDS = 30

#: A full SHA-1 or SHA-256 object name; anything else is refused before it
#: reaches a git argv, where a leading dash would parse as an option.
_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")

#: How many offending paths a verdict line names before it summarises.
_SHOWN_PATHS = 5

GitRunner = Callable[[Sequence[str]], str]
FetchJson = Callable[[str], object]


class BaselineError(Exception):
    """The last green tree could not be established."""


@dataclass(frozen=True)
class Verdict:
    """Whether the heavy jobs must run, and why.

    Attributes:
        code: True when the tested tree may differ from the green one in
            anything other than bookkeeping.
        reason: One line for the job log.
        green: The head SHA of the green run the tree was compared with;
            empty when no baseline was established.
    """

    code: bool
    reason: str
    green: str = ""

    @property
    def code_line(self) -> str:
        """Return ``code=true`` or ``code=false``."""
        return f"code={'true' if self.code else 'false'}"

    @property
    def output_lines(self) -> str:
        """Return the ``$GITHUB_OUTPUT`` lines that carry this verdict."""
        return f"{self.code_line}\ngreen={self.green}\n"


def is_bookkeeping(path: str) -> bool:
    """Return whether *path* is a state-bookkeeping file.

    Args:
        path: A repo-relative, slash-separated path as git lists it.

    Returns:
        True for the bookkeeping files and anything beneath a bookkeeping
        directory; False for every other path.
    """
    return path in BOOKKEEPING_FILES or path.startswith(BOOKKEEPING_PREFIXES)


def runs_url(*, api_url: str, repository: str, branch: str, event: str) -> str:
    """Return the API URL listing the green ``ci.yaml`` runs of one branch.

    Args:
        api_url: The API root, such as ``$GITHUB_API_URL``.
        repository: The ``owner/name`` repository.
        branch: The branch the runs were triggered from.
        event: The triggering event, so push and pull-request runs never
            anchor each other.

    Returns:
        The runs endpoint with the branch, event and success filters.
    """
    query = urllib.parse.urlencode(
        {"branch": branch, "event": event, "status": "success", "per_page": _RUNS_PER_PAGE}
    )
    return (
        f"{api_url.rstrip('/')}/repos/{repository}/actions/workflows/{WORKFLOW_FILE}/runs?{query}"
    )


def last_green_sha(payload: object, *, repository: str) -> str:
    """Return the head SHA of the newest successful run from *repository*.

    Runs are taken in the order the API lists them, newest first. A run
    pushed from another repository (a fork carrying the same branch name)
    is never a baseline.

    Args:
        payload: The decoded runs-endpoint response.
        repository: The ``owner/name`` repository the run must come from.

    Returns:
        The green run's head commit SHA.

    Raises:
        BaselineError: The payload is malformed or names no such run.
    """
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        raise BaselineError("the runs response carries no 'workflow_runs' list")
    for run in runs:
        if not isinstance(run, dict) or run.get("conclusion") != "success":
            continue
        if _dig(run, "head_repository", "full_name") != repository:
            continue
        sha = run.get("head_sha")
        if isinstance(sha, str) and _SHA_RE.fullmatch(sha):
            return sha
    raise BaselineError(f"no successful {WORKFLOW_FILE} run from {repository} on this ref")


def classify(env: Mapping[str, str], *, git: GitRunner, fetch: FetchJson) -> Verdict:
    """Classify the checked-out tree against the last green tree.

    Args:
        env: The Actions environment.
        git: Runs ``git <args>`` in the checkout and returns its stdout.
        fetch: GETs an API URL and returns the decoded JSON body.

    Returns:
        ``code=False`` only when every path changed since the green tree is
        bookkeeping; ``code=True`` otherwise, for every scheduled or
        dispatched run, and whenever the baseline cannot be established.
    """
    try:
        return _classify(env, git=git, fetch=fetch)
    except BaselineError as exc:
        return Verdict(code=True, reason=f"no green baseline, running the full matrix: {exc}")


def _classify(env: Mapping[str, str], *, git: GitRunner, fetch: FetchJson) -> Verdict:
    """Dispatch on the triggering event."""
    event_name = env.get("GITHUB_EVENT_NAME", "")
    if event_name in FULL_RUN_EVENTS:
        return Verdict(code=True, reason=f"a {event_name} run re-proves the whole tree")
    if event_name == "push":
        return _classify_push(env, git=git, fetch=fetch)
    if event_name == "pull_request":
        return _classify_pull_request(env, git=git, fetch=fetch)
    raise BaselineError(f"event {event_name!r} has no green-baseline rule")


def _classify_push(env: Mapping[str, str], *, git: GitRunner, fetch: FetchJson) -> Verdict:
    """Compare the pushed commit with the last green push on its branch."""
    green = _green_sha(env, fetch=fetch, branch=_require(env, "GITHUB_REF_NAME"), event="push")
    where = f"the push since green {green[:12]}"
    return _verdict(_code_paths(diff_paths(git, green, "HEAD")), where=where, green=green)


def _classify_pull_request(env: Mapping[str, str], *, git: GitRunner, fetch: FetchJson) -> Verdict:
    """Compare both merge parents with the last green run of the head branch.

    The checkout is the merge commit, so its first parent is the base tip
    and its second parent is the pull request head.
    """
    repository = _require(env, "GITHUB_REPOSITORY")
    head_repository = _event_head_repository(_require(env, "GITHUB_EVENT_PATH"))
    if head_repository != repository:
        raise BaselineError(f"the pull request head lives in {head_repository!r}")
    green = _green_sha(
        env, fetch=fetch, branch=_require(env, "GITHUB_HEAD_REF"), event="pull_request"
    )
    base = _rev_parse(git, "HEAD^1")
    head = _rev_parse(git, "HEAD^2")
    head_code = _code_paths(diff_paths(git, green, head))
    if head_code:
        return _verdict(head_code, where=f"the head since green {green[:12]}", green=green)
    base_code = _code_paths(base_paths_since(git, green, base))
    where = f"the head and base branch since green {green[:12]}"
    return _verdict(base_code, where=where, green=green)


def diff_paths(git: GitRunner, old: str, new: str) -> list[str]:
    """Return every path whose content differs between two commits.

    Args:
        git: The git runner.
        old: The baseline commit.
        new: The commit under test.

    Returns:
        The changed paths; empty when the two trees are identical.

    Raises:
        BaselineError: git cannot compare the two commits.
    """
    return _nul_split(git(["diff", "--name-only", "--no-renames", "-z", old, new]))


def base_paths_since(git: GitRunner, green_head: str, base: str) -> list[str]:
    """Return every path a base commit absent from *green_head* touched.

    Merge commits are diffed against each parent (``-m``) so a conflict
    resolution made in the merge itself is listed too.

    Args:
        git: The git runner.
        green_head: The head commit of the green pull-request run.
        base: The base-branch tip merged into the tree under test.

    Returns:
        The union of touched paths, possibly with repeats.

    Raises:
        BaselineError: git cannot walk the range.
    """
    argv = ["log", "--format=", "--name-only", "--no-renames", "-m", "-z", f"{green_head}..{base}"]
    return _nul_split(git(argv))


def run_git(args: Sequence[str]) -> str:
    """Run ``git <args>`` in the current directory and return its stdout.

    Args:
        args: The git arguments.

    Returns:
        The command's standard output.

    Raises:
        BaselineError: git is missing or exits non-zero.
    """
    try:
        result = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        raise BaselineError(f"git {args[0]} failed: {exc.stderr.strip()}") from exc
    except OSError as exc:
        raise BaselineError(f"git could not start: {exc}") from exc
    return result.stdout


def github_fetcher(token: str) -> FetchJson:
    """Return a GET-and-decode function for the GitHub REST API.

    Args:
        token: The workflow token; empty sends an anonymous request.

    Returns:
        A function mapping a URL to its decoded JSON body, raising
        :class:`BaselineError` on any transport, HTTP or decoding failure.
    """
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def fetch(url: str) -> object:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=_API_TIMEOUT_SECONDS) as response:
                body: object = json.load(response)
        except (OSError, ValueError) as exc:
            raise BaselineError(f"the runs lookup failed: {exc}") from exc
        return body

    return fetch


def main(
    env: Mapping[str, str] | None = None,
    *,
    git: GitRunner = run_git,
    fetch: FetchJson | None = None,
) -> int:
    """Classify the run and append the verdict to ``$GITHUB_OUTPUT``.

    Args:
        env: The environment to read; ``os.environ`` when omitted.
        git: The git runner.
        fetch: The API fetcher; a token-bearing GitHub fetcher when omitted.

    Returns:
        ``0`` once a verdict is written, ``2`` when ``GITHUB_OUTPUT`` is unset.
    """
    environ = os.environ if env is None else env
    output = environ.get("GITHUB_OUTPUT", "")
    if not output:
        print(
            "ci_changes: GITHUB_OUTPUT is unset, so the verdict has nowhere to go", file=sys.stderr
        )
        return 2
    fetcher = fetch if fetch is not None else github_fetcher(environ.get("GITHUB_TOKEN", ""))
    verdict = classify(environ, git=git, fetch=fetcher)
    with Path(output).open("a", encoding="utf-8") as handle:
        handle.write(verdict.output_lines)
    print(f"{verdict.code_line}: {verdict.reason}")
    return 0


def _green_sha(env: Mapping[str, str], *, fetch: FetchJson, branch: str, event: str) -> str:
    """Look up the newest green run of *branch* for *event*."""
    repository = _require(env, "GITHUB_REPOSITORY")
    url = runs_url(
        api_url=env.get("GITHUB_API_URL") or _DEFAULT_API_URL,
        repository=repository,
        branch=branch,
        event=event,
    )
    return last_green_sha(fetch(url), repository=repository)


def _event_head_repository(event_path: str) -> object:
    """Return the ``owner/name`` of the pull request head in the event payload."""
    try:
        payload: object = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BaselineError(f"the event payload is unreadable: {exc}") from exc
    return _dig(payload, "pull_request", "head", "repo", "full_name")


def _rev_parse(git: GitRunner, revision: str) -> str:
    """Resolve *revision* to a full commit SHA."""
    sha = git(["rev-parse", "--verify", f"{revision}^{{commit}}"]).strip()
    if not _SHA_RE.fullmatch(sha):
        raise BaselineError(f"{revision} resolved to {sha!r}, not a commit")
    return sha


def _require(env: Mapping[str, str], name: str) -> str:
    """Return the non-empty value of *name*."""
    value = env.get(name, "")
    if not value:
        raise BaselineError(f"{name} is unset")
    return value


def _dig(value: object, *keys: str) -> object:
    """Walk nested mappings by *keys*, returning None at the first miss."""
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _nul_split(output: str) -> list[str]:
    """Split ``-z`` git output into its non-empty entries."""
    return [entry for entry in output.split("\0") if entry]


def _code_paths(paths: Iterable[str]) -> list[str]:
    """Return the sorted distinct paths that are not bookkeeping."""
    return sorted({path for path in paths if not is_bookkeeping(path)})


def _verdict(code_paths: Sequence[str], *, where: str, green: str) -> Verdict:
    """Build the verdict for the non-bookkeeping paths found since *green*."""
    if not code_paths:
        return Verdict(code=False, reason=f"{where}: only state bookkeeping changed", green=green)
    shown = ", ".join(code_paths[:_SHOWN_PATHS])
    extra = len(code_paths) - _SHOWN_PATHS
    suffix = f" and {extra} more" if extra > 0 else ""
    return Verdict(code=True, reason=f"{where}: code changed in {shown}{suffix}", green=green)


if __name__ == "__main__":
    raise SystemExit(main())
