"""
Relation resolution for serializer fields — one implementation, one place.

Discovery needs two facts about a DRF ``RelatedField``: **where the relation
points** and **whether the field is effectively required** — plus a third that
says whether the second one was actually determined.  All three come from the
same introspection pass, so they live in one function here rather than being
re-derived by each caller.  A duplicated required-ness rule is how two copies
drift, and this package has more than one consumer for these facts:

* :mod:`frisian_mcp.backends.discovery` marks a field required in the emitted
  tool schema when the serializer says otherwise.
* :mod:`frisian_mcp.route_audit` needs the relation's *target* model to tell
  whether a write on a carved route points at something the route cannot read.

It also owns the side mapping the second consumer reads — see
:func:`record_required_relations`.

**Why this module sits at the top level and not under ``backends/``.**  It was
first written as ``backends/_relations.py``, on the stated reasoning that the
audit path could then import it without pulling in the discovery backend.  That
was wrong and it was measured wrong: ``frisian_mcp/backends/__init__.py``
imports ``discovery`` and ``invocation`` eagerly, so importing *any* submodule
of ``backends`` executes the package ``__init__`` and drags both in.  Measured,
importing ``backends._relations`` pulled 10 ``frisian_mcp`` modules including
``backends.discovery``; from here it pulls 6, the same set ``route_audit``
already had, and neither backend among them.  Keep it out of ``backends/``, or
the claim above silently stops being true again.

Private to the package; not public API.
"""

from __future__ import annotations

from collections.abc import Container, Mapping, Sequence
from enum import Enum
from typing import Any, NamedTuple

from rest_framework.relations import RelatedField, SlugRelatedField

__all__ = [
    "RelationOutcome",
    "RequiredRelation",
    "ResolvedRelation",
    "clear_required_relations",
    "collect_relation",
    "model_label",
    "narrow_relations",
    "record_required_relations",
    "record_write_relations",
    "required_relations_for",
    "resolve_relation",
]


class RelationOutcome(Enum):
    """
    Why a resolution ended the way it did — three outcomes, never two.

    ``required`` is a bool and a bool cannot distinguish "this field is genuinely
    optional" from "introspection could not answer, so we defaulted to False".
    Callers that report to an operator must tell those apart: a check that prints
    the same clean result for "evaluated, nothing found" and "could not evaluate"
    tells the operator their configuration is safe when it has not been examined.
    This package has shipped that mistake before, which is why the outcome is
    carried explicitly rather than inferred from a falsy value.

    Members:
        NOT_APPLICABLE: The field is not a relation, so there is no required-ness
            question to ask.  ``target_model`` is ``None`` and ``required`` is
            ``False`` because the question does not apply, not because it was
            answered.
        RESOLVED: Introspection completed.  ``required`` carries a real answer.
        UNEVALUATED: There is a relation here, but its required-ness could not be
            determined — no queryset to introspect, a queryset with no ``.model``,
            a lookup that raised, or a field kind deliberately excluded from the
            inference.  ``required`` is ``False`` as a safe default and **must not
            be read as "not required"**.  ``target_model`` may still be populated:
            failing to answer required-ness says nothing about where the relation
            points, and a caller asking only "what does this reach?" still gets an
            answer.

    """

    NOT_APPLICABLE = "not_applicable"
    RESOLVED = "resolved"
    UNEVALUATED = "unevaluated"


class ResolvedRelation(NamedTuple):
    """
    What one introspection pass learned about a ``RelatedField``, and how far it got.

    Attributes:
        target_model: The Django model the relation points *at*, taken from the
            field's queryset (``field.queryset.model``).  ``None`` when the field
            is not a relation, or carries no queryset to resolve one from.
        required: Whether the field is treated as effectively required for the
            emitted schema.  See :func:`resolve_relation` for how this is
            derived — and for the discrepancy between how it is derived and what
            it purports to mean.  Only meaningful when *outcome* is
            :attr:`RelationOutcome.RESOLVED`.
        outcome: Which of the three resolution outcomes this is.  Read it before
            trusting *required*; see :class:`RelationOutcome`.

    """

    target_model: type[Any] | None
    required: bool
    outcome: RelationOutcome


