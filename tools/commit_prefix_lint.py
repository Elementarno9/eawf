"""Commit-msg and pushed-range linter for eawf commits.

Enforces:

1. The subject is ``<type>: <summary>``, where ``<type>`` is one of
   ``feat|fix|chore|docs|refactor|test|build|perf|ci|revert|state``.
   The **task-prefix form** ``[EAWF-0137] <type>: <summary>`` names the
   Task in the subject instead of a trailer; it passes, and warns on
   stderr under ``vcs.conventions.subject_style: trailer`` (the
   default). Any other bracket prefix is rejected.

2. State-bookkeeping path whitelist applies to any commit with
   ``type == "state"``. State-scoped commits MUST touch only
   state-bookkeeping paths (``.ea/state.json``, ``.ea/store/**``,
   ``.secrets.baseline`` and ``.ea/specs/**``). Two release companions
   may ride along: publication evidence under a
   ``.ea/artifacts/evidence/<date>-<label>-publication/`` directory, and
   ``.pre-commit-config.yaml`` only beside a staged ``.secrets.baseline``
   (its ``# Baseline-hash:`` comment follows the baseline refresh).
   On an epoch-2 root, where the lifecycle lives in the selected
   generation, ``.ea/generations/**`` and ``.ea/epoch2-opt-in.json`` are
   admitted too. Touching anything else is rejected.

3. A recognized Claude or Codex ``Co-Authored-By`` trailer MUST be
   present (the ``prepare-commit-msg`` stage hook auto-inserts it when
   the active harness is detected; this backstop rejects commits where
   the trailer was hand-deleted), unless the co-author policy disables
   trailers, in which case none may be present.

4. Native Task trailer. On an epoch-2 root a commit may carry
   ``Task: <KEY>`` naming a native Task (``EAWF-0137``). The Task must be
   CLAIMED or RUNNING in the canonical main worktree's selected
   generation, and it is capped at one commit. An unmarked root refuses
   the trailer, since no native Task exists there.

   Exempt from the cap: a commit whose staged paths are all on the
   state-bookkeeping whitelist (the fold amend), an amend of ``HEAD``
   itself (which rewrites the Task's commit rather than adding one), the
   ``state`` type (bookkeeping, already path-gated), and the
   **golden-only** ``test`` commit: one whose every staged path is a
   managed snapshot golden, which the snapshot-pairing gate forces into
   its own paired commit. A ``test`` commit that stages anything else is
   capped like any other type.

   The trailer's position is grammar: it is the last trailer, followed
   only by co-author trailers, and a message names one Task, unless it
   carries a ``Batch`` trailer (see check 6). A task-prefix subject is
   proved and capped the same way, and must agree with a trailer that
   also names one.

5. History stays lintable. ``--check-history <range>`` lints the subject
   and trailer grammar of every commit in a revision range without
   reading state; see ``commit_grammar_history.py``.

6. Pushed commits carry the per-Batch regime. ``--pre-push-lint``, the
   ``pre-push``-stage hook, lints every commit in the pushed range with
   :func:`pushed_commit_error`: every non-``state`` commit names at least
   one Task by ``Task:`` trailer and, when it names several, the one Batch
   they all belong to by ``Batch:`` trailer; a ``state:`` commit is
   confined to committed state paths. Commits stay free locally,
   so scratch history is refused only when it is pushed.

Checks 1-4 run when the first argument is a commit-message file path,
consulting ``git diff --cached --name-only`` for staged paths; no hook
stage calls that mode any longer.

Exit codes:
- ``0`` — accepted (an advisory deprecation warning may still print to
  stderr).
- ``1`` — rejected (message printed to stderr).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coauthor_policy import (
    SUPPORTED_TRAILERS,
    coauthor_disabled,
    has_any_coauthor_trailer,
    has_supported_trailer,
)
from epoch2_lifecycle_view import (
    NATIVE_TASK_KEY_RE,
    _collection_rows,
    epoch2_generation,
    native_task_status,
)

_TYPES = "feat|fix|chore|docs|refactor|test|build|perf|ci|revert|state"

_SUBJECT_BARE_CONVENTIONAL_RE = re.compile(rf"^(?P<type>{_TYPES}):\s+\S.*$")
# Mirrors ``TASK_TRAILER_KEY`` in src/eawf/runtime/integration/commit_policy.py,
# the trailer integration names each Task under; the hook cannot import the
# package, so tests/unit/test_commit_prefix_lint_epoch2.py pins the copy.
_TASK_TRAILER_NAME = "Task"
_TASK_TRAILER_RE = re.compile(rf"^{_TASK_TRAILER_NAME}:\s+(?P<key>\S+)\s*$", re.MULTILINE)
# A pushed commit names each Task it delivers; a Batch trailer names the Batch
# they belong to, and is required once a commit names more than one.
_BATCH_TRAILER_NAME = "Batch"
_BATCH_TRAILER_RE = re.compile(rf"^{_BATCH_TRAILER_NAME}:\s+(?P<key>\S+)\s*$", re.MULTILINE)
_BATCH_KEY_RE = re.compile(r"^BAT-\d{4}$")
# The prefix form names the Task in the subject instead of a trailer. Both
# grammars are read while the project moves to the trailer form; the prefix
# form warns whenever the trailer form is the configured one.
_SUBJECT_TASK_PREFIX_RE = re.compile(
    r"^\[(?P<key>" + NATIVE_TASK_KEY_RE.pattern.strip("^$") + r")\]\s+"
    rf"(?P<type>{_TYPES}):\s+\S.*$"
)
_TASK_PREFIX_DEPRECATION = (
    "deprecated task-prefix subject: {subject!r}\n"
    "vcs.conventions.subject_style is 'trailer': write '<type>: <summary>' with a "
    f"'{_TASK_TRAILER_NAME}: <KEY>' trailer as the last trailer instead. The prefix "
    "form still passes, but it is deprecated."
)
# Git appends the staged diff below this line under ``commit --verbose``;
# nothing after it is message.
_SCISSORS_LINE = "# ------------------------ >8 ------------------------"
_COAUTHOR_TRAILER_LINE_RE = re.compile(r"^Co-Authored-By:", re.IGNORECASE)
# A native Task proves a commit only while its work is live, in the
# generation's own spelling.
_LIVE_TASK_STATUSES = frozenset({"CLAIMED", "RUNNING"})
_SUBJECT_STYLE_BRACKET = "bracket"
_SUBJECT_STYLE_TRAILER = "trailer"
# Mirrors ``vcs.conventions.subject_style`` in src/eawf/kernel/config/defaults.py.
# The hook runs under system Python and cannot import the package, so the two
# defaults are kept in lockstep by hand and pinned by
# tests/unit/kernel/config/test_subject_style_default.py.
_SUBJECT_STYLE_DEFAULT = _SUBJECT_STYLE_TRAILER
_STATE_ONLY_ALLOWED = (
    ".ea/state.json",
    # ``.secrets.baseline`` auto-tracks state.json line numbers; the
    # detect-secrets pre-commit hook regenerates it whenever state.json
    # mutates, and refuses to commit when baseline is left unstaged.
    # State-bookkeeping commits therefore always need it riding along.
    ".secrets.baseline",
)
# The TYPED per-kind JSONLs under ``.ea/store/`` are daemon-written, committed
# stores (audit / evidence / decision / flow / role reports / memory) per the
# authority map, so a state commit carries them alongside ``state.json``.
#
# ``event.jsonl`` is the exception and is NOT committed (gitignored): it is the
# firehose rather than the ledger — one row per mutation plus the raw stdout of
# every spawned agent — so it grows without bound and carries free text nobody
# typed. The prefix below still admits it if a repo chooses to track it; this
# repo does not.
_STATE_ONLY_PREFIXES = (".ea/store/", ".ea/specs/")
# After the cutover the lifecycle bookkeeping lands in the selected
# generation, and the opt-in declaration is committed beside it, so a state
# commit carries those instead of ``state.json``. They are admitted only on
# a marked root: before the cut nothing may write a generation.
_EPOCH2_STATE_PREFIXES = (".ea/generations/",)
_EPOCH2_STATE_ALLOWED = (".ea/epoch2-opt-in.json",)
# The post-merge release pipeline ends in one state commit that lands its
# publication evidence beside the release store, and refreshing the
# detect-secrets baseline bumps the baseline-hash comment in the pre-commit
# config. These stay out of ``_is_state_only_path``: that predicate also
# decides the fold exemption from the commit cap, which evidence and config
# edits must not trigger. Only the pipeline's own
# ``<date>-<label>-publication/`` directory qualifies, so other evidence
# cannot slip into a state commit under the release carve-out.
_STATE_EVIDENCE_PREFIX = ".ea/artifacts/evidence/"
_STATE_EVIDENCE_DIR_SUFFIX = "-publication"
_STATE_BASELINE_HASH_COMPANION = ".pre-commit-config.yaml"
# Mirrors the ``golden_dir`` of every surface in ``SNAPSHOT_SURFACES``
# (src/eawf/surfaces/cli/commands/snapshot.py), the inventory the
# snapshot-pairing gate watches. The hook runs under system Python and cannot
# import the package, so tests/unit/test_commit_prefix_lint_golden_cap.py pins
# this copy against the inventory: a stale copy would cap the very golden
# refresh the pairing gate demands. The trailing slash keeps a sibling
# directory that merely shares a prefix out of the set.
_MANAGED_GOLDEN_DIRS = (
    "tests/golden/agents_md/",
    "tests/golden/audit_dsl/",
    "tests/golden/dispatch/",
    "tests/golden/envelope/",
    "tests/golden/metrics_export/",
    "tests/golden/plugin_install/",
    "tests/golden/spec/",
    "tests/golden/state/",
    "tests/golden/surfaces/cli/scenarios/",
    "tests/golden/telemetry/",
    "tests/snapshots/svg/golden/",
)
_GIT_TIMEOUT_SECONDS = 20.0
# The separators the pushed-range ``git log --format`` spells as ``%x00`` and
# ``%x1e``; a pre-push hook sees an all-zero SHA for a created or deleted ref.
_REC_SEP = "\x00"
_COMMIT_SEP = "\x1e"
_ZERO_SHA_CHAR = "0"


def _find_repo_root(start: Path | None = None) -> Path:
    """Return nearest ancestor with ``.ea`` or ``.git``; cwd fallback."""
    cwd = Path.cwd() if start is None else start
    for parent in [cwd, *cwd.parents]:
        if (parent / ".ea").exists() or (parent / ".git").exists():
            return parent
    return cwd


def _configured_subject_style(repo_root: Path | None = None) -> str:
    """Return ``vcs.conventions.subject_style`` from repo/local config.

    The hook runs under system Python, so this intentionally avoids importing
    package YAML dependencies. It reads the two file-backed repo layers the
    hook can see directly; missing or malformed values fall back to
    :data:`_SUBJECT_STYLE_DEFAULT`.
    """
    root = _find_repo_root(repo_root)
    style = _SUBJECT_STYLE_DEFAULT
    for config_path in (root / ".ea" / "config.yaml", root / ".ea" / "local" / "config.yaml"):
        candidate = _subject_style_from_config(config_path)
        if candidate is not None:
            style = candidate
    return style


def _subject_style_from_config(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    in_vcs = False
    in_conventions = False
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent == 0:
            in_vcs = stripped == "vcs:"
            in_conventions = False
            continue
        if in_vcs and indent == 2:
            in_conventions = stripped == "conventions:"
            continue
        if in_vcs and in_conventions and indent == 4 and stripped.startswith("subject_style:"):
            value = _strip_yaml_scalar(stripped.split(":", 1)[1])
            if value in {_SUBJECT_STYLE_BRACKET, _SUBJECT_STYLE_TRAILER}:
                return value
    return None


def _strip_yaml_scalar(value: str) -> str:
    value = value.split("#", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def _message_lines(text: str) -> list[str]:
    """Return the message's non-blank lines, without comments or the verbose diff."""
    lines: list[str] = []
    for raw in text.splitlines():
        if raw.rstrip() == _SCISSORS_LINE:
            break
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(stripped)
    return lines


