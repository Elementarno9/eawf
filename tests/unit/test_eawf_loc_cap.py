"""Tests for EAWF010 (module-length cap), EAWF002 (log-key naming),
EAWF003 (logger acquisition), and the ``[tool.eawf.lint]`` loader.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from eawf.platform.lint import (
    DEFAULT_MAX_COMPLEXITY,
    DEFAULT_MAX_LOC,
    Eawf010Config,
    Eawf011Config,
    LintConfig,
    load_lint_config,
)
from eawf.platform.lint.eawf002 import (
    LogKeyViolation,
    banned_keys,
)
from eawf.platform.lint.eawf002 import (
    check_source as check_eawf002,
)
from eawf.platform.lint.eawf003 import (
    LoggerNameViolation,
)
from eawf.platform.lint.eawf003 import (
    check_source as check_eawf003,
)
from eawf.platform.lint.eawf010 import (
    RULE_CODE,
    ModuleLengthViolation,
    check_source,
    count_loc,
    find_waiver,
)
from eawf.platform.lint.eawf011 import (
    check_source as check_eawf011,
)

# --- EAWF010: the core success criterion ---------------------------------


def _module_of_loc(loc: int) -> str:
    """Return a syntactically-trivial module of exactly ``loc`` lines."""
    return "\n".join(f"x{i} = {i}" for i in range(loc)) + "\n"


def test_flags_module_over_cap() -> None:
    source = _module_of_loc(701)
    violations = check_source(source)
    assert len(violations) == 1
    assert violations[0].code == RULE_CODE
    assert violations[0].loc == 701
    assert violations[0].max_loc == DEFAULT_MAX_LOC


def test_passes_module_at_cap() -> None:
    # Exactly at the cap is allowed (cap is an inclusive ceiling).
    assert check_source(_module_of_loc(700)) == []


def test_passes_module_under_cap() -> None:
    assert check_source(_module_of_loc(10)) == []


def test_waiver_with_rationale_accepts_oversized_module() -> None:
    source = "# noqa: EAWF010 generated lookup table; split not meaningful\n" + _module_of_loc(710)
    assert check_source(source) == []


def test_bare_waiver_without_rationale_is_rejected() -> None:
    source = "# noqa: EAWF010\n" + _module_of_loc(710)
    violations = check_source(source)
    assert len(violations) == 1
    assert "missing rationale" in violations[0].reason


def test_waiver_with_only_whitespace_rationale_is_rejected() -> None:
    source = "# noqa: EAWF010   \n" + _module_of_loc(710)
    violations = check_source(source)
    assert len(violations) == 1
    assert "missing rationale" in violations[0].reason


def test_waiver_below_a_normal_module_is_ignored() -> None:
    # A waiver buried past the scan window does not silence the cap.
    body = _module_of_loc(710)
    source = body + "# noqa: EAWF010 buried at the very bottom\n"
    violations = check_source(source)
    assert len(violations) == 1
    assert "missing rationale" not in violations[0].reason


def test_waiver_is_a_noop_when_module_is_under_cap() -> None:
    # An under-cap module is clean regardless of any waiver presence.
    source = "# noqa: EAWF010 bare but irrelevant\n" + _module_of_loc(5)
    assert check_source(source) == []


def test_custom_max_loc_threshold() -> None:
    source = _module_of_loc(60)
    assert check_source(source, max_loc=50)[0].loc == 60
    assert check_source(source, max_loc=100) == []


# --- EAWF010: count_loc / find_waiver helpers ----------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", 0),
        ("a = 1\n", 1),
        ("a = 1", 1),  # no trailing newline still counts the line
        ("a = 1\nb = 2\n", 2),
        ("\n\n", 2),  # blank physical lines count
    ],
)
def test_count_loc_boundaries(source: str, expected: int) -> None:
    assert count_loc(source) == expected


def test_find_waiver_returns_rationale() -> None:
    assert find_waiver("# noqa: EAWF010 reason here\n") == "reason here"


def test_find_waiver_returns_empty_for_bare_waiver() -> None:
    assert find_waiver("# noqa: EAWF010\n") == ""


def test_find_waiver_returns_none_when_absent() -> None:
    assert find_waiver("x = 1\n") is None


def test_find_waiver_tolerates_spacing_variants() -> None:
    assert find_waiver("#noqa:EAWF010 reason\n") == "reason"
    assert find_waiver("x = 1  # noqa:  EAWF010   trailing reason\n") == "trailing reason"


def test_module_length_violation_render() -> None:
    violation = ModuleLengthViolation(loc=900, max_loc=700, reason="module is 900 lines (cap 700)")
    assert violation.render().startswith("EAWF010 ")
    assert "900" in violation.render()


# --- EAWF002: log-key naming flag + pass ---------------------------------


def test_eawf002_flags_wave_id_key() -> None:
    source = textwrap.dedent(
        """
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"create_worktree wave_id={wave_id} branch={name}")
        """
    )
    violations = check_eawf002(source)
    assert len(violations) == 1
    assert violations[0].code == "EAWF002"
    assert violations[0].key == "wave"
    assert violations[0].lineno == 4


def test_eawf002_passes_bare_wave_key() -> None:
    source = 'logger.info(f"create_worktree wave={wave_id} branch={name}")\n'
    assert check_eawf002(source) == []


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("create_worktree wave_id={}", ["wave"]),
        ("phase_activate phase_id={} base={}", ["phase"]),
        ("dispatch iter_id={} wave_id={}", ["iter", "wave"]),
        ("create_worktree wave={} request_id={}", []),  # request_id not banned
        ("subwave_id={}", []),  # word-boundary: subwave is not a banned key
        ("phase_activate phase={}", []),
    ],
)
def test_eawf002_banned_keys_detection(message: str, expected: list[str]) -> None:
    assert banned_keys(message) == expected


def test_eawf002_multiple_keys_each_reported() -> None:
    source = 'logger.error(f"dispatch wave_id={w} iter_id={i}")\n'
    violations = check_eawf002(source)
    assert sorted(v.key for v in violations) == ["iter", "wave"]


def test_eawf002_dynamic_and_non_logger_skipped() -> None:
    assert check_eawf002("logger.info(msg)\n") == []
    assert check_eawf002('print("wave_id={}")\n') == []


def test_eawf002_raises_on_syntax_error() -> None:
    with pytest.raises(SyntaxError):
        check_eawf002("logger.info('unterminated\n")


def test_eawf002_violation_render() -> None:
    violation = LogKeyViolation(lineno=3, col_offset=0, message="run wave_id={}", key="wave")
    rendered = violation.render()
    assert rendered.startswith("3:0: EAWF002")
    assert "wave_id= should be bare wave=" in rendered


# --- EAWF003: logger acquisition flag + pass -----------------------------


def test_eawf003_flags_hardcoded_logger_name() -> None:
    source = 'import logging\nlogger = logging.getLogger("eawf")\n'
    violations = check_eawf003(source)
    assert len(violations) == 1
    assert violations[0].code == "EAWF003"
    assert "eawf" in violations[0].reason
    assert violations[0].lineno == 2


def test_eawf003_passes_dunder_name() -> None:
    source = "import logging\nlogger = logging.getLogger(__name__)\n"
    assert check_eawf003(source) == []


def test_eawf003_flags_root_logger() -> None:
    violations = check_eawf003("logger = logging.getLogger()\n")
    assert len(violations) == 1
    assert "root logger" in violations[0].reason


def test_eawf003_flags_bare_getlogger_import_form() -> None:
    source = 'from logging import getLogger\nlogger = getLogger("svc")\n'
    violations = check_eawf003(source)
    assert len(violations) == 1
    assert "svc" in violations[0].reason


def test_eawf003_passes_bare_getlogger_with_dunder() -> None:
    source = "from logging import getLogger\nlogger = getLogger(__name__)\n"
    assert check_eawf003(source) == []


@pytest.mark.parametrize(
    "call",
    [
        "logging.getLogger(name)",  # arbitrary name expression
        "logging.getLogger('a', 'b')",  # too many args
        "logging.getLogger(name='x')",  # keyword form
    ],
)
def test_eawf003_flags_non_dunder_shapes(call: str) -> None:
    assert len(check_eawf003(f"{call}\n")) == 1


def test_eawf003_ignores_non_getlogger_calls() -> None:
    assert check_eawf003("logging.basicConfig(level=10)\n") == []


def test_eawf003_raises_on_syntax_error() -> None:
    with pytest.raises(SyntaxError):
        check_eawf003("getLogger(\n")


def test_eawf003_violation_render() -> None:
    violation = LoggerNameViolation(lineno=2, col_offset=9, reason="some reason")
    assert violation.render() == "2:9: EAWF003 some reason"


# --- [tool.eawf.lint] loader ---------------------------------------------


def _write_pyproject(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_load_lint_config_reads_table(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        [tool.eawf.lint]
        enabled = ["EAWF001", "EAWF010", "EAWF011"]

        [tool.eawf.lint.eawf010]
        max-loc = 500
        exclude = ["src/eawf/big.py"]

        [tool.eawf.lint.eawf011]
        max-complexity = 40
        exclude = ["src/eawf/hot.py"]
        """
    )
    config = load_lint_config(_write_pyproject(tmp_path, body))
    assert isinstance(config, LintConfig)
    assert config.enabled == ("EAWF001", "EAWF010", "EAWF011")
    assert config.eawf010.max_loc == 500
    assert config.eawf010.exclude == frozenset({"src/eawf/big.py"})
    assert config.eawf011.max_complexity == 40
    assert config.eawf011.exclude == frozenset({"src/eawf/hot.py"})


