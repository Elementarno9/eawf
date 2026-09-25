"""AST census: every ``EAWF_`` control variable read via ``os.environ`` stays reserved.

Guards :func:`eawf.kernel.config.layered._collect_env_overrides` against a
phantom top-level config key: a control knob like ``EAWF_DEBUG`` or
``EAWF_COAUTHOR_MODE`` that is read somewhere in ``src/`` or ``tools/`` but
missing from :data:`~eawf.kernel.config.layered._RESERVED_ENV_VARS`
composes into the merged config the moment an operator's shell happens to
export it, and the strict ``extra="forbid"`` schema then rejects the whole
merge with ``extra_forbidden``.

The scan walks every string constant in a file rather than only the
argument of an ``os.environ.get``/``os.getenv`` call. That is deliberately
over-inclusive of *call sites* (it also matches the module-constant-then-
pass-through pattern, e.g. ``_FOO_ENV = "EAWF_FOO"`` used later as
``os.environ.get(_FOO_ENV)``, without tracing the data flow) and proven
safe against false positives from documentation: an ``EAWF_...`` mention
inside a docstring or RST backtick reference is never the *entire* string
value of the constant it appears in, so the exact-match pattern only ever
fires on genuine key literals.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from eawf.kernel.config.layered import _RESERVED_ENV_PREFIXES, _RESERVED_ENV_VARS, merge_config
from eawf.surfaces.cli.commands.config import _ConfigSchema

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Bare ``EAWF_`` control-var shape: matches only a string that IS the
#: whole literal, never a substring of a longer sentence or docstring.
_CONTROL_VAR_PATTERN = re.compile(r"^EAWF_[A-Z][A-Z0-9_]*$")

#: The documented real config-override shape (``EAWF_<SECTION>__<KEY>``).
#: Distinguished from a control var by the double underscore the layered
#: merge's dotted-key conversion requires at the section boundary.
_DOUBLE_UNDERSCORE_OVERRIDE_PATTERN = re.compile(r"^EAWF_[A-Z0-9]+__[A-Z0-9_]+$")


def _scanned_files(root: Path) -> list[Path]:
    """Return every file the census walks under *root*: src/, tools/, tests/conftest.py."""
    files = [*(root / "src").rglob("*.py"), *(root / "tools").rglob("*.py")]
    conftest = root / "tests" / "conftest.py"
    if conftest.exists():
        files.append(conftest)
    return files


def _control_var_literals(source: str, *, filename: str = "<test>") -> frozenset[str]:
    """Return every ``EAWF_``-shaped bare string constant in *source*.

    Args:
        source: Python source text to parse.
        filename: Label attached to a raised ``SyntaxError`` for a clearer
            failure message; does not affect matching.

    Returns:
        The set of distinct matching literals, empty when none are present.

    Raises:
        SyntaxError: *source* is not parseable Python. Not swallowed — a
            broken file under the scan root is a real problem, not a
            reason to skip it silently.
    """
    tree = ast.parse(source, filename=filename)
    return frozenset(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _CONTROL_VAR_PATTERN.match(node.value)
    )


def _is_reserved(name: str) -> bool:
    """Return whether *name* is a reserved control var or a real config override."""
    if name in _RESERVED_ENV_VARS or name.startswith(_RESERVED_ENV_PREFIXES):
        return True
    return bool(_DOUBLE_UNDERSCORE_OVERRIDE_PATTERN.match(name))


# --- _control_var_literals: boundary + error paths --------------------------


def test_control_var_literals_empty_source_is_empty() -> None:
    assert _control_var_literals("") == frozenset()


def test_control_var_literals_ignores_docstring_prose() -> None:
    """A backtick-quoted mention inside a sentence never equals the bare literal."""
    source = '"""See the ``EAWF_HOME`` env var, or set EAWF_DEBUG=1, for details."""\n'
    assert _control_var_literals(source) == frozenset()


def test_control_var_literals_matches_a_direct_get_argument() -> None:
    source = 'import os\nos.environ.get("EAWF_HOME")\n'
    assert _control_var_literals(source) == frozenset({"EAWF_HOME"})


def test_control_var_literals_matches_module_constant_indirection() -> None:
    """The constant-then-pass-through pattern used throughout the codebase."""
    source = 'import os\n_FOO_ENV: str = "EAWF_STATUSLINE_THEME"\nos.environ.get(_FOO_ENV)\n'
    assert _control_var_literals(source) == frozenset({"EAWF_STATUSLINE_THEME"})


def test_control_var_literals_raises_on_invalid_syntax() -> None:
    with pytest.raises(SyntaxError):
        _control_var_literals("def (:\n")


# --- _is_reserved: boundary + error paths ------------------------------------


def test_is_reserved_accepts_an_exact_literal() -> None:
    assert _is_reserved("EAWF_DAEMONLESS")


def test_is_reserved_accepts_a_prefix_family_member() -> None:
    assert _is_reserved("EAWF_DAEMON_LOG_MAX_BYTES")
    assert _is_reserved("EAWF_DAEMON_WAL_RETENTION")


def test_is_reserved_accepts_the_double_underscore_override_shape() -> None:
    assert _is_reserved("EAWF_RUNTIME__KIND")


def test_is_reserved_rejects_a_prefix_lookalike_missing_the_boundary() -> None:
    """Sharing a reserved prefix's characters without its trailing underscore does not count."""
    assert not _is_reserved("EAWF_DAEMON_LOGGING_ENABLED")