def resolve_relation(field: Any, field_name: str) -> ResolvedRelation:
    """
    Resolve a serializer field's relation target and its effective required-ness.

    Single source of truth for both facts.  Callers that want only one of them
    take one; there is deliberately no second copy of this resolution anywhere
    in the package, because a duplicated required-ness rule is how the two
    copies drift.

    **Why an inferred required-ness exists at all.**  Many DRF host apps set
    ``required=False`` on FK serializer fields so that the same serializer
    serves both ``create`` and ``partial_update`` (PATCH).  The model field,
    however, may be ``NOT NULL`` with no default, meaning any ``create`` that
    omits the field fails at the DB layer with a cryptic constraint error.  The
    inference exists to catch that create/partial_update mismatch so the
    dispatcher schema can mark the field required.

    **What this function actually computes today.**  ``required`` is ``True``
    when all of the following hold:

    * *field* is a :class:`~rest_framework.relations.RelatedField` but NOT a
      :class:`~rest_framework.relations.SlugRelatedField` (slug fields work
      with bare strings and are handled separately).
    * The field has a Django QuerySet with an accessible ``.model`` attribute.
    * ``get_field(source)`` **on that queryset's model** resolves, and the
      field it returns is ``null=False`` with no Django-level default.

    The third bullet is the discrepancy, and it is stated plainly here so that
    no caller inherits it unknowingly: ``field.queryset.model`` is the model the
    relation points *at*, not the model that *declares* it.  The lookup
    therefore asks the **target** for a field named after the source, which for
    an ordinary cross-model FK does not exist — ``get_field`` raises
    ``FieldDoesNotExist``, the fallback below swallows it, and the result is
    ``False``.  Measured against the models in ``INSTALLED_APPS`` under the test
    settings, that is every genuinely-required FK in the corpus; it also returns
    ``True`` for a nullable FK whose *target* happens to own a ``NOT NULL``
    field of the same name.  The behaviour is preserved verbatim here because
    this extraction is contracted to change none of it, and correcting the
    receiver moves the emitted ``required`` array on real hosts.

    Falls back to a ``required`` of ``False`` on any introspection failure so
    that a non-standard queryset or computed field never raises during
    discovery — but reports :attr:`RelationOutcome.UNEVALUATED` alongside it, so
    a caller can tell the fallback from a real negative rather than reading both
    as "not required".  ``target_model`` is still reported in that case when it
    was resolved before the failure: an unresolvable required-ness says nothing
    about where the relation points.

    Args:
        field: The DRF serializer field to inspect.
        field_name: The serializer's name for the field, used as the lookup
            source when the field declares no explicit ``source``.

    Returns:
        A :class:`ResolvedRelation` carrying the target, the required-ness,
        and the outcome that says whether the required-ness means anything.

    """
    if not isinstance(field, RelatedField):
        return ResolvedRelation(None, False, RelationOutcome.NOT_APPLICABLE)
    queryset = getattr(field, "queryset", None)
    if queryset is None or not hasattr(queryset, "model"):
        # A relation we cannot introspect — not a relation we have cleared.
        return ResolvedRelation(None, False, RelationOutcome.UNEVALUATED)
    target_model = queryset.model
    # A SlugRelatedField still points somewhere, so its target is reported — a
    # caller asking "what does this relation reach?" gets a straight answer.
    # Its required-ness is not inferred: slug fields accept bare strings and are
    # handled separately by the schema path.  That is an exclusion, not a
    # finding of "optional", so the outcome says so.
    if isinstance(field, SlugRelatedField):
        return ResolvedRelation(target_model, False, RelationOutcome.UNEVALUATED)
    # Use field.source when set (e.g. source="device_role"); fall back to the
    # serializer field name which matches the model attribute in the common case.
    source = getattr(field, "source", None) or field_name
    try:
        model_field = target_model._meta.get_field(source)  # pylint: disable=protected-access
    except Exception:  # pylint: disable=broad-exception-caught
        # The lookup did not resolve, so required-ness is unknown.  Note this is
        # also the branch the owner/target discrepancy above lands in for an
        # ordinary cross-model FK — which is why it reports UNEVALUATED rather
        # than a confident False.
        return ResolvedRelation(target_model, False, RelationOutcome.UNEVALUATED)
    required = not getattr(model_field, "null", True) and not model_field.has_default()
    return ResolvedRelation(target_model, required, RelationOutcome.RESOLVED)