def test_load_lint_config_defaults_when_table_absent(tmp_path: Path) -> None:
    config = load_lint_config(_write_pyproject(tmp_path, "[project]\nname = 'x'\n"))
    assert config.enabled == ()
    assert config.eawf010 == Eawf010Config()
    assert config.eawf010.max_loc == DEFAULT_MAX_LOC
    assert config.eawf010.exclude == frozenset()
    assert config.eawf011 == Eawf011Config()
    assert config.eawf011.max_complexity == DEFAULT_MAX_COMPLEXITY
    assert config.eawf011.exclude == frozenset()


def test_load_lint_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_lint_config(tmp_path / "absent.toml")


def test_repo_pyproject_excludes_keep_tree_green() -> None:
    """The committed pyproject must exempt every pre-existing oversized
    module so wiring EAWF010 does not red ``pre-commit run --all-files``.
    """
    repo_root = Path(__file__).resolve().parents[2]
    config = load_lint_config(repo_root / "pyproject.toml")
    assert "EAWF010" in config.enabled
    offenders: list[str] = []
    for module in sorted((repo_root / "src" / "eawf").rglob("*.py")):
        rel = module.relative_to(repo_root).as_posix()
        if rel in config.eawf010.exclude:
            continue
        violations = check_source(
            module.read_text(encoding="utf-8"), max_loc=config.eawf010.max_loc
        )
        if violations:
            offenders.append(rel)
    assert offenders == [], f"un-excluded oversized modules would red the tree: {offenders}"


def test_repo_pyproject_eawf011_budget_keeps_tree_green() -> None:
    """The committed ``[tool.eawf.lint.eawf011] max-complexity`` budget must be
    at or above the current-tree maximum so wiring EAWF011 as a blocking gate
    does not red ``pre-commit run --all-files``. The budget is a no-regression
    floor (set to the worst existing function), not the 15 design target.
    """
    repo_root = Path(__file__).resolve().parents[2]
    config = load_lint_config(repo_root / "pyproject.toml")
    assert "EAWF011" in config.enabled
    offenders: list[str] = []
    for module in sorted((repo_root / "src" / "eawf").rglob("*.py")):
        rel = module.relative_to(repo_root).as_posix()
        if rel in config.eawf011.exclude:
            continue
        try:
            violations = check_eawf011(
                module.read_text(encoding="utf-8"),
                filename=rel,
                max_complexity=config.eawf011.max_complexity,
            )
        except SyntaxError:
            continue
        offenders.extend(f"{rel}:{v.lineno} {v.name} (cx={v.complexity})" for v in violations)
    assert offenders == [], f"functions over the EAWF011 budget would red the tree: {offenders}"
