"""LINT-001, LINT-002, LINT-030: every shipped rule has one epoch-2 disposition.

The table in :mod:`eawf.platform.lint.dispositions` must be total over the
rules the tree ships, found by rule code rather than by file listing, and
a rule still naming an epoch-1 lifecycle identifier must say why. The
defect the rewrite gate fires on is the real one: EAWF002 banned the
``wave`` / ``iter`` / ``phase`` log keys, epoch-1 vocabulary that no
longer names the lifecycle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.platform.lint.dispositions import (
    LINT_PACKAGE,
    RULE_DISPOSITIONS,
    Disposition,
    RuleDisposition,
    disposition_findings,
    epoch1_references,
    shipped_rules,
)
from eawf.platform.lint.eawf002 import banned_keys

_REPO = Path(__file__).resolve().parents[4]

#: The EAWF002 key set as it stood before the rewrite.
_EPOCH1_EAWF002 = '"""Log keys."""\n_BANNED_KEYS: tuple[str, ...] = ("wave", "iter", "phase")\n'


def _package(tmp_path: Path, modules: dict[str, str]) -> Path:
    """Return a repo root whose lint package holds *modules*."""
    package = tmp_path / LINT_PACKAGE
    package.mkdir(parents=True)
    for name, source in modules.items():
        (package / name).parent.mkdir(parents=True, exist_ok=True)
        (package / name).write_text(source, encoding="utf-8")
    return tmp_path


def _rule(code: str, body: str = "") -> str:
    return f'"""Rule {code}."""\nRULE_CODE = "{code}"\n{body}'


# --- LINT-001: a disposition per shipped rule ---------------------------------


def test_lint_001_every_shipped_rule_carries_exactly_one_disposition() -> None:
    assert [finding.render() for finding in disposition_findings(_REPO)] == []


def test_lint_001_the_table_names_no_rule_the_tree_does_not_ship() -> None:
    assert {row.rule for row in RULE_DISPOSITIONS} == set(shipped_rules(_REPO))


def test_lint_001_the_census_is_read_from_rule_codes_not_the_top_level_listing() -> None:
    """The one numbered rule under the tools sub-directory is still counted."""
    rules = shipped_rules(_REPO)

    assert rules["EAWF020"].parent.name == "tools"
    assert rules["commit-prefix"] == _REPO / "tools" / "commit_prefix_lint.py"


def test_lint_001_a_rule_with_no_disposition_is_a_finding(tmp_path: Path) -> None:
    root = _package(tmp_path, {"eawf099.py": _rule("EAWF099")})

    (finding,) = disposition_findings(root)

    assert finding.rule == "EAWF099"
    assert "no disposition" in finding.render()


def test_lint_001_a_rule_with_two_dispositions_is_a_finding(tmp_path: Path) -> None:
    root = _package(tmp_path, {"eawf001.py": _rule("EAWF001")})
    table = (
        RuleDisposition("EAWF001", "log format", Disposition.CARRY),
        RuleDisposition("EAWF001", "log format", Disposition.REWRITE),
    )

    (finding,) = disposition_findings(root, table)

    assert "2 dispositions" in finding.reason


def test_lint_001_a_retired_rule_that_still_ships_is_a_finding(tmp_path: Path) -> None:
    root = _package(tmp_path, {"eawf001.py": _rule("EAWF001")})
    table = (RuleDisposition("EAWF001", "log format", Disposition.RETIRE),)

    (finding,) = disposition_findings(root, table)

    assert finding.reason == "is retired but still ships"


def test_lint_001_a_checkout_without_the_lint_package_ships_no_rules(tmp_path: Path) -> None:
    assert shipped_rules(tmp_path) == {}
    assert disposition_findings(tmp_path) == []


def test_lint_001_a_module_without_a_rule_code_is_not_a_rule(tmp_path: Path) -> None:
    root = _package(tmp_path, {"helper.py": '"""Shared helper."""\nCODE = "EAWF099"\n'})

    assert shipped_rules(root) == {}


def test_lint_001_an_unparseable_rule_module_raises(tmp_path: Path) -> None:
    root = _package(tmp_path, {"broken.py": "def (:\n"})

    with pytest.raises(SyntaxError):
        shipped_rules(root)


# --- LINT-002: epoch-1 rules are rewritten, with no alias layer ----------------


def test_lint_002_eawf002_bans_the_epoch2_identifier_keys() -> None:
    message = "complete milestone_id={} batch_id={} task_id={} run_id={}"

    assert banned_keys(message) == ["milestone", "batch", "task", "run"]


def test_lint_002_eawf002_translates_no_epoch1_key() -> None:
    """No alias maps the retired keys onto the new ones."""
    assert banned_keys("close_wave wave_id={} iter_id={} phase_id={}") == []


def test_lint_002_the_epoch1_eawf002_is_the_defect_the_gate_fires_on(tmp_path: Path) -> None:
    """Gate fire: the pre-rewrite EAWF002 key set reds the disposition check."""
    root = _package(tmp_path, {"eawf002.py": _rule("EAWF002", _EPOCH1_EAWF002)})
    table = (RuleDisposition("EAWF002", "log keys", Disposition.REWRITE),)

    (finding,) = disposition_findings(root, table)

    assert "names epoch-1 lifecycle identifiers" in finding.reason
    assert "wave" in finding.reason


def test_lint_002_the_shipped_eawf002_names_no_epoch1_identifier() -> None:
    source = (_REPO / LINT_PACKAGE / "eawf002.py").read_text(encoding="utf-8")

    assert epoch1_references(source) == []


# --- LINT-030: no rule references an epoch-1 identifier without a reason -------


def test_lint_030_every_rule_naming_an_epoch1_identifier_states_why() -> None:
    rows = {row.rule: row for row in RULE_DISPOSITIONS}
    unexplained = {
        rule: references
        for rule, path in shipped_rules(_REPO).items()
        if (references := epoch1_references(path.read_text(encoding="utf-8")))
        and rows[rule].epoch1_reference is None
    }

    assert unexplained == {}


def test_lint_030_a_stated_reason_the_rule_no_longer_needs_is_a_finding(tmp_path: Path) -> None:
    root = _package(tmp_path, {"eawf001.py": _rule("EAWF001")})
    table = (
        RuleDisposition("EAWF001", "log format", Disposition.CARRY, epoch1_reference="history"),
    )

    (finding,) = disposition_findings(root, table)

    assert "no longer names an epoch-1 identifier" in finding.reason


@pytest.mark.parametrize(
    "source",
    [
        "def f(wave_id: str) -> None: ...\n",
        "x = obj.phase_ids\n",
        'KEY = "wave"\n',
        'PATTERN = r"^\\[P\\d{2,}-W\\d{2,}\\]"\n',
        'TRAILER = "Eawf-Wave"\n',
        'EXAMPLE = "P30-I04-W03"\n',
    ],
)
def test_lint_030_epoch1_references_finds_each_identifier_shape(source: str) -> None:
    assert len(epoch1_references(source)) == 1


@pytest.mark.parametrize(
    "source",
    [
        '"""Explains the wave_id key and P30-I04-W03, which it replaced."""\n',
        "# wave_id is gone\nx = 1\n",
        'PATH = ".ea/artifacts/audits/A25-P19-I02-W16-backlog-bulk-close.md"\n',
        'KEY = "waveform"\n',
        "def f(subwave_id: str) -> None: ...\n",
        "",
    ],
)
def test_lint_030_epoch1_references_ignores_prose_file_names_and_lookalikes(source: str) -> None:
    assert epoch1_references(source) == []


def test_lint_030_epoch1_references_rejects_unparseable_source() -> None:
    with pytest.raises(SyntaxError):
        epoch1_references("def (:\n")
