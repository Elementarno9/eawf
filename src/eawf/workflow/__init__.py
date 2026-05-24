"""Workflow layer: lifecycle, evidence, skills, and dispatch packages.

The workflow super-package groups the packages that drive the agent-driven
delivery loop on top of the kernel data model:
:mod:`~eawf.workflow.lifecycle` (phase/iter/wave transitions),
:mod:`~eawf.workflow.evidence` (hypotheses, audits, backlog, incidents),
:mod:`~eawf.workflow.skills` (skill flow + context),
:mod:`~eawf.workflow.agents` and :mod:`~eawf.workflow.agent_report`
(agent role specs and typed report bodies),
:mod:`~eawf.workflow.audit_dsl` (declarative audit checks),
:mod:`~eawf.workflow.dispatch` (wave/role dispatch rendering),
:mod:`~eawf.workflow.pr_review` (PR review pass), and
:mod:`~eawf.workflow.estimation` (EU calibration + velocity).
"""

from __future__ import annotations
