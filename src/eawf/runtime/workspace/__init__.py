"""Daemon-owned isolated workspaces and the leases that hand them out.

A mutating Run never picks its own directory. The daemon materializes an
isolated git worktree, files a lease over it, and hands the worker an
opaque handle; resolving that handle back to a path happens here and
nowhere else. The package owns materialization, liveness, revocation and
the reconcile that decides whether residue may be removed or must be
left alone under a recovery handle.
"""

from __future__ import annotations
