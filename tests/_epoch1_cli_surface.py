"""The test modules that still drive the epoch-1 command surface the flag day refuses.

After the flag day every epoch-1 mutating verb refuses on every tree: a plain
epoch-1 tree at the CLI gate, an epoch-2 tree at the ``state.json`` write
chokepoint. The handlers behind those verbs are still in the source tree
until the epoch-1 command surface is deleted, and the modules below are
their only CLI-level coverage, so they run with the CLI gate lifted through
the named ``epoch1_cli_surface`` fixture in ``tests/conftest.py``.

This is a transitional bridge: EAWF-0196 deletes the epoch-1 command
handlers together with these modules and empties the list. Until then the
list only shrinks: a new module that needs the epoch-1 surface is a
defect, and the census in
``tests/contract/surfaces/cli/test_rel_021_flag_day_census.py`` fails when
the list grows or names a module that no longer exists.
"""

from __future__ import annotations

from typing import Final

#: Why every module below may lift the gate.
EPOCH1_CLI_SURFACE_REASON: Final = (
    "covers a retained epoch-1 command handler the flag day refuses; EAWF-0196 deletes "
    "it with the epoch-1 command surface"
)

#: Repository-relative paths of the modules that run with the gate lifted.
EPOCH1_CLI_SURFACE_MODULES: Final = frozenset(
    {
        "tests/contract/platform/profiles/test_enable_and_render_parity.py",
        "tests/contract/workflow/skills/test_flow_refuses_retired.py",
        "tests/golden/surfaces/cli/test_scenarios.py",
        "tests/integration/runtime/daemon/methods/test_checkpoint_receipts.py",
        "tests/integration/runtime/daemon/methods/test_observe_cli.py",
        "tests/integration/runtime/daemon/methods/test_release_cli_store_record.py",
        "tests/integration/runtime/daemon/methods/test_train_advance_rpc.py",
        "tests/integration/runtime/daemon/test_epoch1_refused_on_marked_root.py",
        "tests/integration/runtime/daemon/test_gate_kind_rewrite.py",
        "tests/integration/runtime/daemon/test_orphan_reconcile_lineage.py",
        "tests/integration/surfaces/cli/test_close_rereceipt_cli.py",
        "tests/integration/surfaces/cli/test_close_timeout_terminal.py",
        "tests/integration/surfaces/cli/test_daemon_escalation.py",
        "tests/integration/surfaces/cli/test_daemon_service_lifecycle.py",
        "tests/integration/surfaces/cli/test_daemonless_close_full_gate.py",
        "tests/integration/surfaces/cli/test_daemonless_close_teeth.py",
        "tests/integration/surfaces/cli/test_daemonless_close_waiver_e2e.py",
        "tests/integration/surfaces/cli/test_evidence_attest.py",
        "tests/integration/surfaces/cli/test_in_process_runtime_gate.py",
        "tests/integration/surfaces/cli/test_iter_close_archive_specs.py",
        "tests/integration/surfaces/cli/test_lifecycle_daemon_routing.py",
        "tests/integration/surfaces/cli/test_question_open_decision_cli.py",
        "tests/integration/surfaces/cli/test_reclaim.py",
        "tests/integration/surfaces/cli/test_spec_convert_legacy.py",
        "tests/integration/surfaces/cli/test_spec_sync_cli.py",
        "tests/integration/surfaces/cli/test_success_flag_repeatable.py",
        "tests/integration/surfaces/cli/test_success_fragment_rejected.py",
        "tests/integration/surfaces/cli/test_wave_waivers_cli.py",
        "tests/integration/surfaces/cli/test_windows_w04_policy.py",
        "tests/integration/test_backlog_close_batch.py",
        "tests/integration/test_backlog_closed_row_correction.py",
        "tests/integration/test_cli_agent_report.py",
        "tests/integration/test_cli_artifact_chassis.py",
        "tests/integration/test_cli_artifact_verify.py",
        "tests/integration/test_cli_backfill_wave_intents.py",
        "tests/integration/test_cli_backfill.py",
        "tests/integration/test_cli_close_async.py",
        "tests/integration/test_cli_dispatch_pause.py",
        "tests/integration/test_cli_dispatch_wave.py",
        "tests/integration/test_cli_epoch1_track_unchanged.py",
        "tests/integration/test_cli_estimation.py",
        "tests/integration/test_cli_evidence_artifact_spike_report.py",
        "tests/integration/test_cli_evidence.py",
        "tests/integration/test_cli_iter_audit_gate.py",
        "tests/integration/test_cli_lifecycle.py",
        "tests/integration/test_cli_memory.py",
        "tests/integration/test_cli_research_question_status.py",
        "tests/integration/test_cli_roadmap.py",
        "tests/integration/test_cli_session.py",
        "tests/integration/test_cli_wave_close_commit.py",
        "tests/integration/test_cli_wave_dag.py",
        "tests/integration/test_cli_wave_integration.py",
        "tests/integration/test_cli_wave_policy_cmd.py",
        "tests/integration/test_cli_wave_policy.py",
        "tests/integration/test_dispatch_renderer.py",
        "tests/integration/test_dual_runtime_envelope.py",
        "tests/integration/test_flow_kill_resume.py",
        "tests/integration/test_flow_resume.py",
        "tests/integration/test_lifecycle_ordering.py",
        "tests/integration/test_memory_sync_roundtrip.py",
        "tests/integration/test_phase2_acceptance.py",
        "tests/integration/test_profile_enable_workspace_overlay.py",
        "tests/integration/test_roadmap_dry_run.py",
        "tests/integration/test_store_paths_consistency.py",
        "tests/integration/test_surfaces_smoke.py",
        "tests/integration/test_wave_ack_drift_cmd.py",
        "tests/integration/test_wave_autoland_cli.py",
        "tests/integration/test_wave_budget_cli.py",
        "tests/integration/test_wave_dispatch_cli.py",
        "tests/integration/test_wave_dispatch_runtime.py",
        "tests/integration/test_wave_fix_ci_cli.py",
        "tests/integration/test_wave_land_cli.py",
        "tests/integration/test_wave_review_cli.py",
        "tests/integration/test_wave_verify_commits_blank_trailer.py",
        "tests/integration/test_wave_verify_commits_cmd.py",
        "tests/integration/test_worktree_cli_cleanup.py",
        "tests/integration/test_worktree_cli_create.py",
        "tests/integration/test_worktree_cli_merge_back.py",
        "tests/integration/workflow/evidence/test_corpus_magnitude_split.py",
        "tests/integration/workflow/evidence/test_measured_contract_preflight.py",
        "tests/integration/workflow/lifecycle/test_scope_repoint.py",
        "tests/integration/workflow/lifecycle/test_waivers.py",
        "tests/integration/workflow/lifecycle/test_wave_branch_prune.py",
        "tests/integration/workflow/lifecycle/test_wave_repin_archive.py",
        "tests/integration/workflow/release/test_dev1_burn.py",
        "tests/integration/workflow/release/test_state_tree_brief_verified.py",
        "tests/integration/workflow/verify/test_gate_caller_isolation.py",
        "tests/integration/workflow/verify/test_readiness.py",
        "tests/integration/workflow/verify/test_seams.py",
        "tests/property/test_estimation_property.py",
        "tests/property/test_flow_random_kill.py",
    }
)