def task_trailer_position_error(text: str) -> str | None:
    """Return why the ``Task`` trailer is misplaced, or ``None`` when it is not.

    The trailer is the last one before the agent co-author trailers, so a
    reader takes it from a fixed position rather than searching the body.

    Args:
        text: The full commit message.

    Returns:
        A diagnostic when the message names more than one Task trailer, or
        when anything but co-author trailers follows it; ``None`` when the
        message carries no Task trailer, carries one in place, or carries a
        ``Batch`` trailer, whose commit may name several Tasks of its Batch
        (see :func:`pushed_commit_error`).
    """
    if _BATCH_TRAILER_RE.search(text) is not None:
        return None
    lines = _message_lines(text)
    positions = [index for index, line in enumerate(lines) if _TASK_TRAILER_RE.match(line)]
    if not positions:
        return None
    if len(positions) > 1:
        named = ", ".join(lines[index] for index in positions)
        return f"{_TASK_TRAILER_NAME} trailer repeated ({named}); a commit names exactly one Task"
    end = len(lines)
    while end > 0 and _COAUTHOR_TRAILER_LINE_RE.match(lines[end - 1]):
        end -= 1
    if positions[0] != end - 1:
        return (
            f"{_TASK_TRAILER_NAME} trailer out of place: {lines[positions[0]]!r} is followed by "
            f"{lines[positions[0] + 1]!r}; it must be the last trailer, with only "
            "co-author trailers after it"
        )
    if any(_COAUTHOR_TRAILER_LINE_RE.match(line) for line in lines[: positions[0]]):
        return (
            f"{_TASK_TRAILER_NAME} trailer out of place: {lines[positions[0]]!r} follows a "
            "co-author trailer; it comes before them"
        )
    return None


