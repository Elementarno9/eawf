"""Release-train workflow: the v0.7.0 ladder and the Release status machine.

The typed records live in :mod:`eawf.kernel.spec.release` and the
checkpoint loader in :mod:`eawf.kernel.spec.release_config`; this
package holds the parts that are *this project's* release plan
(:mod:`eawf.workflow.release.train`) and the status machine that walks a
checkpoint from DRAFT to a terminal state
(:mod:`eawf.workflow.release.lifecycle`).
"""

from __future__ import annotations

__all__: list[str] = []
