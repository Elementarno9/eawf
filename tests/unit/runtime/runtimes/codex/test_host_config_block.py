"""The Codex ``config.toml`` managed block: backup, survivors and duplicate keys.

``config.toml`` is shared with the operator and with Codex itself and is
often untracked, so every write goes through a backup, a read-back that
every byte outside the managed block survived, and a refusal to declare
the eawf plugin a second time beside a declaration Codex keeps outside it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.platform.install.managed_block import (
    ManagedBlockError,
    unmanaged_bytes,
    unmanaged_survives,
)
from eawf.runtime.runtimes.codex import plugin_install
from eawf.runtime.runtimes.codex.plugin_install import install_plugin

_BEGIN = "# ---- __eawf_managed begin ----"
_END = "# ---- __eawf_managed end ----"
_BLOCK = (
    f"{_BEGIN}\n[plugins.eawf]\nenabled = true\n"
    "[agents]\nmax_concurrent_threads_per_session = 8\nmax_depth = 1\n"
    f"{_END}\n"
).encode()

# A blank line inside a section is where a pattern-based section removal
# measured swallowing every later section, trust records included.
_BLANK_LINE_FIXTURE = (
    b'model = "o3"\n'
    b"\n"
    b'[projects."/work/one"]\n'
    b'trust_level = "trusted"\n'
    b"\n"
    b"# the operator's note inside the section\n"
    b"\n"
    b'[projects."/work/two"]\n'
    b'trust_level = "trusted"\n'
)


def _config(tmp_path: Path, payload: bytes) -> Path:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_bytes(payload)
    return config


def test_surf_168_blank_line_fixture_survives_install_and_reinstall(tmp_path: Path) -> None:
    config = _config(tmp_path, _BLANK_LINE_FIXTURE)
    install_plugin(tmp_path, home=tmp_path / "home")
    first = config.read_bytes()
    assert unmanaged_bytes(first, begin=_BEGIN, end=_END).startswith(_BLANK_LINE_FIXTURE)
    assert unmanaged_survives(_BLANK_LINE_FIXTURE, first, begin=_BEGIN, end=_END)
    install_plugin(tmp_path, home=tmp_path / "home")
    assert config.read_bytes() == first


def test_surf_168_write_leaves_a_restorable_backup(tmp_path: Path) -> None:
    config = _config(tmp_path, _BLANK_LINE_FIXTURE)
    install_plugin(tmp_path, home=tmp_path / "home")
    backup = config.with_name("config.toml.eawf-backup")
    assert backup.read_bytes() == _BLANK_LINE_FIXTURE


def test_surf_168_first_write_to_a_missing_file_takes_no_backup(tmp_path: Path) -> None:
    install_plugin(tmp_path, home=tmp_path / "home")
    config = tmp_path / ".codex" / "config.toml"
    assert config.read_bytes() == _BLOCK
    assert not config.with_name("config.toml.eawf-backup").exists()


def test_surf_168_unchanged_config_is_not_rewritten_or_backed_up(tmp_path: Path) -> None:
    config = _config(tmp_path, _BLANK_LINE_FIXTURE + b"\n" + _BLOCK)
    install_plugin(tmp_path, home=tmp_path / "home")
    assert config.read_bytes() == _BLANK_LINE_FIXTURE + b"\n" + _BLOCK
    assert not config.with_name("config.toml.eawf-backup").exists()


def test_surf_168_edit_losing_an_unmanaged_section_restores_from_backup(tmp_path: Path) -> None:
    config = _config(tmp_path, _BLANK_LINE_FIXTURE)
    # The measured defect: an edit that swallows every section after a blank line.
    swallowed = _BLANK_LINE_FIXTURE.split(b"\n\n", 1)[0] + b"\n\n" + _BLOCK
    with pytest.raises(ManagedBlockError, match="restored"):
        plugin_install._write_config(config, swallowed, dry_run=False)
    assert config.read_bytes() == _BLANK_LINE_FIXTURE
    assert config.with_name("config.toml.eawf-backup").read_bytes() == _BLANK_LINE_FIXTURE


def test_surf_168_edit_losing_a_section_of_a_new_file_removes_it(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    with pytest.raises(ManagedBlockError):
        plugin_install._write_config(config, b"stray = 1\n" + _BLOCK, dry_run=False)
    assert not config.exists()


@pytest.mark.parametrize(
    "outside",
    [
        b"[plugins.eawf]\nenabled = false\n",
        b'[plugins."eawf@eawf"]\nenabled = true\n',
        b"plugins = { eawf = { enabled = true } }\n",
    ],
    ids=["bare", "qualified", "inline"],
)
def test_surf_168_duplicate_plugin_key_outside_the_block_refuses(
    tmp_path: Path, outside: bytes
) -> None:
    config = _config(tmp_path, outside)
    with pytest.raises(ManagedBlockError, match="already declares"):
        install_plugin(tmp_path, home=tmp_path / "home")
    assert config.read_bytes() == outside
    assert sorted(p.name for p in config.parent.iterdir()) == ["config.toml"]


def test_surf_168_other_plugins_outside_the_block_are_not_duplicates(tmp_path: Path) -> None:
    outside = b'[plugins."other@market"]\nenabled = true\n'
    config = _config(tmp_path, outside)
    install_plugin(tmp_path, home=tmp_path / "home")
    assert config.read_bytes().startswith(outside)


def test_surf_168_invalid_toml_outside_the_block_refuses(tmp_path: Path) -> None:
    config = _config(tmp_path, b"[unterminated\n")
    with pytest.raises(ManagedBlockError, match="not valid TOML"):
        install_plugin(tmp_path, home=tmp_path / "home")
    assert config.read_bytes() == b"[unterminated\n"


# ---- the byte-level survivor check ------------------------------------------


def test_surf_168_unmanaged_survives_empty_prior_and_block_alone() -> None:
    assert unmanaged_survives(b"", _BLOCK, begin=_BEGIN, end=_END)


def test_surf_168_unmanaged_survives_rejects_a_missing_block() -> None:
    assert not unmanaged_survives(b"a = 1\n", b"a = 1\n", begin=_BEGIN, end=_END)


def test_surf_168_unmanaged_survives_rejects_one_changed_byte_after_the_block() -> None:
    prior = b"a = 1\n\n" + _BLOCK + b"b = 2\n"
    assert unmanaged_survives(prior, prior, begin=_BEGIN, end=_END)
    assert not unmanaged_survives(prior, prior[:-2] + b"3\n", begin=_BEGIN, end=_END)


def test_surf_168_unmanaged_survives_rejects_text_trailing_an_appended_block() -> None:
    written = b"a = 1\n\n" + _BLOCK + b"b = 2\n"
    assert not unmanaged_survives(b"a = 1\n", written, begin=_BEGIN, end=_END)


def test_surf_168_unmanaged_survives_rejects_a_non_newline_separator() -> None:
    assert not unmanaged_survives(b"a = 1\n", b"a = 1\nx\n" + _BLOCK, begin=_BEGIN, end=_END)


def test_surf_168_unmanaged_bytes_without_a_block_is_the_input() -> None:
    assert unmanaged_bytes(b"", begin=_BEGIN, end=_END) == b""
    assert unmanaged_bytes(b"a = 1\n", begin=_BEGIN, end=_END) == b"a = 1\n"


def test_surf_168_unmanaged_bytes_refuses_damaged_markers() -> None:
    with pytest.raises(ManagedBlockError):
        unmanaged_bytes(f"{_END}\n{_BEGIN}\n".encode(), begin=_BEGIN, end=_END)
