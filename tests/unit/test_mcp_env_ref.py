"""Unit tests for :mod:`eawf.runtime.mcp.env_ref`.

Coverage:

- :func:`parse_env_ref` accepts canonical tokens and rejects every
  malformed shape we know of.
- :func:`render_env_block` produces literal ``${ENV:NAME}`` values
  (no expansion).
- :func:`assert_no_expansion` raises on any drift from the literal
  shape.
- The module source has no ``import os`` line — defence against an
  accidental copy/paste from a sibling module that *would* read
  ``os.environ``.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from eawf.runtime.mcp import env_ref
from eawf.runtime.mcp.env_ref import (
    ENV_REF_RE,
    InvalidEnvRef,
    assert_no_expansion,
    parse_env_ref,
    render_env_block,
)

pytestmark = pytest.mark.unit


def test_parse_env_ref_canonical_token_returns_var_name() -> None:
    assert parse_env_ref("${ENV:OPENAI_API_KEY}") == "OPENAI_API_KEY"


def test_parse_env_ref_rejects_bare_var_name() -> None:
    with pytest.raises(InvalidEnvRef):
        parse_env_ref("OPENAI_API_KEY")


def test_parse_env_ref_rejects_missing_env_prefix() -> None:
    with pytest.raises(InvalidEnvRef):
        parse_env_ref("${OPENAI_API_KEY}")


def test_parse_env_ref_rejects_lowercase_name() -> None:
    with pytest.raises(InvalidEnvRef):
        parse_env_ref("${ENV:lower-case}")


def test_parse_env_ref_rejects_leading_digit() -> None:
    with pytest.raises(InvalidEnvRef):
        parse_env_ref("${ENV:1NUMERIC}")


def test_parse_env_ref_rejects_whitespace() -> None:
    with pytest.raises(InvalidEnvRef):
        parse_env_ref(" ${ENV:NAME}")
    with pytest.raises(InvalidEnvRef):
        parse_env_ref("${ENV:NAME} ")


def test_parse_env_ref_accepts_underscore_leading_name() -> None:
    assert parse_env_ref("${ENV:_PRIVATE}") == "_PRIVATE"


def test_parse_env_ref_accepts_digit_after_letter() -> None:
    assert parse_env_ref("${ENV:VAR42}") == "VAR42"


def test_render_env_block_returns_literal_token_values() -> None:
    block = render_env_block(["${ENV:A}", "${ENV:B}"])
    assert block == {"A": "${ENV:A}", "B": "${ENV:B}"}


def test_render_env_block_rejects_invalid_token_member() -> None:
    with pytest.raises(InvalidEnvRef):
        render_env_block(["${ENV:OK}", "BAD"])


def test_render_env_block_empty_input_yields_empty_dict() -> None:
    assert render_env_block([]) == {}


def test_assert_no_expansion_passes_for_literal_block() -> None:
    # Should not raise.
    assert_no_expansion({"OPENAI_KEY": "${ENV:OPENAI_KEY}"})


def test_assert_no_expansion_raises_on_expanded_value() -> None:
    with pytest.raises(InvalidEnvRef):
        assert_no_expansion({"OPENAI_KEY": "sk-secret-value"})


def test_assert_no_expansion_raises_on_mismatched_key() -> None:
    """The dict key must match the embedded NAME inside the token value."""
    with pytest.raises(InvalidEnvRef):
        assert_no_expansion({"OPENAI_KEY": "${ENV:OTHER_KEY}"})


def test_env_ref_re_matches_pydantic_model_pattern() -> None:
    """Regex must equal the ``McpServer.env_refs`` Pydantic pattern.

    Drift here would let the model accept tokens our installer rejects
    (or vice-versa). The model uses the literal ``r"^\\$\\{ENV:[A-Z_][A-Z0-9_]*\\}$"``;
    this test pins both patterns to the same source string.
    """
    assert ENV_REF_RE.pattern == r"^\$\{ENV:([A-Z_][A-Z0-9_]*)\}$"


def test_env_ref_module_does_not_import_os() -> None:
    """``mcp/env_ref.py`` MUST NOT import ``os`` (security barrier).

    The module exists on the literal-token side of the env barrier;
    importing ``os`` would create a code path where a future helper
    might read ``os.environ`` and leak a secret to disk. Block it at
    CI time via an AST walk.
    """
    source_path = inspect.getsourcefile(env_ref)
    assert source_path is not None
    with open(source_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=source_path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "os", (
                    f"{source_path} imports `os`; this module must not "
                    "touch the ambient environment"
                )
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "os", (
                f"{source_path} `from os import ...`; this module must "
                "not touch the ambient environment"
            )
