"""Decide whether a CI run must execute the heavy test jobs.

A push that changes only project state under ``.ea/`` or
``.secrets.baseline`` leaves the code tree exactly as it was when CI last
went green, so re-running the test matrix and the twice-green job on it
spends half an hour proving nothing new. This tool compares the
checked-out tree with the last green ``ci.yaml`` tree and writes
``code=false`` to ``$GITHUB_OUTPUT`` only when every change is bookkeeping
(an identical tree changes no path at all). Bookkeeping is any added,
modified, deleted or retyped path under ``.ea/`` and that one file; the
``.ea/`` commit-policy census runs before push, not in the CI test jobs.
Any other path writes ``code=true``. The workflow gates the heavy jobs on
the ``code`` output. The ``green`` output names the green commit the tree
was compared with, so a skipped run can record which green tree it
inherits; it is empty when no baseline was established.

The baseline is the last *successful* run, never ``github.event.before``:
a push whose predecessor was cancelled or red must not inherit a green it
never earned. A run that skipped the heavy jobs is still a sound baseline,
because its code tree equals the tree of the green run it was compared
against.

A run that must execute the full configuration inherits only from a run
that executed it. A draft pull request runs the fast lane, with no
coverage, end-to-end lane or whole-suite macOS shards, so its green proves
less than a push or a ready pull request needs. The proof that a
``pull_request`` run executed the full configuration is its coverage job
concluding success: that job runs only off drafts and only once the
ubuntu shards and the serial lane passed. A run that skipped its heavy
jobs skipped coverage too, so it never proves the full configuration;
the scan moves past it to the run it inherited from.

The baseline each run may take:

- A push to ``release`` or ``release/**`` takes the newest green push run
  of its own ref, so a commit already green on ``dev`` still runs
  twice-green on the release train.
- Any other push takes the newest green run whose head the pushed history
  contains, among push runs of any ref and ``pull_request`` runs that
  executed the full configuration. So ``dev`` fast-forwarded to a ready
  pull request head inherits that head's green run. A pull request run
  proved the merge of its head into the base tip of that moment; the two
  trees agree whenever the base was an ancestor of the head, which a
  fast-forward requires.
- A draft pull request takes the newest green ``pull_request`` run of its
  head branch.
- A pull request that is not a draft, including one just marked ready,
  takes the newest green ``pull_request`` run of its head branch that
  executed the full configuration.

A ``pull_request`` run tests the merge of the head into the base branch,
so a base-branch code change reaches the tested tree even when the head
did not move. Such a run skips only when the head diff since the green run
AND the base-branch commits since it are bookkeeping-only. No run records
the base tip it merged, so the base side is every path touched by a base
commit the green head does not contain: a superset of the real base diff,
which can only turn a skip into a run.

Changes are listed with ``--no-renames``: rename detection reports one
``R`` row for the destination, so a code file moved under ``.ea/``
would lose its source-side deletion. Without it the move reads as a
``D`` plus an ``A``, and both run the matrix.

A ``schedule`` or ``workflow_dispatch`` run has no push or pull request to
diff: it exists to re-prove the whole tree, so it writes ``code=true``
without looking for a baseline.

Every failure to establish the baseline (no green run, an API or git
error, a jobs list without the proof, a fork pull request, any other
event) writes ``code=true``: the tool fails open to the full matrix
instead of failing the run.

Invocation (GitHub Actions, ``changes`` job)::

    python3 tools/ci_changes.py

Inputs are the standard Actions variables ``GITHUB_EVENT_NAME``,
``GITHUB_REPOSITORY``, ``GITHUB_REF_NAME`` (push), ``GITHUB_HEAD_REF`` and
``GITHUB_EVENT_PATH`` (pull request, read for the head repository and the
draft flag), ``GITHUB_API_URL`` and
``GITHUB_OUTPUT``, plus ``GITHUB_TOKEN`` for the runs lookup. The tool is
stdlib-only because the job runs it with the runner's system Python,
before any dependency sync.

Exit codes:

- ``0`` -- a verdict was written, including every fail-open verdict.
- ``2`` -- ``GITHUB_OUTPUT`` is unset, so no verdict can be delivered.
"""

from __future__ import annotations

import http.client
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

#: The one file outside ``.ea/`` whose change never alters what the heavy
#: test jobs exercise.
BOOKKEEPING_FILES: frozenset[str] = frozenset({".secrets.baseline"})

#: Project state lives under this prefix; the test jobs never read it, and
#: the ``.ea/`` commit-policy census runs before push instead of in CI.
EA_PREFIX = ".ea/"

