<!-- Generated from the eawf profile render block `code-craft-single-responsibility`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=code-craft-single-responsibility version=1.1 hash=9088378c04ddf30d -->
# `code-craft-single-responsibility`

Give each function and class exactly one reason to change, keeping parsing, validation, and execution in separate units.

### Rationale

A unit with one reason to change is testable in isolation and safe to edit. When parsing, validation, computation, and rendering share a function, a change to one concern risks the others and tests need elaborate setup.


### Mechanism

Give each function and class exactly one reason to change. Keep parsing separate from validation separate from execution. When one class accretes distinct concerns and outgrows roughly 300 lines or seven public methods, apply the ``refactor-god-class`` playbook: extract the lowest-coupling seam first, one concern per commit.


### Verification

Name each public method's single concern; methods that span two concerns flag the unit for extraction. The cognitive-complexity gate (EAWF011 + ruff C901) backstops this — a function over the threshold fails ``uv run pre-commit run --all-files`` before review.
<!-- END EAWF:managed id=code-craft-single-responsibility -->
