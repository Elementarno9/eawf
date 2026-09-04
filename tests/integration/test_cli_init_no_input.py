"""Integration tests for ``eawf init --no-input``.

Exercises the pure-pipeline branch of the wizard via the actual Typer app.
Covers the v0.1 acceptance set:

- creates ``.ea/state.json`` and ``.ea/config.yaml``;
- writes ``AGENTS.md`` + ``CLAUDE.md`` with the managed-region markers
  emitted by :mod:`eawf.surfaces.render.agents_md`;
- rejects bad ``--profile`` / ``--project-code`` inputs with exit-code 3;
- refuses to clobber an existing ``.ea/`` without ``--force``.

It also owns the README quick-start replay: the documented block is
extracted from ``README.md`` and every command in it is run through the real
Typer app in a fresh temporary repository, so the front door cannot document
a flag the CLI does not have.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
import yaml
from click.testing import Result
from typer.testing import CliRunner

from eawf.kernel.config.schema import EstimationConfig
from eawf.kernel.migrations import current_target_version
from eawf.kernel.state.models import State
from eawf.surfaces.cli.app import app
from eawf.workflow.estimation.buckets import BUCKET_EU

runner = CliRunner()

_REPO_ROOT = Path(__file__).resolve().parents[2]
_README = _REPO_ROOT / "README.md"
_QUICKSTART_PAGE = _REPO_ROOT / "docs" / "tutorial" / "quickstart.md"
_QUICKSTART_MARKER = "<!-- eawf:quickstart -->"


def _invoke_init(target: Path, *extra: str) -> Result:
    """Run ``eawf --no-input init --project-code DEMO --target <tmp>`` plus *extra*."""
    args = ["--no-input", "init", "--project-code", "DEMO", "--target", str(target), *extra]
    return runner.invoke(app, args)


def test_cli_init_no_input_creates_state_and_config(tmp_path: Path) -> None:
    """A baseline invocation lays down state.json + config.yaml + AGENTS.md + CLAUDE.md."""
    res = _invoke_init(tmp_path, "--profile", "core")
    assert res.exit_code == 0, res.stdout

    state_path = tmp_path / ".ea" / "state.json"
    config_path = tmp_path / ".ea" / "config.yaml"
    agents_md = tmp_path / "AGENTS.md"
    claude_md = tmp_path / "CLAUDE.md"

    assert state_path.exists(), "state.json must be written"
    assert config_path.exists(), "config.yaml must be written"
    assert agents_md.exists(), "AGENTS.md must be rendered"
    assert claude_md.exists(), "CLAUDE.md shim must be written"

    state = json.loads(state_path.read_text(encoding="utf-8"))
    State.model_validate(state)
    assert state["schema_version"] == current_target_version()
    assert state["scope_kind"] == "repo"
    assert state["current"]["project_code"] == "DEMO"
    assert state["goals"]["G01"]["scope_id"] == "DEMO"
    assert state["goals"]["G01"]["title"] == "Establish DEMO project intent"

    text = agents_md.read_text(encoding="utf-8")
    assert "BEGIN" in text and "END" in text, "AGENTS.md must contain managed-region markers"

    assert claude_md.read_text(encoding="utf-8") == "@AGENTS.md\n"


def test_cli_init_no_input_validates_profile_membership(tmp_path: Path) -> None:
    """``--profile bogus`` exits 3 (InvalidInput) before any file is written."""
    res = _invoke_init(tmp_path, "--profile", "bogus-not-a-real-profile")
    assert res.exit_code == 1, res.stdout
    assert not (tmp_path / ".ea").exists(), ".ea must not be created when profile validation fails"


def test_cli_init_rejects_invalid_project_code(tmp_path: Path) -> None:
    """``--project-code lowercase`` exits 3 — regex enforced by WizardAnswers."""
    res = runner.invoke(
        app,
        [
            "--no-input",
            "init",
            "--project-code",
            "lowercase",
            "--profile",
            "core",
            "--target",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 1, res.stdout


def test_cli_init_refuses_existing_non_empty_ea_without_force(tmp_path: Path) -> None:
    """Pre-existing .ea/state.json blocks init unless --force is passed."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir()
    (ea_dir / "state.json").write_text("{}", encoding="utf-8")

    res = _invoke_init(tmp_path, "--profile", "core")
    assert res.exit_code == 1, res.stdout

    # With --force, init succeeds.
    res = _invoke_init(tmp_path, "--profile", "core", "--force")
    assert res.exit_code == 0, res.stdout


