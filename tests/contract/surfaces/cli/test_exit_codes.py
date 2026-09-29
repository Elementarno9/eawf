"""SURF-085: exit codes are typed, stable, and defined in one table.

A harness branches on the exit status without parsing prose, so each
outcome needs its own code and each code needs one definition. The cases
below are one per code on the canonical surface, each driving a real
native verb through a stand-in daemon to the outcome that code stands for.
Beside them sit the structural guards: the table names every code once,
the attach-failure code is ``4`` and is distinct from the needs-operator
code and from a usage error, the console's terminal entry states exit with
it, and no CLI module defines an exit value outside the table.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import pytest
from typer.testing import CliRunner

from eawf.runtime.daemon.epoch2_transaction import (
    MutationReceipt,
    TransactionRefusalCode,
    TransactionRefusedError,
)
from eawf.runtime.daemon.methods.domain_envelope import (
    ENVELOPE_SCHEMA_VERSION,
    DomainEnvelope,
    DomainError,
    DomainErrorCode,
    DomainStatus,
    accepted_envelope,
    refused_envelope,
)
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain as domain_cmd
from eawf.surfaces.tui import launch
from tests.contract.surfaces.cli.conftest import FakeDaemon

runner = CliRunner()

_URN = "eawf://WS-CANARY/PRJ-CANARY/REP-CANARY/milestone/MLS-0001"
_SURFACES = Path(__file__).resolve().parents[4] / "src" / "eawf" / "surfaces"

#: An assignment of an integer literal to an exit-named constant.
_EXIT_LITERAL = re.compile(r"^\s*_?[A-Z][A-Z_]*_EXIT(_[A-Z_]+)?\b[^=\n]*=\s*-?\d+\s*$", re.M)


def _accepted() -> dict[str, Any]:
    receipt = MutationReceipt(
        event_name=domain_cmd.MILESTONE_ACCEPT,
        entity_ref=_URN,
        revision_before=3,
        revision_after=4,
        canonical_sequence=12,
        event_id="evt-0001",
        idempotency_key="key-0001",
        occurred_at=datetime(2026, 9, 18, tzinfo=UTC),
        wal_record_id="wal-0001",
    )
    return accepted_envelope(receipt, operation=domain_cmd.MILESTONE_ACCEPT).model_dump(mode="json")


def _conflict() -> dict[str, Any]:
    refusal = TransactionRefusedError(
        code=TransactionRefusalCode.REVISION_CONFLICT,
        detail="the milestone moved on since revision 3",
        entity_ref=_URN,
        remediation="Re-read the milestone and retry at its current revision.",
        revision=5,
    )
    return refused_envelope(refusal, operation=domain_cmd.MILESTONE_ACCEPT).model_dump(mode="json")


def _needs_operator() -> dict[str, Any]:
    return DomainEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        status=DomainStatus.ERROR,
        operation=domain_cmd.MILESTONE_ACCEPT,
        revision_before=3,
        revision_after=3,
        errors=(
            DomainError(
                code=DomainErrorCode.PROTECTED_APPROVAL_REQUIRED,
                message="the acceptance needs a sealed approval",
                entity_ref=_URN,
                remediation="Seal the acceptance approval and retry with its receipt reference.",
            ),
        ),
    ).model_dump(mode="json")


def _answer(daemon: FakeDaemon, result: dict[str, Any]) -> None:
    daemon.result = result


def _raise(daemon: FakeDaemon, error: Exception) -> None:
    daemon.error = error


def _refuse_connect(daemon: FakeDaemon) -> None:
    daemon.connect_error = OSError("connection refused")


#: One case per code: how the stand-in daemon answers, and what revision
#: the command names. A revision of ``0`` is refused before the wire.
_CASES: dict[int, tuple[Any, str]] = {
    exit_codes.OK: (lambda d: _answer(d, _accepted()), "3"),
    exit_codes.USER_ERROR: (lambda d: _answer(d, _accepted()), "0"),
    exit_codes.VALIDATION_ERROR: (
        lambda d: _raise(
            d, DaemonRpcError(cli_errors.RPC_VALIDATION_FAILED, "validation_failed: bad", None)
        ),
        "3",
    ),
    exit_codes.STATE_CONFLICT: (lambda d: _answer(d, _conflict()), "3"),
    exit_codes.DAEMON_UNREACHABLE: (_refuse_connect, "3"),
    exit_codes.INTERNAL_ERROR: (lambda d: _answer(d, {"status": "ok"}), "3"),
    exit_codes.NEEDS_OPERATOR: (lambda d: _answer(d, _needs_operator()), "3"),
}


def test_surf_085_every_code_on_the_surface_has_a_case() -> None:
    """The cases below cover the surface exactly, so a new code needs a case."""
    assert set(_CASES) == set(exit_codes.SURFACE)


@pytest.mark.parametrize("code", exit_codes.SURFACE, ids=exit_codes.name_for)
def test_surf_085_exit_code_case(daemon: FakeDaemon, tmp_path: Path, code: int) -> None:
    """Each outcome exits with its own code through a real native verb."""
    arrange, revision = _CASES[code]
    arrange(daemon)
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "milestone",
            "accept",
            _URN,
            "--expected-revision",
            revision,
            "--idempotency-key",
            "key-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    assert result.exit_code == code, result.output


def test_surf_085_the_table_names_every_code_once() -> None:
    """Values and names are unique, and the surface is in value order."""
    assert list(exit_codes.SURFACE) == sorted(set(exit_codes.SURFACE))
    names = [exit_codes.name_for(code) for code in exit_codes.SURFACE]
    assert len(names) == len(set(names))


def test_surf_085_name_for_a_code_off_the_surface_raises() -> None:
    """Error path: a value the table does not define has no name."""
    with pytest.raises(KeyError):
        exit_codes.name_for(max(exit_codes.SURFACE) + 1)
    with pytest.raises(KeyError):
        exit_codes.name_for(-1)


def test_surf_085_attach_failure_is_four_and_distinct() -> None:
    """Attach failure is 4, apart from needs-operator and from a usage error."""
    assert exit_codes.ATTACH_FAILURE == 4
    assert exit_codes.ATTACH_FAILURE != exit_codes.NEEDS_OPERATOR
    assert click.UsageError("x").exit_code != exit_codes.ATTACH_FAILURE
    assert click.UsageError("x").exit_code != exit_codes.NEEDS_OPERATOR


def test_surf_085_console_terminal_entry_exits_with_the_attach_failure_code() -> None:
    """The console's terminal entry states exit with the table's value."""
    assert launch.TERMINAL_ENTRY_EXIT_CODE == exit_codes.ATTACH_FAILURE


def test_surf_085_no_surface_module_defines_an_exit_value_of_its_own() -> None:
    """An exit constant anywhere under the surfaces is bound to the table.

    A literal would be a second definition able to drift from the one
    harnesses read, which is how a validator came to exit ``4`` -- the
    attach-failure code -- for a schema error.
    """
    offenders = [
        f"{path.relative_to(_SURFACES)}: {match.group(0).strip()}"
        for path in sorted(_SURFACES.rglob("*.py"))
        if path.name != "exit_codes.py"
        for match in _EXIT_LITERAL.finditer(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_surf_085_literal_scan_catches_a_redefinition() -> None:
    """The scan fires on the shape it guards, so a clean result is meaningful."""
    assert _EXIT_LITERAL.search("TERMINAL_ENTRY_EXIT_CODE = 4\n")
    assert _EXIT_LITERAL.search("DECLINED_EXIT = 1\n")
    assert not _EXIT_LITERAL.search("DECLINED_EXIT = exit_codes.USER_ERROR\n")
    assert not _EXIT_LITERAL.search("DEFAULT_MAX_COMPLEXITY = 15\n")
