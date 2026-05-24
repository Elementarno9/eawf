"""Runtime probes — read-only capability snapshots.

This sub-package houses one-shot probes that capture an external runtime's
advertised capabilities (subprocess primary surface, ``--help`` flags,
version string, SDK feature flags) into a typed JSON snapshot. Probes
never mutate state and never speak to the daemon; they exist so a future
diff can detect drift between the runtime's advertised contract and the
eawf adapter's expectations.

The first probe is :mod:`eawf.runtime.runtimes.probes.sdk_baseline`, capturing
the pre-2026-06-15 baseline for ``claude``, ``codex``, and ``opencode``
ahead of the Anthropic credit-pool reinstatement.
"""

from __future__ import annotations
