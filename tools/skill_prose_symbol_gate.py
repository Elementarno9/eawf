"""Prose-vs-symbol drift oracle for the frozen skill registry.

The per-skill body strings in
:mod:`eawf.surfaces.render.skills.registry` document each skill's
canonical algorithm in prose, and that prose names real code symbols and
real repo paths: a dotted ``module.attr`` reference like
``kernel/spec/intent.IntentBrief``, a bare module path like
``platform/lint/eawf022_propose_coverage``, or a source path like
``src/eawf/``. When a skill body is reskinned (or a referenced symbol is
renamed / moved) the prose can drift out of sync with the tree -- it
keeps naming a symbol that no longer imports or a path that no longer
exists. A reader trusts the prose; a drifted reference quietly lies.

This gate is the deterministic oracle that keeps the prose honest. It
parses every skill body, extracts the references it names, and asserts
each resolves -- import-resolution (:mod:`importlib`) for a code symbol,
filesystem existence for a source path. A non-resolving reference fails
the gate, naming the offending token plus the skill whose prose carries
it.

The matcher is deliberately CONSERVATIVE so it never false-positives on
ordinary prose:

- A slash-form reference (``kernel/spec/intent.IntentBrief``) is taken
  only when its first segment is a real top-level ``eawf`` subpackage
  (``kernel`` / ``platform`` / ``workflow`` / ``observability`` /
  ``runtime`` / ``surfaces``). A slash-joined prose phrase like
  ``approve/edit/cancel`` starts with ``approve`` (not a package) and is
  skipped.
- A dotted reference (``eawf.surfaces.render.skills.render`` or
  ``kernel.spec.research.IntentBrief``) is taken only when its first
  dotted segment is ``eawf`` or a real top-level subpackage AND it has at
  least two dotted segments. A bare prose noun (``IntentBrief``,
  ``CriterionSpec``), an option flag (``--depth``), a single word
  (``gate``), or a runtime attribute access on a non-package root
  (``header.skill``) is skipped.
- A path reference is taken only for a placeholder-free token rooted at
  the committed ``src/eawf`` tree. Gitignored / generated / templated
  trees (the ``build/`` plugin tree materialised by ``eawf plugin
  install``, the ``.ea/local`` draft scope, the per-worktree-absent
  ``.ea/profile.yaml``) are NOT reliable existence targets, so a path
  outside ``src/eawf`` is skipped to keep the gate false-positive-free.

The references are pulled from the live :data:`SKILL_REGISTRY` so the
gate always reflects the shipped prose; :func:`scan_body` exposes the
same matcher over an arbitrary ``(skill_name, body)`` pair so a test can
inject a fixture block without editing shipped prose.

Invocation:

    uv run python tools/skill_prose_symbol_gate.py

Exit codes:
- ``0`` -- every named symbol / path in every skill body resolves.
- ``1`` -- at least one reference does not resolve (each finding names
  the offending token + the skill whose prose carries it, on stderr).
"""

from __future__ import annotations

import importlib
import pkgutil
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Protocol

from eawf.surfaces.render.skills.registry import SKILL_REGISTRY


class _SkillBody(Protocol):
    """The slice of ``SkillSpec`` the gate reads: a name and its prose body.

    Typed as a :class:`~typing.Protocol` so :func:`scan_registry` accepts
    both the shipped :data:`SKILL_REGISTRY` specs and a test's lightweight
    stand-in without importing the concrete ``SkillSpec``.
    """

    @property
    def skill_name(self) -> str: ...

    @property
    def body(self) -> str: ...


#: Repo root, derived once from this file's location (``tools/`` is a
#: sibling of ``src/``). Path-existence checks resolve relative to this
#: root so the gate works from any cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: The import root every reference is rooted at.
_TOP_PACKAGE = "eawf"

#: A backtick-fenced token in skill-body prose. The body strings use
#: single-backtick spans for every code symbol and path, so a token is the
#: run of characters between two backticks. Matched non-greedily so
#: adjacent spans on one line stay separate.
_BACKTICK_TOKEN_RE = re.compile(r"`([^`\n]+)`")

#: A slash-form module reference with an optional dotted attribute tail:
#: ``seg/seg/seg`` or ``seg/seg/seg.Attr``. The whole token must be the
#: span (anchored) so a slash-joined prose phrase embedded in a longer
#: span is not mis-parsed.
_SLASH_REF_RE = re.compile(
    r"^(?P<mod>[a-z_][a-z0-9_]*(?:/[a-z_][a-z0-9_]*)+)"
    r"(?:\.(?P<attr>[A-Za-z_][A-Za-z0-9_]*))?$"
)

#: A dotted module reference with an optional attribute tail:
#: ``a.b.c`` or ``a.b.c.Attr``. Requires at least two dotted segments so a
#: bare noun (``IntentBrief``) never matches. The leading segment is
#: validated separately against the package-root set.
_DOTTED_REF_RE = re.compile(r"^(?P<dotted>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)$")

