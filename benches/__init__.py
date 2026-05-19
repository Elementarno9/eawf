"""Performance benchmark suite for eawf.

Run via ``uv run pytest benches/<file>.py --benchmark-only``. CI runs
the same files with ``--benchmark-disable`` so the test bodies are
still type-checked + smoke-loaded without paying the wall-clock cost.
Operator-triggered runs drop the ``--benchmark-disable`` flag.
"""

from __future__ import annotations
