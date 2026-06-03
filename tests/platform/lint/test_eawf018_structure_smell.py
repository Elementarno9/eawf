"""Tests for the EAWF018 structure-smell advisory lint.

Covers the four spike-calibrated heuristics:

- H1 over-long prose block (> 600 chars) — flag above, clean at/below.
- H2 run-on bullet list (> 12 items) — flag above, clean at the boundary.
- H3 over-long single bullet (> 500 chars) — flag above, clean at/below.
- docstring leading paragraph (wrapped lines joined; > 600 chars) — the
  join is load-bearing: a raw-physical-line heuristic finds nothing
  because ruff wraps docstrings at ~88 cols.

plus fence-skipping, empty + single-block boundaries, the tighten-only
config clamp on ``[tool.eawf.lint.eawf018]``, and the advisory contract
(the CLI hook exits 0 and emits a warning even with findings).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from typer.testing import CliRunner

from eawf.platform.lint import (
    DEFAULT_MAX_BULLET_CHARS,
    DEFAULT_MAX_BULLET_RUN,
    DEFAULT_MAX_DOCSTRING_PARA_CHARS,
    DEFAULT_MAX_PROSE_CHARS,
    Eawf018Config,
    load_lint_config,
)
from eawf.platform.lint.eawf018_structure_smell import (
    RULE_CODE,
    check_docstrings,
    check_markdown,
)
from eawf.surfaces.cli.app import app

runner = CliRunner()


def _reasons(findings: list) -> list[str]:
    return [f.reason for f in findings]


# ---- H1 over-long prose block ----------------------------------------------


def test_check_markdown_flags_over_long_prose_block() -> None:
    block = "word " * 130  # ~650 chars on one physical line
    assert len(block.strip()) > DEFAULT_MAX_PROSE_CHARS
    findings = check_markdown(block + "\n")
    assert len(findings) == 1
    assert findings[0].code == RULE_CODE
    assert "over-long prose block" in findings[0].reason
    assert findings[0].lineno == 1


def test_check_markdown_clean_for_prose_block_at_threshold() -> None:
    block = "x" * DEFAULT_MAX_PROSE_CHARS  # exactly at cap -> clean (strictly >)
    assert len(block) == DEFAULT_MAX_PROSE_CHARS
    assert check_markdown(block + "\n") == []


def test_check_markdown_clean_for_short_prose_block() -> None:
    assert check_markdown("A short, healthy paragraph that no one would call bloat.\n") == []


def test_check_markdown_ignores_headings_and_blockquotes() -> None:
    long_tail = "z" * (DEFAULT_MAX_PROSE_CHARS + 50)
    source = f"# {long_tail}\n\n> {long_tail}\n"
    assert check_markdown(source) == []


# ---- H2 run-on bullet list -------------------------------------------------


def test_check_markdown_flags_run_on_bullet_list() -> None:
    items = "\n".join(f"- item {i}" for i in range(DEFAULT_MAX_BULLET_RUN + 1))
    findings = check_markdown(items + "\n")
    runs = [f for f in findings if "run-on bullet list" in f.reason]
    assert len(runs) == 1
    assert runs[0].lineno == 1  # reported at the run's first item


def test_check_markdown_clean_for_bullet_run_at_threshold() -> None:
    items = "\n".join(f"- item {i}" for i in range(DEFAULT_MAX_BULLET_RUN))
    assert _reasons(check_markdown(items + "\n")) == []


def test_check_markdown_blank_line_breaks_bullet_run() -> None:
    # Two short runs separated by a blank line: neither exceeds the cap.
    first = "\n".join(f"- a{i}" for i in range(DEFAULT_MAX_BULLET_RUN))
    second = "\n".join(f"- b{i}" for i in range(DEFAULT_MAX_BULLET_RUN))
    source = f"{first}\n\n{second}\n"
    assert [f for f in check_markdown(source) if "run-on" in f.reason] == []


# ---- H3 over-long single bullet --------------------------------------------


def test_check_markdown_flags_over_long_single_bullet() -> None:
    bullet = "- " + ("word " * 110)  # ~550 chars of bullet text
    assert len(bullet.strip()) > DEFAULT_MAX_BULLET_CHARS
    findings = check_markdown(bullet + "\n")
    long_bullets = [f for f in findings if "over-long bullet" in f.reason]
    assert len(long_bullets) == 1
    assert long_bullets[0].code == RULE_CODE


def test_check_markdown_clean_for_short_bullet() -> None:
    assert _reasons(check_markdown("- A concise, in-scope bullet.\n")) == []


# ---- fence skipping --------------------------------------------------------


def test_check_markdown_ignores_fenced_code_block() -> None:
    long_line = "x" * (DEFAULT_MAX_PROSE_CHARS + 50)
    many_bullets = "\n".join(f"- code item {i}" for i in range(DEFAULT_MAX_BULLET_RUN + 5))
    source = f"```\n{long_line}\n{many_bullets}\n```\n"
    assert check_markdown(source) == []


# ---- empty + single-block boundaries ---------------------------------------


def test_check_markdown_empty_source() -> None:
    assert check_markdown("") == []


def test_check_markdown_single_clean_block() -> None:
    assert check_markdown("Just one healthy line.\n") == []


# ---- docstring paragraph-join (the load-bearing join) ----------------------


def _wrapped_long_docstring() -> str:
    # A long leading description, hard-wrapped at ~70 cols (as ruff would),
    # so NO single physical line is long but the joined paragraph clears the
    # 600-char cap.
    sentence = (
        "This module coordinates the verdict producer and the jury rollup "
        "so the self-eval surface reads a stable cohort, and it does a great "
        "deal more besides, threading the metering writer through the "
        "sandbox boundary and reconciling the routing table on every spawn "
        "so that the billing ledger and the live roadmap never drift apart "
        "even under concurrent worktree dispatch from parallel executors, "
        "while also auditing the cherry-pick frontier, re-asserting the "
        "daemon lease, replaying the event journal, and rolling the "
        "telemetry windows forward so that no downstream surface ever has "
        "to reconstruct lifecycle truth from anything other than the "
        "single canonical state file the daemon owns end to end."
    )
    wrapped = "\n    ".join(textwrap.wrap(sentence, width=70))
    return f'"""\n    {wrapped}\n    """\n'


def test_check_docstrings_flags_long_joined_paragraph() -> None:
    source = "def f():\n    " + _wrapped_long_docstring() + "    return None\n"
    findings = check_docstrings(source)
    assert len(findings) == 1
    assert findings[0].code == RULE_CODE
    assert "over-long docstring paragraph" in findings[0].reason


def test_check_docstrings_raw_physical_lines_are_all_short() -> None:
    # The join is load-bearing: every physical line of the docstring is short
    # (ruff-style ~70-col wrap), so a raw-line heuristic would return 0.
    source = "def f():\n    " + _wrapped_long_docstring() + "    return None\n"
    long_physical_lines = [
        ln for ln in source.splitlines() if len(ln.strip()) > DEFAULT_MAX_DOCSTRING_PARA_CHARS
    ]
    assert long_physical_lines == []
    # ...yet the joined-paragraph model still flags it.
    assert check_docstrings(source) != []


def test_check_docstrings_stops_at_google_args_header() -> None:
    # A short summary followed by a long Args block must NOT be joined past
    # the section header, so the leading paragraph stays under the cap.
    body_lines = "\n".join(f"        arg{i}: a description of argument {i}." for i in range(20))
    source = (
        "def f():\n"
        '    """Do one small thing.\n'
        "\n"
        "    Args:\n"
        f"{body_lines}\n"
        '    """\n'
        "    return None\n"
    )
    assert check_docstrings(source) == []