def test_cli_init_writes_correct_config_yaml(tmp_path: Path) -> None:
    """config.yaml records profiles.enabled, runtime.adapters, acceptance gates.

    P26-W02 (C08 D14): the legacy top-level ``lifecycle`` and ``plugins``
    blocks are no longer emitted, and ``runtime.kind`` is replaced by the
    canonical ``runtime.adapters`` + ``runtime.preference`` pair.
    """
    res = _invoke_init(
        tmp_path,
        "--profile",
        "core",
        "--profile",
        "python",
        "--runtime",
        "claude-code",
        "--lifecycle-depth",
        "wave",
        "--no-acceptance-typecheck",
    )
    assert res.exit_code == 0, res.stdout

    config_text = (tmp_path / ".ea" / "config.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(config_text)
    assert parsed["profiles"]["enabled"] == ["core", "python"]
    assert parsed["project"]["code"] == "DEMO"
    assert parsed["project"]["title"] == "DEMO"
    assert parsed["project"]["slug"] == "demo"
    assert parsed["project"]["domains"] == ["general"]
    assert parsed["project"]["goals"] == ["Establish DEMO project intent"]
    assert parsed["runtime"]["adapters"] == ["claude-code"]
    assert parsed["runtime"]["preference"] == ["claude-code"]
    assert parsed["estimation"]["buckets"]["overrides"] == {
        bucket.value: {"expected_eu": expected_eu} for bucket, expected_eu in BUCKET_EU.items()
    }
    EstimationConfig.model_validate(parsed["estimation"])
    assert "kind" not in parsed["runtime"]
    assert "lifecycle" not in parsed
    assert "plugins" not in parsed
    assert parsed["acceptance"]["tests"] is True
    assert parsed["acceptance"]["lint"] is True
    assert parsed["acceptance"]["typecheck"] is False


def test_cli_init_renders_managed_regions_for_each_block(tmp_path: Path) -> None:
    """AGENTS.md contains BEGIN/END markers per render block in the composed profile."""
    res = _invoke_init(tmp_path, "--profile", "core")
    assert res.exit_code == 0, res.stdout

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    # Core profile ships several render blocks; non-negotiable-rules is the
    # first and most stable. Marker shape (per eawf.surfaces.render.regions):
    # "<!-- BEGIN EAWF:managed id=<id> ... -->".
    assert "BEGIN EAWF:managed id=non-negotiable-rules" in text
    assert "END EAWF:managed id=non-negotiable-rules" in text
    # A representative subset of the additional core blocks must also render.
    assert "BEGIN EAWF:managed id=worktree-discipline" in text
    assert "BEGIN EAWF:managed id=anti-patterns" in text


def test_cli_init_no_input_emits_json_envelope(tmp_path: Path) -> None:
    """``--json`` surfaces the WizardResult payload deterministically."""
    res = runner.invoke(
        app,
        [
            "--json",
            "--no-input",
            "init",
            "--project-code",
            "DEMO",
            "--profile",
            "core",
            "--target",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["project_code"] == "DEMO"
    assert payload["profiles_enabled"] == ["core"]
    assert payload["state_path"].endswith(".ea/state.json")
    assert payload["config_path"].endswith(".ea/config.yaml")
    assert payload["agents_md_path"].endswith("AGENTS.md")
    assert payload["claude_md_path"].endswith("CLAUDE.md")


def test_cli_init_no_input_requires_project_code(tmp_path: Path) -> None:
    """Missing --project-code with --no-input fails fast with exit 3."""
    res = runner.invoke(
        app,
        ["--no-input", "init", "--profile", "core", "--target", str(tmp_path)],
    )
    assert res.exit_code == 1, res.stdout
    assert not (tmp_path / ".ea").exists()


def test_cli_init_refresh_gitignore_is_non_destructive_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh writes only the managed ignore block and needs no project code."""
    from eawf.platform.install import wizard

    target = tmp_path / "existing"
    ea_dir = target / ".ea"
    manifest_path = ea_dir / "indexes" / "generated.json"
    plugin_path = target / ".claude-plugin" / "plugin.json"
    for path, content in (
        (target / "state.json", b'{"sentinel":"state"}\n'),
        (ea_dir / "config.yaml", b"sentinel: config\n"),
        (target / "AGENTS.md", b"sentinel agents\n"),
        (target / "CLAUDE.md", b"sentinel claude\n"),
        (manifest_path, b'{"sentinel":"manifest"}\n'),
        (plugin_path, b'{"sentinel":"plugin"}\n'),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    gitignore_path = target / ".gitignore"
    gitignore_path.write_text(
        "user-before\n\n"
        "# BEGIN EAWF:gitignore\n"
        "stale-pattern\n"
        "# END EAWF:gitignore\n\n"
        "user-after\n",
        encoding="utf-8",
    )
    preserved = {
        path: path.read_bytes()
        for path in (
            target / "state.json",
            ea_dir / "config.yaml",
            target / "AGENTS.md",
            target / "CLAUDE.md",
            manifest_path,
            plugin_path,
        )
    }

    def _full_init_forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("refresh must not invoke the full init pipeline")

    monkeypatch.setattr(wizard, "run_wizard_no_input", _full_init_forbidden)
    monkeypatch.setattr(wizard, "run_wizard_interactive", _full_init_forbidden)
    args = [
        "--json",
        "--no-input",
        "init",
        "--refresh-gitignore",
        "--state-path",
        "state.json",
        "--target",
        str(target),
    ]

    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.stdout
    payload = json.loads(first.stdout)
    assert payload["gitignore_path"] == str(gitignore_path.resolve())
    assert "/state.json.lock" in payload["gitignore_patterns"]
    first_bytes = gitignore_path.read_bytes()

    second = runner.invoke(app, args)
    assert second.exit_code == 0, second.stdout
    assert gitignore_path.read_bytes() == first_bytes

    changed_args = list(args)
    changed_args[5] = "nested/state.json"
    changed = runner.invoke(app, changed_args)
    assert changed.exit_code == 0, changed.stdout
    changed_text = gitignore_path.read_text(encoding="utf-8")
    changed_lines = changed_text.splitlines()
    assert "/nested/state.json.lock" in changed_lines
    assert "/state.json.lock" not in changed_lines
    assert preserved == {path: path.read_bytes() for path in preserved}
    assert "user-before" in changed_text
    assert "user-after" in changed_text
    assert "stale-pattern" not in changed_text


def test_cli_init_refresh_gitignore_rejects_line_injection(tmp_path: Path) -> None:
    """Refresh maps a malicious state path to normal invalid-input output."""
    result = runner.invoke(
        app,
        [
            "--no-input",
            "init",
            "--refresh-gitignore",
            "--state-path",
            "bad\npattern/state.json",
            "--target",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "cannot contain CR or LF" in result.stdout
    assert not (tmp_path / ".gitignore").exists()


# --- README quick-start replay ----------------------------------------------
#
# The front door's characteristic failure is a quick-start that documents a
# flag the CLI never had (``eawf init`` as a scripted command, when the
# non-interactive flag is ``--quick``). Prose review does not catch that;
# executing the documented block does. The block is fenced behind a marker
# comment so extraction is exact rather than heuristic, and the same block is
# pinned in the docs quickstart page so the two front doors cannot diverge.


def _quickstart_commands(markdown: str) -> list[list[str]]:
    """Return the argv of every command in the marked quick-start block.

    Args:
        markdown: Markdown source carrying one ``<!-- eawf:quickstart -->``
            marker followed by a fenced ``bash`` block.

    Returns:
        One argv list per command line, each with the leading ``eawf``
        program name stripped, in document order.

    Raises:
        KeyError: when the marker comment is absent.
        ValueError: when no fenced ``bash`` block follows the marker, or a
            line inside it is not an ``eawf`` invocation.
    """
    head, sep, tail = markdown.partition(_QUICKSTART_MARKER)
    if not sep:
        raise KeyError(f"no {_QUICKSTART_MARKER} marker in the markdown source")
    del head

    lines = tail.splitlines()
    try:
        opened = lines.index("```bash")
    except ValueError as exc:
        raise ValueError("no fenced bash block follows the quick-start marker") from exc

    commands: list[list[str]] = []
    for line in lines[opened + 1 :]:
        if line.startswith("```"):
            return commands
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        argv = shlex.split(stripped.split("#", 1)[0])
        if argv[:1] != ["eawf"]:
            raise ValueError(f"quick-start line is not an eawf command: {stripped!r}")
        commands.append(argv[1:])
    raise ValueError("quick-start bash block is not closed")


def test_readme_quickstart_block_matches_the_docs_quickstart_page() -> None:
    """The two front doors document byte-identical commands."""
    assert _quickstart_commands(_README.read_text(encoding="utf-8")) == _quickstart_commands(
        _QUICKSTART_PAGE.read_text(encoding="utf-8")
    )


def test_readme_quickstart_commands_each_exit_zero_in_a_fresh_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean directory reaches a tracked first workflow via the documented block.

    Runs the README quick-start commands in order, from a fresh empty
    directory, with no arguments injected — exactly what an operator pastes.
    Each must exit 0, and the end state must carry the ledger plus the phase
    the block opens.
    """
    commands = _quickstart_commands(_README.read_text(encoding="utf-8"))
    assert commands, "the README quick-start block must carry at least one command"

    repo = tmp_path / "demo-repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    for argv in commands:
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, (
            f"`eawf {' '.join(argv)}` exited {result.exit_code}\n{result.output}"
        )

    state_path = repo / ".ea" / "state.json"
    assert state_path.is_file(), "the quick-start block must leave a committed ledger"
    state = State.model_validate(json.loads(state_path.read_text(encoding="utf-8")))
    assert state.project is not None and state.project.code == "DEMO-REPO"
    assert state.current.phase_id is not None, "the block must leave a current phase open"
    assert state.phases[state.current.phase_id].status == "active"


def test_quickstart_extractor_rejects_markdown_without_the_marker() -> None:
    """A README that loses the marker is a KeyError, not a silent zero-command pass."""
    with pytest.raises(KeyError, match="marker"):
        _quickstart_commands("# Title\n\n```bash\neawf status\n```\n")


def test_quickstart_extractor_rejects_a_markerless_fence() -> None:
    """A marker with no fenced bash block after it is a ValueError."""
    with pytest.raises(ValueError, match="no fenced bash block"):
        _quickstart_commands(f"{_QUICKSTART_MARKER}\n\njust prose\n")


def test_quickstart_extractor_rejects_an_unclosed_fence() -> None:
    """An unterminated block is a ValueError rather than a truncated command list."""
    with pytest.raises(ValueError, match="not closed"):
        _quickstart_commands(f"{_QUICKSTART_MARKER}\n\n```bash\neawf status\n")


def test_quickstart_extractor_rejects_a_non_eawf_line() -> None:
    """A stray shell line in the executable block is a ValueError."""
    with pytest.raises(ValueError, match="not an eawf command"):
        _quickstart_commands(f"{_QUICKSTART_MARKER}\n\n```bash\ncd /tmp\n```\n")


def test_quickstart_extractor_returns_empty_for_an_empty_block() -> None:
    """The empty boundary: a marked but commentary-only block yields no commands."""
    source = f"{_QUICKSTART_MARKER}\n\n```bash\n# nothing to run yet\n```\n"
    assert _quickstart_commands(source) == []


def test_quickstart_extractor_reads_a_single_command() -> None:
    """The one-command boundary strips the program name and keeps the argv."""
    source = f'{_QUICKSTART_MARKER}\n\n```bash\neawf phase open --auto --title "A B"\n```\n'
    assert _quickstart_commands(source) == [["phase", "open", "--auto", "--title", "A B"]]
