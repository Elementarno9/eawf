"""Unit tests for :mod:`eawf.install.instrument_probe`.

Probe behaviour we exercise here:

- Cache: a second :func:`probe` call within the same process re-reads the
  cached JSON file rather than re-invoking ``shutil.which``.
- ``--reprobe`` (``reprobe=True``): forces re-probing even when a cache file
  exists (and overwrites it).
- Hard tool missing: raises :class:`InstrumentMissing` (CLI exit 6).
- Soft tool missing: emits a warning result (status ``warn``) but does not
  raise.
- ``EA_INSTRUMENT_PROBE`` env var: redirects the cache path even when the
  caller passed an explicit ``cache_path``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from eawf.install import instrument_probe
from eawf.surfaces.cli.errors import UserError


def _make_which(present: set[str]) -> Any:
    """Return a fake ``shutil.which`` that resolves names in *present*."""

    def fake_which(name: str) -> str | None:
        return f"/fake/bin/{name}" if name in present else None

    return fake_which


_VERSION_STDOUTS: dict[str, str] = {
    "git": "git version 2.46.0\n",
    "python": "Python 3.14.3\n",
    "uv": "uv 0.5.0\n",
}


def _stub_subprocess_run(argv: list[str], **_: object) -> mock.Mock:
    """Return a successful :func:`subprocess.run` mock matching probe regexes.

    Looks up the binary name in :data:`_VERSION_STDOUTS` so the canned stdout
    satisfies the per-spec ``version_regex`` shipped in ``core``. Falls back to
    a generic ``"v1.2.3\\n"`` when the binary is not in the map (used by the
    soft-probe synthetic spec).
    """
    name = argv[0] if argv else ""
    return mock.Mock(returncode=0, stdout=_VERSION_STDOUTS.get(name, "v1.2.3\n"), stderr="")


def test_instrument_probe_caches_per_env(tmp_path: Path) -> None:
    """The second call must re-read the cache and skip ``shutil.which``."""
    cache_path = tmp_path / "probe.json"
    fake_which = mock.Mock(side_effect=_make_which({"git", "python", "uv"}))
    with (
        mock.patch("eawf.install.instrument_probe.shutil.which", fake_which),
        mock.patch("eawf.install.instrument_probe.subprocess.run", _stub_subprocess_run),
    ):
        first = instrument_probe.probe(["core"], cache_path=cache_path)
        first_which_calls = fake_which.call_count
        assert first_which_calls > 0
        assert cache_path.exists()

        second = instrument_probe.probe(["core"], cache_path=cache_path)
        # Cache hit: no further ``shutil.which`` invocations.
        assert fake_which.call_count == first_which_calls
        # Reports compare equal (cache round-trip is a no-op for the payload).
        assert second.model_dump() == first.model_dump()


def test_probe_reprobe_invalidates_cache(tmp_path: Path) -> None:
    """``reprobe=True`` must re-run probes and overwrite the cache."""
    cache_path = tmp_path / "probe.json"
    fake_which = mock.Mock(side_effect=_make_which({"git", "python", "uv"}))
    with (
        mock.patch("eawf.install.instrument_probe.shutil.which", fake_which),
        mock.patch("eawf.install.instrument_probe.subprocess.run", _stub_subprocess_run),
    ):
        instrument_probe.probe(["core"], cache_path=cache_path)
        baseline = fake_which.call_count
        # Sanity: the cache exists and a non-reprobe call would skip ``which``.
        assert baseline > 0

        instrument_probe.probe(["core"], cache_path=cache_path, reprobe=True)
        assert fake_which.call_count == 2 * baseline


def test_probe_hard_missing_raises(tmp_path: Path) -> None:
    """A missing hard requirement maps to :class:`InstrumentMissing` (exit 6)."""
    cache_path = tmp_path / "probe.json"
    # ``git`` is hard-required for ``core``; drop it.
    with (
        mock.patch("eawf.install.instrument_probe.shutil.which", _make_which({"python", "uv"})),
        mock.patch("eawf.install.instrument_probe.subprocess.run", _stub_subprocess_run),
    ):
        with pytest.raises(UserError) as exc:
            instrument_probe.probe(["core"], cache_path=cache_path)
        assert "git" in str(exc.value)


def test_probe_soft_missing_warns_not_raises(tmp_path: Path) -> None:
    """Missing soft tools must surface as ``status='warn'`` and not raise."""
    cache_path = tmp_path / "probe.json"

    # Inject a synthetic profile with one hard + one soft requirement.
    fake_specs = {
        "core": [
            instrument_probe.InstrumentSpec(
                name="git",
                kind="hard",
                probe="version",
                version_args=["--version"],
                version_regex=r"^git version",
            ),
            instrument_probe.InstrumentSpec(
                name="optional-tool",
                kind="soft",
                probe="which",
            ),
        ],
    }
    with (
        mock.patch.object(instrument_probe, "INSTRUMENT_REQUIREMENTS", fake_specs),
        mock.patch("eawf.install.instrument_probe.shutil.which", _make_which({"git"})),
        mock.patch("eawf.install.instrument_probe.subprocess.run", _stub_subprocess_run),
    ):
        report = instrument_probe.probe(["core"], cache_path=cache_path)
        soft = next(r for r in report.results if r.name == "optional-tool")
        assert soft.status == "warn"
        hard = next(r for r in report.results if r.name == "git")
        assert hard.status == "ok"


def test_probe_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``EA_INSTRUMENT_PROBE`` must redirect the cache file path."""
    explicit = tmp_path / "explicit.json"
    override = tmp_path / "from-env.json"
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(override))
    with (
        mock.patch(
            "eawf.install.instrument_probe.shutil.which",
            _make_which({"git", "python", "uv"}),
        ),
        mock.patch("eawf.install.instrument_probe.subprocess.run", _stub_subprocess_run),
    ):
        instrument_probe.probe(["core"], cache_path=explicit)
    assert override.exists()
    assert not explicit.exists()
    raw = json.loads(override.read_text(encoding="utf-8"))
    assert raw["probe_version"] == instrument_probe.PROBE_VERSION
    assert any(entry["name"] == "git" for entry in raw["results"])