def test_check_docstrings_clean_for_short_docstring() -> None:
    source = 'def f():\n    """Return the resolved config."""\n    return None\n'
    assert check_docstrings(source) == []


def test_check_docstrings_empty_source() -> None:
    assert check_docstrings("") == []


# ---- tighten-only config clamp ---------------------------------------------


def _write_pyproject(tmp_path: Path, body: str) -> Path:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(body, encoding="utf-8")
    return pyproject


def test_load_lint_config_eawf018_defaults_when_absent(tmp_path: Path) -> None:
    pyproject = _write_pyproject(tmp_path, "[tool.eawf.lint]\nenabled = []\n")
    config = load_lint_config(pyproject)
    assert config.eawf018 == Eawf018Config()
    assert config.eawf018.max_prose_chars == DEFAULT_MAX_PROSE_CHARS


def test_load_lint_config_eawf018_local_override_tightens(tmp_path: Path) -> None:
    pyproject = _write_pyproject(
        tmp_path,
        "[tool.eawf.lint]\nenabled = []\n"
        "[tool.eawf.lint.eawf018]\n"
        "max-prose-chars = 400\n"
        "max-bullet-run = 8\n"
        "max-bullet-chars = 300\n"
        "max-docstring-para-chars = 450\n",
    )
    config = load_lint_config(pyproject)
    assert config.eawf018.max_prose_chars == 400
    assert config.eawf018.max_bullet_run == 8
    assert config.eawf018.max_bullet_chars == 300
    assert config.eawf018.max_docstring_para_chars == 450


