<!-- Generated from the eawf profile render block `commit-prefix`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=commit-prefix version=2.3 hash=798db669a5b2f505 -->
# `commit-prefix`

A Batch commit is ``<type>: <summary>`` with one ``Batch:`` trailer and a ``Task:`` trailer per Task it delivers; each push ends with one ``state:`` commit.

### Commit prefix

A commit names the Batch and the Tasks it delivers in body trailers, under decision D87, which superseded D85. Conventional-commit types, in every form below: ``feat``, ``fix``, ``chore``, ``docs``, ``refactor``, ``test``, ``build``, ``perf``, ``ci``, ``revert``, ``state``.

- **Batch commit** — ``<type>: <summary>`` with no bracket prefix, exactly one ``Batch: BAT-<NNNN>`` trailer and one ``Task: <KEY>`` trailer for every Task it delivers, each a Task of that Batch. A commit delivering a single Task may carry its ``Task:`` trailer alone. The pre-push ``commit-prefix-lint`` hook refuses a pushed commit naming no Task, several Tasks without a ``Batch`` trailer, a Task twice, two ``Batch`` trailers, or a Task of another Batch.
- **Trailing state commit** — ``state: <summary>``, one per push, touching only committed state paths.

**Legacy carriers.** History written before the Batch trailer names a wave instead, through an ``Eawf-Wave`` body trailer or a bracketed subject prefix; the ``vcs.conventions.subject_style`` config leaf selects which one ``tools/commit_prefix_lint.py`` expects, and it defaults to ``trailer``. The lint still reads these forms:

- **Wave trailer form** — ``<type>: <summary>`` with NO bracket prefix, plus an ``Eawf-Wave: P<NN>-I<NN>-W<NN>`` trailer in the body naming the wave the commit advances.
- **Deprecated prefix form** — ``[P<NN>-W<NN>] <type>:`` (or ``[P<NN>-I<NN>-W<NN>] <type>:`` when iter ≥ I02). Still accepted, but the lint exits 0 with a deprecation warning on stderr.
- **State-bookkeeping** — ``[P<NN>] state:`` (or ``[P<NN>-I<NN>] state:`` when iter ≥ I02). Allowed paths: ``.ea/state.json``, the typed stores under ``.ea/store/`` (``audit.jsonl``, ``decision.jsonl``, ``evidence.jsonl``, the role reports), ``.secrets.baseline``, and ``.ea/specs/**``. ``.ea/store/event.jsonl`` is NOT among them: the event store is the firehose, so it is gitignored and stays on the machine that produced it.
- **Phase/iter-scoped artifact docs** — ``[P<NN>] docs:`` (or ``[P<NN>-I<NN>] docs:``) for promoted documentation artifacts no single wave owns. Restricted to ``.ea/artifacts/**``. A commit that stages at least one artifact may also carry ``.ea/state.json`` and ``.secrets.baseline``: rewriting an artifact re-pins its digest in state, and the baseline tracks state.json line numbers. Without an artifact, those two stay rejected.
- **Out-of-phase** — ``<type>: <summary>`` carrying no carrier at all. Accepted ONLY while no phase is PLANNED or ACTIVE (every phase CLOSED or ARCHIVED, and ``state.current.phase_id`` is ``None``). Rejected while any phase is PLANNED or ACTIVE, so work in the gap between phases becomes a wave of the next phase. A bare ``state:`` subject is rejected only while ``state.current.phase_id`` is set.

The bracketed bookkeeping forms are exempt from the deprecation warning, and the retired ``-CORE`` suffix is rejected in anything new.

Body: a few lines on why. Trailers: the ``Task`` trailer, an optional ``Batch`` trailer, and a recognized Claude or Codex ``Co-Authored-By`` trailer.
<!-- END EAWF:managed id=commit-prefix -->