def test_is_reserved_rejects_an_unknown_control_var() -> None:
    assert not _is_reserved("EAWF_TOTALLY_UNSEEDED_VAR")


# --- gate-fire proof: a seeded unreserved read reds the census --------------


def test_seeded_unreserved_read_reds_the_census(tmp_path: Path) -> None:
    """An unreserved ``EAWF_`` read in a fresh tree is not silently accepted."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "leak.py").write_text(
        'import os\nos.environ.get("EAWF_TOTALLY_UNSEEDED_VAR")\n',
        encoding="utf-8",
    )
    (tmp_path / "tools").mkdir()

    unreserved = {
        literal
        for path in _scanned_files(tmp_path)
        for literal in _control_var_literals(path.read_text(encoding="utf-8"), filename=str(path))
        if not _is_reserved(literal)
    }
    assert unreserved == {"EAWF_TOTALLY_UNSEEDED_VAR"}


def test_seeded_reserved_read_does_not_red_the_census(tmp_path: Path) -> None:
    """The same fixture shape, but reserved, proves the check is not vacuous."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "leak.py").write_text(
        'import os\nos.environ.get("EAWF_DAEMONLESS")\n',
        encoding="utf-8",
    )
    (tmp_path / "tools").mkdir()

    unreserved = {
        literal
        for path in _scanned_files(tmp_path)
        for literal in _control_var_literals(path.read_text(encoding="utf-8"), filename=str(path))
        if not _is_reserved(literal)
    }
    assert unreserved == set()


# --- the real tree agrees with the reserved set ------------------------------


def test_every_control_var_literal_in_the_tree_is_reserved() -> None:
    unreserved: dict[str, list[str]] = {}
    for path in _scanned_files(_REPO_ROOT):
        literals = _control_var_literals(path.read_text(encoding="utf-8"), filename=str(path))
        for literal in literals:
            if not _is_reserved(literal):
                unreserved.setdefault(literal, []).append(str(path.relative_to(_REPO_ROOT)))
    assert not unreserved, (
        "unreserved EAWF_ control variable(s) -- add to _RESERVED_ENV_VARS "
        f"(or a verified _RESERVED_ENV_PREFIXES family) in "
        f"eawf.kernel.config.layered: {unreserved}"
    )


# --- regression: reserved control vars never compose into the config -------


def test_reserved_control_vars_never_enter_the_composed_config(tmp_path: Path) -> None:
    """``EAWF_COAUTHOR_MODE`` + ``EAWF_DEBUG`` must not survive the env layer.

    Mirrors what ``eawf config validate`` does: merge, then run the merged
    config through the same strict, ``extra="forbid"`` schema the CLI
    command validates against. Before the census-driven reservations this
    combination exited 2 with ``extra_forbidden`` on ``coauthor_mode``.
    """
    env = {"EAWF_COAUTHOR_MODE": "disabled", "EAWF_DEBUG": "1"}
    merged, sources = merge_config(workspace=tmp_path, repo=tmp_path, env=env)

    assert "coauthor_mode" not in merged
    assert "debug" not in merged
    assert "coauthor_mode" not in sources
    assert "debug" not in sources
    _ConfigSchema.model_validate(merged)
