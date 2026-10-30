"""Worktree subsystem: the git plumbing and tree snapshots dispatch runs in.

Callers import the submodules directly: :mod:`eawf.runtime.worktree.git` for
``git worktree`` and branch plumbing, :mod:`eawf.runtime.worktree.tree_change`
for the before and after snapshots a Run's file changes are read from.
"""

from __future__ import annotations