#: Events that run the heavy jobs unconditionally: a nightly or hand-started
#: run carries no diff and exists to re-prove the whole tree.
FULL_RUN_EVENTS: frozenset[str] = frozenset({"schedule", "workflow_dispatch"})

#: The workflow whose green runs anchor the comparison.
WORKFLOW_FILE = "ci.yaml"

PUSH_EVENT = "push"
PULL_REQUEST_EVENT = "pull_request"

#: The release-train branch; its ``release/`` children belong to it too.
RELEASE_BRANCH = "release"

#: The name of the ci.yaml coverage job. It runs only in the full
#: configuration and only after the ubuntu shards and the serial lane
#: passed, so its success proves a pull request run executed that
#: configuration.
FULL_CONFIGURATION_JOB = "Coverage gate over the combined shards"

#: Jobs lookups one classification may spend before it gives up and runs
#: the full matrix; each costs one API call against the token's rate limit.
PROOF_LOOKUPS = 10

_DEFAULT_API_URL = "https://api.github.com"

#: Recent green runs scanned for a baseline, and jobs listed per run. A
#: push lists the runs of every ref, so the API maximum keeps its own
#: ancestors in reach; a ci.yaml run has far fewer jobs than that.
_RUNS_PER_PAGE = 100

_API_TIMEOUT_SECONDS = 30

#: A full SHA-1 or SHA-256 object name; anything else is refused before it
#: reaches a git argv, where a leading dash would parse as an option.
_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")

#: How many offending paths a verdict line names before it summarises.
_SHOWN_PATHS = 5

GitRunner = Callable[[Sequence[str]], str]

#: One ``(status, path)`` row of ``git diff --name-status``.
Change = tuple[str, str]
FetchJson = Callable[[str], object]


class BaselineError(Exception):
    """The last green tree could not be established."""


@dataclass(frozen=True)
class GreenRun:
    """A successful ci.yaml run that may anchor the comparison.

    Attributes:
        run_id: The run's API id; 0 when the listing carried none, which
            leaves the run unable to prove the full configuration.
        head_sha: The run's head commit.
        event: The event that triggered the run.
    """

    run_id: int
    head_sha: str
    event: str


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
    """Return whether *path* is bookkeeping the heavy test jobs never read.

    Args:
        path: A repo-relative, slash-separated path as git lists it.

    Returns:
        True for ``.secrets.baseline`` and for every path under ``.ea/``,
        whatever its diff status; False for every other path.
    """
    return path in BOOKKEEPING_FILES or path.startswith(EA_PREFIX)


def runs_url(
    *, api_url: str, repository: str, branch: str | None = None, event: str | None = None
) -> str:
    """Return the API URL listing the green ``ci.yaml`` runs.

    Args:
        api_url: The API root, such as ``$GITHUB_API_URL``.
        repository: The ``owner/name`` repository.
        branch: The branch the runs were triggered from; None lists every ref.
        event: The triggering event; None lists every event.

    Returns:
        The runs endpoint with the success filter and any given branch and
        event filters.
    """
    filters = {"branch": branch, "event": event}
    params: dict[str, str | int] = {key: value for key, value in filters.items() if value}
    params.update({"status": "success", "per_page": _RUNS_PER_PAGE})
    query = urllib.parse.urlencode(params)
    return (
        f"{api_url.rstrip('/')}/repos/{repository}/actions/workflows/{WORKFLOW_FILE}/runs?{query}"
    )


def jobs_url(*, api_url: str, repository: str, run_id: int) -> str:
    """Return the API URL listing the jobs of one run's latest attempt.

    Args:
        api_url: The API root, such as ``$GITHUB_API_URL``.
        repository: The ``owner/name`` repository.
        run_id: The run whose jobs are listed.

    Returns:
        The run-jobs endpoint.
    """
    query = urllib.parse.urlencode({"per_page": _RUNS_PER_PAGE})
    return f"{api_url.rstrip('/')}/repos/{repository}/actions/runs/{run_id}/jobs?{query}"


def green_runs(payload: object, *, repository: str) -> list[GreenRun]:
    """Return the successful runs from *repository*.

    Runs keep the order the API lists them, newest first. A run pushed from
    another repository (a fork carrying the same branch name) is never a
    baseline, and a head SHA that is not a full object name is dropped.

    Args:
        payload: The decoded runs-endpoint response.
        repository: The ``owner/name`` repository the run must come from.

    Returns:
        The green runs, newest first; empty when none qualifies.

    Raises:
        BaselineError: The payload carries no ``workflow_runs`` list.
    """
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        raise BaselineError("the runs response carries no 'workflow_runs' list")
    green: list[GreenRun] = []
    for run in runs:
        if not isinstance(run, dict) or run.get("conclusion") != "success":
            continue
        if _dig(run, "head_repository", "full_name") != repository:
            continue
        sha = run.get("head_sha")
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            continue
        raw_id = run.get("id")
        # bool is an int subclass, so ``True`` would pass as run 1.
        run_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else 0
        event = run.get("event")
        green.append(
            GreenRun(
                run_id=max(run_id, 0), head_sha=sha, event=event if isinstance(event, str) else ""
            )
        )
    return green


