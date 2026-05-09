"""Agent-session subsystem for eawf — start / checkpoint / close / recover.

Operates on ``state.agent_sessions`` keyed by session ID. Per-(scope, runtime)
uniqueness rejects accidental dual sessions; recovery walks heartbeat-aged
records and marks them ``stale``.
"""

from __future__ import annotations
