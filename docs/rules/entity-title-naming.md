<!-- Generated from the eawf profile render block `entity-title-naming`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=entity-title-naming version=1.1 hash=ebc04b9347d05744 -->
# `entity-title-naming`

Write every entity title as an imperative noun-phrase of at most 72 characters with no trailing period, and put the long-form purpose in the description.

### Rationale

**Entity-title naming.** Every lifecycle and research entity (``Phase`` / ``Iter`` / ``Wave`` / ``Decision`` / ``Hypothesis`` / ``BacklogItem`` / ``Incident``) carries a bounded ``title`` and an optional long-form ``description``. The bound exists so titles stay scannable in dense renders — the roadmap tree, plan-view table, and dispatch header all lay titles out in a single fixed-width row, and an unbounded sentence either truncates with an ellipsis (losing the tail) or wraps and breaks the column. A trailing period reads as the end of a sentence, but a title is a label, not prose; the period is visual noise that the description, which IS prose, should carry instead.


### Mechanism

Write ``title`` as an imperative noun-phrase of at most 72 characters with no trailing period — e.g. ``Add bounded title to entities`` or ``Enforce sandbox deny-list at dispatch``, never ``Adds a bounded title to every entity.`` (over-cap once the clause grows, and the period is sentence noise). Put the why / the long-form purpose in ``description`` (bounded at 500 characters); the renderers surface it as a detail block under the bounded title, so the two fields split the label from the explanation rather than competing for one line.


### Verification

The model enforces the hard bound: ``title`` is ``Annotated[str, Field(min_length=1, max_length=72)]`` on every entity, so an over-72 title fails :class:`pydantic.ValidationError` at the ingestion boundary. The style backstop is :func:`eawf.surfaces.render.agents_md.lint_entity_title`, which a reviewer (or a future authoring command) runs over a candidate title to flag an over-cap or a trailing-period title before it reaches the model — the same two failure modes the bound and this rule describe.
<!-- END EAWF:managed id=entity-title-naming -->