@dataclass(frozen=True)
class NativeRecords:
    """The canonical generation's native Batches and the Batch of each Task.

    Attributes:
        batch_keys: Every native Batch key the generation holds, whatever
            its status.
        task_batches: Each Task key mapped to the key of the Batch its
            ``batch_ref`` names, or ``None`` when it names none.
    """

    batch_keys: frozenset[str]
    task_batches: Mapping[str, str | None]


def _ref_key(ref: object) -> str | None:
    # A ref is a URN whose last segment is the record key; reading only that
    # segment keeps the check indifferent to how many slots the URN carries.
    return ref.rsplit("/", 1)[-1] if isinstance(ref, str) and ref else None


def _batch_ref(row: dict[str, Any]) -> object:
    # A Task compacted into the ledger keeps its fields inside the envelope's
    # payload, while a document row holds them at the top level.
    payload = row.get("payload")
    if "batch_ref" not in row and isinstance(payload, dict):
        return payload.get("batch_ref")
    return row.get("batch_ref")


def load_native_records(generation: Path) -> NativeRecords:
    """Read the native Batches and Task memberships of *generation*.

    Args:
        generation: The selected generation directory.

    Returns:
        The Batch keys and each Task's Batch key.

    Raises:
        OSError: The generation document cannot be read.
        ValueError: The document or a ledger is not the expected JSON shape.
    """
    batches = _collection_rows(generation, "batch")
    tasks = _collection_rows(generation, "task")
    return NativeRecords(
        batch_keys=frozenset(key for key in batches if _BATCH_KEY_RE.match(key)),
        task_batches={key: _ref_key(_batch_ref(row)) for key, row in tasks.items()},
    )


