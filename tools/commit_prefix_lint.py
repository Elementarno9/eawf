"""Commit-msg + diff scope linter for eawf phase-bundled commits.

Enforces:

1. Subject line must match one of three prefix grammars:

   - **Wave/CORE form** (planned wave deliverable or legacy phase-
     bookkeeping alias):
     ``^\\[P\\d{2,}(-I\\d{2,})?(-W\\d{2,}|-CORE)\\]\\s+<type>:\\s+\\S.*$``
     where ``<type>`` is one of ``feat|fix|chore|docs|refactor|test|
     build|perf|ci|revert|state``. The ``-W##`` or ``-CORE`` suffix
     declares whether the commit advances a planned wave or carries
     cross-wave bookkeeping (P19-W05).

   - **Bare phase/iter form** (post-P26-W23): a bare
     ``[P##(-I##)?]`` prefix with ``type`` ∈ {``state``, ``docs``}:
     ``^\\[P\\d{2,}(-I\\d{2,})?\\]\\s+(state|docs):\\s+\\S.*$``
     ``state`` is the canonical signal for phase/iter-scope
     bookkeeping; ``docs`` carries phase/iter-scoped documentation
     artifacts that no single wave owns (closure audits, promoted
     research / decision / incident briefs). The ``-CORE`` suffix is
     retained as a legacy alias on the wave/CORE form so prior commits
     still validate, but new bookkeeping commits MAY drop the suffix —
     the conventional-commit ``type`` IS the semantic signal.

   - **Bare conventional-commits form** (out-of-phase): a bare
     ``<type>: <subject>`` with no bracket prefix:
     ``^<type>:\\s+\\S.*$``. Accepted ONLY when ``state.current.phase_id``
     is ``None`` (no ACTIVE phase). Rejected when an ACTIVE phase
     exists — those commits MUST carry the bracketed wave/iter/phase
     prefix so the lifecycle bookkeeping stays attributable.

   ``W00`` and ``I00`` are rejected in the bracketed forms: wave /
   iter indices are 1-based by convention, and reactive waves get the
   next available ``W##`` per the feedback-commit-prefix-taxonomy
   memory. Phase / iter / wave id width widened to ``\\d{2,}`` so
   3-digit ids (P100, I100, W100) are accepted once the queue grows
   that far.

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

import json
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

# Subject grammar — three accepted forms:
#
# 1. Wave/CORE form: ``[P##(-I##)?(-W##|-CORE)] <type>: ...`` — the
#    pre-P26-W23 grammar; the ``-W##`` or ``-CORE`` suffix is
#    mandatory.
# 2. State-bookkeeping form: ``[P##(-I##)?] <state|docs>: ...`` —
#    post-P26-W23 grammar; valid only when the conventional-commit
#    type is ``state`` (any path on the state whitelist) or ``docs``
#    (restricted to ``.ea/artifacts/**``).
# 3. Bare conventional-commits form: ``<type>: <subject>`` with no
#    bracket prefix — accepted ONLY when ``state.current.phase_id`` is
#    ``None`` (no ACTIVE phase). Rejected when a phase is ACTIVE so
#    lifecycle bookkeeping stays attributable.
#
# The negative lookaheads ``(?!00)`` on both the iter and wave digit
# pairs reject ``I00`` / ``W00``: wave and iter indices are 1-based
# throughout the eawf state model, and reactive waves append the next
# available ``W##`` per the feedback-commit-prefix-taxonomy memory.
# The digit-width is ``\d{2,}`` (not ``\d{2}``) so 3+ digit ids are
# accepted once the queue grows past P/I/W 99.
_SUBJECT_WAVE_OR_CORE_RE = re.compile(
    r"^\[P\d{2,}(-I(?!00)\d{2,})?(-W(?!00)\d{2,}|-CORE)\]\s+"
    rf"(?P<type>{_TYPES}):\s+\S.*$"
)
_SUBJECT_BARE_RE = re.compile(
    r"^\[P\d{2,}(-I(?!00)\d{2,})?\]\s+"
    r"(?P<type>state|docs):\s+\S.*$"
)
_SUBJECT_BARE_CONVENTIONAL_RE = re.compile(rf"^(?P<type>{_TYPES}):\s+\S.*$")
_CORE_TAG_RE = re.compile(r"^\[P\d{2,}(-I(?!00)\d{2,})?-CORE\]\s+")
_WAVE_TRAILER_NAME = "Eawf-Wave"
_WAVE_TRAILER_RE = re.compile(
    rf"^{_WAVE_TRAILER_NAME}:\s+"
    r"(?P<wave>P\d{2,}(?:-I(?!00)\d{2,})?-W(?!00)\d{2,})\s*$",
    re.MULTILINE,
)
_RELEASE_ANNOTATION_RE = re.compile(r"\(release=v(?P<version>\d+\.\d+\.\d+(?:a\d+|b\d+|rc\d+)?)\)")
# Fires on ANY ``release=`` substring, not just the standalone
# ``(release=`` paren group. The .github/workflows/phase-release.yaml
# extraction regex only tags when the annotation is its own paren group,
# so a fused shape like ``(audit=A-x, release=v0.6.0)`` — a one-character
# malformation — would silently zero the tag + PyPI + npm publish. Detecting
# the broader signal lets the lint reject that shape before it lands.
_RELEASE_ANNOTATION_SIGNAL_RE = re.compile(r"release=")
# Byte-for-byte copy of the phase-release.yaml:46 extraction regex, kept as a
# module constant so the reject diagnostic and the workflow stay in lockstep.
_WORKFLOW_RELEASE_EXTRACTION_RE = r"\(release=(v\d+\.\d+\.\d+(?:a\d+|b\d+|rc\d+)?)\)"
_SUBJECT_STYLE_BRACKET = "bracket"
_SUBJECT_STYLE_TRAILER = "trailer"
_STATE_ONLY_ALLOWED = (
    ".ea/state.json",
    # ``.secrets.baseline`` auto-tracks state.json line numbers; the
    # detect-secrets pre-commit hook regenerates it whenever state.json
    # mutates, and refuses to commit when baseline is left unstaged.
    # State-bookkeeping commits therefore always need it riding along.
    ".secrets.baseline",
)
# Every per-kind JSONL under ``.ea/store/`` is a daemon-written, committed
# store (event / audit / evidence / decision / flow / role reports / memory)
# per the authority map's uniform treatment of ``.ea/store/*.jsonl``. They
# all ride the state-bookkeeping surface: e.g. the deterministic close gate
# appends ``evidence.jsonl`` rows as a wave closes, so a state commit carries
# them alongside ``state.json`` + ``event.jsonl``. ``.ea/specs/`` carries the
# in-band wave spec bodies authored as state.
_STATE_ONLY_PREFIXES = (".ea/store/", ".ea/specs/")

# Bare ``[P##(-I##)?] docs:`` commits carry phase/iter-scoped
# documentation artifacts that no single wave owns (closure audits,
# promoted research / decision / incident briefs). They are restricted to
# the promoted-artifact tree; wave-produced docs use the
# ``[P##-W##] docs:`` wave form, which accepts any path.
_DOCS_BARE_PREFIXES = (".ea/artifacts/",)


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
    ``bracket``.
    """
    root = _find_repo_root(repo_root)
    style = _SUBJECT_STYLE_BRACKET
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


