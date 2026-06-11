"""Register lint for the phase-wide QA-gate register (QUAL-7).

The living scenario-to-gate acceptance artifact at
``docs/reference/qa-gate-register.md`` maps each subsystem scenario to
the gate kind that falsifies it. This module parses the register's
gate-kind column and resolves every cited kind against the live
registry, so the acceptance artifact cannot cite a phantom gate.

A cited kind *resolves* iff it is a registered audit-DSL ``CheckKind``
(the union :func:`~eawf.workflow.audit_dsl.registry.registered_audit_dsl_kinds`
returns -- ``CHECK_REGISTRY`` keys plus the state-scoring
``CLOSE_GATE_KINDS``) OR a known ``GateSpec`` kind (the production-bound
set :func:`~eawf.workflow.verify.readiness.wired_audit_dsl_kinds`
returns). A row naming a kind outside that union is a register defect.

The parser reads only the markdown table rows: a row is a
pipe-delimited line whose second cell is a single back-quoted token
(the gate-kind cell). Header and separator rows are skipped, and a
non-back-quoted second cell is ignored so prose tables elsewhere in the
document do not trip the lint.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

#: Matches a single back-quoted token and captures the inner text, e.g.
#: ``\`command_exit_zero\``` -> ``command_exit_zero``. Used to pull the
#: gate-kind out of the second table cell.
_BACKTICK_TOKEN = re.compile(r"^`([^`]+)`$")


class RegisterCitation(BaseModel):
    """One gate-kind citation parsed from a register table row.

    ``line_no`` is 1-based so a lint finding points at the offending
    source line directly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    line_no: int


class RegisterLintResult(BaseModel):
    """Outcome of linting one QA-gate register.

    ``ok`` is true iff every parsed citation resolved. ``unresolved``
    carries the offending citations (empty on success); ``citations``
    carries every parsed citation so a caller can assert the parser
    found the expected rows.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    citations: list[RegisterCitation]
    unresolved: list[RegisterCitation]


def known_gate_kinds() -> frozenset[str]:
    """Return every gate kind a register row may cite.

    The union of the registered audit-DSL kinds (file-set check kinds
    plus the state-scoring close-gate kinds) and the production-bound
    ``GateSpec`` kinds. A register citation resolves iff its kind is a
    member of this set.

    Returns:
        The frozenset of every resolvable gate-kind string.
    """
    # Local imports keep this module importable without eagerly pulling
    # the verify-spine and registry layers at import time, and avoid a
    # potential import cycle through the readiness module.
    from eawf.workflow.audit_dsl.registry import registered_audit_dsl_kinds
    from eawf.workflow.verify.readiness import wired_audit_dsl_kinds

    return registered_audit_dsl_kinds() | wired_audit_dsl_kinds()


def parse_register_citations(text: str) -> list[RegisterCitation]:
    """Parse the gate-kind citations out of a register markdown body.

    Reads pipe-delimited table rows whose second cell is a single
    back-quoted token (the gate-kind cell). Header rows (whose second
    cell is the literal ``Gate kind``) and separator rows (``|---|``)
    are skipped, as is any row whose second cell is not a lone
    back-quoted token, so prose tables elsewhere in the document do not
    register false citations.

    Args:
        text: The full markdown body of the register.

    Returns:
        The parsed citations in document order.
    """
    citations: list[RegisterCitation] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line.startswith("|"):
            continue
        # ``| a | b | c |`` -> ['a', 'b', 'c'] (drop the empty edge cells).
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        gate_cell = cells[1]
        match = _BACKTICK_TOKEN.match(gate_cell)
        if match is None:
            continue
        citations.append(RegisterCitation(kind=match.group(1), line_no=line_no))
    return citations


def lint_register(text: str) -> RegisterLintResult:
    """Lint a register body: every cited gate kind must resolve.

    Args:
        text: The full markdown body of the register.

    Returns:
        A :class:`RegisterLintResult`; ``ok`` is false when any parsed
        citation names a kind outside :func:`known_gate_kinds`.
    """
    known = known_gate_kinds()
    citations = parse_register_citations(text)
    unresolved = [c for c in citations if c.kind not in known]
    if unresolved:
        offenders = ", ".join(f"{c.kind!r}@L{c.line_no}" for c in unresolved)
        logger.warning(f"lint_register unresolved={len(unresolved)} kinds={offenders}")
    return RegisterLintResult(ok=not unresolved, citations=citations, unresolved=unresolved)


def lint_register_path(path: Path) -> RegisterLintResult:
    """Lint the register at *path*.

    Args:
        path: Repo-relative or absolute path to the register markdown.

    Returns:
        A :class:`RegisterLintResult` for the file's contents.

    Raises:
        FileNotFoundError: when *path* does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"register not found: {str(path)!r}")
    return lint_register(path.read_text(encoding="utf-8"))


__all__ = [
    "RegisterCitation",
    "RegisterLintResult",
    "known_gate_kinds",
    "lint_register",
    "lint_register_path",
    "parse_register_citations",
]
