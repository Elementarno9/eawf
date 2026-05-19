"""Init bootstrap templates (C08 — P25-W16).

Three YAML templates ship in v0.3 per C08 D7 (revised 2026-05-18 per
operator Q24): ``research.yaml``, ``engineering.yaml``,
``reverse-engineering.yaml``. Each template is a ``.ea/config.yaml``
seed that ``eawf init --template <name>`` merges with operator answers
(project_code, project_title) to bootstrap a workspace.

Per C08 D10 the templates encode per-profile defaults for
``dispatch.session_policy_default``:

- ``research.yaml`` → ``continue`` (evidence-driven session reuse)
- ``engineering.yaml`` → ``fresh`` (PR-driven clean slate)
- ``reverse-engineering.yaml`` → ``continue`` (decompilation context)

``spike.yaml`` and ``hybrid.yaml`` are deferred to v0.4+ (Q24 — YAGNI
trim; demand-signal unclear). Discovery + load lives in
:mod:`eawf.profiles.discovery` (``load_init_template``).
"""

from __future__ import annotations
