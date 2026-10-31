<!-- Generated from the eawf profile render block `commit-prefix`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=commit-prefix version=2.5 hash=7c730dcf5b17e5ce -->
# `commit-prefix`

A Batch commit is ``<type>: <summary>`` with one ``Batch:`` trailer and a ``Task:`` trailer per Task it delivers; each push ends with one ``state:`` commit.

### Commit prefix

A commit names the Batch and the Tasks it delivers in body trailers, under decision D92, which superseded D87. Conventional-commit types, in every form below: ``feat``, ``fix``, ``chore``, ``docs``, ``refactor``, ``test``, ``build``, ``perf``, ``ci``, ``revert``, ``state``.

- **Batch commit** — ``<type>: <summary>`` with no bracket prefix, exactly one ``Batch: BAT-<NNNN>`` trailer and one ``Task: <KEY>`` trailer for every Task it delivers, each a Task of that Batch. A commit delivering a single Task may carry its ``Task:`` trailer alone. The pre-push ``commit-prefix-lint`` hook refuses a pushed commit naming no Task, several Tasks without a ``Batch`` trailer, a Task twice, two ``Batch`` trailers, or a Task of another Batch.
- **Trailing state commit** — ``state: <summary>``, one per push, touching only committed state paths.

**Legacy carriers.** History written before the Batch trailer names its scope through an older body trailer or a bracketed subject prefix; the ``vcs.conventions.subject_style`` config leaf selects which one ``tools/commit_prefix_lint.py`` expects, and it defaults to ``trailer``. The lint still reads those forms so old history keeps passing, warns on the deprecated bracket prefix, and rejects the retired ``-CORE`` suffix. Write neither in a new commit: every new commit carries the native trailers above.

Body: a few lines on why. Trailers: the ``Task`` trailer, an optional ``Batch`` trailer, and a recognized Claude or Codex ``Co-Authored-By`` trailer.
<!-- END EAWF:managed id=commit-prefix -->