def _has_wave_trailer(text: str) -> bool:
    return _WAVE_TRAILER_RE.search(text) is not None


def _current_phase_active(state_path: Path | None = None) -> bool:
    """Return True when ``state.current.phase_id`` is non-null.

    Walks upward from cwd to find ``.ea/state.json`` when *state_path*
    is omitted. Missing file, unreadable JSON, or null ``phase_id``
    all read as "no ACTIVE phase" (returns ``False``), which is the
    safe default — it lets the pre-flight chore commit subject parse
    in fresh checkouts and in environments where the lint runs
    outside a state-resident project.
    """
    if state_path is None:
        cwd = Path.cwd()
        for parent in [cwd, *cwd.parents]:
            candidate = parent / ".ea" / "state.json"
            if candidate.is_file():
                state_path = candidate
                break
    if state_path is None or not state_path.is_file():
        return False
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return False
    current = data.get("current")
    if not isinstance(current, dict):
        return False
    return current.get("phase_id") is not None


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
                ".ea/store/**, .secrets.baseline, or .ea/specs/**"
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


def _extract_subject(text: str) -> str:
    """Return the first non-blank, non-comment line in *text*."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def _match_subject(
    subject: str,
    state_path: Path | None,
    *,
    subject_style: str,
    has_wave_trailer: bool,
) -> tuple[re.Match[str] | None, bool, str]:
    """Return (match, is_bare_state_or_docs, error_diag).

    Tries the three accepted forms in order. The bare conventional-commits
    form is rejected when an ACTIVE phase exists; in that case the returned
    match is ``None`` and *error_diag* carries the rejection text. The
    ``is_bare_state_or_docs`` flag tells callers whether the matched form
    is the bracketed bare ``[P##(-I##)?] state|docs:`` form (which still
    needs path-whitelist enforcement).
    """
    wave_match = _SUBJECT_WAVE_OR_CORE_RE.match(subject)
    bare_match = _SUBJECT_BARE_RE.match(subject)
    bracketed = wave_match or bare_match
    if bracketed is not None:
        return bracketed, bare_match is not None, ""
    bare_conventional = _SUBJECT_BARE_CONVENTIONAL_RE.match(subject)
    if bare_conventional is not None:
        if subject_style == _SUBJECT_STYLE_TRAILER:
            if has_wave_trailer:
                return bare_conventional, False, ""
            return (
                None,
                False,
                (
                    f"trailer-style commit missing {_WAVE_TRAILER_NAME} trailer: {subject!r}\n"
                    f"set '{_WAVE_TRAILER_NAME}: P##-I##-W##' in the commit body, "
                    "or switch vcs.conventions.subject_style back to 'bracket'"
                ),
            )
        if _current_phase_active(state_path):
            return (
                None,
                False,
                (
                    f"bare conventional-commits subject rejected: {subject!r}\n"
                    "an ACTIVE phase exists (state.current.phase_id is set); "
                    "commits MUST carry a bracketed [P##-W##] / [P##-I##-W##] / "
                    "[P##] / [P##-I##] prefix so lifecycle bookkeeping stays "
                    "attributable. Bare '<type>: <subject>' is reserved for "
                    "out-of-phase commits (state.current.phase_id is None)."
                ),
            )
        return bare_conventional, False, ""
    return (
        None,
        False,
        (
            f"commit subject rejected: {subject!r}\n"
            "expected '[P##-W##] <type>: <summary>', "
            "'[P##-CORE] <type>: <summary>' (legacy bookkeeping alias), "
            "'[P##] state: <summary>' (canonical bookkeeping form), "
            "'[P##] docs: <summary>' (phase/iter-scoped artifact docs), "
            "or '<type>: <summary>' (bare conventional-commits, only when "
            "no ACTIVE phase is set in state.json) "
            "(W00 and I00 rejected — wave/iter indices are 1-based; "
            "type ∈ feat|fix|chore|docs|refactor|test|build|perf|ci|revert|state; "
            "bare [P##] accepted only for type=state or type=docs)"
        ),
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


def _check_release_annotation(subject: str) -> tuple[int, str] | None:
    """Validate an optional ``(release=vX.Y.Z)`` subject annotation.

    Fires on ANY ``release=`` substring, then accepts only when the
    annotation is shaped as its own paren group
    ``(release=v<MAJOR>.<MINOR>.<PATCH>[aN|bN|rcN])``. A fused shape such as
    ``(audit=A-x, release=v0.6.0)`` carries a ``release=`` signal but does not
    match the standalone group, so it is a hard reject: the
    .github/workflows/phase-release.yaml:46 extraction regex would fail to
    match it and silently skip the tag + PyPI + npm publish.

    Returns ``None`` when no ``release=`` signal is present or the annotation
    is well-formed; otherwise a ``(1, diagnostic)`` rejection naming the
    workflow regex.
    """
    if not _RELEASE_ANNOTATION_SIGNAL_RE.search(subject):
        return None
    if _RELEASE_ANNOTATION_RE.search(subject):
        return None
    return (
        1,
        (
            f"release annotation rejected: {subject!r}\n"
            "a 'release=' signal is present but not shaped as its own paren group "
            "'(release=v<MAJOR>.<MINOR>.<PATCH>[aN|bN|rcN])'. The "
            ".github/workflows/phase-release.yaml:46 extraction regex "
            f"'{_WORKFLOW_RELEASE_EXTRACTION_RE}' matches only the standalone group, "
            "so a fused '(audit=..., release=v...)' shape would silently skip the "
            "tag + PyPI + npm publish"
        ),
    )


def lint(
    message_path: Path,
    staged: list[str],
    env: Mapping[str, str] | None = None,
    state_path: Path | None = None,
    repo_root: Path | None = None,
    subject_style: str | None = None,
) -> tuple[int, str]:
    """Run both checks against *message_path* + *staged* paths.

    Returns ``(exit_code, diagnostic)``. *state_path* lets tests
    inject a fixture ``state.json``; production callers leave it
    unset and the helper walks upward from cwd to find ``.ea/state.json``.
    """
    text = message_path.read_text(encoding="utf-8")
    subject = _extract_subject(text)
    if not subject:
        return 1, "empty commit subject"
    configured_style = subject_style or _configured_subject_style(repo_root)
    match, is_bare_bracketed, err = _match_subject(
        subject,
        state_path,
        subject_style=configured_style,
        has_wave_trailer=_has_wave_trailer(text),
    )
    if match is None:
        return 1, err
    release_annotation = _check_release_annotation(subject)
    if release_annotation is not None:
        return release_annotation
    # Bare conventional-commits (no bracket prefix) has no path whitelist;
    # bracketed forms (wave/CORE + bare state/docs) route through the
    # scoped-path check, which internally gates on commit_type / CORE tag
    # / is_bare to apply the right whitelist.
    if subject.startswith("["):
        scoped = _check_scoped_paths(
            commit_type=match.group("type"),
            subject=subject,
            staged=staged,
            is_bare=is_bare_bracketed,
        )
        if scoped is not None:
            return scoped
    return _check_coauthor(text, {} if env is None else env)


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
