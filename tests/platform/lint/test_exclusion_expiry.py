"""Tests for the bounded-grant shape of the EAWF010 exclusion list.

Covers parsing (the bare-path refusal that rejects the configuration
rather than the commit, the required keys, unknown keys, both date
spellings, duplicate grants), the expiry boundary at exactly the expiry
date and one day past it, the renewal contract (a bare renewal raises; a
renewal validates against a decision id present in state json), and the
state reader that supplies those ids.
"""

from __future__ import annotations

import json
import textwrap
from datetime import date
from pathlib import Path

import pytest

from eawf.platform.lint import DEFAULT_MAX_LOC, load_lint_config
from eawf.platform.lint.exclusion_expiry import (
    ExclusionConfigError,
    ExclusionRenewal,
    ModuleExclusion,
    decision_ids_from_state,
    expired_exclusions,
    parse_exclusion,
    parse_exclusions,
    validate_renewals,
)

_PATH = "src/eawf/surfaces/tui/app.py"


def _entry(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "path": _PATH,
        "expires": "2027-01-31",
        "reason": "TUI app shell awaiting an extract-module split",
    }
    base.update(overrides)
    return base


def _grant(expires: date, *, path: str = _PATH) -> ModuleExclusion:
    return ModuleExclusion(path=path, expires=expires, reason="awaiting a split")


