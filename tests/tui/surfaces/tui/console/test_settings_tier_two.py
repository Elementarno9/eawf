"""The settings stack draws its second tier from the live tree, and only where it is stated.

The stack card has always known how to draw ``DENIED BY``, ``CONSTRAINED BY``, ``NEEDS``
and ``SECRET``, but nothing produced them: the config catalog carried no policy,
capability or secret data, so the tier existed only in hand-edited fixtures. The catalog
now states it per leaf and the effective-settings read fills it from the merged layers,
so the suite pins both halves against a real layer tree: a refusal appears exactly while
the engine would refuse and names the layer behind it, a registry range, a runtime's
certification and a credential reference each reach the card, a credential value never
does, and a key with none of these draws no second tier at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.config.registry.leaf_catalog import LEAF_KEY_REGISTRY
from eawf.kernel.config.registry.leaf_keys import LeafDeny, LeafKey
from eawf.kernel.projection import settings
from tests.tui.surfaces.tui.console import test_settings_provenance as provenance

#: The labels of the second tier, as the stack card starts each row.
TIER_TWO = (" DENIED BY", " CONSTRAINED BY", " NEEDS", " SECRET")


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a repo root whose config layers the probe owns, its home redirected here."""
    home = tmp_path / "home"
    (home / ".config" / "eawf").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    repo = tmp_path / "repo"
    (repo / ".ea" / "local").mkdir(parents=True)
    return repo


def _repo(tree: Path, body: str) -> None:
    """Write the repo layer."""
    provenance._write(tree / ".ea" / "config.yaml", body)


def _stack(tree: Path, key: str) -> list[str]:
    """Return the live stack card of ``key`` at 80 columns."""
    return provenance._frame("settings.stack", provenance._view(tree), key=key, width=80)


def _row(frame: list[str], label: str) -> str:
    """Return the card row that starts with ``label``."""
    return next(row for row in frame if row.startswith(f"│{label}"))


# ---------- DENIED BY: the refusal ship enforces, while it holds ----------


def test_con_123_a_refused_squash_merge_is_denied_by_the_leaf_and_layer_withholding_it(
    tree: Path,
) -> None:
    _repo(tree, "vcs:\n  pr_merge_method: squash\n")
    leaf = provenance._view(tree).leaf("vcs.pr_merge_method")
    assert leaf.deny_chain == ("vcs.squash_allowed = false · built-in",)
    frame = _stack(tree, "vcs.pr_merge_method")
    assert "vcs.squash_allowed = false · built-in" in _row(frame, " DENIED BY")
    repo = next(row for row in frame if row.startswith("│") and " repo " in row)
    assert "⊘ squash" in repo


def test_con_123_the_deny_names_the_layer_that_set_the_withholding_leaf(tree: Path) -> None:
    provenance._write(
        tree.parent / "home" / ".config" / "eawf" / "config.yaml",
        "vcs:\n  squash_allowed: true\n",
    )
    _repo(tree, "vcs:\n  pr_merge_method: squash\n  squash_allowed: false\n")
    leaf = provenance._view(tree).leaf("vcs.pr_merge_method")
    assert leaf.deny_chain == ("vcs.squash_allowed = false · repo",)


def test_con_123_the_denied_token_stays_on_the_settings_route_row(tree: Path) -> None:
    _repo(tree, "vcs:\n  pr_merge_method: squash\n")
    frame = provenance._frame(
        "settings", provenance._view(tree), key="vcs.pr_merge_method", width=120
    )
    assert "⊘ denied" in provenance._leaf_row(frame, "pr_merge_method")


def test_con_123_allowing_squash_lifts_the_deny(tree: Path) -> None:
    _repo(tree, "vcs:\n  pr_merge_method: squash\n")
    provenance._write(tree / ".ea" / "local" / "config.yaml", "vcs:\n  squash_allowed: true\n")
    leaf = provenance._view(tree).leaf("vcs.pr_merge_method")
    assert leaf.deny_chain == ()
    assert not any(row.startswith("│ DENIED BY") for row in _stack(tree, "vcs.pr_merge_method"))


