"""Record the green CI tree and let the release gates accept it.

A release tag must name a commit CI proved green. Reading only the newest
``ci.yaml`` run for the tagged commit blocked two sound releases: a later
push cancels the tagged commit's run while an earlier run on the same
commit went green, and a rebase or a re-push produces a new commit whose
tree CI already tested. This tool closes both, from the two ends of the
pipeline.

``record`` runs in the last ``ci.yaml`` job on a push. It writes a record
of the checked-out tree, which the workflow uploads as the
``ci-green-tree-<tree sha>`` artifact, but only when the heavy test jobs
passed, or when the changes job skipped them because the tree differs
only in bookkeeping from a green tree that is itself recorded. The record
names that inherited tree, so every record traces back to a run whose
heavy jobs actually ran.

``gate`` runs in the ``require-green-ci`` job of both release workflows.
It lists the successful push-event ``ci.yaml`` runs from this repository
and accepts one whose head commit is the tagged commit, or whose tree is
the tagged tree and which uploaded that tree's record. A pull request run
never counts: it tests a synthetic merge of the head into the base, not
the tree the tag names. While no run qualifies and a push run on the
tagged commit is in flight or not yet created, the gate polls; it refuses
once every push run on the commit has finished without one.

Invocation (GitHub Actions)::

    python3 tools/ci_green_tree.py record <record path>
    python3 tools/ci_green_tree.py gate

Both read the standard Actions variables ``GITHUB_REPOSITORY``,
``GITHUB_SHA`` and ``GITHUB_API_URL``, plus ``GITHUB_TOKEN`` for the API.
``record`` also reads ``GITHUB_EVENT_NAME``, ``GITHUB_RUN_ID``,
``GITHUB_OUTPUT`` and ``NEEDS``, the ``toJSON(needs)`` of its job. The
tool is stdlib-only because both jobs run it with the runner's system
Python, before any dependency sync.

Exit codes:

- ``0`` -- ``record`` wrote its decision, recorded or not; ``gate``
  found a qualifying run.
- ``1`` -- ``gate`` refused the release.
- ``2`` -- the invocation or its environment is unusable.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

#: The workflow whose green runs the release gates accept.
WORKFLOW_FILE = "ci.yaml"

#: The artifact name of a green-tree record is this prefix plus the tree sha.
RECORD_PREFIX = "ci-green-tree-"

#: The only event whose run tests the pushed tree itself.
PUSH_EVENT = "push"

#: The ci.yaml job whose outputs carry the skip verdict and its baseline.
CHANGES_JOB = "changes"

#: The ci.yaml jobs the changes job may skip. A record needs each of them
#: to have passed, or all of them to have been skipped against a recorded
#: green tree.
HEAVY_JOBS: tuple[str, ...] = ("test", "twice-green")

#: How long the gate waits for a push run on the tagged commit.
GATE_DEADLINE_SECONDS = 1800

#: The pause between two gate lookups.
POLL_SECONDS = 30

#: Needed-job results a record can stand on; anything else means a needed
#: job failed, was cancelled or reported nothing.
_SETTLED_RESULTS = frozenset({"success", "skipped"})

_DEFAULT_API_URL = "https://api.github.com"

#: Recent runs scanned per lookup. The tag follows the green push closely,
#: so one page reaches it; an older green tree only makes the gate wait.
_RUNS_PER_PAGE = 100

_API_TIMEOUT_SECONDS = 30

#: A full SHA-1 or SHA-256 object name; anything else is refused before it
#: reaches a git argv, where a leading dash would parse as an option.
_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")

_USAGE = "usage: ci_green_tree.py record <record path> | ci_green_tree.py gate"

GitRunner = Callable[[Sequence[str]], str]
FetchJson = Callable[[str], object]
Clock = Callable[[], float]
Sleep = Callable[[float], None]
Log = Callable[[str], None]


class GreenTreeError(Exception):
    """A git or API lookup, or the environment, could not answer."""


class RecordRefusedError(Exception):
    """This run may not record its tree; the message says why."""


@dataclass(frozen=True)
class GreenRun:
    """A ci.yaml run that proves a tree green.

    Attributes:
        run_id: The run's API id.
        head_sha: The commit the run was pushed with.
        basis: ``head sha`` when the run tested the tagged commit itself,
            ``recorded tree`` when it recorded the tagged tree.
    """

    run_id: int
    head_sha: str
    basis: str


@dataclass(frozen=True)
class TreeRecord:
    """The body of a ``ci-green-tree-<tree>`` artifact.

    Attributes:
        tree: The checked-out tree the run vouches for.
        sha: The pushed commit.
        run_id: The recording ci.yaml run.
        heavy_jobs: ``passed`` when the heavy jobs ran green on this run,
            ``inherited`` when the changes job skipped them.
        inherited_tree: The recorded green tree a skip stands on; empty
            when the heavy jobs ran.
        inherited_run_id: The run that recorded ``inherited_tree``; empty
            when the heavy jobs ran.
    """

    tree: str
    sha: str
    run_id: str
    heavy_jobs: str
    inherited_tree: str = ""
    inherited_run_id: str = ""


@dataclass(frozen=True)
class GateOutcome:
    """Whether the release may proceed, and why.

    Attributes:
        passed: True when a qualifying green run was found.
        reason: One line for the job log.
    """

    passed: bool
    reason: str


def record_name(tree: str) -> str:
    """Return the artifact name that records *tree* as green.

    Args:
        tree: A tree object name.

    Returns:
        ``ci-green-tree-`` followed by *tree*.
    """
    return f"{RECORD_PREFIX}{tree}"


def green_runs_url(*, api_url: str, repository: str) -> str:
    """Return the API URL listing the successful push-event ci.yaml runs.

    Args:
        api_url: The API root, such as ``$GITHUB_API_URL``.
        repository: The ``owner/name`` repository.

    Returns:
        The runs endpoint filtered to push events that concluded success.
    """
    query = {"event": PUSH_EVENT, "status": "success", "per_page": _RUNS_PER_PAGE}
    return f"{_workflow_runs_url(api_url, repository)}?{urllib.parse.urlencode(query)}"


def commit_runs_url(*, api_url: str, repository: str, sha: str) -> str:
    """Return the API URL listing every push-event ci.yaml run of one commit.

    Args:
        api_url: The API root.
        repository: The ``owner/name`` repository.
        sha: The commit whose runs are listed, whatever their state.

    Returns:
        The runs endpoint filtered to push events on *sha*.
    """
    query = {"event": PUSH_EVENT, "head_sha": sha, "per_page": _RUNS_PER_PAGE}
    return f"{_workflow_runs_url(api_url, repository)}?{urllib.parse.urlencode(query)}"


def record_url(*, api_url: str, repository: str, run_id: int, tree: str) -> str:
    """Return the API URL listing one run's record artifact for *tree*.

    Args:
        api_url: The API root.
        repository: The ``owner/name`` repository.
        run_id: The run whose artifacts are listed.
        tree: The tree whose record is looked for.

    Returns:
        The run-artifacts endpoint filtered to the record's name.
    """
    query = urllib.parse.urlencode({"name": record_name(tree)})
    return f"{api_url.rstrip('/')}/repos/{repository}/actions/runs/{run_id}/artifacts?{query}"


def green_push_runs(payload: object, *, repository: str) -> list[dict[str, object]]:
    """Return the finished, successful push runs of *repository*.

    The query already filters on event and conclusion. The filter is
    repeated here because it is what the gate's soundness rests on: a pull
    request run tested a merge tree nobody tagged, and a run from another
    repository would let a fork vouch for this one.

    Args:
        payload: The decoded runs-endpoint response, newest run first.
        repository: The ``owner/name`` repository the run must come from.

    Returns:
        The qualifying runs in the order the API listed them.

    Raises:
        GreenTreeError: The payload carries no ``workflow_runs`` list.
    """
    return [
        run
        for run in _runs(payload)
        if run.get("event") == PUSH_EVENT
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and _dig(run, "head_repository", "full_name") == repository
        and _is_run_id(run.get("id"))
    ]


def has_record(payload: object, *, tree: str) -> bool:
    """Return whether a run-artifacts response holds a live record of *tree*.

    Args:
        payload: The decoded run-artifacts response.
        tree: The tree whose record is looked for.

    Returns:
        True when an unexpired artifact carries the record's exact name.

    Raises:
        GreenTreeError: The payload carries no ``artifacts`` list.
    """
    artifacts = payload.get("artifacts") if isinstance(payload, dict) else None
    if not isinstance(artifacts, list):
        raise GreenTreeError("the artifacts response carries no 'artifacts' list")
    name = record_name(tree)
    return any(
        isinstance(artifact, dict)
        and artifact.get("name") == name
        and artifact.get("expired") is False
        for artifact in artifacts
    )


def commit_settled(payload: object, *, repository: str) -> bool:
    """Return whether every push run on a commit has finished.

    Args:
        payload: The decoded response of :func:`commit_runs_url`.
        repository: The ``owner/name`` repository the runs must come from.

    Returns:
        True when at least one push run exists and none is still queued or
        running; False while a run is in flight or none was created yet.

    Raises:
        GreenTreeError: The payload carries no ``workflow_runs`` list.
    """
    runs = [
        run
        for run in _runs(payload)
        if run.get("event") == PUSH_EVENT
        and _dig(run, "head_repository", "full_name") == repository
    ]
    return bool(runs) and all(run.get("status") == "completed" for run in runs)


def find_recorded_run(
    runs: Sequence[Mapping[str, object]],
    *,
    tree: str,
    fetch: FetchJson,
    api_url: str,
    repository: str,
) -> GreenRun | None:
    """Return the first green run that tested *tree* and recorded it.

    Args:
        runs: Runs already filtered by :func:`green_push_runs`.
        tree: The tree to match.
        fetch: GETs an API URL and returns the decoded JSON body.
        api_url: The API root.
        repository: The ``owner/name`` repository.

    Returns:
        The run, or None when no run on *tree* uploaded its record.

    Raises:
        GreenTreeError: An artifacts lookup failed.
    """
    for run in runs:
        run_id = run.get("id")
        head_sha = run.get("head_sha")
        if _dig(run, "head_commit", "tree_id") != tree or not isinstance(head_sha, str):
            continue
        if not isinstance(run_id, int):
            continue
        url = record_url(api_url=api_url, repository=repository, run_id=run_id, tree=tree)
        if has_record(fetch(url), tree=tree):
            return GreenRun(run_id=run_id, head_sha=head_sha, basis="recorded tree")
    return None


def find_green_run(
    *, fetch: FetchJson, api_url: str, repository: str, sha: str, tree: str
) -> GreenRun | None:
    """Return a successful push run that proves the tagged commit green.

    A run on the tagged commit itself qualifies with or without a record,
    as it always has; a run on another commit qualifies only when its tree
    is the tagged tree and it recorded that tree.

    Args:
        fetch: GETs an API URL and returns the decoded JSON body.
        api_url: The API root.
        repository: The ``owner/name`` repository.
        sha: The tagged commit.
        tree: The tagged commit's tree.

    Returns:
        The qualifying run, or None.

    Raises:
        GreenTreeError: A lookup failed or answered malformed JSON.
    """
    runs = green_push_runs(
        fetch(green_runs_url(api_url=api_url, repository=repository)), repository=repository
    )
    for run in runs:
        run_id = run.get("id")
        if run.get("head_sha") == sha and isinstance(run_id, int):
            return GreenRun(run_id=run_id, head_sha=sha, basis="head sha")
    return find_recorded_run(runs, tree=tree, fetch=fetch, api_url=api_url, repository=repository)


def await_green_run(
    *,
    fetch: FetchJson,
    api_url: str,
    repository: str,
    sha: str,
    tree: str,
    clock: Clock,
    sleep: Sleep,
    log: Log,
) -> GateOutcome:
    """Poll until a green run proves the tagged commit, or refuse.

    A failed lookup is retried rather than fatal: the API blips far more
    often than it stays down, and the deadline still bounds the wait.

    Args:
        fetch: GETs an API URL and returns the decoded JSON body.
        api_url: The API root.
        repository: The ``owner/name`` repository.
        sha: The tagged commit.
        tree: The tagged commit's tree.
        clock: Returns a monotonic time in seconds.
        sleep: Pauses for the given number of seconds.
        log: Writes one progress line.

    Returns:
        A passing outcome naming the run, or a refusal once every push run
        on *sha* has finished without one or the deadline has passed.
    """
    deadline = clock() + GATE_DEADLINE_SECONDS
    while True:
        try:
            green = find_green_run(
                fetch=fetch, api_url=api_url, repository=repository, sha=sha, tree=tree
            )
            if green is not None:
                return GateOutcome(
                    passed=True,
                    reason=f"ci.yaml run {green.run_id} on {green.head_sha[:12]} "
                    f"is green by {green.basis}",
                )
            url = commit_runs_url(api_url=api_url, repository=repository, sha=sha)
            if commit_settled(fetch(url), repository=repository):
                return GateOutcome(
                    passed=False,
                    reason=f"every push-event ci.yaml run on {sha[:12]} finished without "
                    f"success, and no green push run recorded tree {tree[:12]}",
                )
            log(f"no green push-event ci.yaml run on {sha[:12]} or tree {tree[:12]} yet")
        except GreenTreeError as exc:
            log(f"the CI lookup failed, retrying: {exc}")
        if clock() >= deadline:
            return GateOutcome(
                passed=False,
                reason=f"timed out after {GATE_DEADLINE_SECONDS}s waiting for a green "
                f"push-event ci.yaml run on {sha[:12]}",
            )
        sleep(POLL_SECONDS)


def plan_record(needs: object, *, event: str) -> str:
    """Decide whether a ci.yaml run may record its tree.

    Args:
        needs: The decoded ``toJSON(needs)`` of the record job.
        event: The triggering event name.

    Returns:
        An empty string when the heavy jobs passed on this run; otherwise
        the green baseline commit the changes job skipped them against,
        whose tree must itself be recorded.

    Raises:
        RecordRefusedError: The run is not a push, a needed job did not
            pass, or the heavy jobs neither all passed nor were all skipped
            against a named baseline.
    """
    if event != PUSH_EVENT:
        raise RecordRefusedError(f"a {event or 'unnamed'} run records no tree")
    _require_settled_needs(needs)
    results = {name: _dig(needs, name, "result") for name in HEAVY_JOBS}
    shown = ", ".join(f"{name}={result}" for name, result in results.items())
    code = _dig(needs, CHANGES_JOB, "outputs", "code")
    if code == "true":
        if any(result != "success" for result in results.values()):
            raise RecordRefusedError(f"the heavy jobs did not all pass: {shown}")
        return ""
    if code == "false":
        if any(result != "skipped" for result in results.values()):
            raise RecordRefusedError(f"the changes job skipped the heavy jobs, yet {shown}")
        green = _dig(needs, CHANGES_JOB, "outputs", "green")
        if not isinstance(green, str) or not _SHA_RE.fullmatch(green):
            raise RecordRefusedError(f"the changes job named no green baseline: {green!r}")
        return green
    raise RecordRefusedError(f"the changes job reported code={code!r}")


def _require_settled_needs(needs: object) -> None:
    """Refuse unless every needed job passed or skipped and the changes job ran."""
    if not isinstance(needs, dict) or not needs:
        raise RecordRefusedError("the record job sees no needed jobs")
    unsettled = sorted(
        str(name) for name, job in needs.items() if _dig(job, "result") not in _SETTLED_RESULTS
    )
    if unsettled:
        raise RecordRefusedError(f"needed jobs did not pass: {', '.join(unsettled)}")
    absent = [name for name in (CHANGES_JOB, *HEAVY_JOBS) if name not in needs]
    if absent:
        raise RecordRefusedError(f"the record job does not need {', '.join(absent)}")
    if _dig(needs, CHANGES_JOB, "result") != "success":
        raise RecordRefusedError("the changes job did not run")


def build_record(
    env: Mapping[str, str], *, needs: object, git: GitRunner, fetch: FetchJson
) -> TreeRecord:
    """Build the record this run may upload.

    Args:
        env: The Actions environment.
        needs: The decoded ``toJSON(needs)`` of the record job.
        git: Runs ``git <args>`` in the checkout and returns its stdout.
        fetch: GETs an API URL and returns the decoded JSON body.

    Returns:
        The record of the checked-out tree.

    Raises:
        RecordRefusedError: :func:`plan_record` refused, or the inherited
            tree carries no record.
        GreenTreeError: The environment, git or the API could not answer.
    """
    inherit = plan_record(needs, event=env.get("GITHUB_EVENT_NAME", ""))
    sha = _require_sha(env, "GITHUB_SHA")
    run_id = _require(env, "GITHUB_RUN_ID")
    tree = tree_of(git, sha)
    if not inherit:
        return TreeRecord(tree=tree, sha=sha, run_id=run_id, heavy_jobs="passed")

    repository = _require(env, "GITHUB_REPOSITORY")
    api_url = env.get("GITHUB_API_URL") or _DEFAULT_API_URL
    inherited_tree = tree_of(git, inherit)
    runs = green_push_runs(
        fetch(green_runs_url(api_url=api_url, repository=repository)), repository=repository
    )
    recorded = find_recorded_run(
        runs, tree=inherited_tree, fetch=fetch, api_url=api_url, repository=repository
    )
    if recorded is None:
        raise RecordRefusedError(
            f"the green baseline {inherit[:12]} has no recorded tree {inherited_tree[:12]}"
        )
    return TreeRecord(
        tree=tree,
        sha=sha,
        run_id=run_id,
        heavy_jobs="inherited",
        inherited_tree=inherited_tree,
        inherited_run_id=str(recorded.run_id),
    )


def tree_of(git: GitRunner, sha: str) -> str:
    """Return the tree object name of commit *sha*.

    Args:
        git: The git runner.
        sha: A full commit object name.

    Returns:
        The commit's tree object name.

    Raises:
        GreenTreeError: git cannot resolve the tree, or answers something
            that is not an object name.
    """
    tree = git(["rev-parse", "--verify", f"{sha}^{{tree}}"]).strip()
    if not _SHA_RE.fullmatch(tree):
        raise GreenTreeError(f"{sha[:12]} resolved to tree {tree!r}, not an object name")
    return tree


def record(path: Path, env: Mapping[str, str], *, git: GitRunner, fetch: FetchJson) -> int:
    """Write this run's tree record, if it may have one, and say so.

    Appends ``recorded=true`` and ``tree=<sha>`` to ``$GITHUB_OUTPUT`` after
    writing the record to *path*, or ``recorded=false`` when the run may
    not record. A refusal leaves the run green: without a record the
    release gates accept only a run on the tagged commit itself.

    Args:
        path: Where the record's JSON body is written.
        env: The Actions environment.
        git: The git runner.
        fetch: The API fetcher.

    Returns:
        ``0`` once a decision is written, ``2`` when ``GITHUB_OUTPUT`` is unset.
    """
    output = env.get("GITHUB_OUTPUT", "")
    if not output:
        print(
            "ci_green_tree: GITHUB_OUTPUT is unset, so the decision has nowhere to go",
            file=sys.stderr,
        )
        return 2
    try:
        needs: object = json.loads(env.get("NEEDS", ""))
    except ValueError:
        needs = None
    try:
        tree_record = build_record(env, needs=needs, git=git, fetch=fetch)
    except (RecordRefusedError, GreenTreeError) as exc:
        _append(output, "recorded=false\n")
        print(f"recorded=false: {exc}")
        return 0
    path.write_text(
        json.dumps(asdict(tree_record), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _append(output, f"recorded=true\ntree={tree_record.tree}\n")
    print(f"recorded=true: {record_name(tree_record.tree)} ({tree_record.heavy_jobs})")
    return 0


def gate(
    env: Mapping[str, str], *, git: GitRunner, fetch: FetchJson, clock: Clock, sleep: Sleep
) -> int:
    """Refuse the release unless CI proved the tagged commit or tree green.

    Args:
        env: The Actions environment.
        git: The git runner, over a checkout that holds the tagged commit.
        fetch: The API fetcher.
        clock: Returns a monotonic time in seconds.
        sleep: Pauses for the given number of seconds.

    Returns:
        ``0`` when a qualifying run was found, ``1`` on a refusal, ``2``
        when the environment or the checkout cannot name the tagged tree.
    """
    try:
        repository = _require(env, "GITHUB_REPOSITORY")
        sha = _require_sha(env, "GITHUB_SHA")
        tree = tree_of(git, sha)
    except GreenTreeError as exc:
        print(f"refusing to publish: {exc}", file=sys.stderr)
        return 2
    outcome = await_green_run(
        fetch=fetch,
        api_url=env.get("GITHUB_API_URL") or _DEFAULT_API_URL,
        repository=repository,
        sha=sha,
        tree=tree,
        clock=clock,
        sleep=sleep,
        log=print,
    )
    if outcome.passed:
        print(f"CI green, release may proceed: {outcome.reason}")
        return 0
    print(f"refusing to publish: {outcome.reason}", file=sys.stderr)
    return 1


def run_git(args: Sequence[str]) -> str:
    """Run ``git <args>`` in the current directory and return its stdout.

    Args:
        args: The git arguments.

    Returns:
        The command's standard output.

    Raises:
        GreenTreeError: git is missing or exits non-zero.
    """
    try:
        result = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        raise GreenTreeError(f"git {args[0]} failed: {exc.stderr.strip()}") from exc
    except OSError as exc:
        raise GreenTreeError(f"git could not start: {exc}") from exc
    return result.stdout


def github_fetcher(token: str) -> FetchJson:
    """Return a GET-and-decode function for the GitHub REST API.

    Args:
        token: The workflow token; empty sends an anonymous request.

    Returns:
        A function mapping a URL to its decoded JSON body, raising
        :class:`GreenTreeError` on any transport, HTTP or decoding failure.
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
            raise GreenTreeError(f"the API lookup failed: {exc}") from exc
        return body

    return fetch


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    git: GitRunner = run_git,
    fetch: FetchJson | None = None,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> int:
    """Dispatch the ``record`` or ``gate`` subcommand.

    Args:
        argv: The arguments after the program name; ``sys.argv[1:]`` when
            omitted.
        env: The environment to read; ``os.environ`` when omitted.
        git: The git runner.
        fetch: The API fetcher; a token-bearing GitHub fetcher when omitted.
        clock: The gate's monotonic clock.
        sleep: The gate's pause between lookups.

    Returns:
        The subcommand's exit code, or ``2`` on a malformed invocation.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    environ = os.environ if env is None else env
    fetcher = fetch if fetch is not None else github_fetcher(environ.get("GITHUB_TOKEN", ""))
    if args == ["gate"]:
        return gate(environ, git=git, fetch=fetcher, clock=clock, sleep=sleep)
    if len(args) == 2 and args[0] == "record":
        return record(Path(args[1]), environ, git=git, fetch=fetcher)
    print(_USAGE, file=sys.stderr)
    return 2


def _workflow_runs_url(api_url: str, repository: str) -> str:
    """Return the ci.yaml runs endpoint without a query."""
    return f"{api_url.rstrip('/')}/repos/{repository}/actions/workflows/{WORKFLOW_FILE}/runs"


def _runs(payload: object) -> list[dict[str, object]]:
    """Return the run objects of a runs-endpoint response."""
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        raise GreenTreeError("the runs response carries no 'workflow_runs' list")
    return [run for run in runs if isinstance(run, dict)]


def _is_run_id(value: object) -> bool:
    """Return whether *value* is an API run id; ``bool`` is an ``int`` too."""
    return isinstance(value, int) and not isinstance(value, bool)


def _append(output: str, lines: str) -> None:
    """Append *lines* to the ``$GITHUB_OUTPUT`` file."""
    with Path(output).open("a", encoding="utf-8") as handle:
        handle.write(lines)


def _require(env: Mapping[str, str], name: str) -> str:
    """Return the non-empty value of *name*."""
    value = env.get(name, "")
    if not value:
        raise GreenTreeError(f"{name} is unset")
    return value


def _require_sha(env: Mapping[str, str], name: str) -> str:
    """Return the value of *name*, which must be a full object name."""
    value = _require(env, name)
    if not _SHA_RE.fullmatch(value):
        raise GreenTreeError(f"{name} is {value!r}, not a full object name")
    return value


def _dig(value: object, *keys: str) -> object:
    """Walk nested mappings by *keys*, returning None at the first miss."""
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
