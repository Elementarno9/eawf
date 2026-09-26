"""Unit tests for the ``eawf hook validate-prose`` chokepoint gate.

Pins the CLI dispatcher around the ``validate_prose`` Layer-2 chokepoint (see
``.ea/local/research/2026-05-29-doc-clarity.md``):

- **fail-open** (no ``--strict``) — a known-bad ``.md`` emits findings but the
  gate exits 0 (advisory, never blocks the local commit);
- **fail-closed** (``--strict``) — the same known-bad ``.md`` exits 1
  (blocks the PR);
- a clean ``.md`` passes both modes (exit 0);
- the Vale leg fails open: with ``vale`` monkeypatched absent, ``--strict``
  still blocks on a deterministic EAWF013/014/017 finding;
- a non-``.md`` arg is a clean no-op; the subcommand registers on ``eawf hook``.
- a byte-pinned fixture under ``tests/golden/`` is skipped even with a manual
  wrap, while the same wrap in authored Markdown outside that tree still
  blocks ``--strict``.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()


# A known-bad markdown: a bare inline URL trips EAWF017. Deterministic — does
# not depend on whether Vale is installed.
_BAD_MD = "Fixed the jitter, see https://example.org/jitter for the trace.\n"

# A clean markdown: refs live in a ## References table, no bare URL, no wrap.
_CLEAN_MD = (
    "Raised the runner budget after continuous-integration jitter flagged false failures [a].\n"
    "\n"
    "## References\n"
    "\n"
    "[a] `src/eawf/observability/perf.py:142`\n"
)

# A manually wrapped paragraph: trips EAWF014, the leg the golden-fixture
# exemption is meant to silence.
_WRAPPED_MD = "A paragraph was manually wrapped\nacross two physical lines.\n"


def _absent_vale(monkeypatch) -> None:
    """Force the Vale leg to fail open (binary absent) for a deterministic run."""
    monkeypatch.setattr("shutil.which", lambda _name: None)


# --- fail-open (local) -------------------------------------------------------


def test_validate_prose_fail_open_exit_0_on_finding(tmp_path, monkeypatch) -> None:
    _absent_vale(monkeypatch)
    bad = tmp_path / "bad.md"
    bad.write_text(_BAD_MD, encoding="utf-8")
    result = runner.invoke(app, ["hook", "validate-prose", str(bad)])
    assert result.exit_code == 0, result.stdout
    # Advisory: the finding is surfaced (warning), not a block.
    assert "EAWF017" in result.stdout


def test_validate_prose_fail_open_json_reports_non_blocking(tmp_path, monkeypatch) -> None:
    _absent_vale(monkeypatch)
    bad = tmp_path / "bad.md"
    bad.write_text(_BAD_MD, encoding="utf-8")
    result = runner.invoke(app, ["--json", "hook", "validate-prose", str(bad)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["hook"] == "validate-prose"
    assert payload["blocking"] is False
    assert payload["violations"] >= 1


# --- fail-closed (strict / CI) -----------------------------------------------


def test_validate_prose_strict_exit_1_on_finding(tmp_path, monkeypatch) -> None:
    _absent_vale(monkeypatch)
    bad = tmp_path / "bad.md"
    bad.write_text(_BAD_MD, encoding="utf-8")
    result = runner.invoke(app, ["hook", "validate-prose", "--strict", str(bad)])
    assert result.exit_code == 1, result.stdout
    assert "EAWF017" in result.stdout


def test_validate_prose_strict_json_reports_blocking(tmp_path, monkeypatch) -> None:
    _absent_vale(monkeypatch)
    bad = tmp_path / "bad.md"
    bad.write_text(_BAD_MD, encoding="utf-8")
    result = runner.invoke(app, ["--json", "hook", "validate-prose", "--strict", str(bad)])
    assert result.exit_code == 1, result.stdout
    payload = json.loads(result.stdout)
    assert payload["blocking"] is True
    assert payload["clean"] is False


def test_validate_prose_strict_blocks_even_when_vale_absent(tmp_path, monkeypatch) -> None:
    # The stability property: the Vale leg fails open (binary absent) but the
    # deterministic EAWF017 leg still blocks the strict gate.
    _absent_vale(monkeypatch)
    bad = tmp_path / "bad.md"
    bad.write_text(_BAD_MD, encoding="utf-8")
    result = runner.invoke(app, ["hook", "validate-prose", "--strict", str(bad)])
    assert result.exit_code == 1, result.stdout


# --- clean passes both modes -------------------------------------------------


def test_validate_prose_clean_passes_fail_open(tmp_path, monkeypatch) -> None:
    _absent_vale(monkeypatch)
    good = tmp_path / "good.md"
    good.write_text(_CLEAN_MD, encoding="utf-8")
    result = runner.invoke(app, ["hook", "validate-prose", str(good)])
    assert result.exit_code == 0, result.stdout
    assert "clean" in result.stdout.lower()


def test_validate_prose_clean_passes_strict(tmp_path, monkeypatch) -> None:
    _absent_vale(monkeypatch)
    good = tmp_path / "good.md"
    good.write_text(_CLEAN_MD, encoding="utf-8")
    result = runner.invoke(app, ["hook", "validate-prose", "--strict", str(good)])
    assert result.exit_code == 0, result.stdout
    assert "clean" in result.stdout.lower()


# --- generated golden-fixture exemption --------------------------------------


def test_validate_prose_strict_skips_generated_golden_fixture(tmp_path, monkeypatch) -> None:
    """A byte-pinned fixture under ``tests/golden/`` is exempt even when wrapped."""
    _absent_vale(monkeypatch)
    rel = "tests/golden/subagent_spec/full_wave.md"
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_WRAPPED_MD, encoding="utf-8")
    result = runner.invoke(app, ["-w", str(tmp_path), "hook", "validate-prose", "--strict", rel])
    assert result.exit_code == 0, result.stdout


def test_validate_prose_strict_still_blocks_authored_md_outside_golden(
    tmp_path, monkeypatch
) -> None:
    """The falsifier: the same wrap outside ``tests/golden/`` still reds."""
    _absent_vale(monkeypatch)
    rel = "docs/note.md"
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_WRAPPED_MD, encoding="utf-8")
    result = runner.invoke(app, ["-w", str(tmp_path), "hook", "validate-prose", "--strict", rel])
    assert result.exit_code == 1, result.stdout
    assert "EAWF014" in result.stdout


# --- surface scoping + registration ------------------------------------------


def test_validate_prose_ignores_non_markdown_arg(tmp_path, monkeypatch) -> None:
    _absent_vale(monkeypatch)
    txt = tmp_path / "note.txt"
    txt.write_text(_BAD_MD, encoding="utf-8")
    # Even in strict mode a non-.md arg is outside the prose surface: clean.
    result = runner.invoke(app, ["hook", "validate-prose", "--strict", str(txt)])
    assert result.exit_code == 0, result.stdout


def test_validate_prose_registered_on_hook_surface() -> None:
    result = runner.invoke(app, ["hook", "--help"])
    assert result.exit_code == 0
    assert "validate-prose" in result.stdout


@pytest.mark.parametrize("strict_flag", [[], ["--strict"]])
def test_validate_prose_filter_registered_for_conditional_scan(strict_flag: list[str]) -> None:
    # The hook must carry a path filter so the conditional staged scan narrows
    # to .md (an unregistered hook would scan the whole staged delta).
    from eawf.platform.lint._conditional import HOOK_FILTERS

    assert "validate-prose" in HOOK_FILTERS