def test_con_123_a_lifting_leaf_that_is_not_true_keeps_the_deny(tree: Path) -> None:
    """Ship lifts the refusal only on a true ``squash_allowed``; a null keeps it."""
    _repo(tree, "vcs:\n  pr_merge_method: squash\n  squash_allowed: null\n")
    leaf = provenance._view(tree).leaf("vcs.pr_merge_method")
    assert leaf.deny_chain == ("vcs.squash_allowed = null · repo",)


@pytest.mark.parametrize("method", ["merge", "rebase", "squash-merge", ""])
def test_con_123_a_value_other_than_the_refused_one_is_not_denied(tree: Path, method: str) -> None:
    _repo(tree, f"vcs:\n  pr_merge_method: '{method}'\n")
    assert provenance._view(tree).leaf("vcs.pr_merge_method").deny_chain == ()


# ---------- CONSTRAINED BY: the registry's range ----------


@pytest.mark.parametrize(
    ("key", "stated"),
    [
        ("planning.max_parallel_waves", "config registry range 1 to 16 · built-in"),
        ("estimation.eu_minutes", "config registry range 5 to 240 · built-in"),
        ("daemon.idle_timeout_seconds", "config registry range at least 1 · built-in"),
    ],
)
def test_ui_053_a_ranged_key_is_constrained_by_its_registry_range(
    tree: Path, key: str, stated: str
) -> None:
    assert provenance._view(tree).leaf(key).constraint_chain == (stated,)
    assert stated in _row(_stack(tree, key), " CONSTRAINED BY")


def test_ui_053_a_range_with_only_a_maximum_reads_at_most() -> None:
    entry = LeafKey(key="probe.cap", domain="probe", type="int", value_range=(None, 9))
    assert settings._constraint_chain(entry) == ("config registry range at most 9 · built-in",)


# ---------- NEEDS: the runtime a key takes effect on, and its certification ----------


@pytest.mark.parametrize(
    ("key", "requirement", "state"),
    [
        ("runtime.models.claude", "claude runtime", "runtime_facts_uncertified"),
        ("runtime.claude.stall_interval_s", "claude runtime", "runtime_facts_uncertified"),
        ("runtime.models.codex", "codex runtime", "runtime facts certified"),
        ("runtime.opencode.stall_interval_s", "opencode runtime", "runtime_facts_uncertified"),
    ],
)
def test_ui_053_a_runtime_key_needs_its_runtime_in_its_certified_state(
    tree: Path, key: str, requirement: str, state: str
) -> None:
    leaf = provenance._view(tree).leaf(key)
    assert (leaf.capability_requirement, leaf.certification_state) == (requirement, state)
    assert f"│ NEEDS      {requirement} · {state}" in _row(_stack(tree, key), " NEEDS")


# ---------- SECRET: a reference, never a value ----------

#: A leaf whose value names credentials. No catalogued leaf does today, so the probe
#: files one for the duration of a test.
SECRET_KEY = "agents.credentials"  # pragma: allowlist secret


@pytest.fixture
def secret_leaf(monkeypatch: pytest.MonkeyPatch) -> str:
    """File a credential-naming mapping leaf in the catalog and return its key."""
    monkeypatch.setitem(
        LEAF_KEY_REGISTRY,
        SECRET_KEY,
        LeafKey(
            key=SECRET_KEY,
            domain="agents",
            type="mapping",
            default={},
            writable_layers=("repo",),
            consumer="probe.read",
            consumer_kind="engine",
            secret_refs=True,
        ),
    )
    return SECRET_KEY


