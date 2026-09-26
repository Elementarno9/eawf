"""The runtime-set conflict guard and the managed-block writes.

The guard runs over the normalized, fully expanded runtime set, so both the
alias spelling and a bare invocation reach it; every configuration write
touches only its delimited block, so operator lines outside it survive byte
for byte.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.platform.install.gitignore_writer import GITIGNORE_PATTERNS, write_gitignore
from eawf.platform.install.managed_block import (
    ManagedBlockError,
    render_managed_block,
    splice_managed_block,
)
from eawf.runtime.runtimes.codex.plugin_install import install_plugin as install_codex_plugin
from eawf.runtime.runtimes.manifest import RuntimeId
from eawf.runtime.runtimes.runtime_set import (
    ALL_RUNTIMES,
    ClaimDetector,
    RuntimeClaim,
    RuntimeSetConflictError,
    claimed_runtimes,
    guard_runtime_set,
    normalize_runtime_set,
)

_BEGIN = "# ---- begin ----"
_END = "# ---- end ----"
_CLAIMED = Path("/opt/host/plugins/cache/eawf/1.0")


def _detectors(**claimed: Path) -> dict[RuntimeId, ClaimDetector]:
    """Return one detector per runtime, claimed where *claimed* names it."""
    by_runtime = {key.replace("_", "-"): path for key, path in claimed.items()}

    def _fixed(path: Path | None) -> ClaimDetector:
        return lambda: path

    return {runtime: _fixed(by_runtime.get(runtime)) for runtime in ALL_RUNTIMES}


# ---- normalize_runtime_set ---------------------------------------------------


def test_normalize_runtime_set_empty_expands_to_every_runtime() -> None:
    assert normalize_runtime_set([]) == ("claude-code", "codex", "opencode")


def test_normalize_runtime_set_single_alias_maps_to_canonical() -> None:
    assert normalize_runtime_set(["claude"]) == ("claude-code",)


def test_normalize_runtime_set_both_spellings_collapse_to_one_entry() -> None:
    assert normalize_runtime_set(["claude-code", "claude", "codex"]) == ("claude-code", "codex")


def test_normalize_runtime_set_orders_canonically() -> None:
    assert normalize_runtime_set(["opencode", "codex", "claude"]) == ALL_RUNTIMES


def test_normalize_runtime_set_unknown_runtime_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown runtime 'gemini'"):
        normalize_runtime_set(["codex", "gemini"])


def test_normalize_runtime_set_bare_string_raises_type_error() -> None:
    with pytest.raises(TypeError, match="sequence of names"):
        normalize_runtime_set("codex")


# ---- claimed_runtimes / guard_runtime_set -----------------------------------


def test_claimed_runtimes_none_claimed_is_empty() -> None:
    assert claimed_runtimes(ALL_RUNTIMES, scope="project", detectors=_detectors()) == ()


def test_claimed_runtimes_user_scope_ignores_codex_and_opencode_claims() -> None:
    detectors = _detectors(claude_code=_CLAIMED, codex=_CLAIMED, opencode=_CLAIMED)
    claims = claimed_runtimes(ALL_RUNTIMES, scope="user", detectors=detectors)
    assert [claim.runtime for claim in claims] == ["claude-code"]


def test_claimed_runtimes_missing_detector_is_unclaimed() -> None:
    claims = claimed_runtimes(("codex",), scope="project", detectors={})
    assert claims == ()


def test_guard_runtime_set_second_install_on_claimed_runtime_raises() -> None:
    with pytest.raises(RuntimeSetConflictError) as caught:
        guard_runtime_set(
            normalize_runtime_set(["claude"]),
            scope="project",
            detectors=_detectors(claude_code=_CLAIMED),
            force=False,
        )
    assert caught.value.claims == (RuntimeClaim(runtime="claude-code", install_path=_CLAIMED),)
    assert "marketplace" in str(caught.value)
    assert str(_CLAIMED) in str(caught.value)


def test_guard_runtime_set_bare_invocation_gates_every_runtime() -> None:
    with pytest.raises(RuntimeSetConflictError) as caught:
        guard_runtime_set(
            normalize_runtime_set([]),
            scope="project",
            detectors=_detectors(opencode=_CLAIMED),
            force=False,
        )
    assert [claim.runtime for claim in caught.value.claims] == ["opencode"]


def test_guard_runtime_set_reports_every_claim_at_once() -> None:
    detectors = _detectors(claude_code=_CLAIMED, codex=_CLAIMED)
    with pytest.raises(RuntimeSetConflictError) as caught:
        guard_runtime_set(ALL_RUNTIMES, scope="project", detectors=detectors, force=False)
    assert [claim.runtime for claim in caught.value.claims] == ["claude-code", "codex"]


def test_guard_runtime_set_force_returns_bypassed_claims() -> None:
    claims = guard_runtime_set(
        ("codex",), scope="project", detectors=_detectors(codex=_CLAIMED), force=True
    )
    assert claims == (RuntimeClaim(runtime="codex", install_path=_CLAIMED),)


def test_guard_runtime_set_unclaimed_returns_empty() -> None:
    assert guard_runtime_set(("codex",), scope="project", detectors=_detectors(), force=False) == ()


# ---- managed blocks ----------------------------------------------------------


def _block(*lines: str) -> bytes:
    return render_managed_block(begin=_BEGIN, end=_END, body_lines=lines)


def test_splice_managed_block_empty_file_is_the_block_alone() -> None:
    assert splice_managed_block(b"", begin=_BEGIN, end=_END, block=_block("a")) == _block("a")


def test_splice_managed_block_appends_after_a_blank_line() -> None:
    spliced = splice_managed_block(b"keep\n", begin=_BEGIN, end=_END, block=_block("a"))
    assert spliced == b"keep\n\n" + _block("a")


def test_splice_managed_block_rewrite_keeps_operator_lines_byte_identical() -> None:
    before = b"# operator\r\n[section]\r\nkey = 1\r\n\r\n"
    after = b"\n\n[trust]\nlevel = 'x'\n\n\n# tail without newline"
    existing = before + _block("old", "lines") + after
    spliced = splice_managed_block(existing, begin=_BEGIN, end=_END, block=_block("new"))
    assert spliced == before + _block("new") + after


def test_splice_managed_block_is_idempotent() -> None:
    once = splice_managed_block(b"x = 1\n", begin=_BEGIN, end=_END, block=_block("a"))
    assert splice_managed_block(once, begin=_BEGIN, end=_END, block=_block("a")) == once


@pytest.mark.parametrize(
    "existing",
    [
        f"{_BEGIN}\noperator = 1\n".encode(),
        f"operator = 1\n{_END}\n".encode(),
        f"{_END}\n{_BEGIN}\n".encode(),
        (f"{_BEGIN}\n{_END}\n" * 2).encode(),
    ],
    ids=["orphan-begin", "orphan-end", "reversed", "duplicated"],
)
def test_splice_managed_block_damaged_markers_raise(existing: bytes) -> None:
    with pytest.raises(ManagedBlockError, match="not one ordered pair"):
        splice_managed_block(existing, begin=_BEGIN, end=_END, block=_block("a"))


def test_splice_managed_block_marker_inside_a_line_is_not_a_marker() -> None:
    existing = f"note = '{_BEGIN}'\n".encode()
    spliced = splice_managed_block(existing, begin=_BEGIN, end=_END, block=_block("a"))
    assert spliced == existing + b"\n" + _block("a")


@pytest.mark.parametrize(
    ("begin", "end", "lines", "message"),
    [
        ("", _END, (), "distinct and non-empty"),
        (_BEGIN, _BEGIN, (), "distinct and non-empty"),
        (_BEGIN, _END, ("a\nb",), "line break"),
        (_BEGIN, _END, (_END,), "repeats a marker"),
    ],
)
def test_render_managed_block_rejects_unspliceable_input(
    begin: str, end: str, lines: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        render_managed_block(begin=begin, end=end, body_lines=lines)


def test_write_gitignore_rewrite_keeps_operator_lines_and_patterns(tmp_path: Path) -> None:
    path = tmp_path / ".gitignore"
    path.write_bytes(b"node_modules/\r\n")
    write_gitignore(tmp_path)
    path.write_bytes(path.read_bytes() + b"# mine\n*.swp")
    before = path.read_bytes()
    write_gitignore(tmp_path)
    assert path.read_bytes() == before
    assert before.startswith(b"node_modules/\r\n\n# BEGIN EAWF:gitignore\n")
    assert before.endswith(b"# END EAWF:gitignore\n# mine\n*.swp")
    for pattern in ("AGENTS.override.md", ".ea/rules/views/", ".claude/skills/eawf-rules-*/"):
        assert pattern in GITIGNORE_PATTERNS
        assert f"\n{pattern}\n".encode() in before


def test_write_gitignore_orphan_begin_refuses_and_keeps_the_file(tmp_path: Path) -> None:
    path = tmp_path / ".gitignore"
    damaged = b"# BEGIN EAWF:gitignore\n.ea/local/\n# operator lines follow\nsecrets/\n"
    path.write_bytes(damaged)
    with pytest.raises(ManagedBlockError):
        write_gitignore(tmp_path)
    assert path.read_bytes() == damaged


def test_install_plugin_codex_config_keeps_operator_toml_byte_identical(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    operator = b'model = "o3"\r\n\r\n[projects."/work"]\r\ntrust_level = "trusted"\r\n\r\n\r\n'
    config.write_bytes(operator)
    install_codex_plugin(tmp_path, home=tmp_path / "home")
    first = config.read_bytes()
    assert first.startswith(operator)
    config.write_bytes(first + b"[tail]\nkept = true\n")
    install_codex_plugin(tmp_path, home=tmp_path / "home")
    assert config.read_bytes() == first + b"[tail]\nkept = true\n"


def test_install_plugin_codex_damaged_config_writes_nothing(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    damaged = b"# ---- __eawf_managed end ----\n# ---- __eawf_managed begin ----\n"
    config.write_bytes(damaged)
    with pytest.raises(ManagedBlockError):
        install_codex_plugin(tmp_path, home=tmp_path / "home")
    assert config.read_bytes() == damaged
    assert sorted(p.name for p in config.parent.iterdir()) == ["config.toml"]
