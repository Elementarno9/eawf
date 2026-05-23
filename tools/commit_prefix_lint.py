"""Commit-msg + diff scope linter for eawf phase-bundled commits.

Enforces:

1. Subject line must match one of two prefix grammars:

   - **Wave/CORE form** (planned wave deliverable or legacy phase-
     bookkeeping alias):
     ``^\\[P\\d{2}(-I\\d{2})?(-W\\d{2}|-CORE)\\]\\s+<type>:\\s+\\S.*$``
     where ``<type>`` is one of ``feat|fix|chore|docs|refactor|test|
     build|perf|ci|revert|state``. The ``-W##`` or ``-CORE`` suffix
     declares whether the commit advances a planned wave or carries
     cross-wave bookkeeping (P19-W05).

   - **Bare phase/iter form** (post-P26-W23): a bare
     ``[P##(-I##)?]`` prefix with ``type`` ∈ {``state``, ``docs``}:
     ``^\\[P\\d{2}(-I\\d{2})?\\]\\s+(state|docs):\\s+\\S.*$``
     ``state`` is the canonical signal for phase/iter-scope
     bookkeeping; ``docs`` carries phase/iter-scoped documentation
     artifacts that no single wave owns (closure audits, promoted
     research / decision / incident briefs). The ``-CORE`` suffix is
     retained as a legacy alias on the wave/CORE form so prior commits
     still validate, but new bookkeeping commits MAY drop the suffix —
     the conventional-commit ``type`` IS the semantic signal.

   ``W00`` and ``I00`` are rejected in both forms: wave / iter indices
   are 1-based by convention, and reactive waves get the next
   available ``W##`` per the feedback-commit-prefix-taxonomy memory.

2. State-bookkeeping path whitelist applies to:

   - any commit with ``type == "state"`` (the canonical semantic
     signal — fires whether the subject carries ``[P##-CORE]``,
     ``[P##-I##-CORE]``, or the bare ``[P##]`` / ``[P##-I##]`` form);
     and
   - any commit whose subject still carries the legacy ``-CORE``
     suffix (back-compat with D16 — pre-P26-W23 lint enforced the
     whitelist on the suffix; this keeps a hypothetical
     ``[P##-CORE] feat: ...`` rejected when it touches non-state
     paths).

   State-scoped commits MUST touch only state-bookkeeping paths
   (``.ea/state.json``, ``.ea/store/event.jsonl``,
   ``.ea/store/audit.jsonl``, ``.secrets.baseline``, and per-wave
   spec files under ``.ea/specs/``). Touching anything else is
   rejected. The primary trigger is the conventional-commit
   ``type``, not the subject-prefix ``-CORE`` suffix — ``type`` is
   the semantic signal, ``-CORE`` is the legacy carrier.

   Bare ``[P##(-I##)?] docs:`` commits are similarly path-gated:
   they MUST touch only ``.ea/artifacts/**`` (promoted documentation
   artifacts). Wave-form ``[P##-W##] docs:`` commits are unrestricted.

3. A recognized Claude or Codex ``Co-Authored-By`` trailer MUST be
   present (the ``prepare-commit-msg`` stage hook auto-inserts it when
   the active harness is detected; this backstop rejects commits where
   the trailer was hand-deleted).

All checks run as a ``commit-msg``-stage pre-commit hook. The first
argument is the commit-message file path (pre-commit passes it). The
linter consults ``git diff --cached --name-only`` for staged paths.

Exit codes:
- ``0`` — accepted.
- ``1`` — rejected (message printed to stderr).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from coauthor_policy import (
    SUPPORTED_TRAILERS,
    coauthor_disabled,
    has_any_coauthor_trailer,
    has_supported_trailer,
)

_TYPES = "feat|fix|chore|docs|refactor|test|build|perf|ci|revert|state"

# Subject grammar — two accepted forms:
#
# 1. Wave/CORE form: ``[P##(-I##)?(-W##|-CORE)] <type>: ...`` — the
#    pre-P26-W23 grammar; the ``-W##`` or ``-CORE`` suffix is
#    mandatory.
# 2. State-bookkeeping form: ``[P##(-I##)?] state: ...`` — post-
#    P26-W23 grammar; valid only when the conventional-commit type is
#    ``state``. The bare ``[P##]`` prefix is accepted because
#    ``type == 'state'`` is the canonical bookkeeping signal.
#
# The negative lookaheads ``(?!00)`` on both the iter and wave digit
# pairs reject ``I00`` / ``W00``: wave and iter indices are 1-based
# throughout the eawf state model, and reactive waves append the next
# available ``W##`` per the feedback-commit-prefix-taxonomy memory.
_SUBJECT_WAVE_OR_CORE_RE = re.compile(
    r"^\[P\d{2}(-I(?!00)\d{2})?(-W(?!00)\d{2}|-CORE)\]\s+"
    rf"(?P<type>{_TYPES}):\s+\S.*$"
)
_SUBJECT_BARE_RE = re.compile(
    r"^\[P\d{2}(-I(?!00)\d{2})?\]\s+"
    r"(?P<type>state|docs):\s+\S.*$"
)
_CORE_TAG_RE = re.compile(r"^\[P\d{2}(-I(?!00)\d{2})?-CORE\]\s+")
_STATE_ONLY_ALLOWED = (
    ".ea/state.json",
    ".ea/store/event.jsonl",
    # ``audit add`` writes one envelope line per audit into
    # ``.ea/store/audit.jsonl``; this lives in the state-bookkeeping
    # surface alongside ``event.jsonl``.
    ".ea/store/audit.jsonl",
    # ``.secrets.baseline`` auto-tracks state.json line numbers; the
    # detect-secrets pre-commit hook regenerates it whenever state.json
    # mutates, and refuses to commit when baseline is left unstaged.
    # State-bookkeeping commits therefore always need it riding along.
    ".secrets.baseline",
)
_STATE_ONLY_PREFIXES = (".ea/specs/",)

# Bare ``[P##(-I##)?] docs:`` commits carry phase/iter-scoped
# documentation artifacts that no single wave owns (closure audits,
# promoted research / decision / incident briefs). They are restricted to
# the promoted-artifact tree; wave-produced docs use the
# ``[P##-W##] docs:`` wave form, which accepts any path.
_DOCS_BARE_PREFIXES = (".ea/artifacts/",)


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


def _is_docs_bare_path(path: str) -> bool:
    return any(path.startswith(p) for p in _DOCS_BARE_PREFIXES)


def _check_scoped_paths(
    *, commit_type: str, subject: str, staged: list[str], is_bare: bool
) -> tuple[int, str] | None:
    """Enforce the per-scope path whitelist for state- and bare-docs commits.

    State-scoped commits (``type == 'state'`` or the legacy ``-CORE`` suffix,
    which stays a trigger for back-compat with D16) must touch only
    state-bookkeeping paths. Bare ``[P##(-I##)?] docs:`` commits must touch
    only ``.ea/artifacts/**``. Wave-form ``[P##-W##] docs:`` commits are
    unrestricted (hence the *is_bare* gate on the docs branch).

    Returns a ``(1, diagnostic)`` rejection when a scoped commit strays
    outside its whitelist, else ``None``.
    """
    if commit_type == "state" or _CORE_TAG_RE.match(subject):
        bad = [p for p in staged if not _is_state_only_path(p)]
        if bad:
            trigger = "state-type" if commit_type == "state" else "[P##-CORE]"
            return 1, (
                f"{trigger} commit touches non-state paths: {bad}\n"
                "state-scoped commits must mutate only .ea/state.json, "
                ".ea/store/event.jsonl, .ea/store/audit.jsonl, "
                ".secrets.baseline, or .ea/specs/**"
            )
    elif is_bare and commit_type == "docs":
        bad = [p for p in staged if not _is_docs_bare_path(p)]
        if bad:
            return 1, (
                f"bare [P##] docs: commit touches non-artifact paths: {bad}\n"
                "bare-prefix docs commits carry phase/iter-scoped artifacts "
                "under .ea/artifacts/** only; wave-produced docs use the "
                "[P##-W##] docs: wave form"
            )
    return None


def lint(
    message_path: Path,
    staged: list[str],
    env: Mapping[str, str] | None = None,
) -> tuple[int, str]:
    """Run both checks against *message_path* + *staged* paths.

    Returns ``(exit_code, diagnostic)``.
    """
    text = message_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    subject = ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        subject = stripped
        break
    if not subject:
        return 1, "empty commit subject"
    wave_match = _SUBJECT_WAVE_OR_CORE_RE.match(subject)
    bare_match = _SUBJECT_BARE_RE.match(subject)
    match = wave_match or bare_match
    if not match:
        return 1, (
            f"commit subject rejected: {subject!r}\n"
            "expected '[P##-W##] <type>: <summary>', "
            "'[P##-CORE] <type>: <summary>' (legacy bookkeeping alias), "
            "'[P##] state: <summary>' (canonical bookkeeping form), "
            "or '[P##] docs: <summary>' (phase/iter-scoped artifact docs) "
            "(W00 and I00 rejected — wave/iter indices are 1-based; "
            "type ∈ feat|fix|chore|docs|refactor|test|build|perf|ci|revert|state; "
            "bare [P##] accepted only for type=state or type=docs)"
        )
    commit_type = match.group("type")
    scoped = _check_scoped_paths(
        commit_type=commit_type,
        subject=subject,
        staged=staged,
        is_bare=bare_match is not None,
    )
    if scoped is not None:
        return scoped
    env_map = {} if env is None else env
    if coauthor_disabled(env_map):
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


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: commit_prefix_lint.py <commit-msg-path>", file=sys.stderr)
        return 1
    message_path = Path(argv[1])
    if not message_path.exists():
        print(f"commit message file missing: {message_path}", file=sys.stderr)
        return 1
    exit_code, diag = lint(message_path, _staged_paths(), env=os.environ)
    if exit_code != 0:
        print(diag, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