#: The committed source-tree root a path reference is checked against.
#: Gitignored / generated trees (``build/``, ``.ea/local``) and the
#: per-worktree-absent ``.ea/profile.yaml`` are intentionally excluded
#: (see module docstring).
_SOURCE_PATH_ROOT = "src/eawf"


class ReferenceKind:
    """The reference families :func:`_classify_token` can emit.

    String constants rather than an enum so a finding's ``kind`` reads
    plainly in the rendered message without an enum import at the call
    site.
    """

    SYMBOL = "symbol"
    PATH = "path"


@dataclass(frozen=True, slots=True)
class SymbolFinding:
    """One non-resolving reference named in a skill body.

    Attributes:
        skill_name: The skill whose prose carries the offending token.
        token: The verbatim reference token (slash / dotted / path form).
        kind: Whether the token was checked as a code ``symbol`` or a
            filesystem ``path``.
        reason: A human-readable explanation of why it did not resolve.
    """

    skill_name: str
    token: str
    kind: str
    reason: str


@cache
def _eawf_package_roots() -> frozenset[str]:
    """Return the real top-level ``eawf`` subpackage names.

    The set is read from the installed ``eawf`` package via
    :func:`pkgutil.iter_modules` so it tracks the tree rather than a
    hardcoded list. It is the anchor that keeps the slash / dotted
    matchers conservative: a reference whose first segment is not in this
    set is prose, not a module path.

    Returns:
        The frozenset of importable top-level subpackage / submodule names
        under ``eawf`` (e.g. ``{"kernel", "platform", "workflow", ...}``).
    """
    package = importlib.import_module(_TOP_PACKAGE)
    return frozenset(module.name for module in pkgutil.iter_modules(package.__path__))


def _resolve_symbol(module_path: str, attr: str | None) -> str | None:
    """Return a failure reason when *module_path*[.*attr*] does not resolve.

    Args:
        module_path: A dotted module path rooted at ``eawf`` (e.g.
            ``eawf.kernel.spec.intent``).
        attr: An optional attribute name to look up on the imported
            module; ``None`` checks the module import alone.

    Returns:
        ``None`` when the module imports and (when given) the attribute
        exists; otherwise a lowercase-leading reason naming what failed.
    """
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError:
        return f"module does not import: {module_path!r}"
    except ImportError as exc:
        return f"module import error for {module_path!r}: {exc}"
    if attr is not None and not hasattr(module, attr):
        return f"module {module_path!r} has no attribute {attr!r}"
    return None


def _slash_ref_finding(skill_name: str, token: str, match: re.Match[str]) -> SymbolFinding | None:
    """Resolve a slash-form module reference, returning a finding on failure.

    Args:
        skill_name: The skill whose prose carries *token*.
        token: The verbatim slash-form reference.
        match: The :data:`_SLASH_REF_RE` match over *token*.

    Returns:
        ``None`` when the reference resolves or its root is not an ``eawf``
        package (prose); otherwise a :class:`SymbolFinding`.
    """
    segments = match.group("mod").split("/")
    if segments[0] not in _eawf_package_roots():
        return None
    module_path = ".".join([_TOP_PACKAGE, *segments])
    # A ``.py`` tail names the module *file* (``kernel/spec/math.py``), not
    # an attribute -- resolve the module import alone, dropping the suffix.
    attr = match.group("attr")
    if attr == "py":
        attr = None
    reason = _resolve_symbol(module_path, attr)
    if reason is None:
        return None
    return SymbolFinding(
        skill_name=skill_name,
        token=token,
        kind=ReferenceKind.SYMBOL,
        reason=reason,
    )


def _dotted_ref_finding(skill_name: str, token: str, match: re.Match[str]) -> SymbolFinding | None:
    """Resolve a dotted module reference, returning a finding on failure.

    The leading segment must be ``eawf`` or a real top-level subpackage; a
    dotted access on any other root (``header.skill``,
    ``research.default_depth``) is runtime / config prose, not a module
    path, and is skipped.

    The whole token is first tried as a module import; if that resolves it
    is a pure module reference (e.g.
    ``eawf.surfaces.render.skills.render``). Otherwise the final segment is
    looked up as an attribute on its parent module (e.g.
    ``eawf.kernel.spec.intent.IntentBrief`` -> ``IntentBrief`` on
    ``eawf.kernel.spec.intent``).

    Args:
        skill_name: The skill whose prose carries *token*.
        token: The verbatim dotted reference.
        match: The :data:`_DOTTED_REF_RE` match over *token*.

    Returns:
        ``None`` when the reference resolves or is prose to skip;
        otherwise a :class:`SymbolFinding`.
    """
    segments = match.group("dotted").split(".")
    head = segments[0]
    if head == _TOP_PACKAGE:
        rooted = segments
    elif head in _eawf_package_roots():
        rooted = [_TOP_PACKAGE, *segments]
    else:
        return None

    full_module = ".".join(rooted)
    if _resolve_symbol(full_module, attr=None) is None:
        return None
    parent_module = ".".join(rooted[:-1])
    reason = _resolve_symbol(parent_module, attr=rooted[-1])
    if reason is None:
        return None
    return SymbolFinding(
        skill_name=skill_name,
        token=token,
        kind=ReferenceKind.SYMBOL,
        reason=reason,
    )


