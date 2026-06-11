"""Tests for the phase-wide QA-gate register lint (P30-I10-W07).

The register at ``docs/reference/qa-gate-register.md`` maps each
subsystem scenario to its firing gate kind. The lint
(:mod:`eawf.workflow.audit_dsl.register_lint`) parses the gate-kind
column and resolves every cited kind against the live registry, so the
acceptance artifact cannot cite a phantom gate.

Coverage:

* the real, committed register passes the lint (C1/C2 binding-proof);
* every cited kind resolves to a registered audit-DSL ``CheckKind`` or
  a known ``GateSpec`` kind (C2 resolver);
* a register row naming an unregistered kind REDS the lint (C2
  negative-path -- the load-bearing assertion);
* parser boundary cases: header / separator / prose rows are skipped;
* the path helper raises on a missing register (error path).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.workflow.audit_dsl.register_lint import (
    known_gate_kinds,
    lint_register,
    lint_register_path,
    parse_register_citations,
)

#: The four domains the register MUST populate per QUAL-7.
_REQUIRED_DOMAINS = ("Fleet", "Trust", "Cadence", "TUI")

_REGISTER_PATH = Path(__file__).resolve().parents[2] / "docs" / "reference" / "qa-gate-register.md"


def _register_text() -> str:
    return _REGISTER_PATH.read_text(encoding="utf-8")


def test_register_file_exists() -> None:
    assert _REGISTER_PATH.is_file(), f"register missing at {_REGISTER_PATH}"


def test_register_populates_required_domains() -> None:
    text = _register_text()
    for domain in _REQUIRED_DOMAINS:
        assert f"## {domain}" in text, f"register missing the {domain!r} domain section"


def test_real_register_passes_lint() -> None:
    result = lint_register(_register_text())
    assert result.ok, f"register cites phantom gates: {result.unresolved}"
    assert result.unresolved == []


def test_real_register_has_citations_per_domain() -> None:
    # Each of the four domains has at least one scenario row, so the
    # parser must find well more than four citations.
    citations = parse_register_citations(_register_text())
    assert len(citations) >= len(_REQUIRED_DOMAINS)


def test_every_cited_kind_resolves() -> None:
    known = known_gate_kinds()
    citations = parse_register_citations(_register_text())
    for citation in citations:
        assert citation.kind in known, f"{citation.kind!r}@L{citation.line_no} is phantom"


def test_phantom_kind_reds_the_lint() -> None:
    phantom = (
        "## Fleet\n"
        "\n"
        "| Scenario | Gate kind | Notes |\n"
        "|---|---|---|\n"
        "| A real scenario | `command_exit_zero` | binds. |\n"
        "| A phantom scenario | `definitely_not_a_real_kind` | should red. |\n"
    )
    result = lint_register(phantom)
    assert not result.ok
    assert len(result.unresolved) == 1
    assert result.unresolved[0].kind == "definitely_not_a_real_kind"
    # The resolving sibling row is parsed but not flagged.
    assert len(result.citations) == 2


def test_phantom_carries_line_number() -> None:
    phantom = "| Scenario | Gate kind | Notes |\n|---|---|---|\n| Bad | `phantom_gate` | x |\n"
    result = lint_register(phantom)
    assert result.unresolved[0].line_no == 3


def test_parser_skips_header_and_separator_rows() -> None:
    body = (
        "| Scenario | Gate kind | Notes |\n|---|---|---|\n| Only data row | `regex_in_file` | x |\n"
    )
    citations = parse_register_citations(body)
    assert len(citations) == 1
    assert citations[0].kind == "regex_in_file"


def test_parser_ignores_non_backtick_second_cell() -> None:
    # A prose table whose second cell is not a lone back-quoted token is
    # not a gate-kind citation and must not trip the lint.
    body = (
        "| Term | Meaning | Notes |\n"
        "|---|---|---|\n"
        "| Fleet | spawn substrate | prose, no gate cell. |\n"
    )
    assert parse_register_citations(body) == []
    assert lint_register(body).ok


def test_parser_skips_non_table_lines() -> None:
    body = "Just prose.\n\nNo tables here, only `command_exit_zero` inline.\n"
    assert parse_register_citations(body) == []


def test_empty_body_passes_vacuously() -> None:
    result = lint_register("")
    assert result.ok
    assert result.citations == []


def test_lint_register_path_reads_real_file() -> None:
    result = lint_register_path(_REGISTER_PATH)
    assert result.ok


def test_lint_register_path_missing_file_raises() -> None:
    missing = _REGISTER_PATH.parent / "does-not-exist-qa-gate-register.md"
    with pytest.raises(FileNotFoundError, match="register not found"):
        lint_register_path(missing)


def test_known_gate_kinds_covers_registered_and_gatespec_kinds() -> None:
    known = known_gate_kinds()
    # A registered audit-DSL CheckKind.
    assert "command_exit_zero" in known
    # The state-scoring close-gate kind.
    assert "backlog_resolution" in known
    # A GateSpec / supplemental kind.
    assert "tui_flow" in known
    # And nothing fabricated leaks in.
    assert "definitely_not_a_real_kind" not in known
