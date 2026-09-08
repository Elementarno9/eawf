"""Test-only console chassis that replays the tracked golden contract through Textual.

Vendored from the 2026-08-25 chassis spike so the 261-frame / 25-journey evidence keeps
running instead of decaying under a gitignored spike directory. Only the console replay
tests import it; no product module may, because the console port replaces this package in
place and inherits the goldens under ``tests/fixtures/console/golden``.
"""

from __future__ import annotations
