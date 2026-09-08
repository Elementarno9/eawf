"""The obsolescence sweep, bound to the allowed-legacy-symbol allowlist.

The deleted vocabulary comes from the shared allowlist file, so the
sweep and the operational-surface census cannot drift apart. A rename is
not a deletion, and an identifier mention alone never obsoletes a row.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.migration.epoch2.allowlist import (
    LegacySymbolAllowlist,
    load_legacy_symbol_allowlist,
    parse_legacy_symbol_allowlist,
)
from eawf.kernel.migration.epoch2.backlog import (
    deleted_terms_of,
    obsolescence_rule,
    sweep_backlog_obsolescence,
)
from eawf.kernel.migration.epoch2.corpus import Epoch1BacklogCorpus

# Measured over the pinned corpus at revision ae04a5c1.
EXPECTED_ROWS_SCANNED = 131
EXPECTED_OBSOLETE_IDS = (
    "B077",
    "B078",
    "B079",
    "B082",
    "B083",
    "B086",
    "B101",
    "B106",
    "B118",
    "B120",
    "B122",
)
EXPECTED_OPEN_OBSOLETE_IDS = ("B118", "B120", "B122")


def test_load_legacy_symbol_allowlist_reads_the_three_directive_groups(
    allowlist: LegacySymbolAllowlist,
) -> None:
    assert allowlist.allowed_surfaces == ("migrate", "audit", "export", "recover")
    assert ("wave", "Task") in allowlist.renames
    assert "close attempt" in allowlist.deleted_terms


def test_load_legacy_symbol_allowlist_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_legacy_symbol_allowlist(tmp_path / "absent.txt")


def test_parse_legacy_symbol_allowlist_empty_text_yields_empty_groups() -> None:
    parsed = parse_legacy_symbol_allowlist("")
    assert parsed.allowed_surfaces == ()
    assert parsed.renames == ()
    assert parsed.deleted_terms == ()


def test_parse_legacy_symbol_allowlist_comments_and_blanks_are_skipped() -> None:
    parsed = parse_legacy_symbol_allowlist("# a comment\n\n   \ndelete: bwrap\n")
    assert parsed.deleted_terms == ("bwrap",)


def test_parse_legacy_symbol_allowlist_unknown_directive_raises() -> None:
    with pytest.raises(ValueError, match="unknown directive"):
        parse_legacy_symbol_allowlist("forbid: bwrap\n")


def test_parse_legacy_symbol_allowlist_missing_separator_raises() -> None:
    with pytest.raises(ValueError, match="expected '<directive>: <payload>'"):
        parse_legacy_symbol_allowlist("delete bwrap\n")


def test_parse_legacy_symbol_allowlist_empty_payload_raises() -> None:
    with pytest.raises(ValueError, match="empty payload"):
        parse_legacy_symbol_allowlist("delete:\n")


def test_parse_legacy_symbol_allowlist_rename_without_arrow_raises() -> None:
    with pytest.raises(ValueError, match="rename needs"):
        parse_legacy_symbol_allowlist("rename: wave\n")


def test_deleted_terms_of_rejects_an_identifier_shaped_term() -> None:
    """An id is provenance; putting one in the deleted set would obsolete on provenance."""
    parsed = parse_legacy_symbol_allowlist("delete: P30-I05-W07\n")
    with pytest.raises(ValueError, match="identifier-shaped"):
        deleted_terms_of(parsed)


def test_sweep_backlog_obsolescence_flags_a_deleted_term(
    allowlist: LegacySymbolAllowlist,
) -> None:
    rows = {"B001": {"title": "Retire the close gate", "status": "open"}}
    result = sweep_backlog_obsolescence(rows=rows, allowlist=allowlist)
    assert result.obsolete_ids == ("B001",)
    assert result.verdicts[0].matched_terms == ("close gate",)


def test_sweep_backlog_obsolescence_id_mention_alone_never_obsoletes(
    allowlist: LegacySymbolAllowlist,
) -> None:
    rows = {
        "B001": {
            "title": "Follow up on P30-I05-W07",
            "description": "Raised by A30 while closing B002.",
            "status": "open",
        }
    }
    result = sweep_backlog_obsolescence(rows=rows, allowlist=allowlist)
    assert result.obsolete_ids == ()
    assert result.verdicts[0].obsolete is False


def test_sweep_backlog_obsolescence_a_rename_is_not_a_deletion(
    allowlist: LegacySymbolAllowlist,
) -> None:
    """wave, iter, phase, backlog and agent session survive under new names."""
    rows = {
        "B001": {
            "title": "Rework the wave, iter and phase dispatch order",
            "description": "Every agent session on the backlog needs it.",
            "status": "open",
        }
    }
    result = sweep_backlog_obsolescence(rows=rows, allowlist=allowlist)
    assert result.obsolete_ids == ()


def test_sweep_backlog_obsolescence_reads_nested_intent_prose(
    allowlist: LegacySymbolAllowlist,
) -> None:
    rows = {"B001": {"intent": {"problem": "The egress proxy blocks the run."}, "status": "open"}}
    result = sweep_backlog_obsolescence(rows=rows, allowlist=allowlist)
    assert result.obsolete_ids == ("B001",)


def test_sweep_backlog_obsolescence_ignores_fields_outside_the_scan_set(
    allowlist: LegacySymbolAllowlist,
) -> None:
    rows = {"B001": {"notes": "bwrap", "status": "open"}}
    result = sweep_backlog_obsolescence(rows=rows, allowlist=allowlist)
    assert result.obsolete_ids == ()


def test_sweep_backlog_obsolescence_on_an_empty_backlog(
    allowlist: LegacySymbolAllowlist,
) -> None:
    result = sweep_backlog_obsolescence(rows={}, allowlist=allowlist)
    assert result.rows_scanned == 0
    assert result.obsolete_ids == ()
    assert result.verdicts == ()


def test_sweep_backlog_obsolescence_rejects_an_id_shaped_deleted_term() -> None:
    parsed = parse_legacy_symbol_allowlist("delete: B054\n")
    with pytest.raises(ValueError, match="identifier-shaped"):
        sweep_backlog_obsolescence(rows={}, allowlist=parsed)


def test_sweep_backlog_obsolescence_over_the_epoch1_full_corpus(
    epoch1_full: Epoch1BacklogCorpus, allowlist: LegacySymbolAllowlist
) -> None:
    result = sweep_backlog_obsolescence(rows=epoch1_full.backlog, allowlist=allowlist)
    assert result.rows_scanned == EXPECTED_ROWS_SCANNED
    assert result.obsolete_ids == EXPECTED_OBSOLETE_IDS
    open_obsolete = tuple(
        row_id
        for row_id in result.obsolete_ids
        if epoch1_full.backlog[row_id].get("status") == "open"
    )
    assert open_obsolete == EXPECTED_OPEN_OBSOLETE_IDS


def test_sweep_backlog_obsolescence_re_runs_byte_identically(
    epoch1_full: Epoch1BacklogCorpus, allowlist: LegacySymbolAllowlist
) -> None:
    """The sweep is a pure function of the rows and the allowlist."""
    first = sweep_backlog_obsolescence(rows=epoch1_full.backlog, allowlist=allowlist)
    shuffled = dict(reversed(list(epoch1_full.backlog.items())))
    second = sweep_backlog_obsolescence(rows=shuffled, allowlist=allowlist)
    assert first.digest() == second.digest()
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_obsolescence_rule_digest_tracks_the_allowlist(
    allowlist: LegacySymbolAllowlist,
) -> None:
    """Editing the shared allowlist moves the rule digest, so the two cannot drift."""
    baseline = obsolescence_rule(allowlist)
    widened = LegacySymbolAllowlist(
        allowed_surfaces=allowlist.allowed_surfaces,
        renames=allowlist.renames,
        deleted_terms=(*allowlist.deleted_terms, "statusline cache"),
    )
    assert baseline.rule_id == "DOM-018"
    assert obsolescence_rule(widened).rule_digest != baseline.rule_digest