def _write_pyproject(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# ---- parsing: an exemption must be a bounded grant --------------------------


def test_parse_exclusion_bare_path_string_raises() -> None:
    with pytest.raises(ExclusionConfigError, match="has no expiry"):
        parse_exclusion(_PATH)


def test_parse_exclusion_bare_path_string_message_shows_the_table_form() -> None:
    with pytest.raises(ExclusionConfigError, match='expires = "YYYY-MM-DD"'):
        parse_exclusion(_PATH)


def test_parse_exclusion_non_table_entry_raises_type_error() -> None:
    with pytest.raises(TypeError, match="must be tables, got int"):
        parse_exclusion(17)


def test_parse_exclusion_reads_every_authored_field() -> None:
    parsed = parse_exclusion(_entry())
    assert parsed.path == _PATH
    assert parsed.expires == date(2027, 1, 31)
    assert parsed.reason.startswith("TUI app shell")
    assert parsed.renewal is None


def test_parse_exclusion_accepts_a_bare_toml_date() -> None:
    assert parse_exclusion(_entry(expires=date(2027, 1, 31))).expires == date(2027, 1, 31)


def test_parse_exclusion_missing_expires_raises() -> None:
    entry = _entry()
    del entry["expires"]
    with pytest.raises(ExclusionConfigError, match="has no expiry"):
        parse_exclusion(entry)


def test_parse_exclusion_missing_path_raises() -> None:
    entry = _entry()
    del entry["path"]
    with pytest.raises(ExclusionConfigError, match="missing the required 'path' key"):
        parse_exclusion(entry)


def test_parse_exclusion_missing_reason_raises() -> None:
    entry = _entry()
    del entry["reason"]
    with pytest.raises(ExclusionConfigError, match="missing the required 'reason' key"):
        parse_exclusion(entry)


def test_parse_exclusion_blank_reason_raises() -> None:
    with pytest.raises(ExclusionConfigError, match="'reason' must be a non-blank string"):
        parse_exclusion(_entry(reason="   "))


def test_parse_exclusion_unknown_key_raises() -> None:
    with pytest.raises(ExclusionConfigError, match=r"unknown key\(s\) \['untli'\]"):
        parse_exclusion(_entry(untli="2027-01-31"))


def test_parse_exclusion_non_iso_expires_raises() -> None:
    with pytest.raises(ExclusionConfigError, match="is not an ISO YYYY-MM-DD date"):
        parse_exclusion(_entry(expires="31/01/2027"))


def test_parse_exclusion_non_date_expires_raises() -> None:
    with pytest.raises(ExclusionConfigError, match="expires must be an ISO YYYY-MM-DD date"):
        parse_exclusion(_entry(expires=2027))


def test_parse_exclusions_empty_list_yields_no_grants() -> None:
    assert parse_exclusions([]) == ()


def test_parse_exclusions_single_entry_yields_one_grant() -> None:
    assert len(parse_exclusions([_entry()])) == 1


def test_parse_exclusions_preserves_authored_order() -> None:
    entries = [_entry(path="src/a.py"), _entry(path="src/b.py"), _entry(path="src/c.py")]
    assert [e.path for e in parse_exclusions(entries)] == ["src/a.py", "src/b.py", "src/c.py"]


def test_parse_exclusions_duplicate_path_raises() -> None:
    with pytest.raises(ExclusionConfigError, match="is granted twice"):
        parse_exclusions([_entry(), _entry(expires="2028-01-31")])


def test_parse_exclusions_one_bare_entry_among_tables_raises() -> None:
    with pytest.raises(ExclusionConfigError, match="has no expiry"):
        parse_exclusions([_entry(), "src/eawf/other.py"])


# ---- the expiry boundary ----------------------------------------------------


def test_is_expired_false_the_day_before_the_expiry() -> None:
    assert _grant(date(2027, 1, 31)).is_expired(today=date(2027, 1, 30)) is False


def test_is_expired_false_exactly_on_the_expiry_date() -> None:
    assert _grant(date(2027, 1, 31)).is_expired(today=date(2027, 1, 31)) is False


def test_is_expired_true_one_day_past_the_expiry_date() -> None:
    assert _grant(date(2027, 1, 31)).is_expired(today=date(2027, 2, 1)) is True


def test_days_remaining_is_zero_on_the_expiry_date() -> None:
    assert _grant(date(2027, 1, 31)).days_remaining(today=date(2027, 1, 31)) == 0


def test_days_remaining_is_negative_one_day_past_the_expiry_date() -> None:
    assert _grant(date(2027, 1, 31)).days_remaining(today=date(2027, 2, 1)) == -1


def test_expired_exclusions_empty_input_returns_empty() -> None:
    assert expired_exclusions([], today=date(2030, 1, 1)) == ()


def test_expired_exclusions_returns_nothing_on_the_expiry_date() -> None:
    grants = [_grant(date(2027, 1, 31)), _grant(date(2027, 4, 30), path="src/b.py")]
    assert expired_exclusions(grants, today=date(2027, 1, 31)) == ()


def test_expired_exclusions_returns_only_the_lapsed_grant() -> None:
    grants = [_grant(date(2027, 1, 31)), _grant(date(2027, 4, 30), path="src/b.py")]
    lapsed = expired_exclusions(grants, today=date(2027, 2, 1))
    assert [entry.path for entry in lapsed] == [_PATH]


def test_expired_exclusions_returns_every_lapsed_grant_in_order() -> None:
    grants = [_grant(date(2027, 1, 31)), _grant(date(2027, 4, 30), path="src/b.py")]
    lapsed = expired_exclusions(grants, today=date(2030, 1, 1))
    assert [entry.path for entry in lapsed] == [_PATH, "src/b.py"]


# ---- rejection lands at configuration load ---------------------------------


def test_load_lint_config_rejects_a_bare_exclusion_entry(tmp_path: Path) -> None:
    body = """
        [tool.eawf.lint]
        enabled = ["EAWF010"]

        [tool.eawf.lint.eawf010]
        max-loc = 500
        exclude = ["src/eawf/big.py"]
        """
    with pytest.raises(ExclusionConfigError, match="has no expiry"):
        load_lint_config(_write_pyproject(tmp_path, body))


def test_load_lint_config_reads_bounded_grants(tmp_path: Path) -> None:
    body = """
        [tool.eawf.lint]
        enabled = ["EAWF010"]

        [tool.eawf.lint.eawf010]
        max-loc = 500
        exclude = [
            { path = "src/eawf/big.py", expires = "2027-01-31", reason = "awaiting a split" },
        ]
        """
    config = load_lint_config(_write_pyproject(tmp_path, body))
    assert config.eawf010.exclude == frozenset({"src/eawf/big.py"})
    assert config.eawf010.exclusions[0].expires == date(2027, 1, 31)


def test_load_lint_config_absent_table_grants_nothing(tmp_path: Path) -> None:
    config = load_lint_config(_write_pyproject(tmp_path, "[project]\nname = 'x'\n"))
    assert config.eawf010.exclusions == ()
    assert config.eawf010.exclude == frozenset()
    assert config.eawf010.max_loc == DEFAULT_MAX_LOC


def test_repo_pyproject_grants_all_carry_an_expiry_and_a_reason() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    config = load_lint_config(repo_root / "pyproject.toml")
    assert config.eawf010.exclusions, "the repo must author its exemptions as bounded grants"
    for entry in config.eawf010.exclusions:
        assert isinstance(entry.expires, date)
        assert entry.reason.strip()


# ---- renewal: an explicit typed decision with a named owner ----------------


def test_exclusion_renewal_bare_string_raises() -> None:
    with pytest.raises(ExclusionConfigError, match="carries a bare renewal"):
        parse_exclusion(_entry(renewal="D-SUP-01"))


def test_exclusion_renewal_bare_true_raises() -> None:
    with pytest.raises(ExclusionConfigError, match="carries a bare renewal"):
        parse_exclusion(_entry(renewal=True))


def test_exclusion_renewal_table_parses_decision_and_owner() -> None:
    parsed = parse_exclusion(_entry(renewal={"decision": "D-SUP-01", "owner": "eawf-maintainers"}))
    assert parsed.renewal == ExclusionRenewal(decision="D-SUP-01", owner="eawf-maintainers")


def test_exclusion_renewal_missing_owner_raises() -> None:
    with pytest.raises(ExclusionConfigError, match="missing the required 'owner' key"):
        parse_exclusion(_entry(renewal={"decision": "D-SUP-01"}))


def test_exclusion_renewal_missing_decision_raises() -> None:
    with pytest.raises(ExclusionConfigError, match="missing the required 'decision' key"):
        parse_exclusion(_entry(renewal={"owner": "eawf-maintainers"}))


def test_exclusion_renewal_blank_owner_raises() -> None:
    with pytest.raises(ExclusionConfigError, match="'owner' must be a non-blank string"):
        parse_exclusion(_entry(renewal={"decision": "D-SUP-01", "owner": ""}))


def test_exclusion_renewal_unknown_key_raises() -> None:
    entry = _entry(renewal={"decision": "D-SUP-01", "owner": "team", "until": "2028-01-01"})
    with pytest.raises(ExclusionConfigError, match=r"renewal carries unknown key\(s\) \['until'\]"):
        parse_exclusion(entry)


def test_exclusion_renewal_validates_against_a_known_decision() -> None:
    grants = parse_exclusions([_entry(renewal={"decision": "D-SUP-01", "owner": "team"})])
    validate_renewals(grants, decision_ids=frozenset({"D-SUP-01", "D-BRANCH-GC"}))


def test_exclusion_renewal_unknown_decision_raises() -> None:
    grants = parse_exclusions([_entry(renewal={"decision": "D-GHOST", "owner": "team"})])
    with pytest.raises(ExclusionConfigError, match=r"absent from state\.json"):
        validate_renewals(grants, decision_ids=frozenset({"D-SUP-01"}))


def test_exclusion_renewal_empty_decision_set_raises() -> None:
    grants = parse_exclusions([_entry(renewal={"decision": "D-SUP-01", "owner": "team"})])
    with pytest.raises(ExclusionConfigError, match=r"absent from state\.json"):
        validate_renewals(grants, decision_ids=frozenset())


def test_exclusion_renewal_unrenewed_grant_needs_no_decision() -> None:
    validate_renewals(parse_exclusions([_entry()]), decision_ids=frozenset())


def test_exclusion_renewal_decision_ids_read_from_a_state_mapping(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"decisions": {"D-ONE": {}, "D-TWO": {}}}), encoding="utf-8")
    assert decision_ids_from_state(state) == frozenset({"D-ONE", "D-TWO"})


def test_exclusion_renewal_decision_ids_read_from_a_state_list(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"decisions": [{"id": "D-ONE"}]}), encoding="utf-8")
    assert decision_ids_from_state(state) == frozenset({"D-ONE"})


def test_exclusion_renewal_decision_ids_empty_when_state_absent(tmp_path: Path) -> None:
    assert decision_ids_from_state(tmp_path / "absent.json") == frozenset()


def test_exclusion_renewal_decision_ids_empty_when_state_records_none(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"waves": {}}), encoding="utf-8")
    assert decision_ids_from_state(state) == frozenset()


def test_exclusion_renewal_decision_ids_raise_on_malformed_state(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        decision_ids_from_state(state)