def ran_full_configuration(payload: object) -> bool:
    """Return whether a run's jobs show the coverage job concluded success.

    Args:
        payload: The decoded run-jobs response.

    Returns:
        True only when a job named :data:`FULL_CONFIGURATION_JOB` concluded
        ``success``; False when it is absent, skipped, failed or cancelled.

    Raises:
        BaselineError: The payload carries no ``jobs`` list.
    """
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise BaselineError("the jobs response carries no 'jobs' list")
    return any(
        isinstance(job, dict)
        and job.get("name") == FULL_CONFIGURATION_JOB
        and job.get("conclusion") == "success"
        for job in jobs
    )


def classify(env: Mapping[str, str], *, git: GitRunner, fetch: FetchJson) -> Verdict:
    """Classify the checked-out tree against the last green tree.

    Args:
        env: The Actions environment.
        git: Runs ``git <args>`` in the checkout and returns its stdout.
        fetch: GETs an API URL and returns the decoded JSON body.

    Returns:
        ``code=False`` only when every change since the green tree is
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
    """Compare the pushed commit with the newest green run that may anchor it.

    A release push compares with the last green push of its own ref. Any
    other push takes a push run of any ref, or a pull request run that
    executed the full configuration, whose head the pushed history
    contains: a green commit on an unrelated branch would diff against
    code the push never had.
    """
    repository = _require(env, "GITHUB_REPOSITORY")
    api_url = env.get("GITHUB_API_URL") or _DEFAULT_API_URL
    ref = _require(env, "GITHUB_REF_NAME")
    if ref == RELEASE_BRANCH or ref.startswith(f"{RELEASE_BRANCH}/"):
        url = runs_url(api_url=api_url, repository=repository, branch=ref, event=PUSH_EVENT)
        runs = green_runs(fetch(url), repository=repository)
        if not runs:
            raise BaselineError(
                f"no successful {WORKFLOW_FILE} push run from {repository} on {ref}"
            )
        green = runs[0].head_sha
    else:
        url = runs_url(api_url=api_url, repository=repository)
        candidates = [
            run
            for run in green_runs(fetch(url), repository=repository)
            if run.event in (PUSH_EVENT, PULL_REQUEST_EVENT)
        ]
        green = _first_baseline(
            candidates,
            contained=lambda run: _contains(git, run.head_sha),
            needs_proof=lambda run: run.event != PUSH_EVENT,
            prove=lambda run: _ran_full(fetch, api_url=api_url, repository=repository, run=run),
            wanted="a push run, or a pull request run that executed the full "
            "configuration, with a head the push contains",
        )
    where = f"the push since green {green[:12]}"
    return _verdict(_code_paths(diff_changes(git, green, "HEAD")), where=where, green=green)


def _classify_pull_request(env: Mapping[str, str], *, git: GitRunner, fetch: FetchJson) -> Verdict:
    """Compare both merge parents with the last green run of the head branch.

    The checkout is the merge commit, so its first parent is the base tip
    and its second parent is the pull request head. A draft may inherit
    any green pull request run; any other pull request only one that
    executed the full configuration.
    """
    repository = _require(env, "GITHUB_REPOSITORY")
    api_url = env.get("GITHUB_API_URL") or _DEFAULT_API_URL
    pull_request = _event_pull_request(_require(env, "GITHUB_EVENT_PATH"))
    head_repository = _dig(pull_request, "head", "repo", "full_name")
    if head_repository != repository:
        raise BaselineError(f"the pull request head lives in {head_repository!r}")
    # Only an explicit ``true`` reads as a draft; anything else takes the
    # stricter ready rule.
    draft = _dig(pull_request, "draft") is True
    url = runs_url(
        api_url=api_url,
        repository=repository,
        branch=_require(env, "GITHUB_HEAD_REF"),
        event=PULL_REQUEST_EVENT,
    )
    wanted = "a pull request run of the head branch"
    green = _first_baseline(
        green_runs(fetch(url), repository=repository),
        contained=lambda run: True,
        needs_proof=lambda run: not draft,
        prove=lambda run: _ran_full(fetch, api_url=api_url, repository=repository, run=run),
        wanted=wanted if draft else f"{wanted} that executed the full configuration",
    )
    base = _rev_parse(git, "HEAD^1")
    head = _rev_parse(git, "HEAD^2")
    head_code = _code_paths(diff_changes(git, green, head))
    if head_code:
        return _verdict(head_code, where=f"the head since green {green[:12]}", green=green)
    base_code = _code_paths(base_changes_since(git, green, base))
    where = f"the head and base branch since green {green[:12]}"
    return _verdict(base_code, where=where, green=green)


def diff_changes(git: GitRunner, old: str, new: str) -> list[Change]:
    """Return every path that differs between two commits, with its diff status.

    Args:
        git: The git runner.
        old: The baseline commit.
        new: The commit under test.

    Returns:
        The ``(status, path)`` rows; empty when the two trees are identical.

    Raises:
        BaselineError: git cannot compare the two commits, or lists a
            malformed row.
    """
    return _status_pairs(git(["diff", "--name-status", "--no-renames", "-z", old, new]))


def base_changes_since(git: GitRunner, green_head: str, base: str) -> list[Change]:
    """Return every change a base commit absent from *green_head* made.

    Merge commits are diffed against each parent (``-m``) so a conflict
    resolution made in the merge itself is listed too.

    Args:
        git: The git runner.
        green_head: The head commit of the green pull-request run.
        base: The base-branch tip merged into the tree under test.

    Returns:
        The ``(status, path)`` rows of every commit, possibly with repeats;
        a path added in one commit and edited in the next lists both.

    Raises:
        BaselineError: git cannot walk the range, or lists a malformed row.
    """
    argv = [
        "log",
        "--format=",
        "--name-status",
        "--no-renames",
        "-m",
        "-z",
        f"{green_head}..{base}",
    ]
    return _status_pairs(git(argv))


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
        # HTTPException covers a body cut short mid-read (IncompleteRead),
        # which is neither an OSError nor a ValueError.
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise BaselineError(f"the runs lookup failed: {exc!r}") from exc
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


def _first_baseline(
    runs: Sequence[GreenRun],
    *,
    contained: Callable[[GreenRun], bool],
    needs_proof: Callable[[GreenRun], bool],
    prove: Callable[[GreenRun], bool],
    wanted: str,
) -> str:
    """Return the head of the newest run that may anchor this one.

    Args:
        runs: The green runs, newest first.
        contained: Whether the run's head is in the tested history.
        needs_proof: Whether the run must prove the full configuration.
        prove: Looks the proof up; one API call per run.
        wanted: Names the qualifying run in the refusal.

    Returns:
        The qualifying run's head commit.

    Raises:
        BaselineError: No run qualifies, the proof lookups ran out, or a
            lookup failed.
    """
    lookups = 0
    for run in runs:
        if not contained(run):
            continue
        if needs_proof(run):
            if lookups == PROOF_LOOKUPS:
                raise BaselineError(f"none of {PROOF_LOOKUPS} proof lookups found {wanted}")
            lookups += 1
            if not prove(run):
                continue
        return run.head_sha
    raise BaselineError(f"no successful {WORKFLOW_FILE} run is {wanted}")


def _contains(git: GitRunner, sha: str) -> bool:
    """Return whether ``HEAD`` contains *sha*.

    ``git merge-base --is-ancestor`` exits non-zero both for a commit that
    is not an ancestor and for one the checkout lacks; either way that
    green run cannot anchor this push.
    """
    try:
        git(["merge-base", "--is-ancestor", sha, "HEAD"])
    except BaselineError:
        return False
    return True


def _ran_full(fetch: FetchJson, *, api_url: str, repository: str, run: GreenRun) -> bool:
    """Return whether *run* executed the full configuration, per its jobs."""
    if not run.run_id:
        return False
    url = jobs_url(api_url=api_url, repository=repository, run_id=run.run_id)
    return ran_full_configuration(fetch(url))


def _event_pull_request(event_path: str) -> object:
    """Return the ``pull_request`` object of the event payload."""
    try:
        payload: object = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BaselineError(f"the event payload is unreadable: {exc}") from exc
    return _dig(payload, "pull_request")


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


def _status_pairs(output: str) -> list[Change]:
    """Pair ``--name-status -z`` output into ``(status, path)`` rows."""
    entries = [entry.strip("\n") for entry in output.split("\0")]
    entries = [entry for entry in entries if entry]
    if len(entries) % 2:
        raise BaselineError(f"git listed {len(entries)} name-status fields, not pairs")
    return list(zip(entries[::2], entries[1::2], strict=True))


def _code_paths(changes: Iterable[Change]) -> list[str]:
    """Return the sorted distinct paths whose change is not bookkeeping."""
    return sorted({path for _status, path in changes if not is_bookkeeping(path)})


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
