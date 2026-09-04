<!-- Generated from the eawf profile render block `commit-prefix`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=commit-prefix version=1.7 hash=b51a517fe59ace9e -->
# `commit-prefix`

Wave commits are written ``<type>: <summary>`` plus an ``Eawf-Wave: P<NN>-I<NN>-W<NN>`` trailer; the bracket prefix form still passes but warns, and ``[P<NN>] state:`` keeps its bracket.

### Commit prefix

A commit names the scope it advances through one of two carriers: an ``Eawf-Wave`` body trailer (the written form) or the legacy bracketed subject prefix. The ``vcs.conventions.subject_style`` config leaf selects which one ``tools/commit_prefix_lint.py`` expects, and it defaults to ``trailer``.

Conventional-commit types, in every form below: ``feat``, ``fix``, ``chore``, ``docs``, ``refactor``, ``test``, ``build``, ``perf``, ``ci``, ``revert``, ``state``.

Subject grammar:

- **Planned wave deliverable, written form** — ``<type>: <summary>`` with NO bracket prefix, plus an ``Eawf-Wave: P<NN>-I<NN>-W<NN>`` trailer in the body naming the wave the commit advances. Write this one.
- **Planned wave deliverable, deprecated prefix form** — ``[P<NN>-W<NN>] <type>:`` (or ``[P<NN>-I<NN>-W<NN>] <type>:`` when iter ≥ I02). Still accepted, but the lint exits 0 with a deprecation warning on stderr; acceptance is withdrawn once the trailer form is universal.
- **State-bookkeeping** — ``[P<NN>] state:`` (or ``[P<NN>-I<NN>] state:`` when iter ≥ I02). The ``state`` conventional-commit type IS the semantic signal for phase- scope bookkeeping; no suffix needed. Allowed paths: ``.ea/state.json``, the typed stores under ``.ea/store/`` (``audit.jsonl``, ``decision.jsonl``, ``evidence.jsonl``, the role reports), ``.secrets.baseline``, and ``.ea/specs/**``. ``.ea/store/event.jsonl`` is NOT among them: the event store is the firehose (one row per lifecycle mutation plus every spawned agent's raw stdout), so it is gitignored and stays on the machine that produced it.
- **Phase/iter-scoped artifact docs** — ``[P<NN>] docs:`` (or ``[P<NN>-I<NN>] docs:``) for documentation artifacts no single wave owns (closure audits, promoted research / decision / incident briefs). Restricted to ``.ea/artifacts/**``; wave-produced docs use the wave form.
- **Out-of-phase** — ``<type>: <summary>`` carrying neither carrier: no bracket prefix and no ``Eawf-Wave`` trailer. Accepted ONLY when ``state.current.phase_id`` is ``None`` (no ACTIVE phase) — e.g. the pre-flight chore commit between phase close and the next ``/roadmap propose``. Such a commit advances no wave, so there is nothing for either carrier to name. Rejected when a phase is ACTIVE so lifecycle bookkeeping stays attributable.

The two bracketed bookkeeping forms are exempt from the deprecation warning: they advance no single wave, so the ``Eawf-Wave`` trailer has nothing to carry and the bracket stays their only scope carrier.

The path whitelist for state-bookkeeping commits triggers on ``type == 'state'`` — the canonical, and only, semantic signal.

Bare ``[P<NN>]`` is accepted for ``type == 'state'`` (any state-bookkeeping path) and ``type == 'docs'`` (restricted to ``.ea/artifacts/**``); for every other bracketed subject the ``-W<NN>`` suffix remains mandatory.

The ``-CORE`` suffix is retired. It survives only in commits already on the trunk, where ``git log`` reads it as the pre-P26-W23 spelling of ``[P<NN>] state:``; the lint rejects it in anything new.

Non-final iter closes are still in-phase state bookkeeping: use ``[P<NN>-I<NN>] state: close iter`` while the phase remains ACTIVE. Bare conventional commits are reserved for the gap after phase close clears ``state.current.phase_id`` and before the next phase activates, such as a pre-flight chore before ``/roadmap propose``.

**Operational coupling: ship + PR-review ride the phase-co-closing iter.** The final iter of a phase is where the PR-review pass + ship CI happen; review-feedback waves append to that iter (``eawf roadmap revise --add-wave``) rather than opening a fresh iter. This keeps the phase-close mutation attributable to one iter close + the same commit (see ``iter-phase-close-timing``).

Body: 3-6 bullets on what changed and why. Trailers: the ``Eawf-Wave`` scope trailer (written form) and a recognized Claude or Codex ``Co-Authored-By`` trailer.
<!-- END EAWF:managed id=commit-prefix -->