def pushed_commit_error(
    message: str, paths: Iterable[str], records: NativeRecords | None
) -> str | None:
    """Return why one pushed commit breaks the per-Batch regime, or ``None``.

    A ``state:`` commit carries only committed state paths. Any other commit
    is ``<type>: <summary>`` naming at least one Task of the canonical
    generation by ``Task: <KEY>`` trailer. A commit naming one Task may omit
    the ``Batch: BAT-<NNNN>`` trailer; a commit naming several carries
    exactly one, and every Task it names belongs to that Batch, which may be
    in any status. No per-Task commit cap applies.

    Args:
        message: The full commit message.
        paths: The paths the commit changes.
        records: The canonical generation's records, or ``None`` when the
            canonical root is not marked epoch 2.

    Returns:
        The first diagnostic, or ``None`` when the commit is accepted.
    """
    subject = _extract_subject(message)
    conventional = _SUBJECT_BARE_CONVENTIONAL_RE.match(subject)
    if conventional is None:
        return f"subject is not '<type>: <summary>': {subject!r}"
    if conventional.group("type") == "state":
        strays = _state_strays(list(paths), epoch2=records is not None)
        return None if not strays else f"state commit touches non-state paths: {strays}"
    task_keys = [match.group("key") for match in _TASK_TRAILER_RE.finditer(message)]
    batch_keys = [match.group("key") for match in _BATCH_TRAILER_RE.finditer(message)]
    if not task_keys:
        return f"carries no {_TASK_TRAILER_NAME} trailers; a commit names at least one Task"
    if len(task_keys) > 1 and not batch_keys:
        return (
            f"carries {len(task_keys)} ({', '.join(task_keys)}) {_TASK_TRAILER_NAME} trailers "
            f"without a {_BATCH_TRAILER_NAME} trailer; a commit naming several Tasks names "
            "their Batch"
        )
    repeated = sorted({key for key in task_keys if task_keys.count(key) > 1})
    if repeated:
        return f"names {_TASK_TRAILER_NAME} {repeated[0]!r} twice; name each Task once"
    if records is None:
        return (
            f"{_TASK_TRAILER_NAME} trailer {task_keys[0]!r}: the canonical root is not marked "
            "epoch 2"
        )
    for task in task_keys:
        if task not in records.task_batches:
            return (
                f"{_TASK_TRAILER_NAME} trailer {task!r} names no Task of the canonical generation"
            )
    return _batch_trailer_error(batch_keys, task_keys, records)


def _batch_trailer_error(
    batch_keys: list[str], task_keys: list[str], records: NativeRecords
) -> str | None:
    # A single-Task commit may omit the Batch trailer; one that is present must
    # name the Batch every Task of the commit belongs to.
    if not batch_keys:
        return None
    if len(batch_keys) > 1:
        return (
            f"carries {len(batch_keys)} ({', '.join(batch_keys)}) {_BATCH_TRAILER_NAME} "
            "trailers; a commit names at most one Batch"
        )
    batch = batch_keys[0]
    if not _BATCH_KEY_RE.match(batch):
        return f"{_BATCH_TRAILER_NAME} trailer {batch!r} is not a Batch key (BAT-<NNNN>)"
    if batch not in records.batch_keys:
        return f"{_BATCH_TRAILER_NAME} trailer {batch!r} names no Batch of the canonical generation"
    for task in task_keys:
        owner = records.task_batches[task]
        if owner != batch:
            return (
                f"{_TASK_TRAILER_NAME} trailer {task!r} belongs to Batch {owner!r}, not {batch!r}"
            )
    return None


def _canonical_state_path(repo_root: Path) -> Path | None:
    """Resolve the main-worktree state through Git's supported common-dir API."""
    proc = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    common_dir = Path(raw)
    if not common_dir.is_absolute():
        return None
    return common_dir.parent / ".ea" / "state.json"


def _staged_paths() -> list[str]:
    """Return paths reported by ``git diff --cached --name-only``.

    Empty list when there are no staged changes (e.g. an amend in the
    working copy, or the hook is invoked outside a commit context).
    """
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _is_state_only_path(path: str) -> bool:
    if path in _STATE_ONLY_ALLOWED:
        return True
    return any(path.startswith(p) for p in _STATE_ONLY_PREFIXES)


def _is_publication_evidence_path(path: str) -> bool:
    """Return whether *path* sits in a release publication evidence directory.

    Args:
        path: A staged repo-relative path.

    Returns:
        True when the first component under ``.ea/artifacts/evidence/`` is a
        directory whose name ends with ``-publication``.
    """
    if not path.startswith(_STATE_EVIDENCE_PREFIX):
        return False
    dirname, sep, _ = path.removeprefix(_STATE_EVIDENCE_PREFIX).partition("/")
    return bool(sep) and dirname.endswith(_STATE_EVIDENCE_DIR_SUFFIX)


def _is_epoch2_state_path(path: str) -> bool:
    return path in _EPOCH2_STATE_ALLOWED or path.startswith(_EPOCH2_STATE_PREFIXES)


