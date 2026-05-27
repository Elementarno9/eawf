"""Unit tests for the canonical wizard-step manifest.

Pin the contract that:

- :data:`~eawf.platform.install.steps.WIZARD_STEPS` always contains exactly thirteen
  steps (matches ``docs/architecture/installation.md`` — adding a thirteenth
  or removing one is a spec change that has to land here first).
- All step ids are unique (so :class:`WizardAnswers` can map id → field
  without collision).
- The first step is the state-path prompt — both surfaces call this
  one before anything else, so reordering would break the operator's
  mental model and the resolver path resolution.
- Every ``kind`` value belongs to the narrow :data:`WizardKind` enum so a
  refactor that adds a new kind cannot silently slip in unrendered.
"""

from __future__ import annotations

import dataclasses
from typing import get_args

import pytest

from eawf.platform.install.steps import WIZARD_STEPS, WizardKind, WizardStep


def test_wizard_steps_count_is_13() -> None:
    """The canonical wizard pins to exactly thirteen prompts."""
    assert len(WIZARD_STEPS) == 13


def test_wizard_step_ids_unique() -> None:
    """No two steps share an id (else ``WizardAnswers`` field mapping breaks)."""
    ids = [step.id for step in WIZARD_STEPS]
    assert len(set(ids)) == len(ids)


def test_wizard_step_state_path_first() -> None:
    """First step is ``state_path`` — both surfaces resolve the path before anything else."""
    assert WIZARD_STEPS[0].id == "state_path"
    assert WIZARD_STEPS[0].kind == "path"


def test_wizard_step_kinds_only_known_kinds() -> None:
    """Every step's ``kind`` must appear in the :data:`WizardKind` Literal."""
    allowed = set(get_args(WizardKind))
    for step in WIZARD_STEPS:
        assert step.kind in allowed, (
            f"step {step.id!r} has unknown kind {step.kind!r}; allowed={sorted(allowed)}"
        )


def test_wizard_step_choices_only_for_choice_kind() -> None:
    """``choices`` may be set only for ``kind="choice"`` (other kinds carry None)."""
    for step in WIZARD_STEPS:
        if step.kind == "choice":
            assert step.choices is not None, f"choice step {step.id!r} missing choices"
            assert step.default in step.choices, (
                f"step {step.id!r} default {step.default!r} not in choices {step.choices!r}"
            )
        else:
            assert step.choices is None, (
                f"non-choice step {step.id!r} (kind={step.kind!r}) carries choices"
            )


def test_wizard_step_dataclass_is_frozen() -> None:
    """``WizardStep`` is frozen so the manifest cannot be mutated at runtime."""
    step = WIZARD_STEPS[0]
    with pytest.raises((AttributeError, TypeError, dataclasses.FrozenInstanceError)):
        step.default = "mutated"  # type: ignore[misc]


def test_wizard_step_cli_flag_starts_with_double_dash() -> None:
    """Every step exposes a ``--flag`` (so the manifest is a faithful CLI mirror)."""
    for step in WIZARD_STEPS:
        assert step.cli_flag.startswith("--"), (
            f"step {step.id!r} has non-flag value {step.cli_flag!r}"
        )


def test_wizard_step_dataclass_signature() -> None:
    """``WizardStep`` exposes the documented public attributes."""
    fields = {"id", "prompt", "kind", "default", "cli_flag", "choices"}
    s = WIZARD_STEPS[0]
    for f in fields:
        assert hasattr(s, f), f"WizardStep missing field {f!r}"
    assert isinstance(s, WizardStep)


def test_run_wizard_interactive_signature_accepts_force() -> None:
    """The interactive surface must forward ``--force`` so re-init works.

    Regression: the interactive wizard previously dropped the ``force`` flag,
    so ``eawf init`` against a pre-existing ``.ea/`` raised even when the
    operator passed ``--force``. Lock the signature here so the regression
    cannot return silently.
    """
    import inspect

    from eawf.platform.install.wizard import run_wizard_interactive

    params = inspect.signature(run_wizard_interactive).parameters
    msg = f"run_wizard_interactive must accept 'force' kwarg; got {list(params)}"
    assert "force" in params, msg
    assert params["force"].default is False