class RequiredRelation(NamedTuple):
    """
    One relation on a write action that the route audit may need to reason about.

    Recorded only for fields the audit cannot dismiss: either the emitted schema
    marks them required, or their required-ness came back
    :attr:`RelationOutcome.UNEVALUATED` so we do not know.  A relation resolved
    as genuinely optional is not recorded — there is nothing to say about it.
    **Which of those two reasons kept it** is carried in *schema_required*; it
    used to be computed and thrown away, and an audit that cannot see it grades a
    field the schema plainly requires as a could-not-evaluate.

    Attributes:
        field_name: The serializer's name for the field.
        target_label: ``"app_label.model_name"`` for the model the relation
            points at, or ``None`` when the target could not be resolved.
        outcome: The resolution outcome for *required-ness*, carried through so
            the audit can separate "required, target off-surface" from "we could
            not tell".  Never collapse the two.
        schema_required: Whether the **emitted tool schema** lists this field in
            its ``required`` array.  A different fact from *outcome*, and keeping
            them apart is the point: the schema's array is what the caller is
            obliged to send, while *outcome* says only whether model
            introspection reached a verdict about the model field behind it.  A
            field can be plainly required by the schema and still carry
            :attr:`RelationOutcome.UNEVALUATED` — on a real host that is the
            common case, not the corner one.  Set by :func:`narrow_relations`,
            the one place that holds the schema's array; ``False`` by default, so
            a caller that never narrows is unchanged.

    """

    field_name: str
    target_label: str | None
    outcome: RelationOutcome
    schema_required: bool = False


#: Tool name -> the relations recorded for it at discovery time.
#:
#: Populated by the discovery backend, where the serializer has *already* been
#: instantiated to build the tool schema, and read by ``route_audit``.  This
#: exists so the audit never has to instantiate a serializer of its own: that
#: was measured at ~1.35 ms each, paid once per uWSGI worker (eight on the
#: reference deployment) and twice more by ``mcp_doctor``, on a cold-start path
#: that is already the least healthy part of the request cycle.
#:
#: A module-level mapping rather than a field on ``_ToolEntry`` deliberately:
#: that type is read by ``dispatch``, the group dispatcher, ``apps.py`` and
#: permission-aware discovery, and none of them need this.
_REQUIRED_RELATIONS: dict[str, tuple[RequiredRelation, ...]] = {}


def model_label(model: type[Any] | None) -> str | None:
    """
    Render *model* as ``"app_label.model_name"``, or ``None`` if it cannot be read.

    One place for the ``_meta`` access so the audit never grows its own copy.
    Metadata only: ``_meta.app_label`` and ``_meta.model_name`` are class
    attributes and reading them constructs nothing and issues no query.
    """
    if model is None:
        return None
    try:
        meta = model._meta  # pylint: disable=protected-access
        return f"{meta.app_label}.{meta.model_name}"
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def record_required_relations(tool_name: str, relations: Sequence[RequiredRelation]) -> None:
    """
    Record the relations discovery resolved for *tool_name*.

    Overwrites any prior entry, so a re-discovery in the same process replaces
    rather than accumulates.  Recording an empty sequence is meaningful and is
    kept: it says "this tool was examined and has nothing to report", which
    :func:`required_relations_for` cannot otherwise distinguish from "never
    examined" — and that distinction is the whole point of the third outcome.

    Args:
        tool_name: The fully-qualified tool name (``"resource.action"``).
        relations: What :func:`resolve_relation` found, already filtered to the
            fields worth carrying.

    """
    _REQUIRED_RELATIONS[tool_name] = tuple(relations)