def _state_strays(staged: list[str], *, epoch2: bool) -> list[str]:
    """Return the staged paths a state commit may not carry.

    Args:
        staged: Paths staged for the commit.
        epoch2: Whether the root is marked epoch 2.

    Returns:
        Every path off the state-bookkeeping whitelist, except publication
        evidence under ``.ea/artifacts/evidence/<date>-<label>-publication/``,
        the pre-commit config when ``.secrets.baseline`` is staged beside it,
        and, on an epoch-2 root, the generations tree and the opt-in
        declaration.
    """
    refreshes_baseline = ".secrets.baseline" in staged
    return [
        path
        for path in staged
        if not _is_state_only_path(path)
        and not _is_publication_evidence_path(path)
        and not (refreshes_baseline and path == _STATE_BASELINE_HASH_COMPANION)
        and not (epoch2 and _is_epoch2_state_path(path))
    ]


def _check_state_paths(
    *, commit_type: str, staged: list[str], epoch2: bool
) -> tuple[int, str] | None:
    """Enforce the state-bookkeeping path whitelist for a ``state`` commit.

    State-scoped commits (``type == 'state'``) must touch only
    state-bookkeeping paths, plus release publication evidence and the
    baseline-hash companion of a staged baseline refresh. On an epoch-2 root
    (*epoch2*) a state commit may also carry ``.ea/generations/**`` and
    ``.ea/epoch2-opt-in.json``.

    Returns a ``(1, diagnostic)`` rejection when a state commit strays
    outside its whitelist, else ``None``.
    """
    if commit_type != "state":
        return None
    bad = _state_strays(staged, epoch2=epoch2)
    if not bad:
        return None
    epoch2_note = ", .ea/generations/**, .ea/epoch2-opt-in.json" if epoch2 else ""
    return 1, (
        f"state-type commit touches non-state paths: {bad}\n"
        "state-scoped commits must mutate only .ea/state.json, "
        f".ea/store/**, .secrets.baseline, .ea/specs/**{epoch2_note} or "
        ".ea/artifacts/evidence/<date>-<label>-publication/**; "
        ".pre-commit-config.yaml may ride "
        "along solely beside a staged .secrets.baseline"
    )


