"""State-mutating service layer for per-wave token budgets.

These functions operate on a live :class:`~eawf.state.models.State`
instance and mutate the targeted :class:`~eawf.state.models.Wave` in
place. They do **not** touch ``state.json`` directly — persistence is the
CLI handler's job (it wraps the call in the locked transaction).

Contract:

* :func:`set_budget` — assign or revise a wave's budget. Negative
  budgets are rejected with :class:`ValueError`. Unknown waves raise
  :class:`KeyError`.
* :func:`record_consumption` — add positive ``tokens`` to
  ``Wave.tokens_consumed`` and return the wave plus its post-add policy
  classification. Negative deltas are :class:`ValueError`. Unknown
  waves raise :class:`KeyError`.
* :func:`check_budget` — read-only classify the wave against
  :mod:`eawf.budget.policy`.
"""

from __future__ import annotations

import logging

from eawf.budget.policy import classify
from eawf.state.models import State, Wave

logger = logging.getLogger(__name__)


def _get_wave_or_raise(state: State, wave_id: str) -> Wave:
    wave = state.waves.get(wave_id)
    if wave is None:
        raise KeyError(f"unknown wave: {wave_id!r}")
    return wave


def set_budget(state: State, wave_id: str, budget: int) -> Wave:
    """Set ``Wave.token_budget`` for *wave_id*.

    Args:
        state: Live state. Mutated in place.
        wave_id: Wave to update.
        budget: Non-negative token cap. ``0`` is permitted (instantly
            "over-budget" once any consumption is recorded).

    Raises:
        KeyError: Wave does not exist.
        ValueError: ``budget`` is negative.
    """
    if budget < 0:
        raise ValueError(f"budget must be non-negative; got {budget}")
    wave = _get_wave_or_raise(state, wave_id)
    wave.token_budget = budget
    logger.info(f"set_budget id={wave_id} budget={budget}")
    return wave


def record_consumption(
    state: State,
    wave_id: str,
    tokens: int,
) -> tuple[Wave, str | None]:
    """Accumulate *tokens* into ``Wave.tokens_consumed`` and classify.

    Args:
        state: Live state. Mutated in place.
        wave_id: Wave to update.
        tokens: Non-negative consumption delta. ``0`` is a no-op for the
            counter; the classification is still re-evaluated.

    Returns:
        Tuple of the mutated :class:`Wave` and the classification
        string from :func:`eawf.budget.policy.classify` after the
        increment (``None`` when no budget configured).

    Raises:
        KeyError: Wave does not exist.
        ValueError: ``tokens`` is negative.
    """
    if tokens < 0:
        raise ValueError(f"tokens must be non-negative; got {tokens}")
    wave = _get_wave_or_raise(state, wave_id)
    wave.tokens_consumed += tokens
    tag = classify(wave.tokens_consumed, wave.token_budget)
    logger.info(
        f"record_consumption id={wave_id} delta={tokens} "
        f"consumed={wave.tokens_consumed} budget={wave.token_budget} tag={tag}"
    )
    return wave, tag


def check_budget(state: State, wave_id: str) -> str | None:
    """Return the policy classification for *wave_id* without mutation.

    Raises:
        KeyError: Wave does not exist.
    """
    wave = _get_wave_or_raise(state, wave_id)
    return classify(wave.tokens_consumed, wave.token_budget)
