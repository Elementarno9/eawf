"""Unit tests for :mod:`eawf.kernel.config.profile`.

The contracts under test:

- ``enable_profile(<id>, layer=<L>, ...)`` writes the profile id into
  ``profiles.enabled`` of the layer's YAML file.
- The same call materialises any required state-keys per
  :data:`KNOWN_PROFILES` (e.g. ``research`` → ``hypotheses``, ``audits``).
- Re-enabling an already-enabled profile is a no-op (file unchanged).
- Unknown profile id → :class:`InvalidInput`.
- Unknown / read-only layer → :class:`InvalidInput`.
- Missing state file → materialisation skipped (returns empty list).
- Malformed state file → :class:`NotFound`.
- The read+mutate+write of ``_materialise_state_keys`` is serialised under
  ``portalock(state.json)`` so a concurrent writer cannot drop the
  freshly-materialised top-level keys (TOCTOU regression).
"""

from __future__ import annotations

import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest
import yaml

from eawf.cli.errors import UserError
from eawf.kernel.config.profile import KNOWN_PROFILES, enable_profile


def _seed_state(state_path: Path, body: dict[str, object]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(body))


# --- Layer-file write contract ---------------------------------------------


def test_enable_writes_profile_to_layer_file(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    result = enable_profile("python", layer="repo", layer_file_path=layer_file, state_path=None)
    assert layer_file.exists()
    body = yaml.safe_load(layer_file.read_text(encoding="utf-8"))
    assert "python" in body["profiles"]["enabled"]
    assert result["profile"] == "python"
    assert result["layer"] == "repo"
    assert result["already_enabled"] is False


def test_enable_creates_intermediate_dirs(tmp_path: Path) -> None:
    layer_file = tmp_path / ".ea" / "local" / "config.yaml"
    enable_profile("python", layer="local", layer_file_path=layer_file, state_path=None)
    assert layer_file.exists()


def test_enable_appends_to_existing_profiles(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    layer_file.write_text("profiles:\n  enabled: [core]\n", encoding="utf-8")
    result = enable_profile("python", layer="repo", layer_file_path=layer_file, state_path=None)
    body = yaml.safe_load(layer_file.read_text(encoding="utf-8"))
    assert body["profiles"]["enabled"] == ["core", "python"]
    assert result["already_enabled"] is False


def test_enable_idempotent_for_already_enabled(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    layer_file.write_text("profiles:\n  enabled: [core, python]\n", encoding="utf-8")
    before = layer_file.read_bytes()
    result = enable_profile("python", layer="repo", layer_file_path=layer_file, state_path=None)
    after = layer_file.read_bytes()
    assert before == after  # file not rewritten
    assert result["already_enabled"] is True


def test_enable_preserves_other_top_level_keys(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    layer_file.write_text(
        "estimation:\n  eu_minutes: 60\nplanning:\n  approval: auto\n",
        encoding="utf-8",
    )
    enable_profile("research", layer="repo", layer_file_path=layer_file, state_path=None)
    body = yaml.safe_load(layer_file.read_text(encoding="utf-8"))
    assert body["estimation"]["eu_minutes"] == 60
    assert body["planning"]["approval"] == "auto"
    assert body["profiles"]["enabled"] == ["research"]


# --- State-key materialisation ----------------------------------------------


def test_research_materialises_state_keys(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    state_path = tmp_path / "state.json"
    _seed_state(state_path, {"schema_version": "1.0"})

    result = enable_profile(
        "research", layer="repo", layer_file_path=layer_file, state_path=state_path
    )
    body = json.loads(state_path.read_text(encoding="utf-8"))
    assert body["hypotheses"] == {}
    assert body["audits"] == {}
    assert set(result["state_keys_materialised"]) == {"hypotheses", "audits"}


def test_existing_state_keys_not_clobbered(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    state_path = tmp_path / "state.json"
    _seed_state(
        state_path,
        {
            "schema_version": "1.0",
            "hypotheses": {"H01-01": {"id": "H01-01"}},
            "audits": {},
        },
    )
    result = enable_profile(
        "research", layer="repo", layer_file_path=layer_file, state_path=state_path
    )
    body = json.loads(state_path.read_text(encoding="utf-8"))
    # Existing data preserved; nothing materialised because keys already present.
    assert body["hypotheses"] == {"H01-01": {"id": "H01-01"}}
    assert result["state_keys_materialised"] == []


def test_python_profile_has_no_required_state_keys(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    state_path = tmp_path / "state.json"
    _seed_state(state_path, {"schema_version": "1.0"})
    result = enable_profile(
        "python", layer="repo", layer_file_path=layer_file, state_path=state_path
    )
    assert result["state_keys_materialised"] == []
    body = json.loads(state_path.read_text(encoding="utf-8"))
    # State unchanged save for pre-existing schema_version.
    assert body == {"schema_version": "1.0"}


def test_state_path_none_skips_materialisation(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    result = enable_profile("research", layer="repo", layer_file_path=layer_file, state_path=None)
    # No state file → nothing materialised, but the layer write still happened.
    assert result["state_keys_materialised"] == []
    body = yaml.safe_load(layer_file.read_text(encoding="utf-8"))
    assert "research" in body["profiles"]["enabled"]


def test_missing_state_path_does_not_error(tmp_path: Path) -> None:
    """A passed state_path that doesn't exist is treated as state_path=None."""
    layer_file = tmp_path / "config.yaml"
    state_path = tmp_path / "missing-state.json"
    result = enable_profile(
        "research", layer="repo", layer_file_path=layer_file, state_path=state_path
    )
    # Module logic treats missing file as a no-op (the caller in the CLI
    # nullifies the path; this tests the lower-level safety net).
    assert result["state_keys_materialised"] == []


# --- Error paths ------------------------------------------------------------


def test_unknown_profile_raises_invalid_input(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    with pytest.raises(UserError) as excinfo:
        enable_profile("not-a-profile", layer="repo", layer_file_path=layer_file, state_path=None)
    assert "unknown profile" in str(excinfo.value)


def test_unknown_layer_raises_invalid_input(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    with pytest.raises(UserError):
        enable_profile("python", layer="garbage", layer_file_path=layer_file, state_path=None)


def test_built_in_layer_is_read_only(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    with pytest.raises(UserError) as excinfo:
        enable_profile("python", layer="built-in", layer_file_path=layer_file, state_path=None)
    assert "read-only" in str(excinfo.value)


def test_env_layer_is_not_writable(tmp_path: Path) -> None:
    """``env`` and ``cli`` are runtime-only; they cannot host a saved profile."""
    layer_file = tmp_path / "config.yaml"
    with pytest.raises(UserError):
        enable_profile("python", layer="env", layer_file_path=layer_file, state_path=None)


def test_malformed_state_file_raises_not_found(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    state_path = tmp_path / "bad-state.json"
    state_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(UserError):
        enable_profile("research", layer="repo", layer_file_path=layer_file, state_path=state_path)


def test_state_top_level_must_be_mapping(tmp_path: Path) -> None:
    layer_file = tmp_path / "config.yaml"
    state_path = tmp_path / "state.json"
    state_path.write_bytes(orjson.dumps([1, 2, 3]))
    with pytest.raises(UserError):
        enable_profile("research", layer="repo", layer_file_path=layer_file, state_path=state_path)


# --- Registry contract -----------------------------------------------------


def test_known_profiles_includes_v0_1_set() -> None:
    """All v0.1 profile ids per docs/architecture/profiles.md are registered."""
    expected = {
        "core",
        "python",
        "research",
        "docs",
        "apps",
        "infra",
        "ml",
        "quant",
        "re",
        "game",
        "robotics",
    }
    assert expected.issubset(KNOWN_PROFILES.keys())


def test_research_profile_requires_hypotheses_and_audits() -> None:
    assert set(KNOWN_PROFILES["research"]) == {"hypotheses", "audits"}


# --- Concurrency: serialise materialise under portalock(state.json) -------


# Path to the minimal-but-valid repo-scope state fixture used to seed
# ``state.json`` for the concurrent-writer regression below. The fixture
# omits ``hypotheses`` and ``audits`` so a ``research`` profile enable
# triggers materialisation.
_VALID_REPO_STATE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)


def test_materialise_state_keys_serialises_with_concurrent_writer(tmp_path: Path) -> None:
    """A racing ``state_transaction`` must not drop materialised state keys.

    Regression for the TOCTOU window in
    :func:`eawf.kernel.config.profile._materialise_state_keys`: the read of
    ``state.json``, the in-memory mutation, and the atomic write must all
    happen under one ``portalock(state_path)`` acquisition, otherwise a
    competing writer that reads the file *before* the materialisation
    write lands can stomp on the freshly-added top-level keys via its
    own stale-view dump.

    The test runs the race repeatedly to exercise both interleavings:

    1. **enable-first / state_transaction-second** — the materialised keys
       must remain present as ``{}`` in the final body (because Pydantic
       round-trips an empty ``dict`` as ``{}``, not ``null``). The bug
       manifests here: without the lock the second writer's stale
       ``model_dump`` writes ``hypotheses: null`` over ``hypotheses: {}``.
    2. **state_transaction-first / enable-second** — the materialiser
       observes the keys already present (as ``null`` from
       ``model_dump(mode="json")``) and the ``state_keys_materialised``
       return list is empty.  This branch is vacuously consistent — the
       function never claims to have added a key it didn't.

    The strong assertion is the consistency check:
    *every key that ``enable_profile`` claims under
    ``state_keys_materialised`` MUST be present in the on-disk body as a
    dict (not ``None``).*  Without the lock, the bug case forces an
    inconsistency.
    """
    from eawf.cli._mutation import state_transaction

    iterations = 5
    for i in range(iterations):
        work = tmp_path / f"iter-{i:02d}"
        work.mkdir()
        state_path = work / "state.json"
        layer_path = work / "config.yaml"

        # Seed with the fully valid repo-scope fixture; it deliberately
        # omits ``hypotheses`` and ``audits`` so the research profile
        # enable triggers materialisation work.
        shutil.copy(_VALID_REPO_STATE, state_path)
        layer_path.write_text("profiles:\n  enabled: []\n", encoding="utf-8")

        # Marker we'll bump in the state_transaction so we can verify the
        # transaction-side mutation also persists.
        before_body = json.loads(state_path.read_text(encoding="utf-8"))
        original_updated_at = before_body["updated_at"]

        barrier = threading.Barrier(2)
        new_updated_at = datetime.now(UTC).replace(microsecond=(i + 1) * 1000)

        def _enable_research(
            _barrier: threading.Barrier = barrier,
            _layer_path: Path = layer_path,
            _state_path: Path = state_path,
        ) -> list[str]:
            _barrier.wait()
            envelope = enable_profile(
                "research",
                layer="repo",
                layer_file_path=_layer_path,
                state_path=_state_path,
            )
            return list(envelope["state_keys_materialised"])

        def _bump_updated_at(
            _barrier: threading.Barrier = barrier,
            _state_path: Path = state_path,
            target_ts: datetime = new_updated_at,
        ) -> None:
            _barrier.wait()
            with state_transaction(_state_path) as state:
                state.updated_at = target_ts

        with ThreadPoolExecutor(max_workers=2) as ex:
            f_enable = ex.submit(_enable_research)
            f_bump = ex.submit(_bump_updated_at)
            materialised = f_enable.result()
            f_bump.result()

        body = json.loads(state_path.read_text(encoding="utf-8"))

        # Strong consistency check: any key the materialiser CLAIMS to
        # have added must be a dict in the final body. Without the
        # portalock this fails in the enable-first / transaction-second
        # ordering because the transaction's stale-view ``model_dump``
        # overwrites the freshly-added keys with ``null``.
        for key in materialised:
            assert key in body, (
                f"iter {i}: materialiser claimed to add {key!r} but it is "
                f"absent from final body: {sorted(body.keys())}"
            )
            assert isinstance(body[key], dict), (
                f"iter {i}: materialiser claimed to add {key!r} as an empty "
                f"dict but final body has {body[key]!r} — concurrent writer "
                "stomped the materialised value"
            )

        # The transaction's effect must always survive: regardless of
        # ordering, ``updated_at`` must reflect the bump (or have been
        # written-and-overwritten by the materialiser to the original,
        # which is also a lost-mutation symptom we want to catch).
        assert body["updated_at"] != original_updated_at, (
            f"iter {i}: state_transaction's updated_at bump was lost — "
            f"final body still shows the original {original_updated_at}"
        )