def _prior_commits(terms: list[str], *, repo_root: Path | None) -> list[str]:
    """Return SHAs reachable from ``HEAD`` whose message carries one of *terms*.

    Scans ``HEAD`` only, never ``--all``: while a Task commit still lives on
    its worktree branch it must not count as its own predecessor, or every
    cherry-pick of it would be read as a second commit. Probe failures (git
    missing, unborn HEAD, timeout) return an empty list, so the cap fails open
    rather than blocking a commit it cannot reason about.
    """
    found: list[str] = []
    for term in terms:
        try:
            proc = subprocess.run(
                ["git", "log", "HEAD", f"--grep={term}", "-F", "--format=%H", "-n", "1"],
                cwd=None if repo_root is None else str(repo_root),
                check=False,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except OSError, subprocess.SubprocessError:
            return []
        if proc.returncode != 0:
            continue
        found.extend(line.strip() for line in proc.stdout.splitlines() if line.strip())
    return found


def _author_date_epoch(raw: str | None) -> str | None:
    """Return the epoch-seconds field of a git author date, else ``None``.

    Args:
        raw: A ``GIT_AUTHOR_DATE`` value in the internal form git exports to
            hooks, such as ``"@1788883700 +0200"``.

    Returns:
        The epoch-seconds digits, or ``None`` when *raw* is absent, empty, or
        spelled in any other format (an operator-supplied ISO date, say).
    """
    if not raw:
        return None
    fields = raw.strip().split()
    if not fields:
        return None
    token = fields[0].removeprefix("@")
    return token if token.isdigit() else None


def _head_identity(repo_root: Path | None) -> tuple[str, str] | None:
    """Return ``(sha, author_epoch)`` for ``HEAD``, or ``None`` when unknown.

    Args:
        repo_root: Directory the probe runs in; ``None`` uses the cwd.

    Returns:
        The tip's SHA and author-date epoch seconds. Probe failures (git
        missing, unborn HEAD, timeout, unparsable output) return ``None`` so
        the caller reads the commit as an ordinary append rather than an amend.
    """
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%H %at", "HEAD"],
            cwd=None if repo_root is None else str(repo_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if proc.returncode != 0:
        return None
    fields = proc.stdout.strip().split()
    if len(fields) != 2 or not fields[1].isdigit():
        return None
    return fields[0], fields[1]


def _amends_head(
    *,
    prior: list[str],
    repo_root: Path | None,
    env: Mapping[str, str],
) -> bool:
    """Return True when this commit rewrites ``HEAD`` instead of appending.

    ``git commit --amend`` reaches a commit-msg hook with the same argv and
    environment keys as an ordinary commit, so the rewrite has to be inferred
    from two signals taken together: the Task's existing commit is ``HEAD``
    (an amend can only rewrite the tip), and ``--amend`` preserves the original
    author date, which git exports to the hook as ``GIT_AUTHOR_DATE``. A fresh
    commit stamps the current time instead, so an author date equal to
    ``HEAD``'s own is the rewrite's fingerprint.

    The residual false-accept window is one clock second wide - a genuine
    second commit written inside the same second as the Task commit reads as
    an amend. That is the cheaper error: an uncapped extra commit costs one
    line of history, while a false reject leaves the operator unable to land
    the fold the cap's own diagnostic prescribes.

    Args:
        prior: SHAs of the commits already carrying the Task.
        repo_root: Directory the git probe runs in; ``None`` uses the cwd.
        env: Environment the hook was invoked with.

    Returns:
        ``True`` when both signals hold, else ``False``.
    """
    env_epoch = _author_date_epoch(env.get("GIT_AUTHOR_DATE"))
    if env_epoch is None:
        return False
    head = _head_identity(repo_root)
    if head is None:
        return False
    head_sha, head_epoch = head
    return head_sha in prior and head_epoch == env_epoch


def _is_managed_golden(path: str) -> bool:
    """Return whether *path* is golden bytes inside a managed snapshot surface.

    Python under a surface directory is the suite that produces the goldens,
    not a golden, matching the snapshot-pairing gate's own reading.
    """
    return not path.endswith(".py") and path.startswith(_MANAGED_GOLDEN_DIRS)


def _check_commit_cap(
    *,
    unit: str,
    terms: list[str],
    commit_type: str,
    staged: list[str],
    repo_root: Path | None,
    env: Mapping[str, str],
) -> tuple[int, str] | None:
    """Cap a Task at one commit, outside the fold amend and the amend proper.

    Args:
        unit: What is capped, as the diagnostic names it (``task <KEY>``).
        terms: Fixed strings that identify the unit in a commit message.
        commit_type: Conventional-commit type of this commit.
        staged: Paths the commit stages.
        repo_root: Directory the git probes run in; ``None`` uses the cwd.
        env: Environment the hook was invoked with.

    Returns:
        A ``(1, diagnostic)`` rejection when the unit already has a commit on
        ``HEAD`` and this one appends deliverable bytes to it, else ``None``.
    """
    if commit_type == "state":
        # Bookkeeping, already confined by the state path whitelist.
        return None
    if commit_type == "test" and all(_is_managed_golden(path) for path in staged):
        # The snapshot-pairing gate forces a managed golden refresh into its
        # own paired ``test:`` commit, the one second commit a Task may have.
        return None
    if all(_is_state_only_path(path) for path in staged):
        # The fold amend (and a bare reword, which stages nothing) adds no
        # deliverable bytes: it folds close bookkeeping into the commit that
        # already carries the Task.
        return None
    prior = _prior_commits(terms, repo_root=repo_root)
    if not prior:
        return None
    if _amends_head(prior=prior, repo_root=repo_root, env=env):
        # An amend rewrites the Task's existing commit rather than adding one,
        # so the Task still ends up with exactly one commit. Rejecting it would
        # refuse the remedy the diagnostic below prescribes.
        return None
    golden_note = (
        "\na test commit follows the Task commit only when every path it "
        "stages is a managed snapshot golden (the 'eawf snapshot list' "
        "inventory); other test changes ride the Task commit."
        if commit_type == "test"
        else ""
    )
    return 1, (
        f"second commit for {unit}: {prior[0][:12]} already carries it\n"
        f"one commit per {unit.split()[0]}: fold this change into its commit with "
        "'git commit --amend', or — when it is genuinely new work — add a "
        f"Task and commit it under its own id.{golden_note}"
    )


def _extract_subject(text: str) -> str:
    """Return the first non-blank, non-comment line in *text*."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def _match_subject(subject: str) -> re.Match[str] | None:
    """Return the match of *subject* against the accepted subject forms.

    The forms are ``<type>: <summary>`` and the task-prefix form
    ``[<KEY>] <type>: <summary>``; ``None`` when neither matches.
    """
    return _SUBJECT_BARE_CONVENTIONAL_RE.match(subject) or _SUBJECT_TASK_PREFIX_RE.match(subject)


def _subject_rejection(subject: str) -> str:
    return (
        f"commit subject rejected: {subject!r}\n"
        f"expected '<type>: <summary>' with a '{_TASK_TRAILER_NAME}: <KEY>' trailer, "
        "or '[<KEY>] <type>: <summary>' (task-prefix form); "
        f"type is one of {_TYPES}"
    )


def _check_coauthor(text: str, env: Mapping[str, str]) -> tuple[int, str]:
    """Return ``(0, "")`` when the co-author trailer policy is satisfied."""
    if coauthor_disabled(env):
        if has_any_coauthor_trailer(text):
            return 1, "co-author trailers are disabled by vcs.coauthor policy"
        return 0, ""
    if not has_supported_trailer(text):
        return 1, (
            f"missing recognized co-author trailer: {SUPPORTED_TRAILERS!r}\n"
            "the prepare-commit-msg hook inserts one when a supported "
            "harness is detected; otherwise paste a recognized trailer manually"
        )
    return 0, ""


def _accept_or_reject(
    text: str,
    env: Mapping[str, str],
    *,
    warning: str,
) -> tuple[int, str]:
    """Return the terminal verdict: the co-author reject, else *warning* at 0.

    A rejection's diagnostic always wins the single message slot; an advisory
    warning is only worth printing on a commit that is actually being accepted.
    """
    code, diag = _check_coauthor(text, env)
    if code != 0:
        return code, diag
    return 0, warning


def _task_prefix_deprecation(subject: str, *, subject_style: str) -> str:
    """Return the deprecation warning for a task-prefix subject, or ``""``.

    The prefix form warns only when ``subject_style`` is ``trailer``, the
    form the project is moving to.
    """
    if subject_style != _SUBJECT_STYLE_TRAILER or not _SUBJECT_TASK_PREFIX_RE.match(subject):
        return ""
    return _TASK_PREFIX_DEPRECATION.format(subject=subject)


def _named_task_keys(subject: str, text: str) -> tuple[list[str], str | None]:
    """Return the Task keys a commit names, and why they disagree if they do.

    Args:
        subject: The commit subject, which may carry the task-prefix form.
        text: The full commit message, which may carry a ``Task`` trailer.

    Returns:
        The distinct keys in carrier order (prefix first), beside a
        diagnostic when the prefix and the trailer name different Tasks.
    """
    prefix = _SUBJECT_TASK_PREFIX_RE.match(subject)
    keys = [prefix.group("key")] if prefix is not None else []
    for trailer in _TASK_TRAILER_RE.finditer(text):
        if trailer.group("key") not in keys:
            keys.append(trailer.group("key"))
    if prefix is not None and len(keys) > 1:
        return keys, (
            f"subject/trailer task mismatch: subject={keys[0]!r} trailer={keys[1]!r}; "
            "a commit names exactly one Task"
        )
    return keys, None


def _check_task_scope(
    *,
    subject: str,
    text: str,
    commit_type: str,
    staged: list[str],
    state_path: Path | None,
    repo_root: Path | None,
    canonical_state_path: Path | None,
    env: Mapping[str, str],
) -> tuple[int, str] | None:
    """Validate the native Task the subject prefix or ``Task`` trailer names.

    The proof reads the canonical main worktree's selected generation, so a
    worktree cannot prove a Task against its own stale copy.

    Returns:
        A ``(1, diagnostic)`` rejection for two carriers naming different
        Tasks, an unmarked root, a malformed key, a Task the canonical
        generation lacks or holds in any status but CLAIMED or RUNNING, or
        a second commit for the Task; ``None`` when every named Task holds
        or none is named.
    """
    keys, mismatch = _named_task_keys(subject, text)
    if mismatch is not None:
        return 1, mismatch
    for key in keys:
        label = f"{_TASK_TRAILER_NAME} trailer rejected: {key!r}"
        if state_path is None or epoch2_generation(state_path.parent) is None:
            return 1, f"{label} names a native Task, and this root is not marked epoch 2"
        if not NATIVE_TASK_KEY_RE.match(key):
            return 1, f"{label} is not a native Task key (<PROJECT>-####)"
        canonical = canonical_state_path or _canonical_state_path(
            repo_root or _find_repo_root(state_path.parent)
        )
        generation = None if canonical is None else epoch2_generation(canonical.parent)
        if generation is None:
            return 1, f"{label}: the canonical main-worktree root is not marked epoch 2"
        try:
            status = native_task_status(generation, key)
        except (OSError, ValueError) as exc:
            return 1, f"managed state decode failed at {generation.name}: {exc}"
        if status not in _LIVE_TASK_STATUSES:
            found = "no such Task" if status is None else f"status {status!r}"
            return 1, (
                f"{label}: the canonical generation has {found}; "
                "a Task proves a commit only while it is CLAIMED or RUNNING"
            )
        capped = _check_commit_cap(
            unit=f"task {key}",
            terms=[f"{_TASK_TRAILER_NAME}: {key}", f"[{key}]"],
            commit_type=commit_type,
            staged=staged,
            repo_root=repo_root,
            env=env,
        )
        if capped is not None:
            return capped
    return None


def lint(
    message_path: Path,
    staged: list[str],
    env: Mapping[str, str] | None = None,
    state_path: Path | None = None,
    repo_root: Path | None = None,
    subject_style: str | None = None,
    canonical_state_path: Path | None = None,
) -> tuple[int, str]:
    """Run every subject, Task, path and trailer check on one commit.

    Args:
        message_path: The commit-message file.
        staged: The paths the commit stages.
        env: The hook environment; ``None`` reads as empty.
        state_path: The root's ``.ea/state.json``, whose directory marks the
            root as epoch 2 or not; ``None`` when the root has no ``.ea``.
        repo_root: The repository the git probes run in; ``None`` uses the cwd.
        subject_style: The ``vcs.conventions.subject_style`` value; ``None``
            reads it from the repository config.
        canonical_state_path: The main worktree's ``.ea/state.json``; ``None``
            resolves it through git.

    Returns:
        ``(exit_code, diagnostic)``. A non-zero code means rejection and the
        diagnostic carries the reason; a zero code with a non-empty diagnostic
        is an accepted commit plus an advisory warning (the caller prints both
        to stderr).
    """
    resolved_env: Mapping[str, str] = {} if env is None else env
    text = message_path.read_text(encoding="utf-8")
    subject = _extract_subject(text)
    if not subject:
        return 1, "empty commit subject"
    match = _match_subject(subject)
    if match is None:
        return 1, _subject_rejection(subject)
    position = task_trailer_position_error(text)
    if position is not None:
        return 1, position
    commit_type = match.group("type")
    scope = _check_task_scope(
        subject=subject,
        text=text,
        commit_type=commit_type,
        staged=staged,
        state_path=state_path,
        repo_root=repo_root,
        canonical_state_path=canonical_state_path,
        env=resolved_env,
    )
    if scope is not None:
        return scope
    scoped = _check_state_paths(
        commit_type=commit_type,
        staged=staged,
        epoch2=state_path is not None and epoch2_generation(state_path.parent) is not None,
    )
    if scoped is not None:
        return scoped
    deprecation = _task_prefix_deprecation(
        subject, subject_style=subject_style or _configured_subject_style(repo_root)
    )
    return _accept_or_reject(text, resolved_env, warning=deprecation)


def _git(args: list[str], *, repo_root: Path | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root) if repo_root else None,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def check_pushed_commits(
    env: Mapping[str, str], *, repo_root: Path | None = None
) -> tuple[int, str]:
    """Lint every non-merge commit a push adds under the per-Batch regime.

    The pushed range is ``PRE_COMMIT_FROM_REF..PRE_COMMIT_TO_REF``; for a new
    branch pre-commit sets the first variable to the parent of the oldest
    commit no remote holds, so the range is still exactly what the push adds.
    A deletion, or a run without the variables, adds nothing and passes. The
    Batch and Task records are read from the canonical main-worktree
    generation, so a worktree cannot prove a commit against a stale copy.

    Args:
        env: The hook environment.
        repo_root: Repository working directory; ``None`` uses the cwd.

    Returns:
        ``(exit_code, diagnostic)``; ``1`` naming each refused commit, or when
        the range or the generation cannot be read; ``0`` otherwise.
    """
    old = env.get("PRE_COMMIT_FROM_REF", "")
    new = env.get("PRE_COMMIT_TO_REF", "")
    if not old.strip(_ZERO_SHA_CHAR) or not new.strip(_ZERO_SHA_CHAR):
        return 0, ""
    log = _git(
        ["log", "--no-merges", "--format=%H%x00%B%x1e", f"{old}..{new}", "--"],
        repo_root=repo_root,
    )
    if log.returncode != 0:
        return 1, f"commit lint: cannot read the commits between {old} and {new}"
    canonical = _canonical_state_path(repo_root or Path.cwd())
    generation = None if canonical is None else epoch2_generation(canonical.parent)
    try:
        records = None if generation is None else load_native_records(generation)
    except (OSError, ValueError) as exc:
        return 1, f"commit lint: managed state decode failed: {exc}"
    refused: list[str] = []
    for record in log.stdout.split(_COMMIT_SEP):
        sha, _, message = record.strip("\n").partition(_REC_SEP)
        if not sha:
            continue
        changed = _git(
            ["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha],
            repo_root=repo_root,
        )
        if changed.returncode != 0:
            return 1, f"commit lint: cannot read the paths of {sha[:12]}"
        error = pushed_commit_error(message, changed.stdout.splitlines(), records)
        if error is not None:
            refused.append(f"{sha[:12]} {error}")
    if not refused:
        return 0, ""
    return 1, "\n".join(
        [
            "commit lint: this push carries commits outside the per-Batch regime:",
            *refused,
            "Squash each Batch's scratch work into one '<type>: <summary>' commit with "
            "one 'Batch: BAT-<NNNN>' trailer and a 'Task: <KEY>' trailer per Task it "
            "delivers, ending with one 'state: <summary>' commit, then push again.",
        ]
    )


def _check_history_main(argv: list[str]) -> int:
    """Run ``--check-history <rev-range>`` and return its exit code."""
    from commit_grammar_history import check_history

    if len(argv) == 3:
        exit_code, diag = check_history(argv[2])
    else:
        exit_code, diag = 1, "usage: commit_prefix_lint.py --check-history <rev-range>"
    if diag:
        print(diag, file=sys.stderr)
    return exit_code


def main(argv: list[str]) -> int:
    if argv[1:2] == ["--check-history"]:
        return _check_history_main(argv)
    if argv[1:2] == ["--pre-push-lint"]:
        exit_code, diag = check_pushed_commits(os.environ)
        if diag:
            print(diag, file=sys.stderr)
        return exit_code
    if len(argv) < 2:
        print("usage: commit_prefix_lint.py <commit-msg-path>", file=sys.stderr)
        return 1
    message_path = Path(argv[1])
    if not message_path.exists():
        print(f"commit message file missing: {message_path}", file=sys.stderr)
        return 1
    repo_root = _find_repo_root()
    managed_state_path = repo_root / ".ea" / "state.json" if (repo_root / ".ea").is_dir() else None
    exit_code, diag = lint(
        message_path,
        _staged_paths(),
        env=os.environ,
        state_path=managed_state_path,
        repo_root=repo_root,
    )
    if diag:
        print(diag, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
