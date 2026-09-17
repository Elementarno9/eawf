"""Run compilation: least-authority layer merge and route selection.

The package turns validated provider configuration and policy layers into
an immutable :class:`~eawf.kernel.runtime.compiled.CompiledRunSpec`. It
spawns nothing and writes no state; the daemon persists what it returns.
"""

from __future__ import annotations