def test_ui_053_a_credential_is_named_by_its_reference_and_never_its_value(
    tree: Path, secret_leaf: str
) -> None:
    _repo(
        tree,
        "agents:\n  credentials:\n    gh:\n      env_refs: ['${ENV:GITHUB_TOKEN}']\n"
        "      token: plain-credential-text\n",
    )
    leaf = provenance._view(tree).leaf(secret_leaf)
    assert leaf.secret_ref == "${ENV:GITHUB_TOKEN}"
    frame = _stack(tree, secret_leaf)
    assert "${ENV:GITHUB_TOKEN}" in _row(frame, " SECRET")
    assert not any("plain-credential-text" in row for row in frame)


def test_ui_053_several_references_are_named_once_each_in_order(
    tree: Path, secret_leaf: str
) -> None:
    _repo(
        tree,
        "agents:\n  credentials:\n    b:\n      env_refs: ['${ENV:B_KEY}', '${ENV:A_KEY}']\n"
        "    a:\n      env_refs: ['${ENV:A_KEY}']\n",
    )
    assert provenance._view(tree).leaf(secret_leaf).secret_ref == "${ENV:A_KEY}, ${ENV:B_KEY}"


def test_ui_053_a_secret_leaf_with_no_reference_draws_no_secret_row(
    tree: Path, secret_leaf: str
) -> None:
    _repo(tree, "agents:\n  credentials:\n    gh:\n      env_refs: ['${env:lower}', 'ENV:X']\n")
    assert provenance._view(tree).leaf(secret_leaf).secret_ref is None


# ---------- drawn only when stated ----------


def test_ui_053_a_key_with_no_policy_draws_no_second_tier(tree: Path) -> None:
    frame = _stack(tree, "audit.default_level")
    for label in TIER_TWO:
        assert not any(row.startswith(f"│{label}") for row in frame), label


def test_ui_053_only_a_catalog_key_with_metadata_carries_a_second_tier(tree: Path) -> None:
    """Every leaf the catalog states nothing of reads with an empty second tier."""
    view = provenance._view(tree)
    for key, entry in LEAF_KEY_REGISTRY.items():
        leaf = view.leaf(key)
        if entry.deny is None:
            assert leaf.deny_chain == (), key
        if entry.value_range is None:
            assert leaf.constraint_chain == (), key
        if entry.runtime is None:
            assert (leaf.capability_requirement, leaf.certification_state) == (None, None), key
        if not entry.secret_refs:
            assert leaf.secret_ref is None, key


def test_con_123_every_stated_second_tier_row_fits_an_80_column_card(
    tree: Path, secret_leaf: str
) -> None:
    _repo(
        tree,
        "vcs:\n  pr_merge_method: squash\n"
        "agents:\n  credentials:\n    gh:\n      env_refs: ['${ENV:GITHUB_TOKEN}']\n",
    )
    for key, label in (
        ("vcs.pr_merge_method", " DENIED BY"),
        ("planning.max_parallel_waves", " CONSTRAINED BY"),
        ("runtime.opencode.stall_interval_s", " NEEDS"),
        (secret_leaf, " SECRET"),
    ):
        frame = _stack(tree, key)
        assert len(frame) == 24 and all(len(row) <= 80 for row in frame), key
        assert any(row.startswith(f"│{label}") for row in frame), key


# ---------- the catalog's metadata is closed ----------


def test_ui_053_a_deny_needs_a_value_and_a_lifting_leaf() -> None:
    with pytest.raises(ValidationError):
        LeafDeny(value="", unless="vcs.squash_allowed")
    with pytest.raises(ValidationError):
        LeafDeny(value="squash", unless="")
    with pytest.raises(ValidationError):
        LeafDeny(value="squash", unless="vcs.squash_allowed", layer="repo")  # type: ignore[call-arg]


def test_ui_053_a_leaf_names_only_a_known_runtime() -> None:
    with pytest.raises(ValidationError):
        LeafKey(key="probe.x", domain="probe", type="int", runtime="gemini")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        LeafKey(key="probe.x", domain="probe", type="int", secret_refs="yes please")  # type: ignore[arg-type]
