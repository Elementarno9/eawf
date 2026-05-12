"""Unit tests for the TOFU trust ledger (P14-W05 / D19)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from eawf.profiles import trust


def _write_profile(path: Path, name: str = "p") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(
            f"""\
            name: {name}
            version: "1.0"
            """
        )
    )


def test_bundled_profile_is_implicitly_trusted(tmp_path: Path) -> None:
    status = trust.verify_trust("core", path=None, ledger={}, no_input=False)
    assert status.is_bundled is True
    assert status.was_already_trusted is True


def test_sha_stable_for_same_bytes(tmp_path: Path) -> None:
    a = tmp_path / "a.yaml"
    a.write_text("hello")
    assert trust.profile_sha256(a) == trust.profile_sha256(a)


def test_unknown_profile_under_no_input_raises(tmp_path: Path) -> None:
    path = tmp_path / "custom.yaml"
    _write_profile(path, "custom")
    with pytest.raises(trust.UntrustedProfileError):
        trust.verify_trust("custom", path=path, ledger={}, no_input=True)


def test_unknown_profile_returns_pending_when_interactive(tmp_path: Path) -> None:
    path = tmp_path / "custom.yaml"
    _write_profile(path, "custom")
    status = trust.verify_trust("custom", path=path, ledger={}, no_input=False)
    assert status.was_already_trusted is False
    assert status.is_bundled is False
    assert status.sha256 == trust.profile_sha256(path)


def test_trusted_profile_passes(tmp_path: Path) -> None:
    path = tmp_path / "custom.yaml"
    _write_profile(path, "custom")
    sha = trust.profile_sha256(path)
    status = trust.verify_trust("custom", path=path, ledger={"custom": sha}, no_input=True)
    assert status.was_already_trusted is True


def test_hash_drift_raises(tmp_path: Path) -> None:
    path = tmp_path / "custom.yaml"
    _write_profile(path, "custom")
    with pytest.raises(trust.TrustDriftError):
        trust.verify_trust(
            "custom",
            path=path,
            ledger={"custom": "deadbeef" * 8},
            no_input=False,
        )


def test_ledger_round_trip(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    trust.save_trust_ledger(config, {"custom": "abc123"})
    loaded = trust.load_trust_ledger(config)
    assert loaded == {"custom": "abc123"}


def test_ledger_load_missing_file(tmp_path: Path) -> None:
    assert trust.load_trust_ledger(tmp_path / "missing.yaml") == {}


def test_ledger_save_preserves_unrelated_keys(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("schema_version: '1.1'\nruntime:\n  adapters: [claude]\n")
    trust.save_trust_ledger(config, {"custom": "abc"})
    text = config.read_text()
    assert "schema_version" in text
    assert "claude" in text
    assert "custom: abc" in text