def test_load_lint_config_eawf018_loosening_override_is_clamped(tmp_path: Path) -> None:
    # A local pyproject may only tighten; an attempt to loosen each cap is
    # pinned back to the calibrated default (min(configured, default)).
    pyproject = _write_pyproject(
        tmp_path,
        "[tool.eawf.lint]\nenabled = []\n"
        "[tool.eawf.lint.eawf018]\n"
        "max-prose-chars = 9000\n"
        "max-bullet-run = 99\n"
        "max-bullet-chars = 9000\n"
        "max-docstring-para-chars = 9000\n",
    )
    config = load_lint_config(pyproject)
    assert config.eawf018.max_prose_chars == DEFAULT_MAX_PROSE_CHARS
    assert config.eawf018.max_bullet_run == DEFAULT_MAX_BULLET_RUN
    assert config.eawf018.max_bullet_chars == DEFAULT_MAX_BULLET_CHARS
    assert config.eawf018.max_docstring_para_chars == DEFAULT_MAX_DOCSTRING_PARA_CHARS


# ---- advisory CLI contract (non-blocking; exits 0 even with findings) ------


def test_hook_exits_zero_and_warns_on_markdown_findings(tmp_path: Path) -> None:
    smelly = tmp_path / "smelly.md"
    smelly.write_text("word " * 130 + "\n", encoding="utf-8")
    result = runner.invoke(app, ["hook", "eawf018-structure-smell", str(smelly)])
    assert result.exit_code == 0, result.stdout
    assert "warning(s)" in result.stdout


def test_hook_exits_zero_and_warns_on_docstring_findings(tmp_path: Path) -> None:
    smelly = tmp_path / "smelly.py"
    smelly.write_text(
        "def f():\n    " + _wrapped_long_docstring() + "    return None\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["hook", "eawf018-structure-smell", str(smelly)])
    assert result.exit_code == 0, result.stdout
    assert "warning(s)" in result.stdout


def test_hook_clean_on_healthy_files(tmp_path: Path) -> None:
    clean_md = tmp_path / "clean.md"
    clean_md.write_text("A healthy paragraph.\n\n- one\n- two\n", encoding="utf-8")
    result = runner.invoke(app, ["hook", "eawf018-structure-smell", str(clean_md)])
    assert result.exit_code == 0, result.stdout
    assert "clean" in result.stdout


def test_hook_skips_unparseable_python(tmp_path: Path) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text("def f(:\n    pass\n", encoding="utf-8")
    result = runner.invoke(app, ["hook", "eawf018-structure-smell", str(broken)])
    assert result.exit_code == 0, result.stdout