#: The acting side of the cross-carve check.  DELETE carries no body, and PATCH
#: is ``partial_update``, which does not enforce ``required`` -- the stored
#: object already holds the relation, so an unreachable target cannot make the
#: call unsatisfiable.  Both are therefore out.
_UNSATISFIABLE_METHODS = frozenset({"post", "put"})


def collect_relation(
    relations_out: dict[str, RequiredRelation] | None,
    field_name: str,
    relation: ResolvedRelation,
    /,
) -> None:
    """
    Add *relation* to *relations_out*, unless there is nothing relational to add.

    No-ops when the collector is ``None`` (the caller did not ask) or when the
    field is not a relation at all.  Keeping the guard here means the schema
    path stays a single call and cannot drift from the recorded shape.
    """
    if relations_out is None or relation.outcome is RelationOutcome.NOT_APPLICABLE:
        return
    relations_out[field_name] = RequiredRelation(
        field_name=field_name,
        target_label=model_label(relation.target_model),
        outcome=relation.outcome,
    )


def narrow_relations(
    relations: dict[str, RequiredRelation], required_names: Container[str]
) -> None:
    """
    Mark the schema-required relations and drop the ones with nothing to say.

    A relation the caller is never obliged to send is not a discoverability
    problem, so a field absent from *required_names* goes -- **unless** its
    required-ness came back :attr:`RelationOutcome.UNEVALUATED`, in which case it
    stays.  We do not know that one is optional, and dropping it would report
    "nothing to see" on precisely the fields we failed to examine.  That
    asymmetry is the whole reason the outcome is carried rather than inferred
    from a falsy ``required``.

    A field that *is* in *required_names* is kept and stamped
    ``schema_required=True``.  This function is the only place holding the
    emitted schema's ``required`` array, so a fact not recorded here is gone: the
    audit downstream sees the relation and the outcome and cannot otherwise tell
    a field the caller must send from one that merely resisted introspection.
    Note this only ever *reads* *required_names* -- nothing here writes back to
    the schema, so what discovery emits is untouched.

    Args:
        relations: The collector to narrow, keyed by serializer field name.
        required_names: The names the emitted schema marks required.

    """
    for name, relation in list(relations.items()):
        if name in required_names:
            relations[name] = relation._replace(schema_required=True)
        elif relation.outcome is not RelationOutcome.UNEVALUATED:
            del relations[name]


def record_write_relations(
    tool_name: str, http_method: str, relations: Mapping[str, RequiredRelation]
) -> None:
    """
    Record *relations* for *tool_name* when its HTTP method can be made unsatisfiable.

    The narrowing lives here rather than at audit time so the mapping holds the
    population that can actually produce a finding, instead of one entry per
    discovered tool -- on a host with a couple of thousand tools that is the
    difference that keeps this cheap.

    A tool outside :data:`_UNSATISFIABLE_METHODS` is not recorded at all, which
    is correct: the audit only ever asks about POST and PUT, so a missing record
    for a GET is never read as "could not evaluate".

    Args:
        tool_name: The fully-qualified tool name (``"resource.action"``).
        http_method: The HTTP method the router bound this action to.
        relations: What discovery resolved, keyed by serializer field name.

    """
    if http_method.lower() in _UNSATISFIABLE_METHODS:
        record_required_relations(tool_name, tuple(relations.values()))


def required_relations_for(tool_name: str) -> tuple[RequiredRelation, ...] | None:
    """
    Return the relations recorded for *tool_name*, or ``None`` if it was never examined.

    ``None`` and ``()`` mean different things and callers must treat them
    differently: ``()`` is "examined, nothing to report" (a clean result), while
    ``None`` is "no record" — a tool discovery never built a schema for, which
    is a could-not-evaluate and must not be reported as clean.
    """
    return _REQUIRED_RELATIONS.get(tool_name)


def clear_required_relations() -> None:
    """Drop every recorded relation.  For tests and for a full re-discovery."""
    _REQUIRED_RELATIONS.clear()