def _path_finding(skill_name: str, token: str) -> SymbolFinding | None:
    """Resolve a committed source-tree path reference, on failure a finding.

    Only placeholder-free tokens (no ``<...>`` span) rooted at
    :data:`_SOURCE_PATH_ROOT` are checked; everything else returns ``None``
    (skip). A trailing slash is tolerated. The check is pure filesystem
    existence relative to the repo root.

    Args:
        skill_name: The skill whose prose carries *token*.
        token: The verbatim path reference.

    Returns:
        ``None`` when the path exists or the token is not a checkable
        source path; otherwise a :class:`SymbolFinding`.
    """
    if "<" in token or ">" in token:
        return None
    if not token.startswith(_SOURCE_PATH_ROOT):
        return None
    relative = token.rstrip("/")
    if (_REPO_ROOT / relative).exists():
        return None
    return SymbolFinding(
        skill_name=skill_name,
        token=token,
        kind=ReferenceKind.PATH,
        reason=f"path does not exist under the repo root: {token!r}",
    )


def _classify_token(skill_name: str, token: str) -> SymbolFinding | None:
    """Classify one backtick token and resolve it, on failure a finding.

    The classification order is slash-form module, then dotted module,
    then source path. A token matching none of the conservative shapes is
    prose and yields ``None``.

    Args:
        skill_name: The skill whose prose carries *token*.
        token: The verbatim token text (backticks already stripped).

    Returns:
        ``None`` when the token is prose or resolves cleanly; otherwise a
        :class:`SymbolFinding` naming the drift.
    """
    slash_match = _SLASH_REF_RE.match(token)
    if slash_match is not None:
        return _slash_ref_finding(skill_name, token, slash_match)
    dotted_match = _DOTTED_REF_RE.match(token)
    if dotted_match is not None:
        return _dotted_ref_finding(skill_name, token, dotted_match)
    return _path_finding(skill_name, token)


def scan_body(skill_name: str, body: str) -> list[SymbolFinding]:
    """Return every non-resolving reference in one skill body's prose.

    The body is scanned for backtick-fenced tokens; each is classified and
    resolved by :func:`_classify_token`. Findings preserve first-seen
    order, de-duplicated on the token text so one repeated drifted symbol
    is reported once per body.

    Args:
        skill_name: The skill the body belongs to (carried onto each
            finding so the message names the offending skill).
        body: The skill-body markdown prose.

    Returns:
        One :class:`SymbolFinding` per distinct non-resolving reference,
        in first-seen order; empty when every reference resolves.
    """
    findings: list[SymbolFinding] = []
    seen: set[str] = set()
    for token_match in _BACKTICK_TOKEN_RE.finditer(body):
        token = token_match.group(1).strip()
        if not token or token in seen:
            continue
        finding = _classify_token(skill_name, token)
        if finding is not None:
            seen.add(token)
            findings.append(finding)
    return findings


def scan_registry(
    registry: Sequence[_SkillBody] | None = None,
) -> list[SymbolFinding]:
    """Return every non-resolving reference across all shipped skill bodies.

    Walks the live :data:`SKILL_REGISTRY` (or an injected *registry*) and
    runs :func:`scan_body` over each spec's ``skill_name`` + ``body``.

    Args:
        registry: A sequence of skill specs exposing ``skill_name`` and
            ``body`` attributes. ``None`` uses the shipped
            :data:`SKILL_REGISTRY`. Injected by tests that build a
            synthetic spec list.

    Returns:
        The concatenation of per-skill findings, in registry order; empty
        when every skill body's prose resolves.
    """
    specs: Sequence[_SkillBody] = SKILL_REGISTRY if registry is None else registry
    findings: list[SymbolFinding] = []
    for spec in specs:
        findings.extend(scan_body(spec.skill_name, spec.body))
    return findings


def _render_findings(findings: Iterable[SymbolFinding]) -> str:
    """Render *findings* as a human-readable multi-line failure message.

    Args:
        findings: The non-resolving references to describe.

    Returns:
        A header line plus one line per finding, each naming the token,
        the carrying skill, and the resolution failure.
    """
    materialized = list(findings)
    lines = [
        f"skill-prose-symbol-gate: {len(materialized)} prose reference(s) do not resolve:",
    ]
    for finding in materialized:
        lines.append(
            f"  - {finding.token!r} in skill {finding.skill_name!r} "
            f"({finding.kind}): {finding.reason}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str]) -> int:
    """Run the prose-vs-symbol gate over the live skill registry.

    Args:
        argv: Process argv (unused beyond ``argv[0]``; the gate takes no
            arguments so the pre-commit hook needs none).

    Returns:
        ``0`` when every reference resolves; ``1`` when any does not (the
        findings are printed to stderr).
    """
    del argv  # the gate scans the live registry; no arguments are consumed
    findings = scan_registry()
    if findings:
        print(_render_findings(findings), file=sys.stderr)
        return 1
    print("skill-prose-symbol-gate: ok (every prose symbol / path reference resolves)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
