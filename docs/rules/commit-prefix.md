<!-- Generated from the eawf profile render block `commit-prefix`. Do not hand-edit: re-run `eawf sync`. -->

# `commit-prefix`

Subjects are ``[P<NN>(-I<NN>)?(-W<NN>)?] <type>: <summary>`` with type from feat|fix|chore|docs|refactor|test|build|perf|ci|revert|state, a 3-6 bullet body, and a bracket-free bare subject only while no phase is ACTIVE.

### Commit prefix

``[P<NN>(-I<NN>)?(-W<NN>)?] <type>: <summary>`` — types: ``feat``, ``fix``, ``chore``, ``docs``, ``refactor``, ``test``, ``build``, ``perf``, ``ci``, ``revert``, ``state``.

Subject grammar (post-P26-W23 + P28-W66 bare-conventional form):

- **Planned wave deliverable** — ``[P<NN>-W<NN>] <type>:`` (or ``[P<NN>-I<NN>-W<NN>] <type>:`` when iter ≥ I02). The ``-W<NN>`` suffix declares the wave the commit advances.
- **State-bookkeeping** — ``[P<NN>] state:`` (or ``[P<NN>-I<NN>] state:`` when iter ≥ I02). The ``state`` conventional-commit type IS the semantic signal for phase- scope bookkeeping; no suffix needed. Allowed paths: ``.ea/state.json``, the typed stores under ``.ea/store/`` (``audit.jsonl``, ``decision.jsonl``, ``evidence.jsonl``, the role reports), ``.secrets.baseline``, and ``.ea/specs/**``. ``.ea/store/event.jsonl`` is NOT among them: the event store is the firehose (one row per lifecycle mutation plus every spawned agent's raw stdout), so it is gitignored and stays on the machine that produced it.
- **Phase/iter-scoped artifact docs** — ``[P<NN>] docs:`` (or ``[P<NN>-I<NN>] docs:``) for documentation artifacts no single wave owns (closure audits, promoted research / decision / incident briefs). Restricted to ``.ea/artifacts/**``; wave-produced docs use the ``[P<NN>-W<NN>] docs:`` form.
- **Bare conventional-commits (out-of-phase)** — ``<type>: <summary>`` with NO bracket prefix. Accepted ONLY when ``state.current.phase_id`` is ``None`` (no ACTIVE phase) — e.g. the pre-flight chore commit between phase close and the next ``/roadmap propose``. Rejected when a phase is ACTIVE so lifecycle bookkeeping stays attributable. Enforced by ``tools/commit_prefix_lint.py``.

The path whitelist for state-bookkeeping commits triggers on ``type == 'state'`` — the canonical, and only, semantic signal.

Bare ``[P<NN>]`` is accepted for ``type == 'state'`` (any state-bookkeeping path) and ``type == 'docs'`` (restricted to ``.ea/artifacts/**``); for every other type the ``-W<NN>`` suffix remains mandatory.

The ``-CORE`` suffix is retired. It survives only in commits already on the trunk, where ``git log`` reads it as the pre-P26-W23 spelling of ``[P<NN>] state:``; the lint rejects it in anything new.

Non-final iter closes are still in-phase state bookkeeping: use ``[P<NN>-I<NN>] state: close iter`` while the phase remains ACTIVE. Bare conventional commits are reserved for the gap after phase close clears ``state.current.phase_id`` and before the next phase activates, such as a pre-flight chore before ``/roadmap propose``.

**Operational coupling: ship + PR-review ride the phase-co-closing iter.** The final iter of a phase is where the PR-review pass + ship CI happen; review-feedback waves append to that iter (``eawf roadmap revise --add-wave``) rather than opening a fresh iter. This keeps the phase-close mutation attributable to one iter close + the same commit (see ``iter-phase-close-timing``).

Body: 3-6 bullets on what changed and why. Trailer: a recognized Claude or Codex ``Co-Authored-By`` trailer.
